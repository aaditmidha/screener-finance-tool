"""Excel exporter using openpyxl."""

import logging
from pathlib import Path
from typing import Any

import openpyxl
from openpyxl.workbook import Workbook

from screener.config import CONFIG

logger = logging.getLogger(__name__)

_cfg = CONFIG["exporters"]["excel"]


def export(data: dict[str, Any], filename: str) -> Path:
    """Write *data* to an Excel workbook and return the output path.

    Args:
        data: Mapping of sheet name → list of row dicts.
        filename: Output filename (without directory prefix).

    Returns:
        Absolute Path to the written file.
    """
    output_dir = Path(_cfg["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / filename

    wb: Workbook = openpyxl.Workbook()
    wb.remove(wb.active)  # remove default blank sheet

    for sheet_name, rows in data.items():
        ws = wb.create_sheet(title=sheet_name[:31])  # Excel sheet name limit
        if not rows:
            continue
        headers = list(rows[0].keys())
        ws.append(headers)
        for row in rows:
            ws.append([row.get(h) for h in headers])

    wb.save(output_path)
    logger.info("Excel exported: %s", output_path)
    return output_path
