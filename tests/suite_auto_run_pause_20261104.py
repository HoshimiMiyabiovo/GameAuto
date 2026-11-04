"""2026-11-04 用户停止后仍自动跑任务的回归。

现象（19:37-19:44）：
  - 用户点停止 → 19:38「更新补跑」又自动启动原神；
  - 19:43 批次失败统一重试又启动一次；
  - 用户连点 12 次停止，看起来像"关不掉"。

修法：用户主动停止后置 `_auto_run_paused`，定时任务与更新补跑都不再触发，
直到用户下次手动点「开始」。

跑法：.venv\\Scripts\\python.exe tests\\suite_auto_run_pause_20261104.py
"""

import io
import sys
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def _fake_bridge():
    return SimpleNamespace(
        _auto_run_paused=False,
        _controller=SimpleNamespace(stop_current_task=False),
        _pending_scheduled=[{"id": "qoder", "apps": []}],
        _is_running=False,
        _toasts=[],
        _request_toast=lambda msg, color=None: None,
    )


def test_stop_pauses_auto_runs():
    print("[1] 停止后暂停定时任务/更新补跑")
    from game_auto.ui.bridge_views.automation import AutomationView
    from game_auto.ui.bridge_views.scheduler import SchedulerView

    b = _fake_bridge()
    AutomationView.stopAutomation(b)
    check("停止标志置 True", b._auto_run_paused is True)
    check("排队中的定时任务被清空", b._pending_scheduled == [], b._pending_scheduled)
    check("controller 收到停止请求", b._controller.stop_current_task is True)

    # 调度检查必须直接返回，不能再自动 startAutomation
    calls = []
    b.startAutomation = lambda *a, **k: calls.append((a, k))
    SchedulerView._check_update_retries(b)
    SchedulerView._check_scheduled_tasks(b)
    check("暂停后更新补跑不触发", not calls, calls)


def test_manual_start_clears_pause():
    print("[2] 用户手动点开始 → 解除暂停")
    from game_auto.ui.bridge_views.automation import AutomationView

    b = _fake_bridge()
    b._auto_run_paused = True
    b._run_lock = __import__("threading").Lock()
    b._run_generation = 0
    b.isRunningChanged = SimpleNamespace(emit=lambda v: None)
    b.clearLog = lambda: None
    b._shutdown_pending = False
    b._shutdown_deferred = False
    b._run_automation_backend = lambda *a, **k: None
    AutomationView.startAutomation(b, '[{"app_key":"GenshinImpact"}]')
    check("再次开始时解除暂停", b._auto_run_paused is False)


def test_bridge_default_flag():
    print("[3] Bridge 初始化带默认值")
    src = (PROJECT_ROOT / "src/game_auto/ui/bridge.py").read_text(encoding="utf-8")
    check("初始化 _auto_run_paused = False", "self._auto_run_paused = False" in src)
    aut = (PROJECT_ROOT / "src/game_auto/ui/bridge_views/automation.py").read_text(encoding="utf-8")
    check("startAutomation 清除暂停", "self._auto_run_paused = False" in aut)
    sch = (PROJECT_ROOT / "src/game_auto/ui/bridge_views/scheduler.py").read_text(encoding="utf-8")
    check("两个调度入口都检查暂停",
          sch.count('getattr(self, "_auto_run_paused", False)') >= 2,
          sch.count('getattr(self, "_auto_run_paused", False)'))


def test_execute_task_keeps_stop_flag():
    print("[4] execute_task 不再清掉停止标志 / 退出等后台收尾")
    ac = PROJECT_ROOT / "src/game_auto/runtime/automation_controller.py"
    src = ac.read_text(encoding="utf-8")
    check("execute_task 不再无条件重置停止标志",
          src.count("self.stop_current_task = False") == 1,
          src.count("self.stop_current_task = False"))
    check("启动前检查停止", "启动前收到停止请求" in src)
    check("拉脚本前检查停止", "拉脚本前收到停止请求" in src)

    gui = (PROJECT_ROOT / "src/game_auto/app/gui.py").read_text(encoding="utf-8")
    check("退出时等后台任务收尾", "后台任务 8 秒内未收尾" in gui)


def main():
    test_stop_pauses_auto_runs()
    test_manual_start_clears_pause()
    test_bridge_default_flag()
    test_execute_task_keeps_stop_flag()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())