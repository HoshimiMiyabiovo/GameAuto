"""原神（BetterGI）。"""

from .base import GameProfile
from .registry import register

PROFILE = GameProfile(
    game_key="genshin",
    app_key="GenshinImpact",
    display_name="原神",
    order=0,
    aliases=("BetterGI",),
    sign_section="mihoyo_sign",
    daily_tool="BetterGI",
    daily_parser="game_auto.services.bettergi_daily_parser:parse_latest_bettergi_log",
    monthly_flow="GENSHIN_MONTHLY",
    monthly_label="原神每月商店",
    update_key="genshin",
    progress_hint="game_auto.games.base:build_maintenance_hint",
    supports_stamina=True,
    hint="原神使用 BetterGI，一键启动并监控。",
    default_app={
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
)

register(PROFILE)
