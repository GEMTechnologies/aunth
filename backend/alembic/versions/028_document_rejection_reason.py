"""A rejection records WHY, not only who.

THE GAP. `documents` stored `approved_by` and `approved_at`, and `DocumentVault.reject` set both when a
person rejected a document - so the record said who turned it down and when, and nothing about what was
wrong with it. The next person had to guess what to change, and an audit trail that answers "who" but
not "why" cannot explain itself to the organisation it acts for.

Nullable: approval does not use it, and rows written before this migration have no reason to invent.

Revision ID: 028_document_rejection_reason
Revises: 027_document_uploaded_by_width
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "028_document_rejection_reason"
down_revision: Union[str, None] = "027_document_uploaded_by_width"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "documents", sa.Column("rejection_reason", sa.String(length=500), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("documents", "rejection_reason")
