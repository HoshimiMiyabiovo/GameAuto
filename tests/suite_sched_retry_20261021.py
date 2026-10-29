"""定时任务“仅补跑今日未完成”回归测试（2026-10-21）。

纯 mock，不启动 GUI。用 UiBridge.__new__ 绕过 QApplication，断言：
  1. skip_if_done_today=True → startAutomation(force_rerun=False)
  2. 缺省/False → 保持旧行为 force_rerun=True
  3. 到点时已有任务在跑 → 进入 _pending_scheduled，不启动
  4. 普通模式（skip=False）的 toast 不带“已完成会跳过”

跑法：.venv\\Scripts\\python.exe artifacts\\test_sched_retry_20261021.py
"""

import json
import sys

sys.path.insert(0, r"E:\GithubProject")

from game_auto.ui.bridge import UiBridge  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def make_bridge(running=False):
    b = UiBridge.__new__(UiBridge)
    b._is_running = running
    b._pending_scheduled = []
    b._toasts = []
    b._request_toast = lambda msg, color=None: b._toasts.append(msg)
    b.loadPostRunPrefs = lambda: json.dumps({"email": False, "shutdown": False})
    b._calls = []
    b.startAutomation = lambda *a, **k: b._calls.append((a, k))
    return b


def task(skip=None, time_str="11:05"):
    t = {
        "id": "t1",
        "name": "绝区零更新日补跑",
        "time": time_str,
        "apps": [{"app_key": "ZenlessZoneZero", "script_choice": None}],
    }
    if skip is not None:
        t["skip_if_done_today"] = skip
    return t


def test_skip_if_done():
    print("[1] skip_if_done_today=True → force_rerun=False")
    b = make_bridge()
    b._fire_scheduled_task(task(True))
    check("startAutomation 调用一次", len(b._calls) == 1, b._calls)
    check("force_rerun=False", b._calls[0][1].get("force_rerun") is False, b._calls)
    check("toast 提示会跳过已完成", any("跳过" in t for t in b._toasts), b._toasts)


def test_normal_force_rerun():
    print("[2] 缺省/False → 保持 force_rerun=True")
    b = make_bridge()
    b._fire_scheduled_task(task(None))
    check("缺省 force_rerun=True", b._calls[0][1].get("force_rerun") is True, b._calls)
    b2 = make_bridge()
    b2._fire_scheduled_task(task(False))
    check("False force_rerun=True", b2._calls[0][1].get("force_rerun") is True, b2._calls)


def test_running_queues():
    print("[3] 到点时已有任务在跑 → 排队，不启动")
    b = make_bridge(running=True)
    b._fire_scheduled_task(task(True))
    check("没有启动", b._calls == [], b._calls)
    check("进入排队", len(b._pending_scheduled) == 1, b._pending_scheduled)
    check("排队任务带 skip 标记", b._pending_scheduled[0].get("skip_if_done_today") is True)


def test_task_level_email_override():
    print("[4] 任务级 email_on_finish=False 覆盖全局")
    b = make_bridge()
    b.loadPostRunPrefs = lambda: json.dumps({"email": True, "shutdown": True})
    b._fire_scheduled_task({
        "id": "q",
        "name": "Qoder 10:01",
        "time": "10:01",
        "apps": [{"app_key": "QoderFlow"}],
        "email_on_finish": False,
    })
    args, _kwargs = b._calls[0]
    check("email 被覆盖为 False", args[2] is False, args)
    check("shutdown 仍跟随全局 True", args[1] is True, args)


def main():
    test_skip_if_done()
    test_normal_force_rerun()
    test_running_queues()
    test_task_level_email_override()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
