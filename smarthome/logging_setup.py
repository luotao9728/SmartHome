"""Logging configuration (console + optional rotating file)."""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Optional

FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


class _ColourFormatter(logging.Formatter):
    COLOURS = {
        "DEBUG": "\033[38;5;245m",
        "INFO": "\033[38;5;39m",
        "WARNING": "\033[38;5;214m",
        "ERROR": "\033[38;5;203m",
        "CRITICAL": "\033[1;38;5;203m",
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        text = super().format(record)
        if sys.stderr.isatty():
            colour = self.COLOURS.get(record.levelname)
            if colour:
                return "%s%s%s" % (colour, text, self.RESET)
        return text


def setup_logging(level: str = "info", *, path: Optional[Path] = None) -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(_ColourFormatter(FORMAT))
    root.addHandler(console)

    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            str(path), maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(logging.Formatter(FORMAT))
        root.addHandler(file_handler)

    # uvicorn's own loggers should follow the same level, minus noise.
    logging.getLogger("uvicorn.error").setLevel(logging.getLogger().level)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)
