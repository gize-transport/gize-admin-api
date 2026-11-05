"""GZ-040: migration 0017's downgrade refuses while seat offer SMS records exist."""

import asyncio

import pytest
from sqlalchemy import text

from gize_api.core.db import create_engine
from tests.support import DatabaseUrls, SettingsFactory, run_migrations

# A fixed statement: an offer SMS record as the service writes one.
OFFER_ROW = (
    "INSERT INTO gize.sms_messages (dedup_key, purpose, to_phone, status)"
    " VALUES ('offer:00000000-0000-0000-0000-000000000001', 'seat_offer',"
    " '+251911000001', 'sent')"
)


async def test_the_downgrade_refuses_while_offer_messages_exist(
    scratch_database: DatabaseUrls, make_settings: SettingsFactory
) -> None:
    await asyncio.to_thread(run_migrations, scratch_database.migrator, "head")
    engine = create_engine(make_settings(database_url=scratch_database.migrator))
    try:
        async with engine.begin() as connection:
            await connection.execute(text(OFFER_ROW))
    finally:
        await engine.dispose()
    with pytest.raises(RuntimeError, match="seat offer SMS records exist"):
        await asyncio.to_thread(run_migrations, scratch_database.migrator, "0016", downgrade=True)
