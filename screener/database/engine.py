"""SQLAlchemy engine and session factory."""

import logging
from pathlib import Path

from sqlalchemy import create_engine, Engine
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


def get_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Return a sessionmaker bound to *engine*.

    Args:
        engine: SQLAlchemy Engine to bind.

    Returns:
        sessionmaker factory.
    """
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)
