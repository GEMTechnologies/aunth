
from sqlalchemy import create_engine, event, Engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from sqlalchemy.pool import StaticPool
from .config import settings
import sqlite3

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

class Base(DeclarativeBase):
    pass

def get_db():
    """Database dependency for FastAPI"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def create_tables():
    """Create all database tables"""
    Base.metadata.create_all(bind=engine)

def drop_tables():
    """Drop all database tables (use with caution)"""
    Base.metadata.drop_all(bind=engine)

class DatabaseManager:
    """Database management utilities"""
    
    @staticmethod
    def get_connection():
        return engine.connect()
    
    @staticmethod
    def execute_sql(sql: str, params: dict = None):
        with engine.connect() as conn:
            return conn.execute(sql, params or {})
    
    @staticmethod
    def health_check() -> bool:
        try:
            with engine.connect() as conn:
                conn.execute("SELECT 1")
            return True
        except Exception:
            return False
