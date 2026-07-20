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


DEFAULT_APPS = {
    "GenshinImpact": {
        "display_name": "原神",
        "script_name": "BetterGI",
        "type": "script",
        "launcher": "",
        "launcher_params": ["startOneDragon"],
        "game": {"process_name": "YuanShen.exe"},
        "timeouts": {"launch": 300, "monitor": 3600},
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
        "type": "script",
        "launcher": "",
        "launcher_params": ["-t", "1", "-e"],
        "script_process_name": "ok-ww.exe",
        "game": {"process_name": "Wuthering Waves.exe"},
        "timeouts": {"launch": 300, "monitor": 3600},
        "initial_wait": 20,
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
    },
    "NevernessToEverness": {
        "display_name": "异环",
        "script_name": "ok-nte",
        "type": "script",
        "launcher": "",
        "script_process_name": "ok-nte.exe",
        "game": {"process_name": "HTGame.exe"},
        "post_launch_ocr_flow": "nte_start",
        "timeouts": {"launch": 300, "monitor": 3600},
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
}
