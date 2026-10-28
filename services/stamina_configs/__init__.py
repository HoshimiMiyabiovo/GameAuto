"""第三方脚本体力刷本配置适配层（第一期 JSON）。"""

from .base import ConfigError, FieldSpec, JsonConfigAdapter
from .registry import ADAPTER_CLASSES, get_adapter, list_configs, load_app_config

__all__ = [
    "ConfigError",
    "FieldSpec",
    "JsonConfigAdapter",
    "ADAPTER_CLASSES",
    "get_adapter",
    "list_configs",
    "load_app_config",
]
