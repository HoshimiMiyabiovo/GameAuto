"""开批前更新预检回归测试（2026-10-21）。

背景：固定 11:05 / 11:35 兜底任务每天空跑。改为开批前预检：
维护中的游戏从本批剔除并按公告开服时间排队，其余游戏照常跑。

纯 mock，不启动 GUI、不读写真实补跑队列。断言：
  1. 维护中的游戏被剔除并排队
  2. 正常游戏留在本批
  3. 独立流程（XMFlow 等）不查维护状态
  4. 维护状态查询异常时 fail-open（按正常启动）
  5. _run_automation_backend 只把 runnable 游戏交给 run_task_batch
  6. 全部游戏维护中时传空列表，不启动脚本

跑法：.venv\\Scripts\\python.exe artifacts\\test_update_preflight_20261021.py
"""

import sys
from datetime import datetime

sys.path.insert(0, r"E:\GithubProject")

import game_auto.ui.bridge as bridge_mod  # noqa: E402
from game_auto.runtime import task_batch_runner  # noqa: E402
from game_auto.services.game_update_calendar import CN_TZ  # noqa: E402
from game_auto.ui.bridge import UiBridge  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


class FakeResult:
    def __init__(self, lines=None):
        self.report = list(lines or [])
        self.warnings = []
        self.app_key = ""
        self.state = "success"
        self.success = True
        self.message = ""


def make_split_bridge():
    b = UiBridge.__new__(UiBridge)
    b.scheduled = []
    b._schedule_update_retry = (
        lambda app_key, script_choice, info, source="": b.scheduled.append(
            (app_key, script_choice, info, source)
        )
    )
    return b


def state(version="3.3"):
    return {
        "under_maintenance": True,
        "version": version,
        "title": "更新维护",
        "open_at": "2026-10-21T11:00:00+08:00",
        "open_at_dt": datetime.now(CN_TZ),
    }


def run_split(targets, states, fail_keys=(), calls=None):
    original = bridge_mod.get_maintenance_state

    def fake(app_key):
        if calls is not None:
            calls.append(app_key)
        if app_key in fail_keys:
            raise RuntimeError("announcement api down")
        return states.get(app_key)

    bridge_mod.get_maintenance_state = fake
    try:
        return make_split_bridge()._split_maintenance_targets(targets)
    finally:
        bridge_mod.get_maintenance_state = original


def test_split_maintenance_targets():
    print("[1] 维护中的游戏剔除并排队，正常游戏保留")
    b = make_split_bridge()
    original = bridge_mod.get_maintenance_state

    def fake(app_key):
        if app_key == "ZenlessZoneZero":
            return state()
        return None

    bridge_mod.get_maintenance_state = fake
    try:
        runnable, deferred = b._split_maintenance_targets([
            ("ZenlessZoneZero", None),
            ("WutheringWaves", "ok-ww"),
        ])
    finally:
        bridge_mod.get_maintenance_state = original

    check("维护游戏不进本批", runnable == [("WutheringWaves", "ok-ww")], runnable)
    check("维护游戏进入 deferred", [d["app_key"] for d in deferred] == ["ZenlessZoneZero"], deferred)
    check("已排队补跑", len(b.scheduled) == 1, b.scheduled)
    check("排队来源为 preflight", b.scheduled and b.scheduled[0][3] == "preflight", b.scheduled)
    check("保留脚本选择", b.scheduled and b.scheduled[0][1] is None, b.scheduled)

    print("[2] 独立流程不查维护状态")
    calls = []
    runnable, deferred = run_split(
        [("XMFlow", None), ("QoderFlow", None)],
        {},
        calls=calls,
    )
    check("独立流程全部保留", runnable == [("XMFlow", None), ("QoderFlow", None)], runnable)
    check("没有 deferred", deferred == [], deferred)
    check("没有调用维护接口", calls == [], calls)

    print("[3] 维护接口异常时 fail-open")
    runnable, deferred = run_split(
        [("ZenlessZoneZero", None)],
        {"ZenlessZoneZero": state()},
        fail_keys={"ZenlessZoneZero"},
    )
    check("异常时按正常启动", runnable == [("ZenlessZoneZero", None)], runnable)
    check("不进入 deferred", deferred == [], deferred)


def make_backend_bridge(split_result):
    b = UiBridge.__new__(UiBridge)
    b._run_generation = 1
    b._email_pending = False
    b._email_sent_date = ""
    b._email_report = []
    b._email_warnings = []
    b._shutdown_pending = False
    b._shutdown_deferred = False
    b._shutdown_scheduled = False
    b.captured_targets = []
    b._split_maintenance_targets = lambda apps: split_result
    b._request_toast = lambda *a, **k: None
    b.emit_event = lambda *a, **k: None
    b.loadDailyProgress = lambda: None
    b._get_controller = lambda: object()
    b._handle_update_retry_result = lambda result: None
    b._unfired_scheduled_today = lambda: []
    b._pending_update_retries = lambda: []
    return b


def run_backend(b, targets):
    original = task_batch_runner.run_task_batch

    def fake_run(controller, target_apps, hooks=None, force_rerun=False):
        b.captured_targets.append(list(target_apps))
        return FakeResult()

    task_batch_runner.run_task_batch = fake_run
    try:
        b._run_automation_backend(targets, False, False, False)
    finally:
        task_batch_runner.run_task_batch = original


def test_backend_uses_runnable_targets():
    print("[4] 一条龙只收到 runnable 游戏")
    deferred = [{
        "app_key": "ZenlessZoneZero",
        "script_choice": None,
        "state": state(),
    }]
    b = make_backend_bridge(([("WutheringWaves", None)], deferred))
    run_backend(b, [
        {"app_key": "ZenlessZoneZero"},
        {"app_key": "WutheringWaves"},
    ])
    check("只把正常游戏交给批次", b.captured_targets == [[("WutheringWaves", None)]], b.captured_targets)

    print("[5] 全部维护中时传空列表")
    b2 = make_backend_bridge(([], deferred))
    run_backend(b2, [{"app_key": "ZenlessZoneZero"}])
    check("批次收到空列表", b2.captured_targets == [[]], b2.captured_targets)


def main():
    test_split_maintenance_targets()
    test_backend_uses_runnable_targets()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
