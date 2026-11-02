"""游戏档案（GameProfile）基类与通用日常日志钩子。

插件式约定：每个游戏一个 ``games/<game>.py`` 模块，模块内构造 ``PROFILE``
并调用 :func:`game_auto.games.registry.register` 注册。档案只描述“这个游戏是谁”：

- 身份：app_key / game_key / 别名 / 显示名 / 排序
- 默认配置：原 settings.defaults.DEFAULT_APPS 的单项
- 数据源：日常日志解析器、日志合并策略、无日志时的占位
- 调度：月商店流程 key、更新维护兜底窗口
- 呈现：QML 专项视图 key、每日进度提示文案

具体玩法（进入游戏、刷本、点击序列）仍留在 ``runtime/`` 各流程模块，
注册表只负责把散落在 settings / bridge / batch runner 里的按游戏分支收口。
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

LOGGER = logging.getLogger("GameAutomation.Games")


def load_ref(ref: str) -> Optional[Callable]:
    """加载 ``"包.模块:函数"`` 形式的延迟引用；空串返回 None。

    每次调用都重新 getattr，测试里 patch 模块属性（如
    ``game_auto.services.ok_nte_daily_parser.parse_latest_ok_nte_log``）仍然生效。
    """
    if not ref:
        return None
    module_name, _, attr = str(ref).partition(":")
    if not module_name or not attr:
        raise ValueError(f"无效的档案引用: {ref!r}（应为 module:function）")
    module = importlib.import_module(module_name)
    return getattr(module, attr)


def merge_replace_daily_log(bridge, game: Dict[str, Any], data: Dict[str, Any], tool_name: str, game_key: str) -> None:
    """默认合并策略：用脚本日志整体替换，但保留 tracker 里已有的 sign_ 任务。"""
    existing_sign = [
        t for t in (game.get("tasks") or [])
        if str(t.get("task_key") or "").startswith("sign_")
    ]
    new_data = dict(data)
    if existing_sign:
        new_data["tasks"] = existing_sign + list(new_data.get("tasks") or [])
    game.clear()
    game.update(new_data)
    LOGGER.info(
        "[Bridge] 已用 %s 日志替换 %s 进度: success=%s sign_tasks=%d",
        tool_name, game_key, new_data.get("is_all_success"), len(existing_sign),
    )


def merge_api_daily_log(
    bridge, game: Dict[str, Any], data: Dict[str, Any], tool_name: str, game_key: str,
    script_task_key: str = "script_ok-ww",
) -> None:
    """鸣潮 / 终末地：脚本日志与 API 便笺互补合并（不整体替换）。

    API 行带体力/活跃度等便笺数据，日志行带脚本执行进度；同名任务以日志为准，
    日志任务前置。脚本本次失败时（DB 里 script_<tool> 为 failed），日志里的
    success 任务降级标注为“上次运行”，完成度不再冒充今日真实进度。
    """
    existing_tasks = list(game.get("tasks") or [])
    log_tasks = list(data.get("tasks") or [])
    log_keys = {(t.get("task_key") or t.get("task_name")) for t in log_tasks}
    sign_tasks = [
        t for t in existing_tasks
        if (t.get("task_key") or "").startswith("sign_")
    ]
    kept_api_tasks = [
        t for t in existing_tasks
        if (t.get("task_key") or t.get("task_name")) not in log_keys
        and not (t.get("task_key") or "").startswith("sign_")
    ]
    # 本次脚本 run 是否失败：DB 里 script_ok-ww 行的状态是最终判据。
    # 日志解析器可能只找到上一次 run 的 open_daily，不能拿它的 100% 覆盖本次失败
    # （2026-11-02 实跑事故）。终末地按历史口径不做该降级（script_task_key 传空）。
    script_failed = bool(script_task_key) and any(
        (t.get("task_key") or t.get("task_name")) == script_task_key
        and t.get("status") == "failed"
        for t in existing_tasks
    )
    if script_failed:
        for t in log_tasks:
            if t.get("status") in ("success", "already"):
                t["message"] = "上次运行"
    game["tasks"] = sign_tasks + log_tasks + kept_api_tasks
    game["is_all_success"] = data.get("is_all_success", game.get("is_all_success"))
    game["has_failed"] = bool(data.get("has_failed")) or bool(game.get("has_failed"))
    if data.get("errors"):
        game["errors"] = (game.get("errors") or []) + list(data.get("errors"))
    game["nickname"] = data.get("nickname") or game.get("nickname", "")
    if script_failed:
        # 脚本本次失败时卡片结论不能停留在「未刷新」：这是失败，不是等服务器刷新。
        game["completion_state"] = "failed"

    # 完成度必须反映真实游玩进度，不能用脚本子任务比例代替：
    # ok-ww 的 progress_percent 是"脚本跑了几步"（全跑完就 100%），
    # 与活跃度 90/100 无关。这里优先取 API 便笺的关键计数。
    real_pct, real_src = bridge._real_completion_percent(
        game_key, sign_tasks + log_tasks + kept_api_tasks)
    if real_pct is not None:
        game["progress_percent"] = real_pct
        game["progress_source"] = real_src
    elif script_failed:
        game["progress_percent"] = 0
        game["progress_source"] = "script_failed"
        for t in game["tasks"]:
            if (t.get("task_key") or t.get("task_name")) == "今日活跃":
                ex = t.get("extra") or {}
                if isinstance(ex, str):
                    try:
                        import json as _json
                        ex = _json.loads(ex)
                    except Exception:  # noqa: BLE001 - 坏 JSON 按空处理
                        ex = {}
                else:
                    ex = dict(ex)
                ex["stale"] = True
                ex["stale_reason"] = "脚本未完成，活跃度未采集"
                t["extra"] = ex
    else:
        game["progress_percent"] = data.get("progress_percent", game.get("progress_percent"))
        game["progress_source"] = "script"
    LOGGER.info(
        "[Bridge] %s：合并脚本日志进度(%d 项) + API 便笺数据(%d 项)，成功=%s 完成度=%s%%(%s)",
        game_key, len(log_tasks), len(kept_api_tasks),
        game["is_all_success"], game["progress_percent"], game.get("progress_source"),
    )


def build_maintenance_hint(app_key: str, logger) -> Optional[Callable[[], bool]]:
    """米游系/鸣潮/暖暖：官方公告处于维护窗口时快速跳过等待。"""
    try:
        from ..services.game_update_calendar import get_maintenance_state
    except Exception:
        return None

    def _hint():
        state = get_maintenance_state(app_key)
        if not state:
            return False
        open_dt = state.get("open_at_dt")
        open_txt = open_dt.strftime("%H:%M") if open_dt else "?"
        logger.info(
            f"[{app_key}] 官方公告维护中"
            f"（{state.get('version') or '版本更新'}），预计 {open_txt} 开服，"
            f"本次跳过等待"
        )
        return True

    return _hint


@dataclass(frozen=True)
class GameProfile:
    """一个游戏的静态档案。"""

    game_key: str
    app_key: str
    display_name: str
    order: int

    # 历史别名（补 app_key.lower() / game_key 之外的特殊写法）
    aliases: Tuple[str, ...] = ()
    # app_config.json 顶层签到节名；无签到数据源留空
    sign_section: str = ""
    # settings.defaults.DEFAULT_APPS 中的默认项
    default_app: Dict[str, Any] = field(default_factory=dict)

    # ── 日常进度 ───────────────────────────────────────────────
    # 日志工具显示名（ok-ww / BetterGI / OneDragon / ...）
    daily_tool: str = ""
    # 日志解析器延迟引用 "module:function"
    daily_parser: str = ""
    # 合并策略延迟引用 "module:function"；空 = merge_replace_daily_log
    daily_merge: str = ""
    # 无当日日志时是否仍生成占位卡片（NTE / 白荆）
    empty_daily: str = ""
    # 白荆类“永远注入占位”的游戏：没有任何日志源也要能看到卡片
    always_placeholder: bool = False
    # 复核兜底钩子 "module:function"：向 data 追加 API 关键任务，
    # 返回 "keep_verdict" 表示直接保持原判（异环活跃度已满）
    verify_augment: str = ""
    # 仅这些脚本方案才做日志复核（空 = 所有方案）
    verify_scripts: Tuple[str, ...] = ()

    # ── 调度 ─────────────────────────────────────────────────
    # 月商店流程 key 与报告标签（runtime 里绑定执行入口）
    monthly_flow: str = ""
    monthly_label: str = ""
    # 无官方公告时的常规维护窗口 (start, end)，用于兜底补跑
    update_window: Tuple[str, str] = ()
    # game_update_calendar 的公告解析 key（崩铁是 "hsr"，与内部 game_key 不同）
    update_key: str = ""
    # 进程等待阶段的进度提示构建器 "module:function"：(app_key, logger) -> callable|None
    progress_hint: str = ""

    # ── 呈现 / 配置 ──────────────────────────────────────────
    # QML 专项视图 key（无专项页留空）
    view_key: str = ""
    # 启动页提示文案
    hint: str = ""
    # 是否有第三方脚本体力刷本配置适配器
    supports_stamina: bool = False

    def latest_daily_log(self) -> Optional[Dict[str, Any]]:
        parser = load_ref(self.daily_parser)
        if parser is None:
            return None
        return parser()

    def daily_merger(self) -> Callable:
        return load_ref(self.daily_merge) or merge_replace_daily_log

    def build_empty_daily(self) -> Optional[Dict[str, Any]]:
        builder = load_ref(self.empty_daily)
        return builder() if builder else None

    def augment_verify(self, data: Dict[str, Any], load_api_tasks: Callable[[str, Any], List[Dict[str, Any]]], logger) -> bool:
        """复核前把 API 关键任务并入脚本数据；返回 True 表示直接保持原判。"""
        hook = load_ref(self.verify_augment)
        if hook is None:
            return False
        return bool(hook(data, load_api_tasks, logger))

    def build_progress_hint(self, logger) -> Optional[Callable[[], bool]]:
        builder = load_ref(self.progress_hint)
        if builder is None:
            return None
        return builder(self.app_key, logger)
