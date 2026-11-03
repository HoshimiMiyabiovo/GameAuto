"""无限暖暖（whimbox）。"""

from .base import GameProfile, ScriptToolSpec
from .registry import register

PROFILE = GameProfile(
    game_key="nikki",
    app_key="InfinityNikki",
    display_name="无限暖暖",
    order=6,
    aliases=("whimbox",),
    daily_tool="whimbox",
    daily_parser="game_auto.services.whimbox_daily_parser:parse_latest_whimbox_log",
    daily_merge="game_auto.games.nikki:merge_daily_log",
    update_window=("10:00", "11:00"),
    update_key="nikki",
    progress_hint="game_auto.games.base:build_maintenance_hint",
    view_key="nikki",
    supports_stamina=True,
    script_tools=(ScriptToolSpec(
        label="whimbox", repo="nikkigallery/Whimbox", tool_key="whimbox",
    ),),
    hint="无限暖暖常规任务；whimbox 一条龙接管每日。",
    default_app={
        "display_name": "无限暖暖",
        "script_name": "whimbox",
        # 2026-11-01 起接线 NikkiLauncher：先由本程序启动/进入主界面并执行
        # 「奇想日历领奖 + 晶石兑换/共鸣商店」，再把已在主界面的游戏交给 whimbox 跑一条龙
        # （whimbox 的 StartGameTask 检测到已在游戏会跳过启动）。日历流程内部失败
        # 只记日志/flow_failures，不阻断 whimbox。
        "prelaunch_flow": "NikkiLauncher",
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
)


def merge_daily_log(bridge, game, data, tool_name, game_key):
    """whimbox 子任务前置插入，同名任务取 whimbox 的更细粒度进度，保留 Pearpal 基线。"""
    existing_tasks = list(game.get("tasks") or [])
    whimbox_tasks = list(data.get("tasks") or [])
    existing_keys = {
        (t.get("task_key") or t.get("task_name")): idx
        for idx, t in enumerate(existing_tasks)
    }
    merged = []
    for wt in whimbox_tasks:
        wkey = wt.get("task_key") or wt.get("task_name")
        if wkey in existing_keys:
            merged.append({**existing_tasks[existing_keys[wkey]], **wt})
        else:
            merged.append(wt)
    whimbox_keys = {(wt.get("task_key") or wt.get("task_name")) for wt in whimbox_tasks}
    for t in existing_tasks:
        if (t.get("task_key") or t.get("task_name")) not in whimbox_keys:
            merged.append(t)
    game["tasks"] = merged
    # 整体成功/失败由 whimbox 决定（更精确）
    game["is_all_success"] = data.get("is_all_success", game.get("is_all_success"))
    game["has_failed"] = data.get("has_failed", game.get("has_failed"))
    if data.get("errors"):
        game["errors"] = data["errors"]
    game["nickname"] = data.get("nickname", game.get("nickname", ""))
    from .base import LOGGER

    LOGGER.info(
        "[Bridge] 已用 %s 日志合并 %s 任务细节: tasks=%d",
        tool_name, game_key, len(merged))


register(PROFILE)
