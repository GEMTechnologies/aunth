"""Widen documents.uploaded_by: an actor reference is not an id.

THE REMAINING TRUNCATION. Migration 026 fixed `storage_key`, and the insert still failed:

    psycopg2.errors.StringDataRightTruncation: value too long for type character varying(36)
    'uploaded_by': 'agent:872c92ce-0e97-484a-a5ca-3854bd36d380'

`uploaded_by` was `String(36)` - the width of a UUID - and the generator writes
`f"agent:{agent.id}"`, which is 42 characters for every value it ever writes. So EVERY generated
document failed, permanently, for every organisation.

WHY THE NAME, NOT THE ID. `uploaded_by` records WHO did it, and the actor is sometimes a person
(`user.id`) and sometimes the agent acting for the organisation. Storing a bare id would make "which
of those two was it" unanswerable, so the prefix is the point and the column was simply too narrow for
the contract the code already used.

128 leaves room for `agent:` plus a uuid forever, and for a future actor kind without another migration.
`approved_by` stays at 36: approval is a human act and holds a bare user id.

Revision ID: 027_document_uploaded_by_width
Revises: 026_document_storage_key_width
"""

from typing import Sequence, Union

from alembic import op

revision: str = "027_document_uploaded_by_width"
down_revision: Union[str, None] = "026_document_storage_key_width"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("ALTER TABLE documents ALTER COLUMN uploaded_by TYPE varchar(128)")


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("ALTER TABLE documents ALTER COLUMN uploaded_by TYPE varchar(36)")
