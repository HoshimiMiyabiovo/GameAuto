"""崩坏：星穹铁道（StarRail OneDragon / March7th Assistant）。"""

from .base import GameProfile
from .registry import register

PROFILE = GameProfile(
    game_key="honkai_sr",
    app_key="HonkaiStarRail",
    display_name="崩坏：星穹铁道",
    order=1,
    aliases=("StarRail", "hsr"),
    sign_section="mihoyo_sign",
    daily_tool="March7th",
    daily_parser="game_auto.services.starrail_daily_parser:parse_latest_starrail_log",
    # StarRailOneDragon 没有可复核的日志文件，只有 March7th 才走日志复核
    verify_scripts=("March7th",),
    monthly_flow="HSR_MONTHLY",
    monthly_label="崩铁每月商店",
    update_key="hsr",
    progress_hint="game_auto.games.base:build_maintenance_hint",
    supports_stamina=True,
    hint="崩铁使用 StarRail OneDragon 或 March7th Assistant。",
    default_app={
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
)

register(PROFILE)
