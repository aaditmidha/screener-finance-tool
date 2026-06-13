"""Tests for the SQLAlchemy engine and session factory."""

from pathlib import Path

from sqlalchemy import inspect, text

from screener.database.engine import build_engine, ensure_schema, get_session_factory


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


def test_ensure_schema_creates_tables(tmp_path: Path) -> None:
    engine = build_engine(str(tmp_path / "new.db"))
    ensure_schema(engine)
    tables = set(inspect(engine).get_table_names())
    assert {"companies", "annual_data", "ar_extracted_data"}.issubset(tables)


def test_ensure_schema_adds_missing_columns(tmp_path: Path) -> None:
    """A legacy companies table missing new columns must be migrated in place."""
    engine = build_engine(str(tmp_path / "legacy.db"))
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE companies (id INTEGER PRIMARY KEY, symbol VARCHAR, "
            "name VARCHAR, sector VARCHAR, industry VARCHAR, last_updated DATETIME)"
        ))
    ensure_schema(engine)
    cols = {c["name"] for c in inspect(engine).get_columns("companies")}
    assert {"data_quality", "view_type", "scrape_error"}.issubset(cols)


def test_ensure_schema_is_idempotent(tmp_path: Path) -> None:
    engine = build_engine(str(tmp_path / "idem.db"))
    ensure_schema(engine)
    ensure_schema(engine)   # second run must not raise
    assert inspect(engine).has_table("companies")
