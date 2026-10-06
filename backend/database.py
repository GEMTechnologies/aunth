from sqlalchemy import create_engine, event, text, Engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from config import settings

# The declarative base must be the ONE defined in models.py. This module used to
# declare its own Base, which created a second metadata registry: models stayed
# invisible to this registry, so create_tables() silently created nothing and
# drop_tables() silently dropped nothing.
from models import Base

# SQLite WAL mode optimization
@event.listens_for(Engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    if 'sqlite' in settings.database_url:
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

def get_db():
    """Database dependency for FastAPI"""
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