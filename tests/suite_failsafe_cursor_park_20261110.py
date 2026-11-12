# -*- coding: utf-8 -*-
"""启动前清理遗留角落鼠标回归（2026-11-10 实跑事故）。

whimbox 每次页面切换后把鼠标停到 (0,0) 避悬停（whimbox/ui/ui.py:185）；
12:14 暖暖超时强杀 whimbox 后遗留光标被 pyautogui 当成用户紧急停止，
12:15 Qoder 每日积分 / 12:16 ZZZ 后勤月商店补跑两个前台流程首个点击连续中止。
机制：run_flow / execute_task 启动边界检测鼠标精确在 (0,0) 时用 win32
SetCursorPos 挪去屏幕中心并记一行日志；流程进行中的 fail-safe 不变，
人类甩角落急停照样生效。

覆盖：
  [1] 遗留 (0,0) → 挪去屏幕中心 + 日志一行
  [2] 不在 (0,0) → 不动鼠标
  [3] 清理函数不走 pyautogui（静态）
  [4] run_flow / execute_task 两入口接线且先于启动链（静态）
  [5] 流程内急停仍收口成 FailSafeAborted（没被顺手关掉）

跑法：.venv\\Scripts\\python.exe tests\\suite_failsafe_cursor_park_20261110.py
"""

import ast
import inspect
import sys
import textwrap
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import game_auto.runtime.foreground_flow_runner as ff  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name}  {detail}")
        FAILURES.append(name)


def _stub_win32(pos):
    moved = []
    stub = mock.Mock()
    stub.GetCursorPos = lambda: pos
    stub.GetSystemMetrics = lambda i: 1920 if i == 0 else 1080
    stub.SetCursorPos = lambda p: moved.append(p)
    return stub, moved


def test_parked_cursor_moved():
    print("[1] 遗留 (0,0) 挪去屏幕中心并记日志")
    stub, moved = _stub_win32((0, 0))
    logger = mock.Mock()
    with mock.patch.object(ff, "win32api", stub):
        ff._clear_parked_failsafe_cursor(logger)
    check("SetCursorPos 到屏幕中心", moved == [(960, 540)], moved)
    check("记一行含 (0,0) 的日志", logger.info.called and "(0,0)" in str(logger.info.call_args), logger.info.call_args)


def test_normal_cursor_untouched():
    print("[2] 不在 (0,0) 不动鼠标")
    for pos in ((960, 540), (1, 1), (0, 1)):
        stub, moved = _stub_win32(pos)
        logger = mock.Mock()
        with mock.patch.object(ff, "win32api", stub):
            ff._clear_parked_failsafe_cursor(logger)
        check(f"{pos} 不挪不记日志", not moved and not logger.info.called, (pos, moved))


def test_helper_not_pyautogui():
    print("[3] 清理函数必须 win32 直挪，不走 pyautogui")
    tree = ast.parse(textwrap.dedent(inspect.getsource(ff._clear_parked_failsafe_cursor)))
    bare = [n for n in ast.walk(tree) if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "pyautogui"]
    check("函数体无 pyautogui 调用", not bare, bare)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "SetCursorPos"]
    check("用 win32api.SetCursorPos", bool(calls))


def _func_node(source, name):
    tree = ast.parse(source)
    return next(n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)


def _call_lines(node, pred):
    return [n.lineno for n in ast.walk(node) if isinstance(n, ast.Call) and pred(n.func)]


def test_entry_wiring():
    print("[4] 两入口接线：启动边界先清理再走启动链")
    ff_src = inspect.getsource(ff.run_flow)
    run_node = _func_node(ff_src, "run_flow")
    clear = _call_lines(run_node, lambda f: isinstance(f, ast.Name) and f.id == "_clear_parked_failsafe_cursor")
    load_cfg = _call_lines(run_node, lambda f: isinstance(f, ast.Name) and f.id == "_load_flow_config")
    check("run_flow 调用清理", bool(clear), clear)
    check("run_flow 清理先于配置加载/启动", bool(clear) and bool(load_cfg) and min(clear) < min(load_cfg), (clear, load_cfg))

    ac_src = (PROJECT_ROOT / "src" / "game_auto" / "runtime" / "automation_controller.py").read_text(encoding="utf-8")
    exec_node = _func_node(ac_src, "execute_task")
    clear_ac = _call_lines(exec_node, lambda f: isinstance(f, ast.Name) and f.id == "_clear_parked_failsafe_cursor")
    prelaunch = _call_lines(exec_node, lambda f: isinstance(f, ast.Attribute) and f.attr == "_run_prelaunch_flow")
    check("execute_task 调用清理", bool(clear_ac), clear_ac)
    check("execute_task 清理先于前置启动", bool(clear_ac) and bool(prelaunch) and min(clear_ac) < min(prelaunch), (clear_ac, prelaunch))


def test_failsafe_still_live():
    print("[5] 流程内急停保护未被关掉")

    def _boom(*a, **k):
        raise ff.pyautogui.FailSafeException("failsafe")

    raised = False
    with mock.patch.object(ff.pyautogui, "moveTo", _boom):
        try:
            ff._pyautogui_click_held(1, 2, 0.08)
        except ff.FailSafeAborted:
            raised = True
    check("按住式点击仍抛 FailSafeAborted", raised)

    raised = False
    with mock.patch.object(ff.pyautogui, "click", _boom):
        try:
            ff._pyautogui_click_guarded(1, 2, hold_sec=0)
        except ff.FailSafeAborted:
            raised = True
    check("瞬时点击仍抛 FailSafeAborted", raised)


def main():
    test_parked_cursor_moved()
    test_normal_cursor_untouched()
    test_helper_not_pyautogui()
    test_entry_wiring()
    test_failsafe_still_live()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())