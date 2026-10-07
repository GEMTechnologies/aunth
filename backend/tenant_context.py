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

Why ``set_config`` with a bound ``local`` flag
----------------------------------------------
``SET LOCAL`` and ``SET`` are utility statements: they cannot carry bind
parameters. Routing every value through a parameterised ``set_config`` call
removes the string interpolation that would otherwise make this module an
injection point.

The ``local`` flag is a parameter rather than a hardcoded choice, because the
two scopes are correct in different situations and both are needed:

``local=True`` (:func:`tenant_scope`, :func:`unscoped`)
    Transaction-scoped. Unbindable by mistake - the transaction ending unbinds
    it. Correct for a bounded unit of work such as the test suite's
    ``tenant_scope`` block.

``local=False`` (:func:`set_tenant`, :func:`clear_tenant`, the request path)
    Session-scoped, so it survives ``COMMIT``. This is required, not merely
    convenient: the request path calls ``Session.commit()`` in several places -
    the last-seen update in ``router.get_current_user``, organisation
    creation, membership changes - and a transaction-local setting would be
    discarded by those commits mid-request, leaving a half-scoped request that
    reads nothing. That fails closed, but it fails on legitimate traffic, so it
    is not acceptable.

Session scope has an obligation attached, and it is met three times over: every
request path clears the setting in a ``finally``; :func:`_rebind_after_transaction`
re-applies it whenever ``Session.commit()`` releases the connection underneath
it; and :func:`apply_tenant_on_checkout` blanks every tenant GUC the moment a
connection leaves the pool. The failure all of this guards against is silent
cross-tenant disclosure, so redundancy is the right posture.

Why the pool backstop is on ``checkout`` and not on ``reset``
-------------------------------------------------------------
This was measured, not assumed. ``Session.commit()`` *returns the connection to
the pool*. SQLAlchemy's ``reset`` event therefore fires in the middle of a
request, and the pool's rollback immediately after undoes any ``set_config``
issued there, because ``SET`` is transactional like everything else. A listener
on ``reset`` looks like a safety net and is actually a no-op - the tenant was
already back at its old value before the next request could read it, or, worse,
survived onto whichever connection the next request happened to draw.

Clearing on ``checkout`` has neither problem. The value is written and then
explicitly committed, so nothing that happens to the connection afterwards can
undo it, and the invariant becomes one that can be stated in a single sentence:
**no unit of work ever sees a tenant it did not itself establish.**
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Iterable, Iterator, List, Optional, TypeVar

from sqlalchemy import event, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

ORG_SETTING = "app.current_org_id"
USER_SETTING = "app.current_user_id"

#: Every tenant GUC, so clearing is exhaustive rather than hand-maintained.
TENANT_GUCS = (ORG_SETTING, USER_SETTING)

#: Where a session remembers which tenant it is acting for. See
#: :func:`_rebind_after_transaction` for why this is session state and not a
#: context variable.
TENANT_BINDING_KEY = "granada_tenant_binding"

T = TypeVar("T")

_warned_non_postgres = False


class TenantAccessDenied(PermissionError):
    """The caller is not entitled to the organisation it named.

    A dedicated exception type so the HTTP layer can translate it into 403
    without this module importing FastAPI.
    """


def _apply(
    conn: Connection,
    setting: str,
    value: Optional[str],
    *,
    local: bool = True,
) -> None:
    """Bind ``value`` to ``setting`` for the current transaction only.

    ``None`` and the empty string both become ``""``, which ``app.current_org``
    collapses to NULL via NULLIF. That is how "no tenant" is expressed, and it
    is why the policies deny rather than default.

    ``local`` selects between the two PostgreSQL scoping modes, and the choice
    is not cosmetic:

    * ``local=True`` (``SET LOCAL``) is safe by construction - it cannot outlive
      the transaction - but it is discarded by ``COMMIT``. The existing request
      path calls ``Session.commit()`` in several places (session last-seen,
      organisation creation, membership changes), so a transaction-local tenant
      would silently disappear mid-request and every subsequent read would be
      denied. That is fail-closed but broken, which is not a trade worth making.
    * ``local=False`` (``set_config`` at session level) survives commits, so the
      tenant genuinely covers the whole request. It also survives the transaction
      being rolled back, which means the caller MUST clear it - see
      :func:`clear_tenant`, which every request path calls in a ``finally``, and
      ``reset_tenant_gucs``, which the connection pool calls on every check-in.

    The second mode is only safe because both of those exist. Neither is
    optional.
    """
    conn.execute(
        text("SELECT set_config(:setting, :value, :local)"),
        {"setting": setting, "value": value or "", "local": local},
    )


def _is_postgres(bind) -> bool:
    """Return True when ``bind`` speaks to PostgreSQL.

    SQLite has no RLS and no ``set_config``; the enforcement is a documented
    no-op there and the PostgreSQL-only tests skip rather than pass silently.
    """
    try:
        engine = getattr(bind, "engine", bind)
        return engine.dialect.name == "postgresql"
    except Exception:  # pragma: no cover - defensive
        return False


def set_tenant(bind: Session | Connection | Engine, org_id: Optional[str], user_id: Optional[str]) -> bool:
    """Establish the tenant for the connection behind ``bind``.

    Returns True when the context was actually applied. A False return is only
    possible on a database without row-level security (SQLite), and it is
    logged once per process at WARNING so a deployment that believes it is
    protected cannot be one that is not.

    ``org_id=None`` means "tenant unknown", which the policies translate into
    denial. There is deliberately no default tenant.

    The binding is recorded on the session as well as applied, because
    ``Session.commit()`` returns the connection to the pool mid-request and the
    setting has to be re-established on whatever connection comes next.
    """
    global _warned_non_postgres
    conn = bind.connection() if isinstance(bind, Session) else bind
    if isinstance(bind, Session):
        bind.info[TENANT_BINDING_KEY] = (org_id, user_id)
    if not _is_postgres(conn):
        if not _warned_non_postgres:
            _warned_non_postgres = True
            logger.warning(
                "row-level security is unavailable on this dialect; tenant "
                "enforcement is application-level only for this connection"
            )
        return False

    _apply(conn, ORG_SETTING, org_id, local=False)
    _apply(conn, USER_SETTING, user_id, local=False)
    return True


def clear_tenant(bind: Session | Connection | Engine) -> None:
    """Unbind every tenant GUC, returning the connection to "unknown tenant".

    Must be called when a request ends, whatever the outcome. Leaving a tenant
    bound on a pooled connection would hand the next request someone else's
    data, which is the single worst failure this mechanism can produce.
    """
    conn = bind.connection() if isinstance(bind, Session) else bind
    if isinstance(bind, Session):
        bind.info[TENANT_BINDING_KEY] = (None, None)
    if not _is_postgres(conn):
        return
    for setting in TENANT_GUCS:
        _apply(conn, setting, None, local=False)


def reset_tenant_gucs(dbapi_connection) -> None:
    """Blank every tenant GUC on a raw DBAPI connection, and commit it.

    Attaches to the engine's ``checkout`` event. Writing the setting is not
    enough: ``set_config(..., false)`` is a ``SET``, and ``SET`` is
    transactional, so a rollback would restore whatever tenant the connection
    last carried. The explicit commit is what makes the blanking stick.

    Dialect is tested on the DBAPI connection itself rather than on settings:
    this listener fires for every engine in the process, and a PostgreSQL
    connection is simply not a ``sqlite3.Connection``.
    """
    import sqlite3

    if isinstance(dbapi_connection, sqlite3.Connection):
        return
    cursor = None
    try:
        cursor = dbapi_connection.cursor()
        for setting in TENANT_GUCS:
            cursor.execute(
                "SELECT set_config(%s, '', false)",
                (setting,),
            )
        dbapi_connection.commit()
    except Exception:  # pragma: no cover - never break a check-out
        logger.exception("failed to blank tenant GUCs on connection check-out")
    finally:
        if cursor is not None:
            try:
                cursor.close()
            except Exception:  # pragma: no cover - best effort
                pass


def apply_tenant_on_checkout(
    dbapi_connection,
    connection_record=None,
    connection_proxy=None,
) -> None:
    """Blank every tenant GUC on a connection as it leaves the pool.

    Registered on ``checkout`` rather than ``reset``. Kept as a distinct name
    from :func:`reset_tenant_gucs` so that the call site reads as the invariant
    it enforces - no unit of work sees a tenant it did not establish - instead of
    as housekeeping.
    """
    reset_tenant_gucs(dbapi_connection)


def _rebind_after_transaction(session: Session, transaction, *rest) -> None:
    """Re-apply a session's tenant after ``commit()`` returns its connection.

    ``Session.commit()`` ends the transaction *and* hands the DBAPI connection
    back to the pool. The next query in the same request checks out a connection
    that has been blanked by :func:`apply_tenant_on_checkout`, so without this
    the tenant would silently disappear the moment any handler commits - the
    last-seen-context update in ``router.get_current_user`` alone would do it on
    every single request.

    The binding lives on ``session.info`` rather than in a context variable
    because FastAPI runs synchronous dependencies and handlers in a worker
    thread, and a ``ContextVar`` mutated inside that thread is not visible to
    the request path that owns it. The session is passed in, so there is nothing
    to lose.

    Re-establishing the tenant is strictly safer than inheriting it: if the
    session never called :func:`set_tenant`, this does nothing and the request
    runs with "tenant unknown", which the policies deny.

    The tail of the signature is ``*rest`` because SQLAlchemy has shipped two
    arities for this event - the legacy form passes ``(session, transaction)``,
    the current form adds the ``Connection`` - and pinning either would break
    the other.
    """
    binding = session.info.get(TENANT_BINDING_KEY)
    if binding is None:
        return
    try:
        conn = rest[0] if rest and rest[0] is not None else session.connection()
    except Exception:  # pragma: no cover - session already closed
        return
    if not _is_postgres(conn):
        return
    try:
        _apply(conn, ORG_SETTING, binding[0], local=False)
        _apply(conn, USER_SETTING, binding[1], local=False)
    except Exception:  # pragma: no cover - the request will fail closed instead
        logger.exception("failed to re-apply the tenant after a transaction end")


event.listen(Session, "after_transaction_end", _rebind_after_transaction)


def bootstrap_function_available(bind: Session | Connection | Engine) -> bool:
    """True when migration 003's ``app.user_org_ids(text)`` is installed."""
    conn = bind.connection() if isinstance(bind, Session) else bind
    if not _is_postgres(conn):
        return False
    try:
        return bool(
            conn.execute(
                text("SELECT to_regprocedure('app.user_org_ids(text)') IS NOT NULL")
            ).scalar()
        )
    except Exception:
        logger.debug("could not probe for app.user_org_ids", exc_info=True)
        return False


def resolve_org_ids(bind: Session | Connection | Engine, user_id: Optional[str]) -> List[str]:
    """Return the organisation ids ``user_id`` is a member of.

    This is the bootstrap step, and it is the reason ADR-0005 keeps
    ``org_members`` ENABLE-but-not-FORCE: the policy on ``org_members`` filters
    by ``org_id``, so an unscoped read returns nothing and the tenant could
    never be established. ``app.user_org_ids`` is SECURITY DEFINER and answers
    the question without disclosing anything the caller does not already own.

    Proving you are a user is not the same as being granted a tenant: the result
    is a *list of ids the caller may use as scopes*, never an authorisation to
    read rows. The caller still has to bind one of them with :func:`set_tenant`,
    and the database still checks every row against it.
    """
    if not user_id:
        return []

    conn = bind.connection() if isinstance(bind, Session) else bind

    if _is_postgres(conn) and bootstrap_function_available(conn):
        rows = conn.execute(
            text("SELECT app.user_org_ids(:uid)"), {"uid": user_id}
        ).scalars()
        return [str(r) for r in rows]

    if _is_postgres(conn):
        # Pre-003 PostgreSQL: the helper does not exist yet. This is correct but
        # weaker - it relies on the application filter rather than the database
        # policy - so it is logged rather than done quietly.
        logger.warning(
            "app.user_org_ids is unavailable; resolving memberships with an "
            "application-side filter, which migration 003 makes unnecessary"
        )

    # SQLite has no RLS, so the plain filter is the whole mechanism there.
    rows = conn.execute(
        text("SELECT org_id FROM org_members WHERE user_id = :uid"),
        {"uid": user_id},
    ).scalars()
    return [str(r) for r in rows]


@dataclass(frozen=True)
class TenantContext:
    """What an authenticated caller is entitled to see, resolved once per request."""

    user_id: str
    org_ids: tuple = field(default_factory=tuple)
    #: Convenience for single-organisation members. ``None`` when the user
    #: belongs to no organisation, or to several - guessing between them would
    #: be exactly the "default tenant" behaviour the directive forbids.
    primary_org_id: Optional[str] = None

    @classmethod
    def resolve(cls, bind: Session | Connection | Engine, user_id: str) -> "TenantContext":
        org_ids = tuple(resolve_org_ids(bind, user_id))
        return cls(
            user_id=user_id,
            org_ids=org_ids,
            primary_org_id=org_ids[0] if len(org_ids) == 1 else None,
        )

    def is_member(self, org_id: Optional[str]) -> bool:
        return bool(org_id) and str(org_id) in self.org_ids

    def require_member(self, org_id: Optional[str]) -> str:
        """Return ``org_id`` if the caller belongs to it, else deny.

        This is the application-tier half of tenant enforcement and it is
        deliberately boring: compare one value against a list the user proved
        they own. The database-tier half is the RLS policy, which refuses the
        rows even if this check is removed.
        """
        if not org_id:
            raise TenantAccessDenied("No organisation was specified")
        if str(org_id) not in self.org_ids:
            raise TenantAccessDenied("Access denied to organisation")
        return str(org_id)


def for_each_tenant(
    bind: Session | Connection | Engine,
    ctx: TenantContext,
    loader: Callable[[], Iterable[T]],
) -> List[T]:
    """Run ``loader`` once per tenant the caller belongs to, scoped to each.

    Some endpoints genuinely span tenants - "list the organisations I belong to"
    is the obvious one. Binding a single tenant would silently drop the others,
    and binding none would be denied by the very policies protecting them. So
    the scope is rotated explicitly, one tenant at a time, and the caller sees
    exactly the rows each scope permits.

    The scope is cleared afterwards whatever happens, so a later failure cannot
    leave a tenant bound on a pooled connection.
    """
    results: List[T] = []
    try:
        for org_id in ctx.org_ids:
            set_tenant(bind, org_id, ctx.user_id)
            results.extend(loader())
    finally:
        clear_tenant(bind)
    return results


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
