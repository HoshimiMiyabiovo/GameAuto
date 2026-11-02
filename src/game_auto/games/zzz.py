"""绝区零（OneDragon Bat / ScriptChainer）。"""

from .base import GameProfile
from .registry import register

PROFILE = GameProfile(
    game_key="zzz",
    app_key="ZenlessZoneZero",
    display_name="绝区零",
    order=2,
    sign_section="mihoyo_sign",
    daily_tool="OneDragon",
    daily_parser="game_auto.services.zzz_daily_parser:parse_latest_zzz_log",
    monthly_flow="ZZZ_MONTHLY",
    monthly_label="绝区零每月商店",
    update_key="zzz",
    progress_hint="game_auto.games.base:build_maintenance_hint",
    supports_stamina=True,
    hint="绝区零使用 OneDragon Bat 或 ScriptChainer。",
    default_app={
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
)

register(PROFILE)
