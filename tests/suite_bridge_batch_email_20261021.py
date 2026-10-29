"""批次串行 + 日报/关机合并回归测试（2026-10-21）。

背景：11:05 定时任务和更新补跑在同一 30s tick 内并发启动，共享的
email/shutdown 开关被后者覆盖，导致 11:06/11:27/11:32 连发 3 封日报，
绝区零还被两个 OneDragon 同时跑了多次。

纯 mock，不启动 GUI。用 UiBridge.__new__ 绕过 QApplication，断言：
  1. startAutomation 同步占位 → 第二次启动被拒
  2. email/shutdown 作为批次参数传入，不再读共享开关
  3. 有后续定时任务/补跑时，邮件和关机都推迟
  4. 全部结束后只发一封邮件（累积各批报告）且只调度一次关机
  5. 关机在发信结束之后才调度
  6. 当天已发过日报 → 后续批次不再重复发
  7. 旧批次的发信线程不会给新批次安排关机
  8. 已过点的定时任务不再阻塞收尾

跑法：.venv\\Scripts\\python.exe artifacts\\test_bridge_batch_email_20261021.py
"""

import json
import sys
import threading
import time

sys.path.insert(0, r"E:\GithubProject")

from game_auto.runtime import task_batch_runner  # noqa: E402
from game_auto.ui.bridge import UiBridge  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


class FakeSignal:
    def __init__(self):
        self.calls = []

    def emit(self, *args):
        self.calls.append(args)


def make_start_bridge():
    b = UiBridge.__new__(UiBridge)
    b._is_running = False
    b._run_lock = threading.Lock()
    b._run_generation = 0
    b._shutdown_pending = False
    b._shutdown_deferred = False
    b._toasts = []
    b._started = []
    b._request_toast = lambda msg, color=None: b._toasts.append(msg)
    b.clearLog = lambda: None
    b.isRunningChanged = FakeSignal()
    b._run_automation_backend = lambda *a, **k: b._started.append((a, k))
    return b


def test_single_batch_gate():
    print("[1] startAutomation 同步占位，同一时刻只允许一个批次")
    b = make_start_bridge()
    apps = json.dumps([{"app_key": "ZenlessZoneZero"}])
    b.startAutomation(apps, True, True, False)
    b.startAutomation(apps, True, True, False)
    deadline = time.time() + 1.0
    while len(b._started) < 1 and time.time() < deadline:
        time.sleep(0.01)
    check("只启动一个批次", len(b._started) == 1, b._started)
    check("第二次被拒并提示", any("已有任务在运行" in t for t in b._toasts), b._toasts)
    check("批次代数 +1", b._run_generation == 1, b._run_generation)
    args, _kwargs = b._started[0]
    check("email 作为参数传入", args[2] is True, args)
    check("shutdown 作为参数传入", args[3] is True, args)

    # 模拟 finished 事件把 UI 状态复位
    b._is_running = False
    b.startAutomation(apps, False, False, False)
    deadline = time.time() + 1.0
    while len(b._started) < 2 and time.time() < deadline:
        time.sleep(0.01)
    check("上一批结束后可再次启动", len(b._started) == 2, b._started)
    check("批次代数 +2", b._run_generation == 2, b._run_generation)


class FakeResult:
    def __init__(self, lines):
        self.report = list(lines)
        self.warnings = []
        self.app_key = "ZenlessZoneZero"
        self.state = "success"
        self.success = True
        self.message = ""


def make_backend_bridge():
    b = UiBridge.__new__(UiBridge)
    b._is_running = False
    b._run_lock = threading.Lock()
    b._run_generation = 1
    b._email_pending = False
    b._email_sent_date = ""
    b._email_report = []
    b._email_warnings = []
    b._shutdown_pending = False
    b._shutdown_deferred = False
    b._shutdown_scheduled = False
    b.pending_followups = []
    b.sent = []
    b.shutdowns = []
    b.events = []
    b._request_toast = lambda *a, **k: None
    b.emit_event = lambda *a, **k: None
    b.loadDailyProgress = lambda: None
    b._get_controller = lambda: object()
    b._handle_update_retry_result = lambda result: None
    b._unfired_scheduled_today = lambda: list(b.pending_followups)
    b._pending_update_retries = lambda: []

    def fake_send(automation_report, automation_warnings):
        b.events.append("email_start")
        time.sleep(0.05)
        b.sent.append((list(automation_report), list(automation_warnings)))
        b.events.append("email_end")
        return True, ""

    def fake_shutdown(delay=60):
        b.events.append("shutdown")
        b.shutdowns.append(delay)
        b._shutdown_scheduled = True
        return True

    b._send_daily_email_report = fake_send
    b._schedule_shutdown = fake_shutdown
    return b


def run_backend(b, lines, email, shutdown):
    fake = FakeResult(lines)
    original = task_batch_runner.run_task_batch

    def fake_run(controller, targets, hooks=None, force_rerun=False):
        return fake

    task_batch_runner.run_task_batch = fake_run
    try:
        b._run_automation_backend([{"app_key": "ZenlessZoneZero"}], False, email, shutdown)
    finally:
        task_batch_runner.run_task_batch = original


def wait_until(predicate, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_email_shutdown_consolidation():
    print("[2] 有后续任务时推迟，全部结束后只发一封邮件、只关一次机")
    b = make_backend_bridge()
    b.pending_followups = [{"id": "later-1135"}]
    run_backend(b, ["✅ 主批次完成"], True, True)
    check("有后续任务时不发信", b.sent == [], b.sent)
    check("有后续任务时不关机", b.shutdowns == [], b.shutdowns)
    check("邮件转入待发", b._email_pending is True)
    check("关机转入待执行", b._shutdown_pending is True)
    check("主批次报告已累积", b._email_report == ["✅ 主批次完成"], b._email_report)

    b.pending_followups = []
    run_backend(b, ["✅ 补跑批次完成"], False, True)
    check("全部结束后发出 1 封", wait_until(lambda: len(b.sent) == 1), b.sent)
    check(
        "邮件包含两个批次",
        b.sent and b.sent[0][0] == ["✅ 主批次完成", "✅ 补跑批次完成"],
        b.sent,
    )
    check("关机只调度 1 次", wait_until(lambda: len(b.shutdowns) == 1), b.shutdowns)
    check(
        "关机在发信结束之后",
        b.events == ["email_start", "email_end", "shutdown"],
        b.events,
    )
    check("记录当日已发日期", bool(b._email_sent_date), b._email_sent_date)

    run_backend(b, ["✅ 当天再跑一次"], True, True)
    time.sleep(0.15)
    check("当天不重复发日报", len(b.sent) == 1, b.sent)
    check("不重复调度关机", len(b.shutdowns) == 1, b.shutdowns)


def test_stale_email_thread_cannot_shutdown_new_batch():
    print("[3] 旧批次发信线程不会给新批次安排关机")
    b = make_backend_bridge()
    b._shutdown_pending = True
    b._run_generation = 2  # 新批次已启动
    scheduled = b._schedule_pending_shutdown(1)  # 旧批次持有 generation=1
    check("返回 False", scheduled is False)
    check("不调用 shutdown", b.shutdowns == [], b.shutdowns)
    check("待执行关机被清空", b._shutdown_pending is False)


def test_missed_scheduled_task_not_blocking():
    print("[4] 已过点的定时任务不再阻塞收尾")
    b = UiBridge.__new__(UiBridge)
    b._pending_scheduled = []
    now_hm = time.strftime("%H:%M")
    tasks = [
        {
            "id": "future",
            "enabled": True,
            "time": "23:59",
            "last_fired_date": "",
        }
    ]
    if now_hm != "00:00":
        tasks.append({
            "id": "past",
            "enabled": True,
            "time": "00:00",
            "last_fired_date": "",
        })
    b._load_scheduled_tasks = lambda: tasks
    unfired = b._unfired_scheduled_today()
    ids = {t.get("id") for t in unfired}
    check("未来的任务仍计数", "future" in ids, ids)
    if now_hm != "00:00":
        check("已过点的任务不计数", "past" not in ids, ids)


def test_cancel_shutdown_clears_pending():
    print("[5] cancelShutdown 清空待执行关机")
    b = UiBridge.__new__(UiBridge)
    b._shutdown_pending = True
    b._shutdown_deferred = True
    b._shutdown_scheduled = False
    b.cancelShutdown()
    check("待执行关机已清空", b._shutdown_pending is False)
    check("推迟标记已清空", b._shutdown_deferred is False)


def main():
    test_single_batch_gate()
    test_email_shutdown_consolidation()
    test_stale_email_thread_cannot_shutdown_new_batch()
    test_missed_scheduled_task_not_blocking()
    test_cancel_shutdown_clears_pending()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
