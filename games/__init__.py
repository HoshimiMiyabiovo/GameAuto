"""插件式游戏档案包。

新增游戏 = 在 ``games/`` 下加一个模块，构造 ``GameProfile`` 并 ``register()``；
settings / bridge / 批处理 / 更新日历会自动发现它，不需要再改映射表。
"""

from .base import GameProfile, load_ref
from .registry import (
    aliases_map,
    app_key_to_game_key,
    app_keys,
    game_key_display_names,
    game_key_to_app_key,
    game_key_to_sign_section,
    game_keys,
    get_profile,
    iter_profiles,
    register,
    resolve_app_key,
    resolve_game_key,
    view_keys,
)

__all__ = [
    "GameProfile",
    "load_ref",
    "aliases_map",
    "app_key_to_game_key",
    "app_keys",
    "game_key_display_names",
    "game_key_to_app_key",
    "game_key_to_sign_section",
    "game_keys",
    "get_profile",
    "iter_profiles",
    "register",
    "resolve_app_key",
    "resolve_game_key",
    "view_keys",
]
