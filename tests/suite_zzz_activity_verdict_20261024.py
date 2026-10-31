"""绝区零（OneDragon）活跃度已满超时兜底回归测试（2026-10-24）。

用户拍板：与原神/崩铁对齐——超时但 OneDragon 日志确认「活跃度已满」
→ 改判成功、不触发统一重试；仅认显式"已满"信号。

断言：
  1. 超时 + 活跃度已满 → 成功；未满/领取成功(未满)/无数据/停止/本就成功 → 原判
  2. KEY_STATUS_TASKS['zzz']：活跃度奖励 success + 其他任务失败 → completed
     （复核不再被边角任务拖垮）
  3. 真实日志端到端：今天 11:22 运行的「活跃度已满」可被解析并触发兜底
  4. 调用点接线：ZenlessZoneZero 分支调用 _apply_zzz_activity_verdict

跑法：.venv\\Scripts\\python.exe artifacts\\test_zzz_activity_verdict_20261024.py
"""

import logging
import sys
from unittest.mock import patch

sys.path.insert(0, r"E:\GithubProject")

from game_auto.runtime import task_batch_runner as tbr  # noqa: E402
from game_auto.services import zzz_daily_parser as zp  # noqa: E402
from game_auto.services.daily_progress_tracker import (  # noqa: E402
    DailyTaskRecord, GameDailyRecord, KEY_STATUS_TASKS,
)

FAILURES = []
LOGGER = logging.getLogger("zzz_test")


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}", flush=True)
    else:
        print(f"  FAIL {name} {detail}", flush=True)
        FAILURES.append(name)


def _fake_data(state="full", extra_failed=None):
    """模拟解析器输出。state 与 status/message 的对应关系与 zzz_daily_parser 一致：
    full → success/活跃度已满；claimed → success/日常奖励领取成功；not_full → failed/活跃度未满。
    """
    msg_status = {
        "full": ("success", "活跃度已满"),
        "claimed": ("success", "日常奖励领取成功"),
        "not_full": ("failed", "活跃度未满"),
    }
    status, message = msg_status[state]
    tasks = [{"task_key": "活跃度奖励", "task_name": "活跃度奖励",
              "status": status, "message": message}]
    if extra_failed:
        tasks.append({"task_key": extra_failed, "task_name": extra_failed,
                      "status": "failed", "message": "执行失败"})
    return {"game_key": "zzz", "game_name": "绝区零",
            "is_all_success": not extra_failed and state != "not_full",
            "has_failed": bool(extra_failed) or state == "not_full",
            "progress_percent": 66, "tasks": tasks,
            "errors": [f"{extra_failed} 失败"] if extra_failed else [], "report_time": ""}


def test_verdict():
    print("[1] 超时兜底判定组合")
    with patch.object(zp, "parse_latest_zzz_log", lambda: _fake_data("full")):
        s, m = tbr._apply_zzz_activity_verdict(False, "超时", LOGGER, False)
    check("超时+已满 → 成功", s is True and "活跃度已满" in m, (s, m))

    with patch.object(zp, "parse_latest_zzz_log", lambda: _fake_data("claimed")):
        s, m = tbr._apply_zzz_activity_verdict(False, "超时", LOGGER, False)
    check("仅领取成功(未确认已满) → 原判", (s, m) == (False, "超时"), (s, m))

    with patch.object(zp, "parse_latest_zzz_log", lambda: _fake_data("not_full")):
        s, m = tbr._apply_zzz_activity_verdict(False, "超时", LOGGER, False)
    check("未满 → 原判", (s, m) == (False, "超时"), (s, m))

    with patch.object(zp, "parse_latest_zzz_log", lambda: None):
        s, m = tbr._apply_zzz_activity_verdict(False, "超时", LOGGER, False)
    check("无数据 → 原判", (s, m) == (False, "超时"), (s, m))

    s, m = tbr._apply_zzz_activity_verdict(False, "超时", LOGGER, True)
    check("用户停止 → 原判", (s, m) == (False, "超时"), (s, m))

    s, m = tbr._apply_zzz_activity_verdict(True, "成功", LOGGER, False)
    check("本就成功 → 原判", (s, m) == (True, "成功"), (s, m))


def test_key_status():
    print("[2] KEY_STATUS_TASKS 口径")
    check("zzz 已登记活跃度奖励", "zzz" in KEY_STATUS_TASKS and KEY_STATUS_TASKS["zzz"] == ("活跃度奖励",))

    tasks = [
        DailyTaskRecord(task_key="活跃度奖励", task_name="活跃度奖励", source="script", status="success"),
        DailyTaskRecord(task_key="体力刷本", task_name="体力刷本", source="script", status="failed"),
    ]
    rec = GameDailyRecord(game_key="zzz", game_name="绝区零", tasks=tasks)
    check("活跃度奖励成功+体力刷本失败 → completed", rec.completion_state() == "completed",
          rec.completion_state())

    tasks_fail = [DailyTaskRecord(task_key="活跃度奖励", task_name="活跃度奖励",
                                  source="script", status="failed")]
    check("活跃度奖励失败 → failed",
          GameDailyRecord(game_key="zzz", game_name="绝区零", tasks=tasks_fail).completion_state() == "failed")


def test_recheck_no_flip():
    print("[3] 事后复核不再被边角失败拖垮")
    with patch.object(zp, "parse_latest_zzz_log", lambda: _fake_data("full", extra_failed="体力刷本")):
        r = tbr._verify_script_log_result("ZenlessZoneZero", None, "绝区零", LOGGER)
    check("已满+体力刷本失败 → 不改判", r is None, r)

    with patch.object(zp, "parse_latest_zzz_log", lambda: _fake_data("not_full", extra_failed="等待大世界画面")):
        r = tbr._verify_script_log_result("ZenlessZoneZero", None, "绝区零", LOGGER)
    check("未满+真实失败 → 改判（旧保护仍在）", r is not None and "等待大世界画面" in r, r)


def test_real_log():
    print("[4] 真实日志端到端")
    data = zp.parse_latest_zzz_log()
    if data is None:
        # 跨天/今天还没跑绝区零时没有当日日志；这是环境依赖，不算回归失败
        print("  SKIP 今天没有 OneDragon 日志（跨天或今天未运行），跳过真实日志端到端")
        return
    act = next((t for t in data["tasks"] if t["task_name"] == "活跃度奖励"), None)
    check("解析出活跃度奖励任务", act is not None, data["tasks"][:3])
    if act and act["message"] == "活跃度已满":
        with patch.object(zp, "parse_latest_zzz_log", lambda: data):
            s, m = tbr._apply_zzz_activity_verdict(False, "超时", LOGGER, False)
        check("真实日志已满 → 兜底生效", s is True, (s, m))
    else:
        check("今天活跃度状态可解释（非已满则不兜底）", act is not None, act)


def test_wiring():
    print("[5] 调用点接线")
    import inspect
    src = inspect.getsource(tbr._run_single_task)
    check("ZenlessZoneZero 分支调用兜底", "app_key == \"ZenlessZoneZero\":" in src
          and "_apply_zzz_activity_verdict" in src)


def main():
    test_verdict()
    test_key_status()
    test_recheck_no_flip()
    test_real_log()
    test_wiring()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
