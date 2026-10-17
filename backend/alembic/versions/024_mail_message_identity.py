"""Link a delivered message to the Granada mailbox it arrived at.

Revision ID: 024_mail_message_identity
Revises: 023_mail_routing

WHY THIS COLUMN EXISTS. `mail_messages.mail_account_id` names the EXTERNAL account that carried a
message - the Cloudflare delivery, a connected Gmail - and one such account carries mail for every
mailbox Granada manages. It therefore cannot say which Granada mailbox a message arrived at. Without
that, an organisation holding several portal accounts reads one merged inbox, and a verification
code for one portal sits in the same undifferentiated list as a code for another.

`mail_routing` already answers "whose mailbox is this?" during ingestion, before any tenant context
exists. This column records that answer on the message itself, so the mailbox survives the request
and a later reader does not have to re-derive it from an address that may since have been renamed or
disabled.

BACKFILL, AND WHY IT IS DELIBERATELY PARTIAL. Existing rows are matched by their first recipient
against `mail_routing` - the same rule a live delivery uses - so a backfilled row and a new row are
labelled identically. A row whose address is not routable is left NULL rather than guessed at: a
rename since the message arrived, or a self-test delivery to an address that never existed, cannot
be attributed correctly, and a wrong mailbox is worse than a missing one because it files one
organisation's mail under another's history.

THE BACKFILL CAN SILENTLY DO NOTHING, EXACTLY AS 023's DID. `mail_messages` has ROW LEVEL SECURITY
with FORCE, and the migration runs as `granada_user`, which owns the table - so the UPDATE matches
zero rows and raises nothing while reporting success. This is tolerable HERE in a way it was not in
023: the column is new, every row that predates it is historical, and the live path writes the
mailbox directly on every delivery from the moment this revision applies. Nothing depends on the
backfill for correctness. An operator wanting the history labelled should run the same UPDATE as a
superuser; the statement is reproduced in docs/GRANADA_EMAIL_HANDOVER.md.

Lives in its own revision rather than folded into 023 so it can be applied to a deployment that
already ran 023 - which production has.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "024_mail_message_identity"
down_revision: Union[str, None] = "023_mail_routing"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "mail_messages"
COLUMN = "mail_identity_id"
INDEX = "ix_mail_messages_mail_identity_id"
FK = "fk_mail_messages_mail_identity_id"


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if _is_postgres():
        op.add_column(TABLE, sa.Column(COLUMN, sa.String(36), nullable=True))
        op.create_foreign_key(FK, TABLE, "mail_identities", [COLUMN], ["id"])
        op.create_index(INDEX, TABLE, [COLUMN])
        # Matched by first recipient against the routing table, and left NULL where the address is
        # not routable rather than attributed to a guess. See the module docstring.
        op.execute(
            f"""
            UPDATE {TABLE} AS m
            SET {COLUMN} = r.identity_id
            FROM mail_routing AS r
            WHERE m.{COLUMN} IS NULL
              AND m.recipients IS NOT NULL
              AND jsonb_typeof(m.recipients::jsonb) = 'array'
              AND lower(coalesce(m.recipients::jsonb ->> 0, '')) = r.address
            """
        )
        return

    # SQLite cannot ALTER a constraint in place, so the non-Postgres path goes through a batch.
    with op.batch_alter_table(TABLE) as batch:
        batch.add_column(sa.Column(COLUMN, sa.String(36), nullable=True))
        batch.create_index(INDEX, [COLUMN])


def downgrade() -> None:
    if _is_postgres():
        op.drop_index(INDEX, table_name=TABLE)
        op.drop_constraint(FK, TABLE, type_="foreignkey")
        op.drop_column(TABLE, COLUMN)
        return

    with op.batch_alter_table(TABLE) as batch:
        batch.drop_index(INDEX)
        batch.drop_column(COLUMN)
