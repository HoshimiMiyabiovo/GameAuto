"""JSON 配置读写与默认值合并。"""

import copy
import json
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from .defaults import DEFAULT_APPS, DEFAULT_APP_ORDER, DEFAULT_QBITTORRENT
from .paths import APP_CONFIG_PATH


_CONFIG_FILE = APP_CONFIG_PATH
_BACKUP_SUFFIX = ".bak"
_MISSING = object()

_cache: Optional[Dict] = None


def _default_config() -> Dict:
    return {
        "_version": "1.0",
        "_comment": "用户配置覆盖文件；默认 apps 定义已内置在代码中，这里只保存差异项。",
        "setup_completed": False,
        "app_order": list(DEFAULT_APP_ORDER),
        "qbittorrent": copy.deepcopy(DEFAULT_QBITTORRENT),
        "log": {"dir": "logs", "file": "auto_runner.log", "backup_count": 5},
        "global": {"min_run_time": 300, "check_interval": 10, "debug_mode": False},
        "apps": copy.deepcopy(DEFAULT_APPS),
    }


def _merge_defaults(defaults: Any, overrides: Any) -> Any:
    if isinstance(defaults, dict) and isinstance(overrides, dict):
        merged = copy.deepcopy(defaults)
        for key, value in overrides.items():
            if key in merged:
                merged[key] = _merge_defaults(merged[key], value)
            else:
                merged[key] = copy.deepcopy(value)
        return merged
    return copy.deepcopy(overrides)


def _extract_overrides(current: Any, defaults: Any) -> Any:
    if isinstance(current, dict) and isinstance(defaults, dict):
        overrides: Dict[str, Any] = {}
        for key, value in current.items():
            if key in defaults:
                child = _extract_overrides(value, defaults[key])
                if child is not _MISSING:
                    overrides[key] = child
            else:
                overrides[key] = copy.deepcopy(value)
        return overrides if overrides else _MISSING

    if current == defaults:
        return _MISSING
    return copy.deepcopy(current)


def _normalize_for_save(config: Dict) -> Dict:
    defaults = _default_config()
    overrides = _extract_overrides(config, defaults)
    persisted = {
        "_version": str(config.get("_version", defaults["_version"])),
        "_comment": defaults["_comment"],
    }
    if isinstance(overrides, dict):
        for key, value in overrides.items():
            if key in {"_version", "_comment"}:
                continue
            persisted[key] = value
    return persisted


def load(force: bool = False) -> Dict:
    global _cache
    if _cache is None or force:
        defaults = _default_config()
        if _CONFIG_FILE.exists():
            try:
                with open(_CONFIG_FILE, encoding="utf-8") as f:
                    raw = json.load(f)
                _cache = _merge_defaults(defaults, raw)
            except (json.JSONDecodeError, OSError):
                _cache = defaults
        else:
            _cache = defaults
    return _cache


def save(config: Dict) -> None:
    global _cache
    persisted = _normalize_for_save(config)
    if _CONFIG_FILE.exists():
        shutil.copy2(_CONFIG_FILE, str(_CONFIG_FILE) + _BACKUP_SUFFIX)
    with open(_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(persisted, f, ensure_ascii=False, indent=2)
    _cache = _merge_defaults(_default_config(), persisted)


def get(key: str, default: Any = None) -> Any:
    return load().get(key, default)


def get_app_order() -> List[str]:
    return load().get("app_order", [])


def get_apps() -> Dict:
    return load().get("apps", {})


def get_app(app_key: str) -> Optional[Dict]:
    return get_apps().get(app_key)


def get_qbittorrent() -> Dict:
    return load().get("qbittorrent", copy.deepcopy(DEFAULT_QBITTORRENT))


def get_log_config() -> Dict:
    cfg = load().get("log", {})
    return {
        "dir": cfg.get("dir", "logs"),
        "file": cfg.get("file", "auto_runner.log"),
        "backup_count": cfg.get("backup_count", 5),
    }


def get_global() -> Dict:
    return load().get("global", {"min_run_time": 300, "check_interval": 10, "debug_mode": False})

def update_app_order(order: List[str]) -> None:
    cfg = load()
    cfg["app_order"] = order
    save(cfg)


def upsert_app(app_key: str, app_data: Dict) -> None:
    cfg = load()
    cfg.setdefault("apps", {})[app_key] = app_data
    save(cfg)


def remove_app(app_key: str) -> None:
    cfg = load()
    cfg.get("apps", {}).pop(app_key, None)
    if app_key in cfg.get("app_order", []):
        cfg["app_order"].remove(app_key)
    save(cfg)


def update_qbittorrent(data: Dict) -> None:
    cfg = load()
    cfg["qbittorrent"] = data
    save(cfg)


def update_log_config(data: Dict) -> None:
    cfg = load()
    cfg["log"] = data
    save(cfg)


def update_global(data: Dict) -> None:
    cfg = load()
    cfg["global"] = data
    save(cfg)


def is_setup_completed() -> bool:
    return bool(load().get("setup_completed", False))


def mark_setup_completed() -> None:
    cfg = load()
    cfg["setup_completed"] = True
    save(cfg)
