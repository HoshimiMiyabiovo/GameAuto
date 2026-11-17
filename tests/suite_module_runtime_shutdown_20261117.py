"""GUI 退出回收 ok-ww 常驻 module runner 回归（2026-11-16 退出漏杀事故）。

背景：ok-ww module runner 设计为常驻等待重连；GUI 退出钩子原来只置
stop_current_task 标志，空闲/已中断的 runner 会以无窗口后台 python 的形态
一直挂着（11-16 深夜实锤：GUI 22:53 退出，22:42 会话的 runner python 与
鸣潮启动器残留到次日）。修法：AutomationController.shutdown_module_runtime()
= 停当前会话 + 扫杀孤儿 runner；gui._cleanup_on_quit 显式调用。

节清单：
  [1] 行为：shutdown_module_runtime 调用会话停止与孤儿清理
  [2] 行为：清理路径抛异常被吞（quit 路径永不崩）
  [3] 行为：孤儿清理只杀 cmdline 命中 okww_module runner.py 的进程
      （跳过自身 / 当前会话 / 无关 python）
  [4] 接线静态断言：gui._cleanup_on_quit 调 shutdown_module_runtime；
      ci_suites 登记本套件

跑法：`.venv\\Scripts\\python.exe tests/suite_module_runtime_shutdown_20261117.py`
"""
from __future__ import annotations

import io as _io
import os
import sys
from pathlib import Path
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from game_auto.runtime import automation_controller as ac

FAILURES = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'} {label}" + ("" if ok else f"  {detail}"))
    if not ok:
        FAILURES.append(label)


def make_ctl():
    import logging

    ctl = ac.AutomationController.__new__(ac.AutomationController)
    ctl.logger = logging.getLogger("test.module_shutdown")
    ctl.stop_current_task = False
    ctl._module_session = None
    return ctl


class FakeProc:
    def __init__(self, pid, name, cmdline):
        self.pid = pid
        self.info = {"pid": pid, "name": name, "cmdline": cmdline}

    def __repr__(self):
        return f"FakeProc({self.pid})"


def test_shutdown_calls_both():
    print("[1] shutdown_module_runtime 调用会话停止与孤儿清理")
    ctl = make_ctl()
    calls = []
    with patch.object(ctl, "_stop_module_session", side_effect=lambda: calls.append("stop")), \
         patch.object(ctl, "_cleanup_stale_module_runners",
                      side_effect=lambda: calls.append("sweep")):
        ctl.shutdown_module_runtime()
    check("会话停止被调用", "stop" in calls, calls)
    check("孤儿清理被调用", "sweep" in calls, calls)
    check("调用顺序 stop → sweep", calls == ["stop", "sweep"], calls)


def test_shutdown_swallows_errors():
    print("[2] 清理路径抛异常被吞（quit 路径永不崩）")
    ctl = make_ctl()

    def boom():
        raise RuntimeError("boom")

    ok = True
    try:
        with patch.object(ctl, "_stop_module_session", side_effect=boom), \
             patch.object(ctl, "_cleanup_stale_module_runners", side_effect=boom):
            ctl.shutdown_module_runtime()
    except Exception as exc:  # noqa: BLE001
        ok = False
        print("  异常泄漏:", exc)
    check("两级异常都被吞，方法正常返回", ok)


def test_sweep_scopes():
    print("[3] 孤儿清理的作用域（只杀 okww_module runner.py）")
    ctl = make_ctl()

    class FakeSession:
        pid = 777  # 当前会话 runner：清理时必须跳过

    ctl._module_session = FakeSession()

    orphan = FakeProc(501, "python.exe",
                      [r"E:\...\python.exe", r"E:\...\tools\okww_module\runner.py", "--okww-root", "X"])
    current = FakeProc(777, "python.exe",
                       [r"E:\...\python.exe", r"E:\...\tools\okww_module\runner.py", "--okww-root", "Y"])
    unrelated = FakeProc(502, "python.exe", [r"C:\other\python.exe", "-m", "unrelated"])
    table = [orphan, current, unrelated,
             FakeProc(os.getpid(), "python.exe", [r"E:\...\tools\okww_module\runner.py"])]

    killed = []
    with patch.object(ac.psutil, "process_iter",
                      side_effect=lambda attrs=None: iter(table)), \
         patch.object(ac.ProcessSupervisor, "safe_kill_process",
                      side_effect=lambda pid: killed.append(pid)):
        ctl._cleanup_stale_module_runners()
    check("孤儿 runner 被杀", 501 in killed, killed)
    check("当前会话 runner 跳过", 777 not in killed, killed)
    check("无关 python 不误杀", 502 not in killed, killed)
    check("自身 PID 永不自杀", os.getpid() not in killed, killed)


def test_wiring():
    print("[4] 接线静态断言")
    gui_src = (PROJECT / "src/game_auto/app/gui.py").read_text(encoding="utf-8")
    check("gui._cleanup_on_quit 调 shutdown_module_runtime",
          "shutdown_module_runtime" in gui_src)
    ctrl_src = (PROJECT / "src/game_auto/runtime/automation_controller.py").read_text(encoding="utf-8")
    check("controller 有 shutdown_module_runtime 定义",
          "def shutdown_module_runtime" in ctrl_src)
    ci = (PROJECT / "tests/ci_suites.txt").read_text(encoding="utf-8")
    check("ci_suites.txt 登记本套件", "suite_module_runtime_shutdown_20261117.py" in ci)


def main():
    test_shutdown_calls_both()
    test_shutdown_swallows_errors()
    test_sweep_scopes()
    test_wiring()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED {len(FAILURES)}: {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
