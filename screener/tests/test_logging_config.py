"""Tests for the central logging setup."""

import logging
from pathlib import Path

import pytest

from screener.logging_config import setup_logging


@pytest.fixture()
def restore_root_handlers():
    """Snapshot and restore the root logger so tests don't pollute pytest's."""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    yield
    for handler in list(root.handlers):
        root.removeHandler(handler)
    for handler in saved_handlers:
        root.addHandler(handler)
    root.setLevel(saved_level)


def _cfg(tmp_path: Path, level: str = "DEBUG") -> dict:
    return {
        "logging": {
            "level": level,
            "format": "%(levelname)s|%(name)s|%(message)s",
            "file": str(tmp_path / "logs" / "test.log"),
            "max_bytes": 1024,
            "backup_count": 1,
        }
    }


def test_sets_level_and_handlers(tmp_path: Path, restore_root_handlers) -> None:
    root = setup_logging(_cfg(tmp_path))
    assert root.level == logging.DEBUG
    assert len(root.handlers) == 2          # console + rotating file


def test_idempotent_on_rerun(tmp_path: Path, restore_root_handlers) -> None:
    """Repeated setup (e.g. Streamlit reruns) must not stack handlers."""
    setup_logging(_cfg(tmp_path))
    root = setup_logging(_cfg(tmp_path))
    assert len(root.handlers) == 2


def test_writes_to_log_file(tmp_path: Path, restore_root_handlers) -> None:
    setup_logging(_cfg(tmp_path))
    logging.getLogger("screener.test").info("hello file")
    log_file = tmp_path / "logs" / "test.log"
    assert log_file.exists()
    assert "hello file" in log_file.read_text(encoding="utf-8")


def test_console_only_when_no_file(tmp_path: Path, restore_root_handlers) -> None:
    cfg = _cfg(tmp_path)
    cfg["logging"]["file"] = None
    root = setup_logging(cfg)
    assert len(root.handlers) == 1
