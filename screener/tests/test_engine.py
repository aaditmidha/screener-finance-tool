"""Tests for the SQLAlchemy engine and session factory."""

from pathlib import Path

from sqlalchemy import text

from screener.database.engine import build_engine, get_session_factory


def test_build_engine_creates_parent_dir(tmp_path: Path) -> None:
    db_path = tmp_path / "nested" / "dir" / "test.db"
    engine = build_engine(str(db_path))
    assert db_path.parent.exists()
    assert engine.url.drivername == "sqlite"


def test_engine_executes_queries(tmp_path: Path) -> None:
    engine = build_engine(str(tmp_path / "q.db"))
    with engine.connect() as conn:
        assert conn.execute(text("SELECT 1")).scalar() == 1


def test_session_factory_yields_working_sessions(tmp_path: Path) -> None:
    engine = build_engine(str(tmp_path / "s.db"))
    factory = get_session_factory(engine)
    with factory() as session:
        assert session.execute(text("SELECT 1")).scalar() == 1
