"""GZ-012: the audit log writer and the table's guarantees.

Rows are written in the change's own transaction (no change, no row), keep only what is
needed (values of declared non-personal fields; personal ones as "[redacted]"), and the
app role can only add and read them: append-only is enforced by the database's grants.
"""

import enum
import uuid
from datetime import UTC, date, datetime
from typing import Any

import asyncpg
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gize_api.core.audit import REDACTED, AuditedEntity, AuditEntry, record, snapshot
from gize_api.staff.models import StaffRole
from tests.factories import StaffFactory
from tests.support import DatabaseUrls

THING = AuditedEntity(
    type="thing", fields=("label", "secret_name", "status"), visible=frozenset({"label", "status"})
)
THING_ID = uuid.UUID(int=5)


async def _entries(session: AsyncSession) -> list[AuditEntry]:
    async with session.begin():
        return list((await session.scalars(select(AuditEntry))).all())


async def test_a_change_is_recorded_with_who_what_and_the_request(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("gize_api.core.audit.current_request_id", lambda: "req-12345678")
    async with db_session.begin():
        actor = await StaffFactory().create(db_session)
        entry = record(
            db_session,
            actor_id=actor.id,
            action="thing.updated",
            entity=THING,
            entity_id=THING_ID,
            before={"label": "old", "secret_name": "Abebe", "status": "draft"},
            after={"label": "new", "secret_name": "Kebede", "status": "draft"},
        )
    assert entry is not None
    (stored,) = await _entries(db_session)
    assert (stored.actor_staff_id, stored.action, stored.entity_type, stored.entity_id) == (
        actor.id,
        "thing.updated",
        "thing",
        THING_ID,
    )
    assert stored.request_id == "req-12345678"
    # Only what changed; a personal field shows that it changed, never its value.
    assert stored.before == {"label": "old", "secret_name": REDACTED}
    assert stored.after == {"label": "new", "secret_name": REDACTED}
    assert stored.created_at is not None


async def test_a_creation_and_a_removal(db_session: AsyncSession) -> None:
    values = {"label": "x", "secret_name": "Abebe", "status": None}
    async with db_session.begin():
        record(
            db_session, actor_id=None, action="thing.created", entity=THING,
            entity_id=THING_ID, before=None, after=values,
        )  # fmt: skip
        record(
            db_session, actor_id=None, action="thing.deleted", entity=THING,
            entity_id=THING_ID, before=values, after=None,
        )  # fmt: skip
    created, deleted = sorted(await _entries(db_session), key=lambda entry: entry.action)
    assert created.before is None
    assert created.after == {"label": "x", "secret_name": REDACTED, "status": None}
    assert deleted.before == {"label": "x", "secret_name": REDACTED, "status": None}
    assert deleted.after is None


async def test_nothing_changed_records_nothing(db_session: AsyncSession) -> None:
    values = {"label": "x", "secret_name": "Abebe", "status": "draft"}
    async with db_session.begin():
        entry = record(
            db_session, actor_id=None, action="thing.updated", entity=THING,
            entity_id=THING_ID, before=values, after=dict(values),
        )  # fmt: skip
    assert entry is None
    assert await _entries(db_session) == []


class Colour(enum.StrEnum):
    RED = "red"


async def test_values_are_stored_as_json(db_session: AsyncSession) -> None:
    entity = AuditedEntity(
        type="thing", fields=("a", "b", "c", "d", "e"), visible=frozenset("abcde")
    )
    after = {
        "a": uuid.UUID(int=1),
        "b": datetime(2026, 10, 29, 8, 0, tzinfo=UTC),
        "c": date(2026, 10, 29),
        "d": Colour.RED,
        "e": StaffRole.ADMIN,
    }
    async with db_session.begin():
        record(
            db_session, actor_id=None, action="thing.created", entity=entity,
            entity_id=THING_ID, before=None, after=after,
        )  # fmt: skip
    (stored,) = await _entries(db_session)
    assert stored.after == {
        "a": str(uuid.UUID(int=1)),
        "b": "2026-10-29T08:00:00+00:00",
        "c": "2026-10-29",
        "d": "red",
        "e": "admin",
    }


async def test_a_snapshot_reads_the_declared_fields(db_session: AsyncSession) -> None:
    entity = AuditedEntity(
        type="staff", fields=("full_name", "role", "is_active"), visible=frozenset({"role"})
    )
    async with db_session.begin():
        staff = await StaffFactory().create(db_session, full_name="Abebe", role=StaffRole.ADMIN)
    assert snapshot(entity, staff) == {
        "full_name": "Abebe",
        "role": StaffRole.ADMIN,
        "is_active": True,
    }


async def test_misuse_is_refused(db_session: AsyncSession) -> None:
    values = {"label": "x", "secret_name": "y", "status": "z"}
    with pytest.raises(RuntimeError, match="transaction"):
        record(
            db_session, actor_id=None, action="thing.created", entity=THING,
            entity_id=THING_ID, before=None, after=values,
        )  # fmt: skip
    async with db_session.begin():
        with pytest.raises(ValueError, match="action"):
            record(
                db_session, actor_id=None, action="Thing Created", entity=THING,
                entity_id=THING_ID, before=None, after=values,
            )  # fmt: skip
        with pytest.raises(ValueError, match="before or after"):
            record(
                db_session, actor_id=None, action="thing.created", entity=THING,
                entity_id=THING_ID, before=None, after=None,
            )  # fmt: skip
        with pytest.raises(ValueError, match="phone"):  # undeclared fields are refused
            record(
                db_session, actor_id=None, action="thing.created", entity=THING,
                entity_id=THING_ID, before=None, after={**values, "phone": "+251911000000"},
            )  # fmt: skip
        with pytest.raises(TypeError, match="JSON"):
            record(
                db_session, actor_id=None, action="thing.created", entity=THING,
                entity_id=THING_ID, before=None, after={**values, "label": object()},
            )  # fmt: skip


def test_an_entity_must_declare_its_visible_fields() -> None:
    with pytest.raises(ValueError, match="visible"):
        AuditedEntity(type="thing", fields=("a",), visible=frozenset({"b"}))
    with pytest.raises(ValueError, match="type"):
        AuditedEntity(type="Thing!", fields=("a",), visible=frozenset())


async def test_a_failed_transaction_leaves_no_audit_row(
    committed_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """The acceptance criterion: the audit row lives and dies with the change."""

    async def a_change_that_fails() -> None:
        async with committed_sessionmaker() as session, session.begin():
            record(
                session, actor_id=None, action="thing.created", entity=THING,
                entity_id=THING_ID, before=None, after={"label": "x"},
            )  # fmt: skip
            await session.flush()  # the row reached the database...
            raise RuntimeError("the change failed")  # ...and is rolled back with it

    with pytest.raises(RuntimeError, match="the change failed"):
        await a_change_that_fails()
    async with committed_sessionmaker() as session, session.begin():
        assert await session.scalar(select(func.count()).select_from(AuditEntry)) == 0


async def _insert(session: AsyncSession, values: dict[str, Any]) -> None:
    async with session.begin():
        session.add(AuditEntry(entity_type="thing", entity_id=THING_ID, **values))
        await session.flush()


async def test_the_database_refuses_malformed_rows(db_session: AsyncSession) -> None:
    for values in (
        {"action": "not an action", "after": {"a": 1}},
        {"action": "thing.created", "before": None, "after": None},
    ):
        with pytest.raises(IntegrityError):
            await _insert(db_session, values)


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE gize.audit_log SET action = 'thing.forged'",
        "DELETE FROM gize.audit_log",
        "TRUNCATE gize.audit_log",
    ],
)
async def test_the_app_role_cannot_change_or_remove_audit_rows(
    database: DatabaseUrls, statement: str
) -> None:
    conn = await asyncpg.connect(database.app, timeout=3)
    try:
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await conn.execute(statement)
    finally:
        await conn.close()


async def test_the_app_role_may_add_and_read_only(database: DatabaseUrls) -> None:
    conn = await asyncpg.connect(database.app, timeout=3)
    try:
        granted = {
            privilege
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE")
            if await conn.fetchval(
                "SELECT has_table_privilege('gize_app', 'gize.audit_log', $1)", privilege
            )
        }
    finally:
        await conn.close()
    assert granted == {"SELECT", "INSERT"}
