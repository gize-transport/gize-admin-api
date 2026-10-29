"""Staff business rules.

Staff management (GZ-013) is for active admins (the routes' `require_role`); every write
re-reads the caller under the staff-changes lock, so an admin deactivated or demoted a
moment ago is refused. No change may leave the system without an active admin who has
signed in at least once (an invited admin may never accept: owner decision).
"""

import uuid
from typing import Protocol

import structlog
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.core import audit
from gize_api.core.errors import AppError, ErrorCode
from gize_api.core.idempotency import IdempotentRequest, claim, peek, record, replay_response
from gize_api.core.logging import log_security_event
from gize_api.core.pagination import paginate
from gize_api.core.ratelimit import STAFF_INVITE, RateLimiter
from gize_api.core.security import CurrentStaff
from gize_api.integrations.supabase_auth import (
    AuthAccountExistsError,
    AuthAdminError,
    AuthEmailRejectedError,
)
from gize_api.staff.models import STAFF_AUDIT, Staff, StaffRole
from gize_api.staff.repository import (
    get_staff,
    get_staff_by_email,
    lock_staff_changes,
    other_signed_in_admin_exists,
    staff_members,
)
from gize_api.staff.schemas import (
    StaffInvite,
    StaffListParams,
    StaffMe,
    StaffPage,
    StaffRead,
    StaffUpdate,
)

logger = structlog.get_logger("gize_api.staff")


class Inviter(Protocol):
    async def invite_user(self, email: str, *, full_name: str) -> uuid.UUID: ...


async def get_me(session: AsyncSession, caller: CurrentStaff) -> StaffMe:
    async with session.begin():
        staff = await get_staff(session, caller.id)
        # Deactivated (or removed) since `current_staff` read it moments ago.
        if staff is None or not staff.is_active:
            raise AppError(ErrorCode.FORBIDDEN)
        return StaffMe(id=staff.id, full_name=staff.full_name, email=staff.email, role=staff.role)


async def list_staff(session: AsyncSession, params: StaffListParams) -> StaffPage:
    """Newest first, deactivated members included unless filtered out."""
    async with session.begin():
        page = await paginate(
            session,
            staff_members(role=params.role, is_active=params.is_active),
            params,
            created_at=Staff.created_at,
            id=Staff.id,
            newest_first=True,
        )
        return StaffPage(
            items=[StaffRead.model_validate(staff) for staff in page.items],
            next_cursor=page.next_cursor,
        )


async def get_member(session: AsyncSession, staff_id: uuid.UUID) -> StaffRead:
    async with session.begin():
        staff = await get_staff(session, staff_id)
        if staff is None:
            raise AppError(ErrorCode.NOT_FOUND)
        return StaffRead.model_validate(staff)


async def invite(
    session: AsyncSession,
    auth: Inviter,
    limiter: RateLimiter,
    caller: CurrentStaff,
    invitation: StaffInvite,
    idempotent: IdempotentRequest,
) -> Response:
    """Invite through Supabase, then add the member: 201 with a Location, or the replay.

    Supabase is called between two transactions (never inside one). A retry of a finished
    request is answered from its stored response without calling Supabase; a retry after
    Supabase answered but the write failed invites again (Supabase returns the same user,
    who gets a second email) and completes the write.

    The STAFF_INVITE limit counts only requests about to send an email: not refusals or
    replays, which a retrying client could otherwise use up the admin's hour with.
    """
    scope = f"staff:{caller.id}"
    async with session.begin():
        await _require_active_admin(session, caller)  # before an email goes out
        stored = await peek(session, scope=scope, request=idempotent)
        if stored is None and await get_staff_by_email(session, invitation.email) is not None:
            raise AppError(ErrorCode.STAFF_EMAIL_TAKEN)
    if stored is not None:
        return replay_response(stored)

    await limiter.check(STAFF_INVITE, staff_id=str(caller.id))
    user_id = await _send_invite(auth, invitation)

    async with session.begin():
        claimed = await claim(session, scope=scope, request=idempotent)
        if claimed.stored is not None:  # the same request finished meanwhile
            return replay_response(claimed.stored)
        await lock_staff_changes(session)
        await _require_active_admin(session, caller)
        if await get_staff_by_email(session, invitation.email) is not None:
            raise AppError(ErrorCode.STAFF_EMAIL_TAKEN)
        if await get_staff(session, user_id) is not None:  # on staff under another email
            raise AppError(ErrorCode.AUTH_ACCOUNT_EXISTS)
        staff = Staff(
            id=user_id,
            full_name=invitation.full_name,
            email=invitation.email,
            role=invitation.role,
            is_active=True,
        )
        session.add(staff)
        await session.flush()
        audit.record(
            session,
            actor_id=caller.id,
            action="staff.invited",
            entity=STAFF_AUDIT,
            entity_id=user_id,
            before=None,
            after=audit.snapshot(STAFF_AUDIT, staff),
        )
        payload = StaffRead.model_validate(staff).model_dump(mode="json")
        location = f"/v1/staff/{user_id}"
        await record(session, claimed, status=201, body=payload, headers={"Location": location})
    log_security_event(
        "staff_invited",
        sentry=invitation.role is StaffRole.ADMIN,  # a new admin is an alert
        actor_staff_id=str(caller.id),
        staff_id=str(user_id),
        role=invitation.role.value,
    )
    return JSONResponse(payload, status_code=201, headers={"Location": location})


async def update(
    session: AsyncSession, caller: CurrentStaff, staff_id: uuid.UUID, changes: StaffUpdate
) -> StaffRead:
    """Change a member's role and/or status. Setting what is already there changes and
    records nothing, so a retry is harmless."""
    async with session.begin():
        await lock_staff_changes(session)
        await _require_active_admin(session, caller)
        staff = await get_staff(session, staff_id)
        if staff is None:
            raise AppError(ErrorCode.NOT_FOUND)
        role_before, active_before = staff.role, staff.is_active
        role = role_before if changes.role is None else changes.role
        is_active = active_before if changes.is_active is None else changes.is_active
        was_admin = role_before is StaffRole.ADMIN and active_before
        stays_admin = role is StaffRole.ADMIN and is_active
        if (
            was_admin
            and not stays_admin
            and not await other_signed_in_admin_exists(session, staff.id)
        ):
            raise AppError(ErrorCode.LAST_ACTIVE_ADMIN)

        before = audit.snapshot(STAFF_AUDIT, staff)
        staff.role = role
        role_changed = audit.snapshot(STAFF_AUDIT, staff)
        staff.is_active = is_active
        # Two entries when both change: each action says what happened.
        audit.record(
            session,
            actor_id=caller.id,
            action="staff.role_changed",
            entity=STAFF_AUDIT,
            entity_id=staff.id,
            before=before,
            after=role_changed,
        )
        audit.record(
            session,
            actor_id=caller.id,
            action="staff.reactivated" if is_active else "staff.deactivated",
            entity=STAFF_AUDIT,
            entity_id=staff.id,
            before=role_changed,
            after=audit.snapshot(STAFF_AUDIT, staff),
        )
        await session.flush()
        await session.refresh(staff, ["updated_at"])
        result = StaffRead.model_validate(staff)

    fields = {"actor_staff_id": str(caller.id), "staff_id": str(staff_id)}
    if role != role_before:
        log_security_event(
            "staff_role_changed",
            sentry=role is StaffRole.ADMIN,  # a new admin is an alert
            **fields,
            role_before=role_before.value,
            role=role.value,
        )
    if is_active != active_before:
        log_security_event(
            "staff_reactivated" if is_active else "staff_deactivated",
            sentry=is_active and role is StaffRole.ADMIN,  # an admin back is an alert
            **fields,
            role=role.value,
        )
    return result


async def _require_active_admin(session: AsyncSession, caller: CurrentStaff) -> None:
    """The caller, re-read: demoted or deactivated since the request began is 403."""
    staff = await get_staff(session, caller.id)
    if staff is None or not staff.is_active or staff.role is not StaffRole.ADMIN:
        log_security_event(
            "permission_denied", sentry=False, reason="changed", staff_id=str(caller.id)
        )
        raise AppError(ErrorCode.FORBIDDEN)


async def _send_invite(auth: Inviter, invitation: StaffInvite) -> uuid.UUID:
    try:
        return await auth.invite_user(invitation.email, full_name=invitation.full_name)
    except AuthAccountExistsError:
        raise AppError(ErrorCode.AUTH_ACCOUNT_EXISTS) from None
    except AuthEmailRejectedError:
        raise RequestValidationError(
            [{"type": "value_error", "loc": ("body", "email"), "msg": "not accepted"}]
        ) from None
    except AuthAdminError as exc:
        # The message names the cause (never the key or the address): an operator's cue.
        logger.error("supabase_invite_failed", reason=str(exc))
        retry = None if exc.retry_after is None else {"Retry-After": str(exc.retry_after)}
        raise AppError(ErrorCode.SERVICE_UNAVAILABLE, headers=retry) from None
