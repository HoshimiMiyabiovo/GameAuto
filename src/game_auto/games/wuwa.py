"""鸣潮（ok-ww）。"""

from .base import GameProfile, ScriptToolSpec
from .registry import register

PROFILE = GameProfile(
    game_key="wuwa",
    app_key="WutheringWaves",
    display_name="鸣潮",
    order=3,
    # exe 在 <安装根>/Wuthering Waves Game/ 下，扫描根上溯 1 层
    disk_root_up_levels=1,
    aliases=("ok-ww",),
    sign_section="wuwa_kujiequ",
    daily_tool="ok-ww",
    daily_parser="game_auto.services.ok_ww_daily_parser:parse_latest_ok_ww_log",
    daily_merge="game_auto.games.wuwa:merge_daily_log",
    verify_augment="game_auto.games.wuwa:augment_verify",
    update_window=("04:00", "11:00"),
    update_key="wuwa",
    progress_hint="game_auto.games.base:build_maintenance_hint",
    view_key="wuwa",
    supports_stamina=True,
    script_tools=(ScriptToolSpec(
        label="ok-ww", repo="ok-oldking/ok-wuthering-waves", pyappify=True,
        tool_key="ok-ww",
        # win32.zip 是 pyappify 在线包（~5MB，首启自补全运行时）；
        # setup.exe 是几百 MB 安装器，绝不匹配
        asset_patterns=(r"^ok-ww-win32\.zip$",),
        launcher_name="ok-ww.exe",
    ),),
    hint="鸣潮使用 ok-ww，一键启动并监控。",
    default_app={
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
        # ok-ww 日常 2-4 分钟即可跑完；短时长已由 execute_task 用
        # "Successfully Executed Task" 日志复核，不能再按 MIN_RUN_TIME 降级 warning
        # （否则 272s 的成功会被记成 skipped，并跳过每日 API 回填）。
        "skip_min_run_warning": True,
        # 拉 ok-ww 前必须等游戏窗口稳定，否则 ok-ww 会误判游戏没起而重复启动客户端。
        # 这是条件等待（窗口一稳立刻走），不是盲睡。300s 是给首启/着色器编译留余量：
        # 11-02 用 120s 时窗口还在加载，流程把游戏杀了（用户实际已看到启动画面）。
        "wait_game_window_before_script": True,
        "game_window_wait_sec": 300,
        "game_window_min_area": 307200,
        "post_launch_click_game": True,
        "post_launch_click_timeout": 8,
        # 阶段 1：headless ok-ww runner + DailyTask。默认关闭，验证后再打开。
        # 打开后 game_auto 不再拉起 ok-ww.exe，而是常驻 runner 并调用其 task。
        "module_mode": {
            "enabled": False,
            "task": "DailyTask",
            "timeout": 1800,
            "close_game": True,
            "runtime_root": "",
            "humanize_config": "",
        },
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
)


def merge_daily_log(bridge, game, data, tool_name, game_key):
    """鸣潮：日志 + 库街区便笺互补合并。"""
    from .base import merge_api_daily_log

    merge_api_daily_log(bridge, game, data, tool_name, game_key)


def augment_verify(data, load_api_tasks, logger):
    """复核前并入官方「今日活跃」计数（有 API 数据时以真实活跃度为准）。"""
    data.setdefault("tasks", []).extend(load_api_tasks("wuwa", logger))


register(PROFILE)
