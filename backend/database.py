"""Database engine and session plumbing."""

import sqlite3
import logging

from sqlalchemy import create_engine, event, text, Engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from config import settings

from tenant_context import apply_tenant_on_checkout

logger = logging.getLogger(__name__)

# The declarative base must be the ONE defined in models.py. This module used to
# declare its own Base, which created a second metadata registry: models stayed
# invisible to this registry, so create_tables() silently created nothing and
# drop_tables() silently dropped nothing.
from models import Base


# SQLite WAL mode optimization
@event.listens_for(Engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    """Apply SQLite pragmas to SQLite connections and to nothing else.

    This listener is attached to the ``Engine`` class, so it fires for *every*
    engine in the process, including engines this module never created.

    It used to be gated on ``settings.database_url``, which is the wrong
    question: that setting describes the application engine, not the connection
    being opened. Any PostgreSQL engine built while settings happened to point
    at SQLite - exactly what the test suite does, and what a multi-database
    deployment does - received ``PRAGMA foreign_keys=ON`` and PostgreSQL
    rejected it with a syntax error.

    Testing the connection itself is both simpler and correct: a psycopg2
    connection is simply not a ``sqlite3.Connection``, so non-SQLite drivers
    are skipped no matter what the settings say.
    """
    if not isinstance(dbapi_connection, sqlite3.Connection):
        return

    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA temp_store=memory")
    cursor.execute("PRAGMA mmap_size=268435456")  # 256MB
    cursor.close()

# Engine configuration
engine_kwargs = {
    "pool_pre_ping": True,
    "echo": settings.database_echo,
}

# SQLite specific configuration
if settings.database_url.startswith("sqlite"):
    engine_kwargs.update({
        "poolclass": StaticPool,
        "connect_args": {
            "check_same_thread": False,
            "timeout": 20
        }
    })

engine = create_engine(settings.database_url, **engine_kwargs)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@event.listens_for(Engine, "checkout")
def blank_tenant_on_checkout(dbapi_connection, connection_record, connection_proxy):
    """Refuse to hand out a pooled connection that still carries a tenant.

    The tenant GUCs are session-level so they survive the ``Session.commit()``
    calls the request path already performs. That is paid for with an
    obligation: a connection must never reach the next unit of work still
    impersonating the previous one.

    This listener used to sit on ``reset`` - on check-*in* - which reads like a
    stronger guarantee and is in fact weaker than useless. ``Session.commit()``
    returns the connection to the pool, so that event fired in the middle of
    every request that commits; the pool then rolled back, and because ``SET``
    is transactional, the rollback undid the very blanking the listener had just
    performed. Measured directly against PostgreSQL: with the listener on
    ``reset`` a pooled connection still reported
    ``app.current_org_id = 'TENANT-A'`` to the next session that borrowed it.

    Clearing on ``checkout`` instead makes the invariant a single sentence: no
    unit of work ever sees a tenant it did not itself establish. Anything that
    legitimately needs a tenant calls ``set_tenant``, which re-establishes it on
    whatever connection it is handed - including after a ``commit()`` releases
    the previous one.

    ``clear_tenant`` in the request dependency remains the primary mechanism;
    this is the backstop for every path that misses it - an exception between
    set and clear, a dependency that was never entered, a background task
    holding a session.
    """
    apply_tenant_on_checkout(dbapi_connection)


def get_db():
    """Database dependency for FastAPI.

    Deliberately tenant-agnostic. This is the dependency for endpoints that run
    *before* a tenant can exist or be known - registration, login, token
    refresh, password reset - and for unauthenticated endpoints such as
    ``/health``. Those requests operate with no tenant bound, so under
    row-level security they see no tenant rows. That is the correct outcome: an
    endpoint that needs tenant data must ask for ``router.get_tenant_db``.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def create_tables():
    """Create all database tables.

    This is a development convenience only. Production schema changes go
    through Alembic migrations (see alembic/versions/).
    """
    Base.metadata.create_all(bind=engine)


def drop_tables(confirm: bool = False):
    """Drop all database tables.

    Destructive and irreversible. Requires ``confirm=True`` so that an
    accidental call cannot destroy tenant data.
    """
    if not confirm:
        raise RuntimeError(
            "drop_tables() destroys all data. Call drop_tables(confirm=True) "
            "only against a disposable database."
        )
    Base.metadata.drop_all(bind=engine)


class DatabaseManager:
    """Database management utilities"""

    @staticmethod
    def get_connection():
        return engine.connect()

    @staticmethod
    def execute_sql(sql: str, params: dict = None):
        """Execute a literal SQL statement.

        ``sql`` must be a complete statement and ``params`` uses bound
        parameters. The previous implementation passed a bare string to
        ``Connection.execute``, which SQLAlchemy 2.0 rejects with
        ArgumentError ("Textual SQL expression should be explicitly declared
        as text(...)").
        """
        with engine.connect() as conn:
            return conn.execute(text(sql), params or {})

    @staticmethod
    def health_check() -> bool:
        """Return True when the database answers.

        The previous implementation executed the bare string ``"SELECT 1"``,
        which raised ArgumentError on every call. The exception was swallowed
        and False returned, so main.py treated a healthy database as down and
        refused to start.
        """
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return True
        except Exception:
            return False
