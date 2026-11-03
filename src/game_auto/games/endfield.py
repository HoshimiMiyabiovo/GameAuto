"""明日方舟：终末地（ok-ef）。"""

from .base import GameProfile, ScriptToolSpec
from .registry import register

PROFILE = GameProfile(
    game_key="endfield",
    app_key="ArknightsEndfield",
    display_name="终末地",
    order=4,
    aliases=("Endfield", "ok-ef"),
    sign_section="skendfield_sign",
    daily_tool="ok-ef",
    daily_parser="game_auto.services.ok_ef_daily_parser:parse_latest_ok_ef_report",
    daily_merge="game_auto.games.endfield:merge_daily_log",
    verify_augment="game_auto.games.endfield:augment_verify",
    update_key="endfield",
    view_key="endfield_center",
    supports_stamina=True,
    script_tools=(ScriptToolSpec(
        label="ok-ef", repo="AliceJump/ok-end-field", pyappify=True,
        tool_key="ok-ef",
        # ok-ef 的 zip 名里带版本号（ok-ef-win32-v2.0.10.zip）
        asset_patterns=(r"^ok-ef-win32-v[\d.]+\.zip$",),
        launcher_name="ok-ef.exe",
    ),),
    hint="终末地使用 ok-ef，并支持协议终端/森空岛数据查询。",
    default_app={
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
)


def merge_daily_log(bridge, game, data, tool_name, game_key):
    """终末地：日志 + 森空岛/协议终端便笺互补合并。"""
    from .base import merge_api_daily_log

    merge_api_daily_log(bridge, game, data, tool_name, game_key, script_task_key="")


def augment_verify(data, load_api_tasks, logger):
    """复核前并入查询接口的「日活跃」计数。"""
    data.setdefault("tasks", []).extend(
        load_api_tasks(str(data.get("game_key") or "endfield"), logger))


register(PROFILE)
