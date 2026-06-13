"""SQLAlchemy engine, session factory, and lightweight schema migration."""

import logging
from pathlib import Path

from sqlalchemy import create_engine, Engine, inspect, text
from sqlalchemy.orm import sessionmaker, Session

from screener.config import CONFIG

logger = logging.getLogger(__name__)


def build_engine(db_path: str | None = None) -> Engine:
    """Create and return a SQLAlchemy Engine pointed at the SQLite database.

    Args:
        db_path: Filesystem path to the .db file. Defaults to config value.

    Returns:
        Configured SQLAlchemy Engine.
    """
    path = db_path or CONFIG["database"]["path"]
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    url = f"sqlite:///{path}"
    echo = CONFIG["database"]["echo_sql"]
    engine = create_engine(url, echo=echo, future=True)
    logger.info("Database engine created: %s", url)
    return engine


def ensure_schema(engine: Engine) -> None:
    """Create missing tables and add any missing (additive) columns.

    The project has no migration framework (it's a single-user cache DB), so a
    pre-existing database can lag the ORM after a model change.
    :func:`Base.metadata.create_all` adds new *tables* but never new *columns*
    on existing ones — so this also runs ``ALTER TABLE ... ADD COLUMN`` for any
    ORM column missing from an existing table. All schema changes to date have
    been additive and nullable, which SQLite's ADD COLUMN supports safely.

    Args:
        engine: The SQLAlchemy engine to migrate in place.
    """
    from screener.database.models import Base

    Base.metadata.create_all(engine)
    inspector = inspect(engine)
    for table in Base.metadata.sorted_tables:
        if not inspector.has_table(table.name):
            continue
        existing = {col["name"] for col in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing:
                continue
            col_type = column.type.compile(dialect=engine.dialect)
            with engine.begin() as conn:
                conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN {column.name} {col_type}'))
            logger.info("Schema migration: added %s.%s (%s)", table.name, column.name, col_type)


def get_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Return a sessionmaker bound to *engine*.

    Args:
        engine: SQLAlchemy Engine to bind.

    Returns:
        sessionmaker factory.
    """
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)
