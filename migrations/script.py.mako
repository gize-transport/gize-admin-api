"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

Expand/contract: additive and backward-compatible with the deployed code. Every new table
calls enable_row_level_security(op, "<table>"). downgrade() must undo upgrade() exactly,
except that it never widens a privilege (say so in a comment). Unused imports are removed
by ruff when the file is written (alembic.ini post_write_hooks).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
${imports if imports else ""}
from gize_api.core.rls import enable_row_level_security

revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
