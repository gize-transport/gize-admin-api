"""GZ-012: the audit log writer and the table's guarantees.

Rows are written in the change's own transaction (no change, no row), keep only what is
needed (values of declared non-personal fields; personal ones as "[redacted]"), and the
app role can only add and read them: append-only is enforced by the database's grants.
"""

import asyncio
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
from tests.support import DatabaseUrls, run_migrations

THING = AuditedEntity(
    type="thing", fields=("label", "secret_name", "status"), visible=frozenset({"label", "status"})
)
THING_ID = uuid.UUID(int=5)
FULL = {"label": "x", "secret_name": "Abebe", "status": "draft"}


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
            db_session, actor_id=None, operator=True, action="thing.created", entity=THING,
            entity_id=THING_ID, before=None, after=values,
        )  # fmt: skip
        record(
            db_session, actor_id=None, operator=True, action="thing.deleted", entity=THING,
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
            db_session, actor_id=None, operator=True, action="thing.updated", entity=THING,
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
            db_session, actor_id=None, operator=True, action="thing.created", entity=entity,
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
            db_session, actor_id=None, operator=True, action="thing.created", entity=THING,
            entity_id=THING_ID, before=None, after=values,
        )  # fmt: skip
    async with db_session.begin():
        with pytest.raises(ValueError, match="action"):
            record(
                db_session, actor_id=None, operator=True, action="Thing Created", entity=THING,
                entity_id=THING_ID, before=None, after=values,
            )  # fmt: skip
        with pytest.raises(ValueError, match="before or after"):
            record(
                db_session, actor_id=None, operator=True, action="thing.created", entity=THING,
                entity_id=THING_ID, before=None, after=None,
            )  # fmt: skip
        with pytest.raises(ValueError, match="phone"):  # undeclared fields are refused
            record(
                db_session, actor_id=None, operator=True, action="thing.created", entity=THING,
                entity_id=THING_ID, before=None, after={**values, "phone": "+251911000000"},
            )  # fmt: skip
        with pytest.raises(TypeError, match="JSON"):
            record(
                db_session, actor_id=None, operator=True, action="thing.created", entity=THING,
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
                session, actor_id=None, operator=True, action="thing.created", entity=THING,
                entity_id=THING_ID, before=None, after=FULL,
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
        insertable = {
            column
            for column in ("id", "created_at", "action", "before", "after", "request_id")
            if await conn.fetchval(
                "SELECT has_column_privilege('gize_app', 'gize.audit_log', $1, 'INSERT')", column
            )
        }
    finally:
        await conn.close()
    assert granted == {"SELECT"}  # INSERT only on the content columns
    assert insertable == {"action", "before", "after", "request_id"}


@pytest.mark.parametrize(
    "after",
    [
        {"label": "x"},  # a missing field would read as "cleared"
        {"label": "x", "secret_name": "y"},
    ],
)
async def test_every_declared_field_is_required(
    db_session: AsyncSession, after: dict[str, object]
) -> None:
    async with db_session.begin():
        with pytest.raises(ValueError, match="exactly"):
            record(
                db_session, actor_id=None, operator=True, action="thing.created", entity=THING,
                entity_id=THING_ID, before=None, after=after,
            )  # fmt: skip


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (float("nan"), "finite"),
        (float("inf"), "finite"),
        (datetime(2026, 10, 29, 8, 0), "time zone"),  # noqa: DTZ001 - the case under test
    ],
)
async def test_values_json_or_postgres_would_refuse_are_refused(
    db_session: AsyncSession, value: object, message: str
) -> None:
    async with db_session.begin():
        with pytest.raises(ValueError, match=message):
            record(
                db_session, actor_id=None, operator=True, action="thing.created", entity=THING,
                entity_id=THING_ID, before=None, after={**FULL, "label": value},
            )  # fmt: skip


async def test_a_missing_actor_must_be_meant(db_session: AsyncSession) -> None:
    """None is for operator commands only; a service that forgot its actor fails loudly."""
    async with db_session.begin():
        actor = await StaffFactory().create(db_session)
        with pytest.raises(ValueError, match="actor"):
            record(
                db_session, actor_id=None, action="thing.created", entity=THING,
                entity_id=THING_ID, before=None, after=FULL,
            )  # fmt: skip
        with pytest.raises(ValueError, match="actor"):
            record(
                db_session, actor_id=actor.id, operator=True, action="thing.created",
                entity=THING, entity_id=THING_ID, before=None, after=FULL,
            )  # fmt: skip


async def test_the_app_role_cannot_choose_the_id_or_time(database: DatabaseUrls) -> None:
    """They come from the database, so a bug cannot backdate an entry."""
    conn = await asyncpg.connect(database.app, timeout=3)
    try:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO gize.audit_log (action, entity_type, entity_id, after)"
                " VALUES ('thing.created', 'thing', $1, '{}')",
                THING_ID,
            )
            backdated = datetime(2000, 1, 1, tzinfo=UTC)
            for column, value in (("created_at", backdated), ("id", uuid.uuid4())):
                with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                    async with conn.transaction():
                        await conn.execute(
                            f"INSERT INTO gize.audit_log ({column}, action, entity_type,"  # noqa: S608 - test constants
                            " entity_id, after) VALUES ($2, 'thing.created', 'thing', $1, '{}')",
                            THING_ID,
                            value,
                        )
            raise RollbackError
    except RollbackError:
        pass
    finally:
        await conn.close()


class RollbackError(Exception):
    """Ends the probe's transaction: the row it added is not kept."""


async def test_restrictive_policies_back_up_the_grants(database: DatabaseUrls) -> None:
    """If UPDATE or DELETE were ever granted again, row-level security still refuses them."""
    conn = await asyncpg.connect(database.migrator, timeout=3)
    try:
        rows = await conn.fetch(
            "SELECT cmd, permissive, roles::text[] AS roles, qual FROM pg_policies"
            " WHERE schemaname = 'gize' AND tablename = 'audit_log' AND permissive = 'RESTRICTIVE'"
        )
    finally:
        await conn.close()
    assert {(row["cmd"], tuple(row["roles"]), row["qual"]) for row in rows} == {
        ("UPDATE", ("gize_app",), "false"),
        ("DELETE", ("gize_app",), "false"),
    }


async def test_the_downgrade_refuses_to_drop_recorded_history(
    scratch_database: DatabaseUrls,
) -> None:
    await asyncio.to_thread(run_migrations, scratch_database.migrator, "head")
    conn = await asyncpg.connect(scratch_database.migrator, timeout=3)
    try:
        await conn.execute(
            "INSERT INTO gize.audit_log (action, entity_type, entity_id, after)"
            " VALUES ('thing.created', 'thing', $1, '{}')",
            THING_ID,
        )
        with pytest.raises(Exception, match="audit_log has rows"):
            await asyncio.to_thread(
                run_migrations, scratch_database.migrator, "0003", downgrade=True
            )
        await conn.execute("DELETE FROM gize.audit_log")  # as its owner, after a backup
    finally:
        await conn.close()
    await asyncio.to_thread(run_migrations, scratch_database.migrator, "0003", downgrade=True)
