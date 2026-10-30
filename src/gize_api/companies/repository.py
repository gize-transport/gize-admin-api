"""Company queries: live (not deleted) companies only. Names compared with the database's
`lower()`, as the unique index is."""

import uuid

from sqlalchemy import Select, exists, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from gize_api.companies.models import Company
from gize_api.core.db import LIKE_ESCAPE, escape_like


async def get_live_company(
    session: AsyncSession, company_id: uuid.UUID, *, for_update: bool = False
) -> Company | None:
    """`for_update` locks the row until the transaction ends: a change racing a delete waits,
    then finds the company gone."""
    statement = select(Company).where(Company.id == company_id, Company.not_deleted())
    if for_update:
        statement = statement.with_for_update()
    return await session.scalar(statement)


async def name_taken(session: AsyncSession, name: str, *, except_id: uuid.UUID | None) -> bool:
    statement = exists().where(
        func.lower(Company.name) == func.lower(literal(name)), Company.not_deleted()
    )
    if except_id is not None:
        statement = statement.where(Company.id != except_id)
    return bool(await session.scalar(select(statement)))


def live_companies(*, q: str | None, is_active: bool | None) -> Select[Company]:
    """The filtered companies, unordered: `paginate` orders and bounds them."""
    statement = select(Company).where(Company.not_deleted())
    if q is not None:
        statement = statement.where(
            func.lower(Company.name).startswith(
                func.lower(literal(escape_like(q))), escape=LIKE_ESCAPE
            )
        )
    if is_active is not None:
        statement = statement.where(Company.is_active.is_(is_active))
    return statement
