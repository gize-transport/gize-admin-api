"""GZ-041: migration 0016's downgrade refuses while launch SMS records exist: they are
records of messages that went out, never deleted to roll back."""

import asyncio

import pytest
from sqlalchemy import text

from gize_api.core.db import create_engine
from tests.support import DatabaseUrls, SettingsFactory, run_migrations

# A fixed statement: a launch SMS record as the service writes one.
LAUNCH_ROW = (
    "INSERT INTO gize.sms_messages (dedup_key, purpose, to_phone, status)"
    " VALUES ('launch:00000000-0000-0000-0000-000000000001:1', 'route_launch',"
    " '+251911000001', 'sent')"
)


async def test_the_downgrade_refuses_while_launch_messages_exist(
    scratch_database: DatabaseUrls, make_settings: SettingsFactory
) -> None:
    await asyncio.to_thread(run_migrations, scratch_database.migrator, "head")
    engine = create_engine(make_settings(database_url=scratch_database.migrator))
    try:
        async with engine.begin() as connection:
            await connection.execute(text(LAUNCH_ROW))
    finally:
        await engine.dispose()
    with pytest.raises(RuntimeError, match="launch SMS records exist"):
        await asyncio.to_thread(run_migrations, scratch_database.migrator, "0015", downgrade=True)
