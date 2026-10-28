"""体力刷本配置适配器注册表。

第一期（JSON）：原神 BetterGI / 鸣潮 ok-ww / 终末地 ok-ef / 异环 ok-nte / 暖暖 whimbox。
崩铁 March7th、绝区零 ZZZ OneDragon 是 YAML，第二期接（需要 ruamel.yaml 保注释）。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from ...settings.paths import APP_CONFIG_PATH
from .base import ConfigError
from .bettergi import BetterGIConfigAdapter
from .ok_ef import OkEfConfigAdapter
from .ok_nte import OkNteConfigAdapter
from .ok_ww import OkWwConfigAdapter
from .whimbox import WhimboxConfigAdapter

LOGGER = logging.getLogger("GameAutomation.StaminaConfigs")

ADAPTER_CLASSES = (
    BetterGIConfigAdapter,
    OkWwConfigAdapter,
    OkEfConfigAdapter,
    OkNteConfigAdapter,
    WhimboxConfigAdapter,
)


def load_app_config() -> Dict[str, Any]:
    if not APP_CONFIG_PATH.is_file():
        return {}
    try:
        data = json.loads(APP_CONFIG_PATH.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        LOGGER.warning("[刷本配置] 读取 app_config.json 失败: %s", exc)
        return {}


def get_adapter(game_key: str, app_config: Optional[Dict[str, Any]] = None):
    for cls in ADAPTER_CLASSES:
        if cls.game_key == game_key:
            return cls(app_config if app_config is not None else load_app_config())
    raise ConfigError(f"不支持的刷本配置: {game_key}")


def list_configs(app_config: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    cfg = app_config if app_config is not None else load_app_config()
    out: List[Dict[str, Any]] = []
    for cls in ADAPTER_CLASSES:
        try:
            item = cls(cfg).read()
            item["error"] = ""
        except Exception as exc:
            item = {
                "game_key": cls.game_key,
                "display_name": cls.display_name,
                "app_key": cls.app_key,
                "files": {},
                "primary_file": "",
                "fields": [],
                "values": {},
                "running": "",
                "error": str(exc),
            }
        out.append(item)
    return out
