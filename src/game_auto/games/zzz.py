"""绝区零（OneDragon Bat / ScriptChainer）。"""

from .base import GameProfile, ScriptToolSpec
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
    script_tools=(
        ScriptToolSpec(
            label="OneDragon Bat",
            repo="OneDragon-Anything/ZenlessZoneZero-OneDragon",
            option_name="OneDragonBat",
            tool_key="zzz-one-dragon",
        ),
        # ScriptChainer 上游仓库不明：只查本地版本；按版本号命名 zip 分发、
        # 无自更新，目录名可信
        ScriptToolSpec(
            label="OneDragon ScriptChainer",
            option_name="OneDragonScriptChainer",
            trust_dir_version=True,
            tool_key="script-chainer",
        ),
    ),
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
            # 2026-11-07：跳过 ScriptChainer 中间壳，直拉 OneDragon 原生 CLI
            # 一条龙（-o 运行、-c 跑完关游戏；见 OneDragon exe_launcher.py）。
            # launcher 留空与 ScriptChainer 同构——路径由 script_tool_installer
            # 安装后双写，或 GUI 路径配置页手填；本机实例已在 app_config.json
            # 填 D:\down\ZZZ\ZenlessZoneZero-OneDragon\OneDragon-Launcher.exe。
            {
                "name": "OneDragonDirect",
                "display_name": "OneDragon 一条龙（直连）",
                "type": "normal",
                "launcher": "",
                "launcher_params": ["-o", "-c"],
                "script_process_name": "pythonw.exe",
            },
        ],
        "game": {"process_name": "ZenlessZoneZero.exe"},
        "timeouts": {"launch": 300, "monitor": 3600},
        "initial_wait": 20,
        "zzz_daily_log_monitor": {
            "log_dir": "",
        },
        # OneDragon-Launcher.exe 只是引导器（拉起安装目录自带 .venv\scripts\pythonw.exe
        # 跑一条龙链路后 ~50 秒自退），快照按 PID / 按名都只能杀到引导器——
        # 2026-11-08 实证：用户退出 GUI 后链路"复活"自己跑完整条龙，把游戏留在
        # 功能菜单（同异环 ok-nte / 鸣潮 ok-ww 的包装器坑）。pythonw.exe 在
        # WRAPPER_PROCESS_NAMES 里禁止裸名杀，按命令行特征串补刀；特征串取
        # 安装目录名（OneDragon 的解释器绝对路径必含它，本机其他 Python 进程没有），
        # script_tool_installer 按仓库名装目录，换机不变。
        "cleanup_cmd_patterns": [
            {"name": "pythonw.exe", "cmdline": "ZenlessZoneZero-OneDragon"},
        ],
    },
)

register(PROFILE)
