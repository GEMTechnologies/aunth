"""Establishing the tenant context for row-level security.

Row-level security (migration ``003_row_level_security``) is only useful if
the database is told which tenant the current unit of work belongs to. This
module is the single, auditable place where that happens.

The rule this module enforces
-----------------------------
A tenant context is only ever set from a value that has already been
authenticated. There is no "default organisation", no fallback to a first row
in ``organisations``, and no way to make an unscoped request succeed. If the
caller cannot name its tenant, the correct outcome is that the database
returns nothing - which is what :func:`unscoped` deliberately arranges.

Why ``set_config(..., true)`` rather than ``SET LOCAL``
------------------------------------------------------
``SET LOCAL`` is session-transaction scoped, which is correct, but it is a
utility statement that cannot carry bind parameters. Routing every value
through a parameterised ``set_config`` call removes the string interpolation
that would otherwise make this module an injection point. The third argument
``true`` keeps the setting local to the transaction, so a pooled connection
returns to "unknown tenant" when the transaction ends.

The ``SET LOCAL`` scope is load-bearing
---------------------------------------
Without it, a connection returned to the pool would carry the previous
request's tenant and the next request would silently inherit it. That is the
exact failure this whole mechanism exists to prevent.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator, Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

logger = logging.getLogger(__name__)

ORG_SETTING = "app.current_org_id"
USER_SETTING = "app.current_user_id"


def _apply(conn: Connection, setting: str, value: Optional[str]) -> None:
    """Bind ``value`` to ``setting`` for the current transaction only.

    ``None`` and the empty string both become ``""``, which ``app.current_org``
    collapses to NULL via NULLIF. That is how "no tenant" is expressed, and it
    is why the policies deny rather than default.
    """
    conn.execute(
        text("SELECT set_config(:setting, :value, true)"),
        {"setting": setting, "value": value or ""},
    )


@contextmanager
def tenant_scope(
    bind: Engine | Connection,
    org_id: Optional[str],
    user_id: Optional[str] = None,
) -> Iterator[Connection]:
    """Run a block with ``org_id`` established as the current tenant.

    The block is executed inside a transaction that also carries the setting,
    so the tenant can never outlive the work it was granted for.
    """
    with bind.begin() as conn:
        _apply(conn, ORG_SETTING, org_id)
        _apply(conn, USER_SETTING, user_id)
        yield conn


@contextmanager
def unscoped(bind: Engine | Connection) -> Iterator[Connection]:
    """Run a block with the tenant deliberately unknown.

    Used for authentication-time work - verifying a password, completing an
    OAuth exchange, resolving which organisations a user belongs to - where no
    tenant has been established yet. Under RLS this returns no tenant rows at
    all, which is the intended behaviour rather than a limitation.
    """
    with bind.begin() as conn:
        _apply(conn, ORG_SETTING, None)
        _apply(conn, USER_SETTING, None)
        yield conn


def create_tenant_and_membership(
    bind: Engine | Connection,
    org_id: str,
    user_id: str,
    org_name: str,
    org_slug: str,
    role_id: str,
    role_key: str,
    role_name: str,
) -> None:
    """Create an organisation, its founder role and its founder membership.

    This is the registration path, and it is the only place that legitimately
    inserts a tenant before any request could possibly have named it.

    The apparent chicken-and-egg problem - "RLS blocks inserting a row into
    the table that defines the tenant" - is solved by ordering: the
    organisation id is allocated first, the context is set to that *new* id,
    and only then are the rows inserted. The ``WITH CHECK`` clause accepts
    them because the context already equals the id being written.

    The role must follow the organisation, because ``roles.org_id`` is a
    foreign key to ``organisations.id`` - there is no ordering in which a
    tenant role can be inserted first.

    Returning None keeps every caller on the single transaction opened here;
    a second commit boundary would let the organisation become visible before
    its founder role and membership existed.
    """
    with bind.begin() as conn:
        _apply(conn, ORG_SETTING, org_id)
        _apply(conn, USER_SETTING, user_id)

        conn.execute(
            text(
                "INSERT INTO organisations "
                "(id, name, slug, owner_user_id, created_by, created_at) "
                "VALUES (:id, :name, :slug, :owner, :created_by, now())"
            ),
            {
                "id": org_id,
                "name": org_name,
                "slug": org_slug,
                "owner": user_id,
                "created_by": user_id,
            },
        )

        # org_id is set explicitly, so the role is a tenant role rather than
        # a system role and is therefore invisible to other tenants.
        conn.execute(
            text(
                "INSERT INTO roles (id, org_id, key, name, is_system) "
                "VALUES (:id, :org_id, :key, :name, false)"
            ),
            {
                "id": role_id,
                "org_id": org_id,
                "key": role_key,
                "name": role_name,
            },
        )

        conn.execute(
            text(
                "INSERT INTO org_members (org_id, user_id, role_id, joined_at) "
                "VALUES (:org_id, :user_id, :role_id, now())"
            ),
            {"org_id": org_id, "user_id": user_id, "role_id": role_id},
        )
    logger.info("tenant created", extra={"org_id": org_id, "user_id": user_id})
