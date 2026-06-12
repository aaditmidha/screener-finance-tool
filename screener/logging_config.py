"""Central logging setup driven by the ``logging`` block in config.yaml.

The config defines the level, format, and a rotating file handler, but nothing
applies it until :func:`setup_logging` is called — typically once at process
start (CLI entry point or the Streamlit app). Library modules themselves only
ever call ``logging.getLogger(__name__)`` and never configure handlers, so
importing the package has no logging side effects.
"""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from screener.config import CONFIG

logger = logging.getLogger(__name__)


def setup_logging(config: dict[str, Any] | None = None) -> logging.Logger:
    """Configure the root logger from the ``logging`` config section.

    Idempotent: existing handlers on the root logger are cleared first so
    repeated calls (e.g. Streamlit reruns) don't multiply log output.

    Args:
        config: Full config dict. Defaults to the global CONFIG; injectable
            for testing.

    Returns:
        The configured root logger.
    """
    cfg = (config or CONFIG)["logging"]
    level = getattr(logging, str(cfg["level"]).upper(), logging.INFO)
    formatter = logging.Formatter(cfg["format"])

    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    log_file = cfg.get("file")
    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            path,
            maxBytes=cfg.get("max_bytes", 10 * 1024 * 1024),
            backupCount=cfg.get("backup_count", 5),
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    logger.debug("Logging configured at level %s", logging.getLevelName(level))
    return root
