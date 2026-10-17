"""One account's credential, for the worker that must read the mailbox, guarded by the tenant binding.

THE CONFLICT THIS RESOLVES. ADR-0011 denies `granada_fleet` ALL access to `credential_secrets` through
the RESTRICTIVE policy `credential_secrets_fleet_denied`, and the executor runs as `granada_fleet`. So
when a per-account IMAP transport was needed, the worker could not read the credential and the mailbox
parked with "mail transport for imap not configured" for ever - connected, and unreadable.

Removing that policy would be the wrong fix: it is the property that stops one organisation's worker
reading another's secrets.

WHAT THIS ADDS INSTEAD is the same shape as `fleet_active_agent_ids()`: ONE narrow question, answered by
a SECURITY DEFINER function owned by the table owner, and nothing else.

WHY IT IS SAFE. The function is `SECURITY DEFINER`, so it runs as `granada_user` and the restrictive
policy - which is scoped to the role `granada_fleet` - does not apply inside it. `credential_secrets` has
FORCE row-level security, so the PERMISSIVE policy still applies with the owner's context:
`org_id = app.current_org()`. The function additionally asserts the account belongs to that same
organisation.

Consequences, stated plainly:
  * UNBOUND CALLER GETS NOTHING. `app.current_org()` is NULL, the predicate is false, and no row is
    returned. The executor binds the tenant before running a job, which is what makes it work there.
  * A FORGED ACCOUNT ID GETS NOTHING. An account in another organisation fails the assertion, so this
    cannot be used to reach across tenants even by a caller holding the id.
  * IT RETURNS CIPHERTEXT, NOT THE SECRET. The payload is Fernet-encrypted with a key the database does
    not hold, so the function returns bytes that are useless without the application's key. Keeping the
    decryption in the application means the database never learns a mailbox password.

Revision ID: 025_mail_credential_ciphertext
Revises: 024_mail_message_identity
"""

from typing import Sequence, Union

from alembic import op

revision: str = "025_mail_credential_ciphertext"
down_revision: Union[str, None] = "024_mail_message_identity"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

FUNCTION = "app.mail_credential_ciphertext(text)"


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgres():
        return

    op.execute(
        """
        CREATE OR REPLACE FUNCTION app.mail_credential_ciphertext(p_account_id text)
        RETURNS text
        LANGUAGE sql
        SECURITY DEFINER
        STABLE
        SET search_path = public, pg_temp
        AS $$
            SELECT s.ciphertext
              FROM public.mail_accounts a
              JOIN public.credential_secrets s
                ON s.org_id = a.org_id
               AND s.ref = a.credentials_ref
             WHERE a.id = p_account_id
               -- The guard. `app.current_org()` is NULL when the caller is unbound, so an unbound
               -- caller matches nothing; a caller bound to another organisation matches nothing.
               AND a.org_id = app.current_org()
             LIMIT 1
        $$;
        """
    )

    op.execute(
        f"""
        COMMENT ON FUNCTION {FUNCTION} IS
            'The encrypted credential for one mail account, and only when the caller is bound to that '
            'account''s organisation. Exists so a worker can build a per-account transport without '
            'being granted access to credential_secrets. Returns ciphertext; the key is never in the '
            'database.';
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION {FUNCTION} FROM PUBLIC")
    for role in ("granada_app", "granada_fleet"):
        op.execute(
            f"""
            DO $$
            BEGIN
              IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                GRANT EXECUTE ON FUNCTION {FUNCTION} TO {role};
              END IF;
            END
            $$;
            """
        )


def downgrade() -> None:
    if _is_postgres():
        op.execute(f"DROP FUNCTION IF EXISTS {FUNCTION}")
