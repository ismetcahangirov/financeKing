"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}

Every migration is either reversible or explicitly irreversible with a stated reason.
An irreversible migration discovered during an incident is a very bad moment, so the
reason belongs in the raised error where the operator will actually read it -- not in a
comment they would have to go looking for.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
${imports if imports else ""}

revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
