# -*- coding: utf-8 -*-
"""每日进度 API 回填门控回归（2026-10-31）。

背景：今天鸣潮脚本实际成功（272s），但批处理按 MIN_RUN_TIME=300 降级为
warning（落库 skipped）；终末地脚本被日志复核改判失败。两者都因此没触发
跑完后的 API 回填，每日进度里完全没有「今日活跃 / 日活跃」。

本套件锁定：
1. 成功必查 API；失败/短时长 warning 时鸣潮/终末地/异环仍查；
2. 用户停止、维护跳过不查；
3. 鸣潮不再因 2-4 分钟真实跑完被降级 warning；
4. 今日活跃/日活跃能按 current/total 落库，并被完成度逻辑识别；
5. 调用点确实用统一门控，而不是旧的 state == success。

跑法：.venv\\Scripts\\python.exe tests\\suite_daily_api_query_gate_20261031.py
"""

import copy
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import game_auto.clients.daily_checker as dc  # noqa: E402
import game_auto.runtime.task_batch_runner as tbr  # noqa: E402
import game_auto.settings.defaults as defaults  # noqa: E402

FAILURES = []
LOGGER = logging.getLogger("test.daily.api_gate")
LOGGER.addHandler(logging.NullHandler())


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def test_gate():
    print("[1] 任务结束后是否查 API")
    cases = [
        ("success", "wuwa", False, True),
        ("success", "endfield", False, True),
        ("success", "genshin", False, True),
        ("failed", "wuwa", False, True),
        ("failed", "endfield", False, True),
        ("failed", "nte", False, True),
        ("failed", "genshin", False, False),
        ("failed", "honkai_sr", False, False),
        ("warning", "wuwa", False, True),
        ("warning", "endfield", False, True),
        ("warning", "genshin", False, False),
        ("skipped", "endfield", False, False),
        ("success", "wuwa", True, False),
        ("failed", "endfield", True, False),
    ]
    for state, game_key, stopped, expected in cases:
        got = tbr._should_auto_query_api_progress(state, game_key, stopped)
        check(f"{state}/{game_key}/stopped={stopped} -> {expected}", got is expected, got)


def test_wuwa_short_run():
    print("[2] 鸣潮短时长不再降级 warning")
    wuwa = defaults.DEFAULT_APPS.get("WutheringWaves") or {}
    check("WutheringWaves 配置 skip_min_run_warning",
          wuwa.get("skip_min_run_warning") is True, wuwa.get("skip_min_run_warning"))
    # 其他游戏不应被误开
    check("GenshinImpact 未误开 skip_min_run_warning",
          not (defaults.DEFAULT_APPS.get("GenshinImpact") or {}).get("skip_min_run_warning"))


def test_wuwa_recording():
    print("[3] 鸣潮今日活跃落库字段")
    calls = []

    class Tracker:
        def record_task(self, **kwargs):
            calls.append(kwargs)

    status = dc.GameDailyStatus(
        game_key="wuwa", game_name="鸣潮", uid="1", nickname="n",
        is_daily_completed=True,
        tasks=[dc.DailyTaskItem(name="今日活跃", current=100, total=100, is_completed=True)],
    )
    dc._record_wuwa_to_tracker(Tracker(), status)
    check("记录 1 项", len(calls) == 1, calls)
    if calls:
        call = calls[0]
        check("task_key=今日活跃", call.get("task_key") == "今日活跃", call)
        check("source=community", call.get("source") == "community", call)
        check("status=success", call.get("status") == "success", call)
        check("extra 带 100/100",
              call.get("extra") == {"current": 100, "total": 100}, call.get("extra"))

    calls.clear()
    stale = dc.GameDailyStatus(
        game_key="wuwa", game_name="鸣潮", uid="1", nickname="n",
        is_daily_completed=False,
        tasks=[dc.DailyTaskItem(name="今日活跃", current=0, total=100, is_completed=False)],
    )
    dc._record_wuwa_to_tracker(Tracker(), stale)
    if calls:
        extra = calls[0].get("extra") or {}
        check("未刷新 0/100 标 stale", extra.get("stale") is True, extra)
        check("未刷新状态 pending", calls[0].get("status") == "pending", calls[0])


def test_auto_query_call():
    print("[4] _auto_query_daily_progress 实际调用")
    calls = {}

    def fake_check(config=None, game_keys=None, tracker=None):
        calls["config"] = config
        calls["game_keys"] = game_keys
        calls["tracker"] = tracker
        return SimpleNamespace(games=[
            SimpleNamespace(errors=[], game_name="鸣潮",
                            tasks=[SimpleNamespace(is_completed=True)])
        ])

    sentinel_tracker = object()
    # _auto_query_daily_progress 用的是 task_batch_runner 模块顶部的 get_tracker，
    # 必须 patch 这个引用，patch services 模块本身不会生效。
    with patch.object(dc, "check_games_progress", fake_check), \
         patch.object(tbr, "get_tracker", lambda *_a, **_k: sentinel_tracker), \
         patch("game_auto.settings.manager.load", lambda **_k: {"apps": {}}):
        tbr._auto_query_daily_progress(SimpleNamespace(logger=LOGGER), "wuwa", "鸣潮")

    check("按游戏只查一只", calls.get("game_keys") == ["wuwa"], calls.get("game_keys"))
    check("传入 tracker 落库", calls.get("tracker") is sentinel_tracker, calls.get("tracker"))


def _snapshot(game_key, api_tasks):
    return SimpleNamespace(games=[
        SimpleNamespace(game_key=game_key, tasks=[
            SimpleNamespace(
                task_key=t["task_key"], task_name=t["task_name"],
                source=t["source"], status=t["status"],
                message=t.get("message", ""), extra=dict(t.get("extra") or {}),
            )
            for t in api_tasks
        ])
    ])


class _FakeTracker:
    def __init__(self, snapshot):
        self._snapshot = snapshot

    def load_today(self):
        return self._snapshot


def test_api_overrides_log_recheck():
    print("[5] API 活跃度覆盖边角日志失败")
    ef_data = {
        "game_key": "endfield",
        "game_name": "终末地",
        "is_all_success": False,
        "errors": ["⭐送礼: 多次收礼失败，停止重试；⭐造装备: 未找到制作按钮，任务失败"],
        "tasks": [
            {"task_key": "⭐送礼", "task_name": "⭐送礼", "status": "failed", "message": "失败"},
            {"task_key": "⭐造装备", "task_name": "⭐造装备", "status": "failed", "message": "失败"},
        ],
    }
    api_ef = [{"task_key": "日活跃", "task_name": "日活跃", "source": "community",
               "status": "success", "message": "100/100",
               "extra": {"current": 100, "total": 100}}]
    # 复核会往 parser 返回的 tasks 里追加 API 任务，每次必须给新副本
    with patch.object(tbr, "get_tracker", lambda *_a, **_k: _FakeTracker(_snapshot("endfield", api_ef))), \
         patch("game_auto.services.ok_ef_daily_parser.parse_latest_ok_ef_report", lambda: copy.deepcopy(ef_data)):
        result = tbr._verify_script_log_result("ArknightsEndfield", "ok-ef", "终末地", LOGGER)
    check("终末地 日活跃 100/100 不改判失败", result is None, result)

    with patch.object(tbr, "get_tracker", lambda *_a, **_k: _FakeTracker(_snapshot("endfield", []))), \
         patch("game_auto.services.ok_ef_daily_parser.parse_latest_ok_ef_report", lambda: copy.deepcopy(ef_data)):
        result = tbr._verify_script_log_result("ArknightsEndfield", "ok-ef", "终末地", LOGGER)
    check("终末地无 API 数据时仍按日志改判", bool(result), result)

    ww_data = {
        "game_key": "wuwa",
        "game_name": "鸣潮",
        "is_all_success": False,
        "errors": ["边角任务失败"],
        "tasks": [
            {"task_key": "边角", "task_name": "边角", "status": "failed", "message": "失败"},
        ],
    }
    api_ww = [{"task_key": "今日活跃", "task_name": "今日活跃", "source": "community",
               "status": "success", "message": "100/100",
               "extra": {"current": 100, "total": 100}}]
    with patch.object(tbr, "get_tracker", lambda *_a, **_k: _FakeTracker(_snapshot("wuwa", api_ww))), \
         patch("game_auto.services.ok_ww_daily_parser.parse_latest_ok_ww_log", lambda: copy.deepcopy(ww_data)):
        result = tbr._verify_script_log_result("WutheringWaves", "ok-ww", "鸣潮", LOGGER)
    check("鸣潮 今日活跃 100/100 不改判失败", result is None, result)

    with patch.object(tbr, "get_tracker", lambda *_a, **_k: _FakeTracker(_snapshot("wuwa", []))), \
         patch("game_auto.services.ok_ww_daily_parser.parse_latest_ok_ww_log", lambda: copy.deepcopy(ww_data)):
        result = tbr._verify_script_log_result("WutheringWaves", "ok-ww", "鸣潮", LOGGER)
    check("鸣潮无 API 数据时仍按日志改判", bool(result), result)


def test_key_progress_and_percent():
    print("[6] 完成度口径能识别活跃度")
    tasks = {
        "wuwa": [{"task_name": "今日活跃", "current": 100, "total": 100}],
        "endfield": [{"task_name": "日活跃", "current": 100, "total": 100}],
    }
    from game_auto.ui.bridge_backends.daily_progress import _real_completion_percent
    for key, items in tasks.items():
        pct, source = _real_completion_percent(key, items)
        check(f"{key} 取到 100%", pct == 100, (pct, source))
        check(f"{key} 来源是关键任务",
              source in ("今日活跃", "日活跃"), source)


def test_source_wiring():
    print("[7] 调用点接线（只留稳定锚点，不锁参数格式/变量名/注释位置）")
    src = Path(tbr.__file__).read_text(encoding="utf-8")
    check("统一门控函数存在", hasattr(tbr, "_should_auto_query_api_progress"))
    check("统一门控被调用（def 之外还有调用点）",
          src.count("_should_auto_query_api_progress(") >= 2)
    check("成功和失败共用回填（def 之外还有调用点）",
          src.count("_auto_query_daily_progress(") >= 2)
    # 鸣潮的复核增强已收进游戏档案（games/wuwa.py 的 augment_verify）
    wuwa_src = (Path(__file__).resolve().parent.parent / "src" / "game_auto" / "games" / "wuwa.py").read_text(encoding="utf-8")
    check("鸣潮复核也合并 API 关键进度", "load_api_tasks(" in wuwa_src)

    # 2026-11-01 实跑事故：函数体内 `from ..settings import resolve_game_key`
    # 会把该名字变成整个 _run_single_task 的局部变量，前面的 API 门控行先用它
    # 直接 UnboundLocalError。模块顶部已有 import，函数内不得再局部导入。
    check("_run_single_task 不局部遮蔽 resolve_game_key",
          "resolve_game_key" not in tbr._run_single_task.__code__.co_varnames,
          tbr._run_single_task.__code__.co_varnames)
    check("resolve_game_key 模块级可用", callable(getattr(tbr, "resolve_game_key", None)))


def main():
    test_gate()
    test_wuwa_short_run()
    test_wuwa_recording()
    test_auto_query_call()
    test_api_overrides_log_recheck()
    test_key_progress_and_percent()
    test_source_wiring()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
