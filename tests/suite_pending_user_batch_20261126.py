"""用户批次排队 + 补跑启动宽限回归（2026-11-26 实跑事故）。

事故：用户点「开始」时正好撞上异环更新补跑（单 app 批次、动辄 20 分钟），
旧实现只弹一句「已有任务在运行中」就把请求丢了；用户看到队列里永远只有
异环，以为「补跑怎么又只有异环」。另外补跑在程序启动 30s 内就抢跑，
重启程序必先跑一轮异环，用户的批次更排不上队。

本套件锁：
  1. 忙时点开始 → 请求入队（不丢），只保留最新一次；
  2. 结批后 drain 排队批次并优先于排队的定时任务；
  3. 启动宽限期内补跑不自动开跑，宽限期后才按到点执行。

跑法：.venv\\Scripts\\python.exe tests\\suite_pending_user_batch_20261126.py
"""

import sys
import time
import json
import threading
from datetime import datetime, timedelta

sys.path.insert(0, r"E:\GithubProject\game_auto\src")

from game_auto.ui.bridge_views import automation as av  # noqa: E402
from game_auto.ui.bridge_views import scheduler as sv  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


class FakeLogger:
    def __init__(self):
        self.lines = []

    def _add(self, msg, *a):
        text = str(msg)
        if a:
            try:
                text = text % a
            except Exception:
                text = f"{text} {a}"
        self.lines.append(text)

    def info(self, msg, *a, **k):
        self._add(msg, *a)

    def warning(self, msg, *a, **k):
        self._add(msg, *a)

    def error(self, msg, *a, **k):
        self._add(msg, *a)

    def debug(self, msg, *a, **k):
        self._add(msg, *a)


class Patch:
    def __init__(self, target, **kwargs):
        self.target = target
        self.kwargs = kwargs
        self.old = {}

    def __enter__(self):
        for key, value in self.kwargs.items():
            self.old[key] = getattr(self.target, key)
            setattr(self.target, key, value)
        return self

    def __exit__(self, *exc):
        for key, value in self.old.items():
            setattr(self.target, key, value)
        return False


def make_queue_host():
    host = av.AutomationView.__new__(av.AutomationView)
    host.logger = FakeLogger()
    host.toasts = []
    host._request_toast = lambda msg, color="": host.toasts.append((msg, color))
    host._pending_user_batch = None
    return host


def test_busy_start_is_queued_not_dropped():
    print("[1] 忙时点开始：请求入队，不再只弹一句就丢掉")
    host = make_queue_host()
    host._run_lock = threading.Lock()
    host._is_running = True
    apps = '[{"app_key":"BaiJing","script_choice":""}]'
    started = []
    with Patch(av.AutomationView, _queue_user_batch=lambda self, *a: started.append(a)):
        av.AutomationView.startAutomation(host, apps, False, False, False)
    check("进了排队分支", len(started) == 1, started)
    check("带上用户勾选", started and started[0][0] == apps, started)


def test_queue_keeps_latest_and_clears_on_take():
    print("[2] 排队只保留最新一次点击；取出即清空")
    host = make_queue_host()
    module_log = FakeLogger()
    with Patch(av, logger=module_log):
        av.AutomationView._queue_user_batch(host, '[{"app_key":"A"}]', False, False, False)
        av.AutomationView._queue_user_batch(host, '[{"app_key":"B"}]', True, False, True)
    pending = av.AutomationView._take_pending_user_batch(host)
    check("以最新勾选为准", pending and pending["apps"] == '[{"app_key":"B"}]', pending)
    check("关机/强制重跑标记透传",
          pending and pending["shutdown"] is True and pending["force_rerun"] is True, pending)
    check("取出后清空", av.AutomationView._take_pending_user_batch(host) is None)
    check("排队时给用户 toast 说明", any("已排队" in m for m, _ in host.toasts), host.toasts)
    check("排队写日志（事后可查）", any("已排队" in line for line in module_log.lines), module_log.lines)


def test_drain_after_batch():
    print("[3] 结批后自动启动排队的用户批次，且优先于定时任务")
    host = make_queue_host()
    calls = []
    host.startAutomation = lambda *a: calls.append(a)
    host._pending_user_batch = {
        "apps": '[{"app_key":"WoolFlow"}]', "shutdown": False, "email": True, "force_rerun": True,
    }
    drained = av.AutomationView._drain_pending_user_batch(host)
    check("drain 返回 True（调用方跳过定时任务）", drained is True, drained)
    check("按排队参数启动", calls and calls[0][0] == '[{"app_key":"WoolFlow"}]', calls)
    check("标记透传", len(calls[0]) == 4 and calls[0][2] is True and calls[0][3] is True, calls)
    check("没有排队时返回 False", av.AutomationView._drain_pending_user_batch(host) is False)


def make_sched_host(due_entry):
    host = sv.SchedulerView.__new__(sv.SchedulerView)
    host.logger = FakeLogger()
    host._auto_run_paused = False
    host._is_running = False
    host._update_retry_lock = threading.Lock()
    host._entries = [due_entry]
    host.started = []
    host._load_update_retries = lambda: list(host._entries)
    host._save_update_retries = lambda items: host._entries.__setitem__(slice(None), items)
    host.loadPostRunPrefs = lambda: "{}"
    host.startAutomation = lambda *a, **k: host.started.append(a)
    return host


def test_startup_grace_blocks_early_retry():
    print("[4] 程序启动宽限：刚起来不抢跑更新补跑")
    now = datetime.now(sv.CN_TZ)
    entry = {
        "app_key": "NevernessToEverness",
        "script_choice": None,
        "date": now.strftime("%Y-%m-%d"),
        "attempt": 0,
        "max_attempts": 3,
        "due_at": (now - timedelta(minutes=1)).isoformat(timespec="seconds"),
    }
    host = make_sched_host(entry)
    with Patch(sv, _BOOT_MONOTONIC=time.monotonic()):
        sv.SchedulerView._check_update_retries(host)
    check("宽限期内不启动补跑", host.started == [], host.started)
    check("attempt 没被提前消耗", host._entries[0]["attempt"] == 0, host._entries[0])

    host = make_sched_host(entry)
    with Patch(sv, _BOOT_MONOTONIC=time.monotonic() - sv.UPDATE_RETRY_STARTUP_GRACE_SEC - 1):
        sv.SchedulerView._check_update_retries(host)
    check("过宽限后按到点执行", len(host.started) == 1, host.started)
    check("只带该游戏", "NevernessToEverness" in host.started[0][0], host.started)
    check("attempt 递增", host._entries[0]["attempt"] == 1, host._entries[0])


def test_bridge_drains_user_batch_before_scheduled():
    print("[5] 接线锚点：finished 处理里用户批次先于排队的定时任务")
    try:
        from game_auto.ui import bridge as br
        src = __import__("inspect").getsource(br.UiBridge._handle_ui_event)
    except Exception as exc:  # 无 Qt 环境时跳过接线检查
        print(f"  SKIP 无法导入 Bridge: {exc}")
        return
    check("finished 里先 drain 用户批次", "_drain_pending_user_batch()" in src)
    check("drain 成功即 return（不再跑定时任务）",
          src.find("_drain_pending_user_batch()") < src.find("if self._pending_scheduled:"), src[-600:])


if __name__ == "__main__":
    test_busy_start_is_queued_not_dropped()
    test_queue_keeps_latest_and_clears_on_take()
    test_drain_after_batch()
    test_startup_grace_blocks_early_retry()
    test_bridge_drains_user_batch_before_scheduled()
    if FAILURES:
        print(f"\nFAILED {len(FAILURES)}: {FAILURES}")
        sys.exit(1)
    print("\nALL PASS")
