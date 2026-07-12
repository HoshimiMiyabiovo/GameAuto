import copy
from typing import Dict, Optional

from . import manager as cm
from .discovery import apply_runtime_discovery
from .defaults import APP_KEY_ALIASES, DEFAULT_APPS, DEFAULT_APP_ORDER, DEFAULT_QBITTORRENT
from .paths import LOG_DIR as PROJECT_LOG_DIR, LOG_FILE as PROJECT_LOG_FILE, WORKFLOW_DB_PATH as PROJECT_WORKFLOW_DB_PATH
from ..services.launch_task_catalog import merge_launch_apps


LOG_DIR = str(PROJECT_LOG_DIR)
LOG_FILE = str(PROJECT_LOG_FILE)
WORKFLOW_DB_PATH = str(PROJECT_WORKFLOW_DB_PATH)

MIN_RUN_TIME = 300
CHECK_INTERVAL = 10
DEBUG_MODE = True
BACKUP_COUNT = 5

APP_ORDER = list(DEFAULT_APP_ORDER)
QBITTORRENT: Dict[str, str] = copy.deepcopy(DEFAULT_QBITTORRENT)
APPS: Dict[str, Dict] = copy.deepcopy(DEFAULT_APPS)


def apply_runtime_config(config: Optional[Dict] = None) -> Dict:
    runtime_config = copy.deepcopy(config or cm.load())
    runtime_config = apply_runtime_discovery(runtime_config)
    app_order, apps = merge_launch_apps(
        runtime_config.get("app_order") or DEFAULT_APP_ORDER,
        runtime_config.get("apps") or DEFAULT_APPS,
    )
    qbittorrent = runtime_config.get("qbittorrent") or DEFAULT_QBITTORRENT

    runtime_config["app_order"] = list(app_order)
    runtime_config["apps"] = copy.deepcopy(apps)

    APP_ORDER[:] = list(app_order)

    QBITTORRENT.clear()
    QBITTORRENT.update(copy.deepcopy(qbittorrent))

    APPS.clear()
    APPS.update(copy.deepcopy(apps))
    return runtime_config


def refresh_runtime_config(force: bool = False) -> Dict:
    return apply_runtime_config(cm.load(force=force))


refresh_runtime_config()


def get_app_display_name(app_key: str) -> str:
    app_key = resolve_app_key(app_key)
    cfg = APPS.get(app_key, {})
    return cfg.get("display_name", app_key)


def get_script_display_name(app_key: str, script_choice: Optional[str] = None) -> str:
    app_key = resolve_app_key(app_key)
    cfg = APPS.get(app_key, {})
    if script_choice and "launcher_options" in cfg:
        opt = next((o for o in cfg["launcher_options"] if o["name"] == script_choice), None)
        if opt:
            return opt.get("display_name", opt["name"])
    return cfg.get("script_name", "默认脚本")


def resolve_app_key(app_key: str) -> str:
    """
    将历史 key / 大小写变体统一解析为当前标准 key。
    """
    if app_key in APPS:
        return app_key
    if app_key in APP_KEY_ALIASES:
        return APP_KEY_ALIASES[app_key]
    lowered = str(app_key).lower()
    if lowered in APP_KEY_ALIASES:
        return APP_KEY_ALIASES[lowered]
    return app_key