# -*- coding: utf-8 -*-
"""绝区零「点击进入游戏」按住式点击回归（2026-11-04 提权实机取证）。

用户当场开着游戏，两次实验：
  普通权限：UIPI 静默拦截，前台 guarded / 前台按住 / 后台 PostMessage 全无效；
  管理员权限：瞬时 `pyautogui.click` **仍然点不动**，`mouseDown` 按住 80ms
             再 `mouseUp` 立刻出现「登录成功」+ UID。
结论：权限对齐（UIPI）与按住，两个条件缺一不可。

覆盖：
  [1] `_pyautogui_click_held` 的 down → sleep → up 顺序与多击
  [2] fail-safe 仍收口成 FailSafeAborted
  [3] `_click_window_local(hold_sec>0)` 走按住式，坐标换算正确
  [4] 不传 hold_sec 保持旧行为；后台 PostMessage 忽略按住
  [5] 两份 flow_configs 的两个绝区零流程第 1 步配置正确
  [6] run_flow 接线静态断言
  [7] 本机提权实机结果文件（存在才断言）

跑法：.venv\\Scripts\\python.exe tests\\suite_zzz_click_hold_20261104.py
"""

import json
import sys
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import game_auto.runtime.foreground_flow_runner as ff  # noqa: E402

FAILURES = []
RESULT_FILE = PROJECT_ROOT / "data" / "artifacts" / "zzz_click_probe_admin_result.json"


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name}  {detail}")
        FAILURES.append(name)


def test_held_click_order():
    print("[1] 按住式点击：down → 等待 → up")
    calls = []
    with mock.patch.object(ff.pyautogui, "moveTo", lambda *a, **k: calls.append(("move", a))), \
            mock.patch.object(ff.pyautogui, "mouseDown", lambda *a, **k: calls.append(("down", k))), \
            mock.patch.object(ff.pyautogui, "mouseUp", lambda *a, **k: calls.append(("up", k))), \
            mock.patch.object(ff.time, "sleep", lambda s: calls.append(("sleep", s))):
        ff._pyautogui_click_held(100, 200, 0.08)
    check("顺序是 move/down/sleep/up", [c[0] for c in calls] == ["move", "down", "sleep", "up"], calls)
    check("按住时长 0.08s", any(c[0] == "sleep" and abs(c[1] - 0.08) < 1e-9 for c in calls), calls)
    check("鼠标先移到 (100,200)", calls and calls[0][1] == (100, 200), calls)

    calls.clear()
    with mock.patch.object(ff.pyautogui, "moveTo", lambda *a, **k: None), \
            mock.patch.object(ff.pyautogui, "mouseDown", lambda *a, **k: calls.append("down")), \
            mock.patch.object(ff.pyautogui, "mouseUp", lambda *a, **k: calls.append("up")), \
            mock.patch.object(ff.time, "sleep", lambda s: None):
        ff._pyautogui_click_held(1, 2, 0.05, clicks=2)
    check("clicks=2 时按下/抬起各两次",
          calls.count("down") == 2 and calls.count("up") == 2, calls)


def test_failsafe_still_guarded():
    print("[2] fail-safe 依旧收口")

    def _boom(*a, **k):
        raise ff.pyautogui.FailSafeException("failsafe")

    raised = False
    with mock.patch.object(ff.pyautogui, "moveTo", lambda *a, **k: None), \
            mock.patch.object(ff.pyautogui, "mouseDown", _boom):
        try:
            ff._pyautogui_click_held(1, 2, 0.08)
        except ff.FailSafeAborted:
            raised = True
    check("抛 FailSafeAborted 而不是裸异常", raised)


def test_click_window_local_dispatch():
    print("[3][4] _click_window_local 把 hold_sec 透传给统一收口")
    calls = []
    patches = (
        mock.patch.object(ff, "_window_rect", return_value=(10, 20, 800, 600)),
        mock.patch.object(ff, "_release_modifier_keys", lambda: None),
        mock.patch.object(ff, "_focus_window", lambda *a, **k: None),
        mock.patch.object(ff, "_post_click_window", return_value=True),
        mock.patch.object(ff, "_pyautogui_click_guarded",
                          lambda *a, **k: calls.append((a, k))),
    )
    from contextlib import ExitStack
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        ff._click_window_local(1, 100, 200, hold_sec=0.08)
        check("窗口局部坐标换算成屏幕 (110,220)",
              bool(calls) and tuple(calls[0][0][:2]) == (110, 220), calls)
        check("透传 hold_sec=0.08",
              bool(calls) and abs(calls[0][1].get("hold_sec", 0) - 0.08) < 1e-9, calls)

        calls.clear()
        ff._click_window_local(1, 100, 200)
        check("不传 hold_sec → None（用全局默认按住）",
              bool(calls) and calls[0][1].get("hold_sec", "missing") is None, calls)

        calls.clear()
        ff._click_window_local(1, 100, 200, hold_sec=0)
        check("显式 0 → 瞬时逃生口",
              bool(calls) and calls[0][1].get("hold_sec") == 0, calls)

        calls.clear()
        ff._click_window_local(1, 100, 200, background=True, hold_sec=0.08)
        check("后台 PostMessage 成功时不走前台点击", not calls, calls)


def test_configs():
    print("[5] 两个绝区零流程的进入游戏步骤")
    for name in ("flow_configs.json", "flow_configs.example.json"):
        cfg = json.loads((PROJECT_ROOT / name).read_text(encoding="utf-8"))
        for flow in ("ZZZ_MONTHLY", "ZZZ_NOTORIOUS_HUNT"):
            step = cfg[flow]["steps"][0]
            check(f"{name}/{flow} 前台点击", step.get("background_click") is False, step)
            check(f"{name}/{flow} 关掉 click_fallback", step.get("click_fallback") is False, step)
            check(f"{name}/{flow} click_hold_sec=0.08",
                  abs(float(step.get("click_hold_sec", 0)) - 0.08) < 1e-9, step)
            check(f"{name}/{flow} 仍是消失型确认",
                  step.get("confirm_type") == "disappear", step)


def test_run_flow_wiring():
    print("[6] run_flow 接线")
    src = Path(ff.__file__).read_text(encoding="utf-8")
    check("未配置 click_hold_sec 时为 None（用全局默认）",
          'cfg.get("click_hold_sec", None)' in src)
    check("传给 _click_window_local", "hold_sec=effective_hold" in src)
    check("screen 分支也走统一收口",
          "_pyautogui_click_guarded(center[0], center[1], clicks=clicks," in src)
    check("后台点击不误用按住",
          "effective_hold = None if use_background else hold_sec" in src)


def test_global_default_hold():
    print("[8] 全局默认：所有前台点击都带极短按住，显式 0 才瞬时")
    held, instant = [], []
    with mock.patch.object(ff, "_pyautogui_click_held",
                           lambda x, y, h, *a, **k: held.append((x, y, h))), \
            mock.patch.object(ff.pyautogui, "click",
                              lambda *a, **k: instant.append((a, k))):
        ff._pyautogui_click_guarded(10, 20)
        ff._pyautogui_click_guarded(10, 20, hold_sec=0)
        ff._pyautogui_click_guarded(10, 20, hold_sec=0.08)
    check("不传 hold_sec → 默认按住",
          bool(held) and abs(held[0][2] - ff.DEFAULT_CLICK_HOLD_SEC) < 1e-9, held)
    check("显式 0 → 瞬时 pyautogui.click", len(instant) == 1, instant)
    check("显式 0.08 → 按住 0.08",
          len(held) == 2 and abs(held[1][2] - 0.08) < 1e-9, held)
    check("默认时长是极短的 0.06s",
          abs(ff.DEFAULT_CLICK_HOLD_SEC - 0.06) < 1e-9, ff.DEFAULT_CLICK_HOLD_SEC)


def test_no_bare_click_left():
    print("[9] src 里不再有裸 pyautogui.click")
    hits = []
    for path in (PROJECT_ROOT / "src").rglob("*.py"):
        for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "pyautogui.click(" in line:
                hits.append((path.name, no, line.strip()[:80]))
    check("只剩统一收口里的瞬时逃生口",
          len(hits) == 1 and hits[0][0] == "foreground_flow_runner.py", hits)


def test_real_probe_evidence():
    print("[7] 本机提权实机结果")
    if not RESULT_FILE.is_file():
        print("  SKIP 没有本机实机结果文件")
        return
    data = json.loads(RESULT_FILE.read_text(encoding="utf-8"))
    check("诊断进程确实提权", data.get("elevated_self") is True, data.get("elevated_self"))
    check("UIPI 护栏不再报错", data.get("uipi_reason") == "", data.get("uipi_reason"))
    steps = data.get("steps") or []
    check("瞬时 guarded 点击仍失败",
          bool(steps) and steps[0].get("method") == "guarded"
          and steps[0].get("button_still_there") is True, steps)
    check("按住式点击按钮消失",
          len(steps) >= 2 and steps[1].get("method") == "held"
          and steps[1].get("button_still_there") is False, steps)
    check("结论是点进去了", "点进去了" in (data.get("conclusion") or ""), data.get("conclusion"))


def main():
    test_held_click_order()
    test_failsafe_still_guarded()
    test_click_window_local_dispatch()
    test_configs()
    test_run_flow_wiring()
    test_global_default_hold()
    test_no_bare_click_left()
    test_real_probe_evidence()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
