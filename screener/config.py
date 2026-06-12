"""Loads and exposes config.yaml as a typed dict throughout the package.

On import this also loads a project-root ``.env`` file (if present) so secrets
such as ``GROQ_API_KEY`` are available via ``os.environ`` without being
hardcoded or committed.
"""

import logging
from pathlib import Path
from typing import Any

import yaml

_ROOT = Path(__file__).parent.parent
_CONFIG_PATH = _ROOT / "config.yaml"

logger = logging.getLogger(__name__)


def _load_dotenv() -> None:
    """Load a project-root ``.env`` into the environment if available.

    Silently no-ops when python-dotenv is not installed or no ``.env`` exists,
    so the package never hard-depends on either.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        logger.debug("python-dotenv not installed; skipping .env load")
        return
    env_path = _ROOT / ".env"
    if env_path.exists():
        load_dotenv(env_path)
        logger.debug("Loaded environment from %s", env_path)


_load_dotenv()


def load_config(path: Path = _CONFIG_PATH) -> dict[str, Any]:
    """Load YAML config from *path* and return as a nested dict.

    Args:
        path: Filesystem path to the YAML config file.

    Returns:
        Parsed configuration dictionary.

    Raises:
        FileNotFoundError: If the config file does not exist.
    """
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with path.open("r", encoding="utf-8") as fh:
        data: dict[str, Any] = yaml.safe_load(fh)
    logger.debug("Loaded config from %s", path)
    return data


# Module-level singleton — import this everywhere instead of calling load_config().
CONFIG: dict[str, Any] = load_config()
