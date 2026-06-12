"""PDF exporter using ReportLab."""

import logging
from pathlib import Path
from typing import Any

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen.canvas import Canvas

from screener.config import CONFIG

logger = logging.getLogger(__name__)

_cfg = CONFIG["exporters"]["pdf"]


def export(sections: list[dict[str, Any]], filename: str) -> Path:
    """Write a simple text-based PDF report and return the output path.

    Args:
        sections: List of dicts with keys 'title' (str) and 'lines' (list[str]).
        filename: Output filename (without directory prefix).

    Returns:
        Absolute Path to the written PDF.
    """
    output_dir = Path(_cfg["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / filename

    canvas = Canvas(str(output_path), pagesize=A4)
    font = _cfg.get("font", "Helvetica")
    width, height = A4
    y = height - 50

    for section in sections:
        canvas.setFont(f"{font}-Bold", 13)
        canvas.drawString(50, y, section.get("title", ""))
        y -= 20
        canvas.setFont(font, 10)
        for line in section.get("lines", []):
            if y < 60:
                canvas.showPage()
                y = height - 50
                canvas.setFont(font, 10)
            canvas.drawString(60, y, line)
            y -= 15
        y -= 10

    canvas.save()
    logger.info("PDF exported: %s", output_path)
    return output_path
