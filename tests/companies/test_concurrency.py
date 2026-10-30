"""GZ-029: company changes at the same time (committed rows)."""

import asyncio

import asyncpg
import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.factories import CompanyFactory
from tests.staff.support import add_admin, add_staff, signed_in, with_key
from tests.support import DatabaseUrls, wait_for_lock_waiters

ACME = {
    "name": "Acme Ethiopia",
    "contact_name": "Hanna Bekele",
    "contact_phone": "0911234567",
    "contact_email": "hanna@acme.et",
}


async def test_two_creates_of_one_name_make_one_company(
    committed_api: httpx.AsyncClient, committed_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with committed_sessionmaker() as session:
        member = await add_staff(session)
    responses = await asyncio.gather(
        *(committed_api.post("/v1/companies", json=ACME, headers=with_key(member.id)) for _ in "ab")
    )
    assert sorted(r.status_code for r in responses) == [201, 409]
    assert {r.json()["code"] for r in responses if r.status_code == 409} == {"COMPANY_NAME_TAKEN"}


@pytest.mark.parametrize(("method", "body"), [("PATCH", {"notes": "x"}), ("DELETE", None)])
async def test_a_change_waiting_on_a_delete_finds_the_company_gone(
    committed_api: httpx.AsyncClient,
    committed_sessionmaker: async_sessionmaker[AsyncSession],
    database: DatabaseUrls,
    method: str,
    body: object,
) -> None:
    async with committed_sessionmaker() as session:
        admin = await add_admin(session)
        async with session.begin():
            company = await CompanyFactory().create(session)
    conn = await asyncpg.connect(database.migrator, timeout=3)
    try:
        async with conn.transaction():
            await conn.execute("SELECT 1 FROM gize.companies WHERE id = $1 FOR UPDATE", company.id)
            request = asyncio.create_task(
                committed_api.request(
                    method, f"/v1/companies/{company.id}", json=body, headers=signed_in(admin.id)
                )
            )
            await wait_for_lock_waiters(database)
            await conn.execute(
                "UPDATE gize.companies SET deleted_at = now() WHERE id = $1", company.id
            )
    finally:
        await conn.close()
    response = await request
    assert (response.status_code, response.json()["code"]) == (404, "NOT_FOUND")
