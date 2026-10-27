from typing import Dict, List

from .paths import TEMPLATES_DIR


DEFAULT_APP_ORDER: List[str] = [
    "GenshinImpact",
    "HonkaiStarRail",
    "ZenlessZoneZero",
    "WutheringWaves",
    "ArknightsEndfield",
    "NevernessToEverness",
    "InfinityNikki",
    "BaiJing",
]


APP_KEY_ALIASES = {
    "BetterGI": "GenshinImpact",
    "StarRail": "HonkaiStarRail",
    "ok-ww": "WutheringWaves",
    "Endfield": "ArknightsEndfield",
    "ok-nte": "NevernessToEverness",
    "nte": "NevernessToEverness",
    "genshinimpact": "GenshinImpact",
    "honkaistarrail": "HonkaiStarRail",
    "wutheringwaves": "WutheringWaves",
    "arknightsendfield": "ArknightsEndfield",
    "nevernesstoeverness": "NevernessToEverness",
    "zenlesszonezero": "ZenlessZoneZero",
    "infinitynikki": "InfinityNikki",
    "genshin": "GenshinImpact",
    "wuwa": "WutheringWaves",
    "endfield": "ArknightsEndfield",
    "nikki": "InfinityNikki",
    "zzz": "ZenlessZoneZero",
    "honkai_sr": "HonkaiStarRail",
    "baijing": "BaiJing",
    "bai_jing": "BaiJing",
}


# ── 标准 game_key 体系 ──────────────────────────────────────────────
# 内部统一使用小写式 game_key（如 "genshin", "nikki", "endfield"），
# 与 APPS 字典的大驼峰式 app_key（如 "GenshinImpact"）通过下方映射表互转。
# 所有 tracker / parser / daily_checker / bridge 模块统一使用 game_key。
GAME_KEYS: Dict[str, str] = {
    "genshin":   "原神",
    "honkai_sr": "崩坏：星穹铁道",
    "zzz":       "绝区零",
    "wuwa":      "鸣潮",
    "endfield":  "终末地",
    "nte":       "异环",
    "nikki":     "无限暖暖",
    "baijing":   "白荆回廊",
}

# app_key（大驼峰）→ game_key（小写）反向映射
APP_KEY_TO_GAME_KEY: Dict[str, str] = {
    "GenshinImpact":         "genshin",
    "HonkaiStarRail":        "honkai_sr",
    "ZenlessZoneZero":       "zzz",
    "WutheringWaves":        "wuwa",
    "ArknightsEndfield":     "endfield",
    "NevernessToEverness":   "nte",
    "InfinityNikki":         "nikki",
    "BaiJing":               "baijing",
}

# game_key → 签到配置节名（app_config.json 中的顶层键）
GAME_KEY_TO_SIGN_SECTION: Dict[str, str] = {
    "endfield":  "skendfield_sign",
    "wuwa":      "wuwa_kujiequ",
    "nte":       "tajiduo_sign",
    "genshin":   "mihoyo_sign",
    "honkai_sr": "mihoyo_sign",
    "zzz":       "mihoyo_sign",
}


DEFAULT_QBITTORRENT: Dict[str, str] = {
    "path": "",
    "process_name": "qbittorrent.exe",
}


# 白荆回廊「有猫零售 · 有猫兑换」每月购买（2026-10-25）
# 商店每月刷新，可以买点小东西；具体买什么当月还没定，所以清单默认留空。
# items 每项支持两种写法：
#   "心迹"                                            → 只写商品名
#   {"name": "心迹", "max_price": 160, "tab": "小猫礼品"} → 带预算上限与页签
# max_price = 本次购买总价上限（有猫）；max_qty = 是否一次买满剩余库存（默认 false）。
# 清单为空 → 每月任务只报「待定」，不进商店、不买东西（runtime/baijing_cat_exchange.py）。
DEFAULT_BAIJING_CAT_EXCHANGE: Dict = {
    "enabled": True,
    "tab": "",
    "items": [],
}


DEFAULT_APPS = {
    "GenshinImpact": {
        "display_name": "原神",
        "script_name": "BetterGI",
        # 不配 prelaunch_flow：一条龙仍由 BetterGI 自己启动游戏（2026-10-24 默认）。
        # GenshinLauncher 流程（复刻 BetterGI GameLoading 进入主界面方法）已实现在
        # flow_configs.json，要启用时在 app_config.json 的 apps.GenshinImpact
        # 加 "prelaunch_flow": "GenshinLauncher"。
        "type": "script",
        "launcher": "",
        "launcher_params": ["startOneDragon"],
        "game": {"process_name": "YuanShen.exe"},
        "timeouts": {"launch": 300, "monitor": 5400},
        "initial_wait": 20,
        "bettergi_log_monitor": {
            "log_dir": "",
            "file_pattern": "better-genshin-impact*.log",
            "date_format": "%Y%m%d",
            "poll_interval": 2.0,
            "encoding": "utf-8",
        },
    },
    "HonkaiStarRail": {
        "display_name": "崩坏：星穹铁道",
        # 不配 prelaunch_flow：一条龙仍由 March7th 自己启动游戏（2026-10-24 默认）。
        # StarRailLauncher 流程（复刻三月七进入主界面方法）已实现在 flow_configs.json，
        # 要启用时在 app_config.json 的 apps.HonkaiStarRail 加 "prelaunch_flow": "StarRailLauncher"。
        "type": "script",
        "launcher_options": [
            {
                "name": "StarRailOneDragon",
                "display_name": "星铁一条龙",
                "type": "script",
                "launcher": "",
                "launcher_params": [],
            },
            {
                "name": "March7th",
                "display_name": "March7th Assistant",
                "type": "normal",
                "launcher": "",
                "launcher_params": [],
            },
        ],
        "game": {"process_name": "StarRail.exe"},
        "timeouts": {"launch": 300, "monitor": 3600},
        "initial_wait": 20,
        "starrail_daily_log_monitor": {
            "log_dir": "",
            # March7th Assistant 报错后停留在界面不退出，监控到日志报错即提前结束任务
            "abort_on_error": True,
        },
    },
    "ZenlessZoneZero": {
        "display_name": "绝区零",
        "type": "script",
        "launcher_options": [
            {
                "name": "OneDragonBat",
                "display_name": "OneDragon Bat",
                "type": "script",
                "launcher": "",
                "launcher_params": [],
            },
            {
                "name": "OneDragonScriptChainer",
                "display_name": "OneDragon ScriptChainer",
                "type": "normal",
                "launcher": "",
                "launcher_params": ["--chain", "1"],
                "script_process_name": "OneDragon ScriptChainer Runner.exe",
            },
        ],
        "game": {"process_name": "ZenlessZoneZero.exe"},
        "timeouts": {"launch": 300, "monitor": 3600},
        "initial_wait": 20,
        "zzz_daily_log_monitor": {
            "log_dir": "",
        },
    },
    "WutheringWaves": {
        "display_name": "鸣潮",
        "script_name": "ok-ww",
        "prelaunch_flow": "WuwaLauncher",
        "type": "script",
        "launcher": "",
        "launcher_params": ["-t", "1", "-e"],
        "script_process_name": "ok-ww.exe",
        "game": {"process_name": "Wuthering Waves.exe"},
        "timeouts": {"launch": 300, "monitor": 1800},
        # 鸣潮走 prelaunch 先把游戏拉起，ok-ww 启动后无需再盲等初始化：
        # 后续点击/监控都不依赖脚本就绪（监控只看游戏退出），5 秒足够进程落盘。
        "initial_wait": 5,
        # 拉 ok-ww 前必须等游戏窗口稳定，否则 ok-ww 会误判游戏没起而重复启动客户端。
        # 这是条件等待（窗口一稳立刻走），不是盲睡。
        "wait_game_window_before_script": True,
        "game_window_wait_sec": 120,
        "game_window_min_area": 307200,
        "post_launch_click_game": True,
        "post_launch_click_timeout": 8,
        "ok_ww_log_monitor": {
            "log_dir": "",
            "file_pattern": "app.*",
            "error_markers": [
                "TaskExecutor:Daily Task exception",
                "WaitFailedException",
            ],
            "completion_markers": [
                "任务完成",
                "任务结束",
                "每日任务已完成",
            ],
            "stall_after_error_sec": 180,
            "stall_without_update_sec": 300,
            "poll_interval": 2.0,
            "encoding": "utf-8",
            "restart_delay_sec": 10,
            "max_retries": 1,
        },
        "ok_ww_daily_log_monitor": {
            "log_dir": "",
        },
        # ok-ww.exe 只是 pyappify 包装器（参考异环 ok-nte 同款坑）：
        # 包装器先退，真正干活的 pythonw 引擎按名杀不到，只能按命令行补刀，
        # 否则一条龙结束后引擎一直活着（实测 1.6G 常驻）。
        "cleanup_cmd_patterns": [
            {"name": "pythonw.exe", "cmdline": "ok-ww"},
        ],
    },
    "ArknightsEndfield": {
        "display_name": "明日方舟：终末地",
        "script_name": "ok-ef",
        "type": "script",
        "launcher": "",
        "launcher_params": ["-t", "1", "-e"],
        "script_process_name": "ok-ef.exe",
        "game": {"process_name": "Endfield.exe"},
        "timeouts": {"launch": 300, "monitor": 3600},
        "initial_wait": 20,
        "force_launch": True,
        "ok_script_log_monitor": {
            "log_dir": "",
            "file_pattern": "app.*",
            "error_markers": [
                "ERROR [ok-ef]:",
                "发生异常，继续游戏",
                "info_set 错误 ",
                "Please start in game world and in team!",
            ],
            "stall_after_error_sec": 180,
            "stall_without_update_sec": 180,
            "poll_interval": 2.0,
            "encoding": "utf-8",
            "restart_delay_sec": 10,
            "max_retries": 1,
        },
        # 同异环：ok-ef.exe 是 pyappify 包装器，pythonw 引擎按命令行补刀，
        # 否则收尾后引擎常驻吃内存。
        "cleanup_cmd_patterns": [
            {"name": "pythonw.exe", "cmdline": "ok-ef"},
        ],
    },
    "NevernessToEverness": {
        "display_name": "异环",
        "script_name": "ok-nte",
        "type": "script",
        "launcher": "",
        "script_process_name": "ok-nte.exe",
        "game": {"process_name": "HTGame.exe"},
        "post_launch_ocr_flow": "nte_start",
        # 异环冷启动（官方启动器 NTEGame.exe 更新/检查 + HTGame 拉起）实测可达 8 分钟，
        # 大更新 far 不止。launch 只管"没动静"的情况；更新中另有 update 预算
        # （见 _wait_for_game_process_snapshot），更新完继续跑日常，不跳过。
        # NTEGame.exe 是实际跑起来的官方启动器进程名（注意不是同目录的 NTELauncher.exe），
        # 必须进清理名单，否则超时杀完 ok-nte 它还会继续拉游戏。
        # ok-nte.exe 只是 pyappify 包装器，真正干活的是 pythonw 引擎子进程
        # （包装器提前退出后按名杀不到，只能按命令行补刀）。
        "cleanup_processes": ["NTEGame.exe", "NTELauncher.exe"],
        "cleanup_cmd_patterns": [
            {"name": "pythonw.exe", "cmdline": "ok-nte"},
        ],
        "timeouts": {"launch": 600, "update": 7200, "monitor": 3600},
        "initial_wait": 20,
        "post_launch_click_game": True,
        "post_launch_click_timeout": 8,
        "ok_script_log_monitor": {
            "log_dir": "",
            "file_pattern": "app.*",
            "error_markers": [
                "ERROR [ok-nte]:",
                "发生异常，继续游戏",
                "info_set 错误 ",
                "WaitFailedException",
            ],
            "ignore_error_patterns": [
                "Failed to register hotkey",
            ],
            "stall_after_error_sec": 180,
            "stall_without_update_sec": 180,
            "poll_interval": 2.0,
            "encoding": "utf-8",
            "restart_delay_sec": 10,
            "max_retries": 1,
            "ui_stall_fallback": {
                "enabled": True,
                "daily_weekly_template": str(TEMPLATES_DIR / "ok-nte 日常周常.png"),
                "daily_weekly_target_text": "日常周常",
                "daily_task_template": str(TEMPLATES_DIR / "ok-nte 日常任务-2.png"),
                "daily_task_target_text": "日常任务",
                "start_template": str(TEMPLATES_DIR / "ok-nte 开始.png"),
                "start_target_text": "开始",
                "window_process_names": ["ok-nte.exe"],
                "title_keywords": ["ok-nte"],
                "search_timeout_sec": 12,
                "post_task_click_wait_sec": 1.5,
                "row_search_height": 120,
                "window_find_timeout_sec": 30,
            },
        },
    },
    "InfinityNikki": {
        "display_name": "无限暖暖",
        "script_name": "whimbox",
        # 不配 prelaunch_flow：一条龙直接拉 whimbox，由它自己启动游戏（10-22 验证可行）。
        # NikkiLauncher 流程定义保留在 flow_configs.json，留作手动/其他用途，默认不接线。
        "type": "script",
        "launcher": "",
        "launcher_params": ["startOneDragon"],
        "game": {"process_name": "X6Game-Win64-Shipping.exe"},
        "timeouts": {"launch": 300, "monitor": 3600},
        "initial_wait": 20,
        "whimbox_log_monitor": {
            "log_dir": "",
            "file_pattern": "whimbox-*.log",
            "completion_markers": [
                "✅ 一条龙结束",
                "一条龙任务完成: 任务结果如下：",
            ],
            "poll_interval": 2.0,
            "encoding": "utf-8",
        },
    },
    "BaiJing": {
        "display_name": "白荆回廊",
        "type": "normal",
        "launcher": "D:\\Program Files\\Game\\Seed_OB_Prod\\1.1.1.7\\Seed_OB_Prod.exe",
        "launcher_params": [],
        "game": {"process_name": "GateMK-Win64-Shipping.exe"},
        "timeouts": {"launch": 300, "monitor": 1800},
        "initial_wait": 5,
        "post_launch_click_game": True,
        "post_launch_click_timeout": 8,
        "post_launch_ingame_flow": "baijing",
    },
}
