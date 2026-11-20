"""GZ-082: the other staff lists sort by name, A to Z ignoring case (GZ-081's `sort`).

Drivers, companies, areas and staff by their name; corridors, which have none of their
own, by their A area's name, then their B area's. Newest first stays the default. The
same name always comes in one order (by id), so pages split between them repeat and skip
no one. Sign-in, roles and a database failure are covered per list in the lists' own
tests and `tests/test_authorization.py`.
"""

import base64
import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from tests.factories import AreaFactory, CompanyFactory, CorridorFactory
from tests.routes.support import add_driver
from tests.staff.support import add_admin, add_staff, signed_in
from tests.support import every_page

type Make = Callable[[AsyncSession, str], Awaitable[uuid.UUID]]


async def _staff(session: AsyncSession, name: str) -> uuid.UUID:
    return (await add_staff(session, full_name=name)).id


async def _area(session: AsyncSession, name: str) -> uuid.UUID:
    async with session.begin():
        return (await AreaFactory().create(session, name=name)).id


async def _company(session: AsyncSession, name: str) -> uuid.UUID:
    async with session.begin():
        return (await CompanyFactory().create(session, name=name)).id


LISTS: dict[str, tuple[str, Make]] = {
    "drivers": ("/v1/drivers", add_driver),
    "companies": ("/v1/companies", _company),
    "areas": ("/v1/areas", _area),
    "staff": ("/v1/staff", _staff),
}


@pytest.fixture
async def viewer(db_session: AsyncSession) -> uuid.UUID:
    """An admin listing (the staff list is admins' only): named to sort last in it."""
    return (await add_admin(db_session, full_name="Zewdu Viewer")).id


@pytest.mark.parametrize("name", sorted(LISTS))
async def test_staff_sort_by_name(
    api: httpx.AsyncClient, db_session: AsyncSession, viewer: uuid.UUID, name: str
) -> None:
    path, make = LISTS[name]
    made = {label: await make(db_session, label)
            for label in ("Chaltu", "abebe", "Almaz", "bekele")}  # fmt: skip
    expected = [made[label] for label in ("abebe", "Almaz", "bekele", "Chaltu")]
    if name == "staff":
        expected.append(viewer)
    headers = signed_in(viewer)
    assert await every_page(api, path, headers, sort="name") == [str(i) for i in expected]
    newest = await every_page(api, path, headers)
    assert newest == await every_page(api, path, headers, sort="newest")
    assert set(newest) == {str(i) for i in expected}


@pytest.mark.parametrize("name", ["drivers", "staff"])
async def test_the_same_name_ignoring_case_keeps_one_order_across_pages(
    api: httpx.AsyncClient, db_session: AsyncSession, viewer: uuid.UUID, name: str
) -> None:
    """Names aren't unique for people: ties come by id."""
    path, make = LISTS[name]
    same = [await make(db_session, label) for label in ("Abebe Kebede", "abebe kebede",
                                                        "ABEBE KEBEDE")]  # fmt: skip
    after = await make(db_session, "Almaz Tesfaye")
    expected = [*sorted(same), after, *([viewer] if name == "staff" else [])]
    assert await every_page(api, path, signed_in(viewer), sort="name") == [str(i) for i in expected]


async def test_deleted_rows_are_not_listed_by_name(
    api: httpx.AsyncClient, db_session: AsyncSession, viewer: uuid.UUID
) -> None:
    async with db_session.begin():
        live = await AreaFactory().create(db_session, name="Bole")
        await AreaFactory().create(db_session, name="Ayat", deleted_at=datetime.now(UTC))
        company = await CompanyFactory().create(db_session, name="Acme")
        await CompanyFactory().create(db_session, name="Abay", deleted_at=datetime.now(UTC))
    headers = signed_in(viewer)
    assert await every_page(api, "/v1/areas", headers, sort="name") == [str(live.id)]
    assert await every_page(api, "/v1/companies", headers, sort="name") == [str(company.id)]


async def test_corridors_sort_by_their_a_area_then_their_b_area(
    api: httpx.AsyncClient, db_session: AsyncSession, viewer: uuid.UUID
) -> None:
    async with db_session.begin():
        area = {name: (await AreaFactory().create(db_session, name=name)).id
                for name in ("Bole", "ayat", "Mexico", "CMC", "piassa")}  # fmt: skip

        async def corridor(a: str, b: str) -> uuid.UUID:
            row = await CorridorFactory().create(
                db_session, origin_area_id=area[a], destination_area_id=area[b]
            )
            return row.id

        made = {pair: await corridor(*pair) for pair in (
            ("Mexico", "piassa"), ("Bole", "Mexico"), ("ayat", "piassa"), ("Bole", "CMC"),
            ("Bole", "ayat"), ("ayat", "Mexico"),
        )}  # fmt: skip
        await CorridorFactory().create(
            db_session, origin_area_id=area["ayat"], destination_area_id=area["Bole"],
            deleted_at=datetime.now(UTC),
        )  # fmt: skip
    expected = [("ayat", "Mexico"), ("ayat", "piassa"), ("Bole", "ayat"), ("Bole", "CMC"),
                ("Bole", "Mexico"), ("Mexico", "piassa")]  # fmt: skip
    headers = signed_in(viewer)
    assert await every_page(api, "/v1/corridors", headers, sort="name") == [
        str(made[pair]) for pair in expected
    ]
    filtered = await every_page(
        api, "/v1/corridors", headers, sort="name", origin_area_id=str(area["Bole"])
    )
    assert filtered == [str(made[pair]) for pair in expected[2:5]]


ALL = [*sorted(LISTS), "corridors"]


def _path(name: str) -> str:
    return "/v1/corridors" if name == "corridors" else LISTS[name][0]


@pytest.mark.parametrize("name", ALL)
@pytest.mark.parametrize("sort", ["code", "-name", "NAME", ""])
async def test_an_unknown_sort_is_422(
    api: httpx.AsyncClient, viewer: uuid.UUID, name: str, sort: str
) -> None:
    response = await api.get(_path(name), params={"sort": sort}, headers=signed_in(viewer))
    assert (response.status_code, response.json()["code"]) == (422, "VALIDATION_FAILED")


def _cursor(payload: object) -> str:
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()


@pytest.mark.parametrize("name", ALL)
@pytest.mark.parametrize(
    "cursor",
    [
        "garbage",
        _cursor(["name", "A\u0000", str(uuid.uuid4())]),
        _cursor(["name", ["Bole", "\ud800"], str(uuid.uuid4())]),
        _cursor(["newest", "2026-11-20T06:30:00+00:00", str(uuid.uuid4())]),
    ],
    ids=["garbage", "nul", "surrogate", "another-sort"],
)
async def test_a_tampered_or_foreign_cursor_is_422(
    api: httpx.AsyncClient, viewer: uuid.UUID, name: str, cursor: str
) -> None:
    response = await api.get(
        _path(name), params={"sort": "name", "cursor": cursor}, headers=signed_in(viewer)
    )
    assert (response.status_code, response.json()["code"]) == (422, "INVALID_CURSOR")


@pytest.mark.parametrize("name", ALL)
async def test_a_cursor_from_another_sort_is_422(
    api: httpx.AsyncClient, db_session: AsyncSession, viewer: uuid.UUID, name: str
) -> None:
    for label in ("Abebe", "Almaz"):
        if name == "corridors":
            async with db_session.begin():
                await CorridorFactory().create(db_session)
        else:
            await LISTS[name][1](db_session, label)
    headers = signed_in(viewer)
    for made, used in (("name", "newest"), ("newest", "name")):
        first = await api.get(_path(name), params={"sort": made, "limit": "1"}, headers=headers)
        cursor = first.json()["next_cursor"]
        assert cursor is not None
        response = await api.get(
            _path(name), params={"sort": used, "cursor": cursor}, headers=headers
        )
        assert (response.status_code, response.json()["code"]) == (422, "INVALID_CURSOR")


async def test_staff_names_up_to_200_characters_page_by_name(
    api: httpx.AsyncClient, db_session: AsyncSession, viewer: uuid.UUID
) -> None:
    """Staff names are longer than other names: a page boundary on a long one works."""
    made = [await _staff(db_session, "Abebe " + letter * 190) for letter in "abc"]
    assert await every_page(api, "/v1/staff", signed_in(viewer), sort="name") == [
        *(str(i) for i in made),
        str(viewer),
    ]
