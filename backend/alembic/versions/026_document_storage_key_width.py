"""Widen documents.storage_key: a path does not fit in a UUID-sized column.

THE DEFECT, MEASURED IN PRODUCTION:

    psycopg2.errors.StringDataRightTruncation: value too long for type character varying(36)
    INSERT INTO documents (... 'storage_key':
      'generated/3d4edeec-f4dc-4e5f-a99d-29100780b3f2/75e25071-.../75e25071-cover_letter.md' ...)

`storage_key` was declared `String(36)` - the width of a UUID - but it holds a STORAGE PATH, built as
`f"generated/{org_id}/{opportunity_id}/{filename}"`, which is 89 characters for the shortest real case
and grows with both ids. Every insert failed.

WHAT THAT LOOKED LIKE FROM OUTSIDE, and why it cost so long to find:
  * `documents` stayed at 0, so every submission package stayed "missing mandatory material"
  * `_handle_document_generate` raised, so no `missing`/`generated` summary was ever returned and the
    operator was told nothing
  * the executor retried the same deterministic failure every five seconds, for ever, leaving the job
    QUEUED with attempt=0 between crashes - which reads as "waiting to be picked up" rather than
    "crashing"
  * the workflow sat RUNNING, so the health check's stranded-workflow query (RUNNING with no queued
    job) never matched

A path is not an identifier. 512 is chosen rather than TEXT so a runaway key is still rejected rather
than silently stored; the longest legitimate key is bounded by two uuids and a filename.

Revision ID: 026_document_storage_key_width
Revises: 025_mail_credential_ciphertext
"""

from typing import Sequence, Union

from alembic import op

revision: str = "026_document_storage_key_width"
down_revision: Union[str, None] = "025_mail_credential_ciphertext"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # A no-op on SQLite, where the column already accepts any length.
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("ALTER TABLE documents ALTER COLUMN storage_key TYPE varchar(512)")
    # The same reasoning applies to the evidence reference the submission path writes; it is a
    # reference into the same store and is built the same way.
    op.execute("ALTER TABLE documents ALTER COLUMN scope_ref TYPE varchar(200)")


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("ALTER TABLE documents ALTER COLUMN storage_key TYPE varchar(36)")
    op.execute("ALTER TABLE documents ALTER COLUMN scope_ref TYPE varchar(36)")
