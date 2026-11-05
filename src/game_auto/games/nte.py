"""异环（ok-nte + 前置流程）。

异环的专属口径都收在本模块：
- 默认配置（post_launch_ensure_main / post_launch_ocr_flow / 更新预算）
- 日常日志缺失时的「游戏内每日」占位
- 与塔吉多社区任务合并（保留 BBS 签到，丢弃游戏签到）
- 活跃度满 100 时复核保持原判
- 更新中快速跳过（ok-nte 日志更新行 + 官方维护窗口）
"""

from pathlib import Path

from .base import GameProfile, ScriptToolSpec
from .registry import register

# 与 settings.paths.TEMPLATES_DIR 保持一致。这里不 import settings.paths：
# 注册表会在 settings 包初始化期间被扫描，游戏模块再反向 import settings
# 会触发 defaults 的半初始化（DEFAULT_APPS 只拿到部分档案）。
_TEMPLATES_DIR = Path(__file__).resolve().parents[3] / "assets" / "templates"

PROFILE = GameProfile(
    game_key="nte",
    app_key="NevernessToEverness",
    display_name="异环",
    order=5,
    aliases=("ok-nte", "nte"),
    sign_section="tajiduo_sign",
    daily_tool="ok-nte",
    daily_parser="game_auto.services.ok_nte_daily_parser:parse_latest_ok_nte_log",
    daily_merge="game_auto.games.nte:merge_daily_log",
    empty_daily="game_auto.games.nte:build_empty_daily",
    verify_augment="game_auto.games.nte:augment_verify",
    monthly_flow="NTE_MONTHLY",
    monthly_label="异环每月商店",
    update_window=("06:00", "11:00"),
    update_key="nte",
    progress_hint="game_auto.games.nte:build_progress_hint",
    view_key="tajiduo",
    supports_stamina=True,
    script_tools=(ScriptToolSpec(
        label="ok-nte", repo="BnanZ0/ok-nte", pyappify=True,
        tool_key="ok-nte",
        # 精确匹配在线包：该仓库另有 full/body 两个 300MB+ zip，宽正则会下错
        asset_patterns=(r"^ok-nte-win32\.zip$",),
        launcher_name="ok-nte.exe",
    ),),
    hint="异环会先处理启动器，再接入 ok-nte。",
    default_app={
        "display_name": "异环",
        "script_name": "ok-nte",
        "type": "script",
        "launcher": "",
        "script_process_name": "ok-nte.exe",
        "game": {"process_name": "HTGame.exe"},
        "post_launch_ocr_flow": "nte_start",
        # ok-nte 的 LauncherTask 只在游戏进程不存在时才走"启动器→进入游戏"；
        # 游戏被玩家留在标题页时它会直接切窗口识别，导致每个任务 10 分钟超时
        # （2026-10-29 两次实战）。启动脚本后补一步：看到「进入游戏」就点掉。
        "post_launch_ensure_main": "nte",
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
                "daily_weekly_template": str(_TEMPLATES_DIR / "ok-nte 日常周常.png"),
                "daily_weekly_target_text": "日常周常",
                "daily_task_template": str(_TEMPLATES_DIR / "ok-nte 日常任务-2.png"),
                "daily_task_target_text": "日常任务",
                "start_template": str(_TEMPLATES_DIR / "ok-nte 开始.png"),
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
)


def build_empty_daily():
    """ok-nte 今日未执行时的占位：保留「游戏内每日」一行，标记为待执行。"""
    return {
        "game_key": "nte",
        "game_name": "异环",
        "uid": "",
        "nickname": "ok-nte",
        "is_all_success": False,
        "has_failed": False,
        "progress_percent": 0,
        "tasks": [{
            "task_key": "游戏内每日",
            "task_name": "游戏内每日",
            "status": "pending",
            "message": "待 ok-nte 执行",
        }],
        "errors": [],
        "report_time": "",
        "log_date": "",
    }


def merge_daily_log(bridge, game, data, tool_name, game_key):
    """游戏内进度由 ok-nte 判定，社区任务（BBS 签到/浏览/点赞/分享）保留。

    历史 bug：过滤条件写成"endswith(签到)"，把 BBS 签到（+80 金币）和异环签到
    也误删，导致签到行只剩兜底文案。这里只丢「游戏签到」这一项。
    """
    existing_tasks = list(game.get("tasks") or [])
    ok_nte_tasks = list(data.get("tasks") or [])
    kept = [
        t for t in existing_tasks
        if str(t.get("task_name", "")) != "游戏签到"
    ]
    community_done = all(t.get("status") == "success" for t in kept) if kept else True
    game["tasks"] = list(kept) + ok_nte_tasks
    game["is_all_success"] = bool(data.get("is_all_success")) and community_done
    game["has_failed"] = bool(data.get("has_failed")) or bool(game.get("has_failed"))
    if data.get("errors"):
        game["errors"] = (game.get("errors") or []) + list(data.get("errors"))
    game["nickname"] = data.get("nickname", game.get("nickname", ""))
    game["progress_percent"] = data.get("progress_percent", game.get("progress_percent"))
    from .base import LOGGER

    LOGGER.info(
        "[Bridge] 异环：合并 ok-nte 游戏内进度(%d 项) + 社区任务(%d 项)，成功=%s",
        len(ok_nte_tasks), len(kept), game["is_all_success"])


def augment_verify(data, load_api_tasks, logger):
    """复核保护：脚本日志确认「日常领取」完成时保持原判，边角失败不改判。

    2026-11-05 事故后 roleHome 的 dayvalue 不再可信（无日期归属、字段各自滞后、
    与游戏内实时值双向矛盾），复核保护从「活跃度满」改为脚本自身的核心任务
    证据：日常领取 success = 当日日常（含活跃度奖励领取）已做。
    """
    data.setdefault("tasks", []).extend(load_api_tasks("nte", logger))
    for t in data["tasks"]:
        if (str(t.get("task_name") or "") == "日常领取"
                and str(t.get("status") or "") in ("success", "already")):
            return True
    return False


def build_progress_hint(app_key, logger):
    """更新日快速跳过：ok-nte 日志出现更新行，或官方公告处于维护窗口。"""
    try:
        from game_auto.services.ok_nte_daily_parser import is_update_in_progress
        from game_auto.services.game_update_calendar import get_maintenance_state
    except Exception:
        return None

    def _hint():
        if is_update_in_progress():
            return True
        return bool(get_maintenance_state(app_key))

    return _hint


register(PROFILE)
