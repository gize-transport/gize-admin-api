"""GZ-029: company accounts, the postpaid customers that corporate routes belong to.

Any active staff member manages companies (including deactivating one); only admins delete
(soft delete). The contact person's details are personal data: redacted in the audit log.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from gize_api.companies import service
from gize_api.companies.models import Company
from gize_api.core.audit import REDACTED
from gize_api.core.db import get_session
from gize_api.core.errors import AppError, ErrorCode
from gize_api.main import GizeAPI, create_app
from tests.factories import CompanyFactory
from tests.staff.support import add_admin, add_staff, audit_entries, signed_in, with_key
from tests.support import SettingsFactory

COMPANIES = "/v1/companies"
START = datetime(2026, 10, 29, 6, 30, tzinfo=UTC)
ACME = {
    "name": "Acme Ethiopia",
    "contact_name": "Hanna Bekele",
    "contact_phone": "0911 23 45 67",
    "contact_email": " Hanna@Acme.ET ",
}


async def _company(session: AsyncSession, **values: object) -> uuid.UUID:
    async with session.begin():
        return (await CompanyFactory().create(session, **values)).id


async def _reread(session: AsyncSession, company_id: uuid.UUID) -> Company:
    async with session.begin():
        return await session.get_one(Company, company_id)


def _member(company_id: uuid.UUID) -> str:
    return f"{COMPANIES}/{company_id}"


async def _changes(
    session: AsyncSession, company_id: uuid.UUID
) -> list[tuple[str, object, object]]:
    return [(e.action, e.before, e.after) for e in await audit_entries(session, company_id)]


# Create


async def test_staff_add_a_company(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    member = await add_staff(db_session)
    body = {**ACME, "notes": "  Pays by bank transfer.\nInvoices to finance.  "}
    response = await api.post(COMPANIES, json=body, headers=with_key(member.id))
    assert response.status_code == 201
    created = response.json()
    assert created == {
        "id": created["id"],
        "name": "Acme Ethiopia",
        "contact_name": "Hanna Bekele",
        "contact_phone": "+251911234567",  # one spelling
        "contact_email": "hanna@acme.et",
        "billing": "postpaid",
        "notes": "Pays by bank transfer.\nInvoices to finance.",
        "is_active": True,
        "created_at": created["created_at"],
        "updated_at": created["updated_at"],
    }
    assert response.headers["Location"] == f"{COMPANIES}/{created['id']}"
    assert response.headers["Cache-Control"] == "no-store"
    (entry,) = await audit_entries(db_session, uuid.UUID(created["id"]))
    assert (entry.action, entry.actor_staff_id, entry.before) == (
        "company.created",
        member.id,
        None,
    )
    assert entry.after == {
        "name": "Acme Ethiopia",
        "contact_name": REDACTED,  # personal data
        "contact_phone": REDACTED,
        "contact_email": REDACTED,
        "billing": "postpaid",
        "notes": REDACTED,
        "is_active": True,
        "deleted_at": None,
    }


async def test_notes_are_optional(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    member = await add_staff(db_session)
    for notes in (None, "   "):
        body = {**ACME, "name": f"Acme {notes!r}", **({} if notes is None else {"notes": notes})}
        response = await api.post(COMPANIES, json=body, headers=with_key(member.id))
        assert (response.status_code, response.json()["notes"]) == (201, None)


async def test_a_retried_create_replays(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    member = await add_staff(db_session)
    headers = with_key(member.id)
    first = await api.post(COMPANIES, json=ACME, headers=headers)
    again = await api.post(COMPANIES, json=ACME, headers=headers)
    assert (first.status_code, again.status_code) == (201, 201)
    assert (again.json(), again.headers["Idempotent-Replayed"]) == (first.json(), "true")
    missing = await api.post(COMPANIES, json=ACME, headers=signed_in(member.id))
    assert missing.status_code == 428


async def test_a_name_in_use_is_409_ignoring_case(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member_id = (await add_staff(db_session)).id
    await _company(db_session, name="Acme Ethiopia")
    body = {**ACME, "name": "ACME  ethiopia"}
    response = await api.post(COMPANIES, json=body, headers=with_key(member_id))
    assert (response.status_code, response.json()["code"]) == (409, "COMPANY_NAME_TAKEN")


async def test_a_deleted_companys_name_can_be_used_again(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member = await add_staff(db_session)
    await _company(db_session, name="Acme Ethiopia", deleted_at=START)
    assert (await api.post(COMPANIES, json=ACME, headers=with_key(member.id))).status_code == 201


@pytest.mark.parametrize(
    "change",
    [
        {"name": ""},
        {"name": "x" * 101},
        {"contact_name": " "},
        {"contact_phone": "0111234567"},  # a landline
        {"contact_phone": "not a phone"},
        {"contact_email": "hanna"},
        {"contact_email": "hanna@acme"},
        {"notes": "x" * 1001},
        {"notes": "bell \u0007"},
        {"notes": "\u0958" * 1000},  # 1000 characters that NFC makes 2000
        {"billing": "prepaid"},  # always postpaid: not a field
        {"is_active": False},  # a new company is active
        {"surprise": 1},
    ],
)
async def test_invalid_bodies_are_422(
    api: httpx.AsyncClient, db_session: AsyncSession, change: dict[str, object]
) -> None:
    member = await add_staff(db_session)
    response = await api.post(COMPANIES, json={**ACME, **change}, headers=with_key(member.id))
    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_FAILED")


@pytest.mark.parametrize("missing", ["name", "contact_name", "contact_phone", "contact_email"])
async def test_required_fields(
    api: httpx.AsyncClient, db_session: AsyncSession, missing: str
) -> None:
    member = await add_staff(db_session)
    body = {key: value for key, value in ACME.items() if key != missing}
    response = await api.post(COMPANIES, json=body, headers=with_key(member.id))
    assert response.status_code == 422


# List and read


async def test_staff_list_companies_newest_first(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member = await add_staff(db_session)
    older = await _company(db_session, name="Acme", created_at=START)
    newer = await _company(db_session, name="Zemen", created_at=START + timedelta(1))
    await _company(db_session, deleted_at=START, created_at=START + timedelta(2))
    response = await api.get(COMPANIES, headers=signed_in(member.id))
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert [item["id"] for item in response.json()["items"]] == [str(newer), str(older)]


async def test_filters(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    member = await add_staff(db_session)
    acme = await _company(db_session, name="Acme Ethiopia")
    acme_old = await _company(db_session, name="Acme Old", is_active=False)
    await _company(db_session, name="Zemen Bank")

    async def found(**params: str) -> set[str]:
        response = await api.get(COMPANIES, params=params, headers=signed_in(member.id))
        return {item["id"] for item in response.json()["items"]}

    assert await found(q="acme") == {str(acme), str(acme_old)}
    assert await found(q="acme", is_active="true") == {str(acme)}
    assert await found(is_active="false") == {str(acme_old)}
    assert await found(q="  ") == await found()  # blank is no filter
    assert await found(q="50%") == set()


async def test_pages_follow_the_cursor(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    member = await add_staff(db_session)
    made = [await _company(db_session, created_at=START + timedelta(minutes=i)) for i in range(5)]
    seen: list[str] = []
    cursor: str | None = None
    for _ in range(3):
        params = {"limit": "2", **({"cursor": cursor} if cursor else {})}
        body = (await api.get(COMPANIES, params=params, headers=signed_in(member.id))).json()
        seen += [item["id"] for item in body["items"]]
        cursor = body["next_cursor"]
    assert (seen, cursor) == ([str(c) for c in reversed(made)], None)


async def test_a_page_takes_the_same_queries_however_long(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member = await add_staff(db_session)
    bind = db_session.bind
    assert isinstance(bind, AsyncConnection)
    counts = []
    for rows in (1, 20):
        async with db_session.begin():
            await CompanyFactory().create_batch(db_session, rows)
        statements: list[str] = []

        def capture(*args: object, into: list[str] = statements) -> None:
            into.append(str(args[2]))

        event.listen(bind.sync_engine, "before_cursor_execute", capture)
        try:
            assert (await api.get(COMPANIES, headers=signed_in(member.id))).status_code == 200
        finally:
            event.remove(bind.sync_engine, "before_cursor_execute", capture)
        counts.append(len([s for s in statements if s.lstrip().upper().startswith("SELECT")]))
    assert counts == [2, 2]


@pytest.mark.parametrize(
    ("params", "code"),
    [
        ({"surprise": "1"}, "VALIDATION_FAILED"),
        ({"is_active": "maybe"}, "VALIDATION_FAILED"),
        ({"q": "x" * 101}, "VALIDATION_FAILED"),
        ({"cursor": "garbage"}, "INVALID_CURSOR"),
    ],
)
async def test_invalid_list_parameters_are_422(
    api: httpx.AsyncClient, db_session: AsyncSession, params: dict[str, str], code: str
) -> None:
    member = await add_staff(db_session)
    response = await api.get(COMPANIES, params=params, headers=signed_in(member.id))
    assert (response.status_code, response.json()["code"]) == (422, code)


async def test_staff_read_one_company(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    member = await add_staff(db_session)
    company = await _company(db_session, name="Acme Ethiopia", is_active=False)
    response = await api.get(_member(company), headers=signed_in(member.id))
    assert (response.status_code, response.json()["is_active"]) == (200, False)


@pytest.mark.parametrize("deleted", [False, True])
async def test_unknown_or_deleted_companies_are_404(
    api: httpx.AsyncClient, db_session: AsyncSession, deleted: bool
) -> None:
    admin_id = (await add_admin(db_session)).id
    target = await _company(db_session, deleted_at=START) if deleted else uuid.uuid4()
    for method in ("GET", "PATCH", "DELETE"):
        response = await api.request(
            method, _member(target), json={"notes": "x"}, headers=signed_in(admin_id)
        )
        assert (response.status_code, response.json()["code"]) == (404, "NOT_FOUND"), method


# Edit


async def test_staff_change_the_contact(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    member = await add_staff(db_session)
    company = await _company(db_session, contact_phone="+251911234567")
    body = {"contact_phone": "07 1234 5678", "contact_name": "Abebe Kebede"}
    response = await api.patch(_member(company), json=body, headers=signed_in(member.id))
    assert response.status_code == 200
    assert response.json()["contact_phone"] == "+251712345678"
    assert await _changes(db_session, company) == [
        (
            "company.updated",
            {"contact_name": REDACTED, "contact_phone": REDACTED},
            {"contact_name": REDACTED, "contact_phone": REDACTED},
        )
    ]


async def test_an_empty_note_clears_the_notes(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member = await add_staff(db_session)
    company = await _company(db_session, notes="Old note")
    response = await api.patch(_member(company), json={"notes": "  "}, headers=signed_in(member.id))
    assert (response.status_code, response.json()["notes"]) == (200, None)


async def test_deactivating_and_reactivating_are_their_own_entries(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member = await add_staff(db_session)  # staff may deactivate (owner decision, GZ-025)
    company = await _company(db_session, name="Acme")
    off = await api.patch(_member(company), json={"is_active": False}, headers=signed_in(member.id))
    assert (off.status_code, off.json()["is_active"]) == (200, False)
    both = {"is_active": True, "name": "Acme Ethiopia"}
    on = await api.patch(_member(company), json=both, headers=signed_in(member.id))
    assert on.status_code == 200
    assert sorted(await _changes(db_session, company)) == [
        ("company.deactivated", {"is_active": True}, {"is_active": False}),
        ("company.reactivated", {"is_active": False}, {"is_active": True}),
        ("company.updated", {"name": "Acme"}, {"name": "Acme Ethiopia"}),
    ]


async def test_an_edit_to_what_is_there_records_nothing(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member = await add_staff(db_session)
    company = await _company(db_session, name="Acme", contact_phone="+251911234567")
    body = {"name": " Acme ", "contact_phone": "0911234567", "is_active": True}
    response = await api.patch(_member(company), json=body, headers=signed_in(member.id))
    assert response.status_code == 200
    assert await _changes(db_session, company) == []


async def test_renaming_to_a_name_in_use_is_409(
    api: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    member_id = (await add_staff(db_session)).id
    await _company(db_session, name="Zemen Bank")
    company = await _company(db_session, name="Acme")
    response = await api.patch(
        _member(company), json={"name": "zemen bank"}, headers=signed_in(member_id)
    )
    assert (response.status_code, response.json()["code"]) == (409, "COMPANY_NAME_TAKEN")
    assert (await _reread(db_session, company)).name == "Acme"


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"name": None},
        {"contact_email": None},
        {"contact_phone": "123"},
        {"billing": "prepaid"},
        {"is_active": "no"},
        {"surprise": 1},
    ],
)
async def test_invalid_edits_are_422(
    api: httpx.AsyncClient, db_session: AsyncSession, body: dict[str, object]
) -> None:
    member = await add_staff(db_session)
    company = await _company(db_session)
    response = await api.patch(_member(company), json=body, headers=signed_in(member.id))
    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_FAILED")


# Delete


async def test_admins_delete_a_company(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    admin = await add_admin(db_session)
    company = await _company(db_session)
    response = await api.delete(_member(company), headers=signed_in(admin.id))
    assert (response.status_code, response.content) == (204, b"")
    assert (await _reread(db_session, company)).deleted_at is not None
    (entry,) = await audit_entries(db_session, company)
    assert (entry.action, entry.actor_staff_id) == ("company.deleted", admin.id)


async def test_staff_cannot_delete(api: httpx.AsyncClient, db_session: AsyncSession) -> None:
    member = await add_staff(db_session)
    company = await _company(db_session)
    assert (await api.delete(_member(company), headers=signed_in(member.id))).status_code == 403
    assert (await _reread(db_session, company)).deleted_at is None


# Who may call


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", COMPANIES),
        ("GET", COMPANIES),
        ("GET", f"{COMPANIES}/{uuid.UUID(int=1)}"),
        ("PATCH", f"{COMPANIES}/{uuid.UUID(int=1)}"),
        ("DELETE", f"{COMPANIES}/{uuid.UUID(int=1)}"),
    ],
)
async def test_callers_must_be_active_staff(
    api: httpx.AsyncClient, db_session: AsyncSession, method: str, path: str
) -> None:
    gone_id = (await add_admin(db_session, is_active=False)).id
    key = {"Idempotency-Key": str(uuid.uuid4())}
    assert (await api.request(method, path, json=ACME, headers=key)).status_code == 401
    for caller in (uuid.uuid4(), gone_id):
        headers = {**signed_in(caller), **key}
        assert (await api.request(method, path, json=ACME, headers=headers)).status_code == 403


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", COMPANIES),
        ("GET", COMPANIES),
        ("GET", f"{COMPANIES}/{uuid.UUID(int=1)}"),
        ("PATCH", f"{COMPANIES}/{uuid.UUID(int=1)}"),
        ("DELETE", f"{COMPANIES}/{uuid.UUID(int=1)}"),
    ],
)
async def test_the_database_down_is_503(
    api: httpx.AsyncClient,
    api_app: GizeAPI,
    unreachable_sessions: AsyncSession,
    method: str,
    path: str,
) -> None:
    async def down() -> AsyncIterator[AsyncSession]:
        yield unreachable_sessions

    api_app.dependency_overrides[get_session] = down
    response = await api.request(method, path, json=ACME, headers=with_key(uuid.uuid4()))
    assert response.status_code == 503


async def test_the_database_refuses_a_phone_not_in_e164(db_session: AsyncSession) -> None:
    with pytest.raises(IntegrityError):
        async with db_session.begin():
            await CompanyFactory().create(db_session, contact_phone="0911234567")


def test_companies_are_in_the_contract(make_settings: SettingsFactory) -> None:
    schema = create_app(make_settings()).openapi()
    collection = schema["paths"][COMPANIES]
    member = schema["paths"][f"{COMPANIES}/{{company_id}}"]
    assert {op["operationId"] for op in collection.values()} == {
        "companies_create",
        "companies_list",
    }
    assert {op["operationId"] for op in member.values()} == {
        "companies_get",
        "companies_update",
        "companies_delete",
    }
    assert collection["post"]["responses"]["409"]["x-error-codes"] == [
        "COMPANY_NAME_TAKEN",
        "IDEMPOTENCY_IN_PROGRESS",
    ]
    schemas = schema["components"]["schemas"]
    billing = schemas["CompanyRead"]["properties"]["billing"]
    assert billing == {"$ref": "#/components/schemas/CompanyBilling"}
    assert schemas["CompanyBilling"]["enum"] == ["postpaid"]
    assert schemas["CompanyUpdate"]["minProperties"] == 1
    assert set(schemas["CompanyCreate"]["required"]) == {
        "name",
        "contact_name",
        "contact_phone",
        "contact_email",
    }


async def test_other_constraint_errors_are_not_reported_as_a_name_taken(
    db_session: AsyncSession,
) -> None:
    await _company(db_session, name="Acme")

    async def flush_new(**values: object) -> None:
        async with db_session.begin():
            db_session.add(Company(**{**ACME, "contact_phone": "+251911234567", **values}))
            await service._flush(db_session)

    with pytest.raises(IntegrityError):
        await flush_new(name="Zemen", contact_phone="0911234567")  # not E.164
    with pytest.raises(AppError) as taken:
        await flush_new(name="ACME", contact_email="hanna@acme.et")
    assert taken.value.code is ErrorCode.COMPANY_NAME_TAKEN
