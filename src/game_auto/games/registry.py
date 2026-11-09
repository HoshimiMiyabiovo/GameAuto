"""插件式游戏档案注册表。

- 每个 ``games/<game>.py`` 模块在导入时 ``register(PROFILE)`` 自注册；
- 首次访问时自动扫描 ``games`` 包内所有非下划线模块，新增游戏只需加一个文件；
- settings / bridge / batch runner / 更新日历统一从这里取身份、数据源与调度信息，
  不再各自维护 app_key→game_key 映射表。

排序由 ``GameProfile.order`` 决定（对应 DEFAULT_APP_ORDER 的顺序）。
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import Dict, List, Optional

from .base import GameProfile

_PACKAGE = __name__.rsplit(".", 1)[0]
_REGISTRY: Dict[str, GameProfile] = {}
_LOOKUP: Dict[str, GameProfile] = {}
_LOADED = False

_MODULE_EXCLUDES = {"base", "registry"}


def register(profile: GameProfile) -> GameProfile:
    """注册一个游戏档案（模块导入时调用）。重复 game_key 视为 bug。"""
    if profile.game_key in _REGISTRY:
        raise ValueError(f"重复注册 game_key: {profile.game_key}")
    _REGISTRY[profile.game_key] = profile
    return profile


def _discover() -> None:
    global _LOADED
    if _LOADED:
        return
    # 失败时回滚标记重试（2026-11-09 审计 P2-2）：先置位后 import 的话，任一
    # games 模块顶层抛错（新模块有 bug/可选依赖缺失）会让注册表停留在半
    # 初始化状态且永不重试，消费方在宽 except 里降级成"部分游戏悄无声息
    # 消失"。回滚后下次访问重新尝试导入并显式报错。已成功的模块
    # importlib 会命中 sys.modules 缓存，重试不会重复执行顶层代码。
    try:
        for mod in pkgutil.iter_modules(importlib.import_module(_PACKAGE).__path__):
            if mod.name.startswith("_") or mod.name in _MODULE_EXCLUDES:
                continue
            importlib.import_module(f"{_PACKAGE}.{mod.name}")
    except Exception:
        _LOADED = False
        raise
    _LOADED = True


def _ensure_loaded() -> None:
    if not _LOADED:
        _discover()


def _build_lookup() -> Dict[str, GameProfile]:
    global _LOOKUP
    if _LOOKUP:
        return _LOOKUP
    _ensure_loaded()
    lookup: Dict[str, GameProfile] = {}
    for profile in _REGISTRY.values():
        lookup[profile.game_key] = profile
        lookup[profile.app_key] = profile
        lookup[profile.app_key.lower()] = profile
        lookup[profile.display_name] = profile
        for alias in profile.aliases:
            if alias:
                lookup[alias] = profile
    _LOOKUP = lookup
    return _LOOKUP


def get_profile(any_key: str) -> Optional[GameProfile]:
    """任意 key（app_key / game_key / 别名 / 显示名）→ 档案；未知返回 None。"""
    if not any_key:
        return None
    lookup = _build_lookup()
    key = str(any_key)
    if key in lookup:
        return lookup[key]
    lowered = key.lower()
    return lookup.get(lowered)


def iter_profiles() -> List[GameProfile]:
    """全部档案，按 order 升序。"""
    _ensure_loaded()
    return sorted(_REGISTRY.values(), key=lambda p: (p.order, p.game_key))


def app_keys() -> List[str]:
    return [p.app_key for p in iter_profiles()]


def game_keys() -> List[str]:
    return [p.game_key for p in iter_profiles()]


def aliases_map() -> Dict[str, str]:
    """历史别名 → app_key（不含 identity 映射，保持旧 APP_KEY_ALIASES 语义）。"""
    out: Dict[str, str] = {}
    for profile in iter_profiles():
        for alias in profile.aliases:
            out[alias] = profile.app_key
        out[profile.app_key.lower()] = profile.app_key
    return out


def app_key_to_game_key() -> Dict[str, str]:
    return {p.app_key: p.game_key for p in iter_profiles()}


def game_key_to_app_key() -> Dict[str, str]:
    return {p.game_key: p.app_key for p in iter_profiles()}


def game_key_to_sign_section() -> Dict[str, str]:
    return {p.game_key: p.sign_section for p in iter_profiles() if p.sign_section}


def game_key_display_names() -> Dict[str, str]:
    return {p.game_key: p.display_name for p in iter_profiles()}


def view_keys() -> List[str]:
    return [p.view_key for p in iter_profiles() if p.view_key]


def resolve_app_key(any_key: str) -> str:
    profile = get_profile(any_key)
    return profile.app_key if profile else str(any_key)


def resolve_game_key(any_key: str) -> str:
    profile = get_profile(any_key)
    return profile.game_key if profile else str(any_key)
