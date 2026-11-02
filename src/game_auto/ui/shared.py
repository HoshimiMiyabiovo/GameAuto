from __future__ import annotations

import glob
import logging
import os
import queue
from pathlib import Path
from typing import Optional, Tuple

from ..settings.paths import APP_CONFIG_PATH, BASE_DIR, FLET_EXAMPLES_DIR as PROJECT_FLET_EXAMPLES_DIR, WORKFLOW_DB_PATH as PROJECT_WORKFLOW_DB_PATH


CONFIG_FILE = APP_CONFIG_PATH
WORKFLOW_DB_PATH = PROJECT_WORKFLOW_DB_PATH
FLET_EXAMPLES_DIR = PROJECT_FLET_EXAMPLES_DIR
DEFAULT_ENDFIELD_PATH = ""
DEFAULT_WUWA_PATH = ""
WUWA_KUJIEQU_CONFIG_KEY = "wuwa_kujiequ"
TAJIDUO_SIGN_CONFIG_KEY = "tajiduo_sign"
SKENDFIELD_SIGN_CONFIG_KEY = "skendfield_sign"


def _find_default_nikki_json_path() -> str:
    patterns = [
        str(Path.home() / "Downloads" / "nikki-resonance-data-*.json"),
        str(BASE_DIR / "gacha-records-*.json"),
    ]
    for pattern in patterns:
        matches = sorted(glob.glob(pattern), reverse=True)
        for candidate in matches:
            if os.path.exists(candidate):
                return candidate
    return ""


DEFAULT_NIKKI_PATH = _find_default_nikki_json_path()


class UILogHandler(logging.Handler):
    """Sends log records to the UI via queue or bridge.

    Supports two backends:
    - Flet: pass a ``queue.Queue[Tuple[str, dict]]``
    - PyHuskarUI: call ``set_bridge(bridge_instance)`` once, then pass no queue
    """

    _bridge: "Optional[object]" = None  # UiBridge reference (PyHuskarUI mode)

    @classmethod
    def set_bridge(cls, bridge: "object") -> None:
        """Register the UiBridge for PyHuskarUI log delivery."""
        cls._bridge = bridge

    def __init__(self, ui_queue: "queue.Queue[Tuple[str, dict]] | None" = None):
        super().__init__()
        self.ui_queue = ui_queue

    def emit(self, record: logging.LogRecord):
        try:
            message = self.format(record)
        except Exception:
            message = record.getMessage()
        # PyHuskarUI mode → use bridge signal (thread-safe)
        if self._bridge is not None:
            try:
                self._bridge.emit_event("log", message=message)
            except Exception:
                pass  # avoid recursive log errors
            return
        # Flet mode → use queue
        if self.ui_queue is not None:
            self.ui_queue.put(("log", {"message": message}))