"""2026-11-05 无限暖暖「商店又没跑」回归。

事故：08:32 NikkiLauncher 步骤2 打不开奇想日历（点右上角图标 + 按 L 3 次
全失败），`_run_flow_inner` 却返回 True「已记录，继续后续任务」→
`[NikkiLauncher] 流程执行成功`，实际晶石兑换 / 共鸣商店整段没执行；
task_meta 里本月两个 nikki 商店标记一条都没有。11-04 的「按 H 后未进入商城」
也是同一条按键链路。

根因：L / H / ESC 全走 `pyautogui.press` 的瞬时虚拟键事件，与绝区零 ESC
同一个坑；统一改带硬件扫描码的 `press_key_scancode`。

跑法：.venv\\Scripts\\python.exe tests\\suite_nikki_shop_scancode_20261105.py
"""

from __future__ import annotations

import ast
import io
import logging
import sys
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import game_auto.runtime.foreground_flow_runner as ff  # noqa: E402
import game_auto.runtime.nikki_calendar as nc  # noqa: E402
import game_auto.runtime.nikki_launcher as nl  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} :: {detail}")
        FAILURES.append(name)


def _source(module):
    return Path(module.__file__).read_text(encoding="utf-8")


def test_scancode_helper():
    print("[1] press_key_scancode 发带扫描码的按下/抬起")
    for key, vk in (("l", 76), ("h", 72), ("esc", 27), ("escape", 27), ("f3", 114)):
        events = []
        with mock.patch("win32api.keybd_event", side_effect=lambda *a: events.append(a)), \
                mock.patch("win32api.MapVirtualKey", return_value=1), \
                mock.patch.object(ff.time, "sleep", lambda *_: None):
            ff.press_key_scancode(key)
        ok = (len(events) == 2 and events[0][0] == vk and events[0][1] == 1
              and events[0][2] == 0 and bool(events[1][2] & 0x0002))
        check(f"{key} → vk={vk} 按下+抬起且带扫描码", ok, events)

    events = []
    with mock.patch("win32api.keybd_event", side_effect=lambda *a: events.append(a)), \
            mock.patch("win32api.MapVirtualKey", return_value=1), \
            mock.patch.object(ff.time, "sleep", side_effect=RuntimeError("boom")):
        try:
            ff.press_key_scancode("l")
        except RuntimeError:
            pass
    check("sleep 抛异常也 finally 松开键", len(events) == 2, events)

    with mock.patch("win32api.keybd_event"), mock.patch("win32api.MapVirtualKey", return_value=1):
        try:
            ff.press_key_scancode("ctrl+alt+delete")
            raised = False
        except ValueError:
            raised = True
    check("无法映射的键明确报错，不静默", raised)


def test_nikki_uses_helper():
    print("[2] 暖暖的 L / H / ESC 全部改走公共函数")
    calls = []
    with mock.patch.object(ff, "press_key_scancode", side_effect=lambda *a, **k: calls.append(a)):
        nc._press_key("l")
        nc._press_key("h")
        nl._press_esc()
    check("两处按键 + ESC 都调用 press_key_scancode",
          calls == [("l",), ("h",), ("esc",)], calls)
    for module, label in ((nc, "nikki_calendar"), (nl, "nikki_launcher")):
        # 只看真实调用，不算 docstring/注释里说明旧实现的字样
        press_calls = [
            n for n in ast.walk(ast.parse(_source(module)))
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "press"
        ]
        check(f"{label} 不再调用 pyautogui.press", press_calls == [], len(press_calls))

    nc_src = _source(nc)
    check("_open_calendar 仍通过 _focus_and_press 按 L",
          '_focus_and_press(hwnd, "l")' in nc_src)
    check("_open_shop 仍通过 _focus_and_press 按 H",
          '_focus_and_press(hwnd, "h")' in nc_src)
    check("_focus_and_press 保留修饰键释放（防 L/H 被当组合键）",
          "_release_modifier_keys()" in nc_src)


def test_calendar_failure_is_visible():
    print("[3] 日历打不开时不能冒充「商店跑完了」")
    src = _source(nc)
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_run_flow_inner")
    body = ast.get_source_segment(src, fn) or ""
    check("失败文案写明晶石兑换/共鸣商店未执行",
          "本月晶石兑换/共鸣商店未执行" in body, body[:200])
    # 函数里另一个分支（未识别主界面）用的是 logger.warning，只看日历失败这一段
    branch = body.split("if not _open_calendar", 1)[-1]
    check("日历失败分支用 logger.error 而不是 warning 盖过去",
          'logger.error("[无限暖暖·奇想日历] %s", message)' in branch
          and "奇想日历未打开（已记录，继续后续任务）" not in branch)
    check("保留 return True 容错（prelaunch 返回 False 会毁掉整个暖暖日常）",
          "return True, message" in body)
    check("旧的模糊文案已移除", "奇想日历未打开（已记录，继续后续任务）" not in body)


def test_monthly_meta_evidence():
    print("[4] 月度门控与今日取证")
    src = _source(nc)
    check("晶石兑换月度键", "monthly_sequence:nikki_crystal_exchange" in src)
    check("共鸣商店月度键", "monthly_sequence:nikki_resonance_shop" in src)
    out = PROJECT_ROOT / "data" / "artifacts"
    check("今天奇想日历失败截图在",
          (out / "flow_failures" / "20261105_083255_NikkiLauncher_奇想日历-打开.jpg").exists())


def main():
    test_scancode_helper()
    test_nikki_uses_helper()
    test_calendar_failure_is_visible()
    test_monthly_meta_evidence()


if __name__ == "__main__":
    main()
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILED: {FAILURES}")
        sys.exit(1)
    print("\nALL PASS")
