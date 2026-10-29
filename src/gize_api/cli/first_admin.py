"""Make an existing Supabase user the first Gize admin (GZ-014).

    python -m gize_api.cli.first_admin --user-id <supabase user id> --name "Full Name"

Create the user in the Supabase dashboard first (Authentication → Users → Add user); its
ID is the user's UID there. The command reads the user from Supabase's admin API (needs
SUPABASE_SECRET_KEY), then, in one transaction, refuses if any admin exists and otherwise
creates the user as an active admin (or promotes their staff row). It holds the lock every
staff change takes across the check and the write, so two runs at once cannot both
succeed, nor can a run and an admin's change in the portal interleave. Afterwards admins
add staff in the admin portal (GZ-013).

Exit codes: 0 done, 1 refused or a dependency failed (nothing changed), 2 usage or settings.
"""

import argparse
import asyncio
import sys
import uuid
from collections.abc import Sequence
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gize_api.core.audit import record, snapshot
from gize_api.core.config import ConfigError, Settings, load_settings
from gize_api.core.db import (
    create_engine,
    create_sessionmaker,
    database_sqlstate,
    is_database_misconfigured,
    is_database_unavailable,
)
from gize_api.core.http import OutboundClient
from gize_api.core.logging import configure_logging, log_security_event
from gize_api.core.sentry import configure_sentry
from gize_api.integrations.supabase_auth import AuthAdminError, AuthUser, SupabaseAuthAdmin
from gize_api.staff.models import (
    EMAIL_MAX_LENGTH,
    FULL_NAME_MAX_LENGTH,
    STAFF_AUDIT,
    Staff,
    StaffRole,
)
from gize_api.staff.repository import (
    admin_exists,
    get_staff,
    get_staff_by_email,
    lock_staff_changes,
)
from gize_api.staff.schemas import check_full_name

LOCK_NOT_AVAILABLE = "55P03"  # the role's lock_timeout ran out


class FirstAdminRefusedError(Exception):
    """The command will not make this user the first admin; nothing was changed."""


class _ReadsUsers(Protocol):
    async def get_user(self, user_id: uuid.UUID) -> AuthUser | None: ...


async def create_first_admin(
    sessions: async_sessionmaker[AsyncSession],
    auth: _ReadsUsers,
    user_id: uuid.UUID,
    full_name: str,
) -> None:
    """Raise FirstAdminRefusedError (or AuthAdminError) without changing anything, or commit."""
    # Supabase first: never hold a transaction open across an outside call.
    email = _eligible_email(await auth.get_user(user_id))
    async with sessions() as session, session.begin():
        await lock_staff_changes(session)  # the lock every staff change takes
        if await admin_exists(session):
            raise FirstAdminRefusedError("an admin already exists: add staff in the admin portal")
        other = await get_staff_by_email(session, email)
        if other is not None and other.id != user_id:
            raise FirstAdminRefusedError("another staff member already uses this email")
        staff = await get_staff(session, user_id)
        before = None if staff is None else snapshot(STAFF_AUDIT, staff)
        if staff is None:
            staff = Staff(
                id=user_id, full_name=full_name, email=email, role=StaffRole.ADMIN, is_active=True
            )
            session.add(staff)
        else:
            staff.full_name, staff.email = full_name, email
            staff.role, staff.is_active = StaffRole.ADMIN, True
        record(
            session,
            actor_id=None,
            operator=True,  # an operator at a shell, not a staff member
            action="staff.first_admin_created",
            entity=STAFF_AUDIT,
            entity_id=user_id,
            before=before,
            after=snapshot(STAFF_AUDIT, staff),
        )
    log_security_event("first_admin_created", staff_id=str(user_id))


def _eligible_email(user: AuthUser | None) -> str:
    if user is None:
        raise FirstAdminRefusedError("no Supabase user has this ID: create the user first")
    if user.deleted:
        raise FirstAdminRefusedError("the Supabase user is deleted")
    if user.banned:
        raise FirstAdminRefusedError("the Supabase user is banned")
    if user.anonymous:
        raise FirstAdminRefusedError("the Supabase user is an anonymous sign-in")
    if user.email is None:
        raise FirstAdminRefusedError("the Supabase user has no email")
    if not user.email_confirmed:
        raise FirstAdminRefusedError("the Supabase user's email is not confirmed")
    if user.email.find("@") < 1 or len(user.email) > EMAIL_MAX_LENGTH:
        raise FirstAdminRefusedError("the Supabase user's email cannot be stored")
    return user.email


def _full_name(value: str) -> str:
    name = value.strip()
    if not name or len(name) > FULL_NAME_MAX_LENGTH:
        raise argparse.ArgumentTypeError(f"must be 1 to {FULL_NAME_MAX_LENGTH} characters")
    try:
        return check_full_name(name)  # the rule the API applies to names
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


async def _run(settings: Settings, user_id: uuid.UUID, full_name: str) -> None:
    auth = SupabaseAuthAdmin.for_settings(
        settings,
        telemetry=None,
        outbound=OutboundClient(allowed_hosts={settings.supabase_host}),
    )
    engine = create_engine(settings)
    try:
        await create_first_admin(create_sessionmaker(engine), auth, user_id, full_name)
    finally:
        await auth.aclose()
        await engine.dispose()


def _database_problem(exc: Exception) -> str | None:
    """What to tell the operator about a database failure, or None if it is not one."""
    if database_sqlstate(exc) == LOCK_NOT_AVAILABLE:
        return "another staff change is in progress; try again in a moment"
    if not is_database_unavailable(exc):
        return None
    if is_database_misconfigured(exc):
        return "the database refused the connection; check DATABASE_URL"
    # The connection may have dropped during COMMIT: the change may have been saved.
    return (
        "the database is unavailable: the change may not have been saved; "
        "running the command again is safe"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m gize_api.cli.first_admin",
        description="Make an existing Supabase user the first Gize admin.",
    )
    parser.add_argument(
        "--user-id", type=uuid.UUID, required=True, help="the user's UID in Supabase Auth"
    )
    parser.add_argument("--name", type=_full_name, required=True, help="their full name")
    args = parser.parse_args(argv)
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"first_admin: {exc}", file=sys.stderr)  # noqa: T201 - CLI
        return 2
    if settings.supabase_secret_key is None:
        print("first_admin: set SUPABASE_SECRET_KEY (see README)", file=sys.stderr)  # noqa: T201 - CLI
        return 2
    configure_logging(settings.log_level)
    # The new admin is in the audit log; Sentry also alerts on it (a security event).
    configure_sentry(settings)
    try:
        asyncio.run(_run(settings, args.user_id, args.name))
    except FirstAdminRefusedError as exc:
        print(f"first_admin: refused: {exc}", file=sys.stderr)  # noqa: T201 - CLI
        return 1
    except AuthAdminError as exc:
        print(f"first_admin: {exc}; nothing was changed", file=sys.stderr)  # noqa: T201 - CLI
        return 1
    except Exception as exc:
        problem = _database_problem(exc)
        if problem is None:
            raise
        print(f"first_admin: {problem}", file=sys.stderr)  # noqa: T201 - CLI
        return 1
    print(f"first_admin: done: {args.user_id} is now an admin", file=sys.stderr)  # noqa: T201 - CLI
    return 0


if __name__ == "__main__":  # pragma: no cover - run from a shell
    sys.exit(main())
