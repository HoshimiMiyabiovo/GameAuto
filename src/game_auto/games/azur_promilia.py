"""蓝色星原：旅谣（ok-ap）。"""

from .base import GameProfile, ScriptToolSpec
from .registry import register

PROFILE = GameProfile(
    game_key="azur_promilia",
    app_key="AzurPromilia",
    display_name="蓝色星原：旅谣",
    order=9,
    # 本体 exe 在 <安装根>/AzurPromilia_Game/ 下，上溯 1 层把启动器一并计入
    disk_root_up_levels=1,
    aliases=("Azur Promilia", "蓝色星原", "ok-ap"),
    daily_tool="ok-ap",
    daily_parser="game_auto.services.ok_ap_daily_parser:parse_latest_ok_ap_report",
    hint="蓝色星原：旅谣使用 ok-ap，一键启动并监控。",
    script_tools=(ScriptToolSpec(
        label="ok-ap", repo="longer-sausage/ok-ap", pyappify=True,
        tool_key="ok-ap", launcher_name="ok-ap.exe",
        # 上游只发布 setup.exe，现有安装器不支持 exe，故只做版本检测不自动下载
    ),),
    default_app={
        "display_name": "蓝色星原：旅谣",
        "type": "script",
        # 模板不带机器路径：本机真实脚本写在 app_config.apps.AzurPromilia
        "launcher": "",
        "launcher_params": ["-t", "DailyTask", "-h", "-e"],
        "script_process_name": "ok-ap.exe",
        "game": {"process_name": "AzurPromilia.exe"},
        "timeouts": {"launch": 600, "monitor": 3600},
        "initial_wait": 20,
        "ok_script_log_monitor": {
            "log_dir": "",
            "file_pattern": "ok-script*.log",
            "abort_on_error": True,
            "require_date_token": False,
            "error_markers": [
                "任务失败标记 |",
                "以下任务未完成或失败",
                "exception stopped",
                "DailyTask_Exception",
                "Failed to ensure main.",
                "Start game failed, please start game first",
            ],
            "completion_markers": [
                "Successfully Executed Task, Exiting Game and App!",
                "日常完成!",
            ],
            "stall_after_error_sec": 180,
            "poll_interval": 2.0,
            "encoding": "utf-8",
            "read_timeout": 10.0,
        },
        # ok-ap.exe 只是 pyappify 包装器，真正干活的 pythonw 引擎按命令行补刀
        "cleanup_cmd_patterns": [
            {"name": "pythonw.exe", "cmdline": "ok-ap"},
        ],
        # 游戏可能经官方启动器拉起，收尾一并清掉，避免遮挡下一个游戏
        "cleanup_processes": ["launcher.exe"],
        "close_launcher_processes": ["launcher.exe"],
    },
)

register(PROFILE)
