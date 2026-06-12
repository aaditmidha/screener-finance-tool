"""Tests for the config loader module."""

from pathlib import Path

import pytest

from screener import config as config_module
from screener.config import CONFIG, load_config


class TestLoadConfig:
    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_config(tmp_path / "nope.yaml")

    def test_loads_custom_yaml(self, tmp_path: Path) -> None:
        custom = tmp_path / "c.yaml"
        custom.write_text("section:\n  key: 42\n", encoding="utf-8")
        data = load_config(custom)
        assert data == {"section": {"key": 42}}

    def test_default_path_is_project_config(self) -> None:
        data = load_config()
        assert data.keys() == CONFIG.keys()


class TestConfigSingleton:
    def test_singleton_is_populated(self) -> None:
        assert isinstance(CONFIG, dict)
        assert "scraper" in CONFIG

    def test_dotenv_loader_is_safe_to_call(self) -> None:
        """_load_dotenv must never raise, with or without a .env present."""
        config_module._load_dotenv()
