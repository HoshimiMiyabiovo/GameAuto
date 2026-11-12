"""默认配置。

游戏身份与默认项（DEFAULT_APPS / app_key 别名 / game_key 映射）统一由
``game_auto.games`` 注册表按插件模块生成，这里只保留注册表以外的全局默认值
（qbittorrent、白荆有猫兑换、暖暖晶石/共鸣商店清单）。
"""

import copy
from typing import Dict, List

from ..games import (
    aliases_map,
    app_key_to_game_key,
    game_key_display_names,
    game_key_to_sign_section,
    iter_profiles,
)


DEFAULT_APP_ORDER: List[str] = [p.app_key for p in iter_profiles()]


APP_KEY_ALIASES = aliases_map()


# ── 标准 game_key 体系 ──────────────────────────────────────────────
# 内部统一使用小写式 game_key（如 "genshin", "nikki", "endfield"），
# 与 APPS 字典的大驼峰式 app_key（如 "GenshinImpact"）通过下方映射表互转。
# 所有 tracker / parser / daily_checker / bridge 模块统一使用 game_key。
GAME_KEYS: Dict[str, str] = game_key_display_names()

# app_key（大驼峰）→ game_key（小写）反向映射
APP_KEY_TO_GAME_KEY: Dict[str, str] = app_key_to_game_key()

# game_key → 签到配置节名（app_config.json 中的顶层键）
GAME_KEY_TO_SIGN_SECTION: Dict[str, str] = game_key_to_sign_section()


DEFAULT_QBITTORRENT: Dict[str, str] = {
    "path": "",
    "process_name": "qbittorrent.exe",
}


# 第三方脚本自动安装配置（services/script_tool_installer.py 消费）：
# install_root 空 = 用 paths.SCRIPT_TOOLS_DEFAULT_DIR（<项目>/data/script_tools）；
# overrides 按 ScriptToolSpec.tool_key 覆盖单个工具的安装目录
# （2026-11-03 用户确认：默认跟项目走、全局根可改、每工具可单独自定义）。
DEFAULT_SCRIPT_TOOLS: Dict = {
    "install_root": "",
    "overrides": {},
}


# 白荆回廊「有猫零售 · 有猫兑换 · 小猫礼品」每月购买（2026-11-01 用户确认清单）
# 用户 2026-11-01 明确：只换两张抽卡券（同调线索 / 一刹闪回）+ 心迹，别的都不换。
# 两张券每月库存各 5（单价 40），按用户选择「买满库存」逐件购买（qty=5）；
# 心迹库存 1（单价 160）。items 每项支持两种写法：
#   "心迹"                                             → 只写商品名（买 1 件）
#   {"name": "同调线索", "qty": 5, "tab": "小猫礼品"}   → 带数量与页签
# max_price = 单次购买弹窗总价上限（有猫）；max_qty = 一次点「最大」买满（默认 false，
# 本页签的「最大」按钮未实测，优先用 qty 逐件买）。
# 清单为空 → 每月任务只报「待定」，不进商店、不买东西（runtime/baijing_cat_exchange.py）。
DEFAULT_BAIJING_CAT_EXCHANGE: Dict = {
    "enabled": True,
    "tab": "小猫礼品",
    "items": [
        {"name": "同调线索", "qty": 5},
        {"name": "一刹闪回", "qty": 5},
        {"name": "心迹", "qty": 1},
    ],
}


# 无限暖暖「晶石兑换」每月兑换抽卡道具（2026-11-01 用户确认）
# 共鸣水晶 / 启示水晶各本月限购 3 个（单价 120 晶石），按用户习惯买满库存。
# 由 NikkiLauncher 的 action=nikki_calendar 在进主界面后执行；按月门控
# （meta 键 monthly_sequence:nikki_crystal_exchange），逐件购买并以「本月限购」递减到账。
DEFAULT_NIKKI_CRYSTAL_EXCHANGE: Dict = {
    "enabled": True,
    "items": [
        {"name": "共鸣水晶", "qty": 3},
        {"name": "启示水晶", "qty": 3},
    ],
}


# 无限暖暖「商城 → 共鸣商店 → 静谧水滴兑换」每月兑换（2026-11-01 用户确认）
# 必须切到「静谧水滴兑换」页签（用万象星实/棱镜等专属商品校验），只换两张抽卡道具：
# 启示水晶 / 共鸣水晶，各买满本月限购 5 个（单价 40）。按月门控
# monthly_sequence:nikki_resonance_shop；**澎湃浪花兑换页不碰**。
DEFAULT_NIKKI_RESONANCE_SHOP: Dict = {
    "enabled": True,
    "items": [
        {"name": "启示水晶", "qty": 5},
        {"name": "共鸣水晶", "qty": 5},
    ],
}


# 每个游戏的默认配置由注册表提供（games/<game>.py 的 default_app）
DEFAULT_APPS: Dict[str, Dict] = {
    p.app_key: copy.deepcopy(p.default_app) for p in iter_profiles()
}


# 全局热键游戏截图（2026-11-10）：GUI 启动即常驻待命，按热键截当前前台窗口。
# hotkey 用用户可读写法（services/game_shot_service.to_pynput_hotkey 转 pynput）；
# 默认 Ctrl+Alt+S 而不是 Win+Alt+PrtSc —— 后者被 Xbox Game Bar 自己占着，注册会失败。
# capture: window=只截前台窗口 / screen=整屏；format: png / jpg；
# sound 必须在：游戏独占全屏时 GUI 窗口在后台，应用内 toast 用户根本看不见。
DEFAULT_GAME_SHOT: Dict = {
    "enabled": True,
    "hotkey": "ctrl+alt+s",
    "capture": "window",
    "format": "png",
    "jpeg_quality": 90,
    "max_width": 0,
    "sound": True,
    "toast": True,
    "min_interval_sec": 0.8,
}
