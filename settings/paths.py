from __future__ import annotations

from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
APP_CONFIG_PATH = BASE_DIR / "app_config.json"
LOG_DIR = BASE_DIR / "logs"
LOG_FILE = LOG_DIR / "auto_runner.log"
TOOLS_DIR = BASE_DIR / "tools"
TESSERACT_DIR = TOOLS_DIR / "tesseract"
TESSERACT_EXE = TESSERACT_DIR / "tesseract.exe"
TESSDATA_DIR = TESSERACT_DIR / "tessdata"
TEMPLATES_DIR = BASE_DIR / "模板"
ARTIFACTS_DIR = BASE_DIR / "artifacts"
OCR_DEBUG_DIR = ARTIFACTS_DIR / "ocr_debug"
FLOW_CONFIG_PATH = BASE_DIR / "flow_configs.json"
WORKFLOW_DB_PATH = BASE_DIR / "workflow.sqlite3"
ASSETS_DIR = BASE_DIR / "assets"
FLET_EXAMPLES_DIR = TOOLS_DIR / "flet" / "sdk" / "python" / "examples"