# -*- coding: utf-8 -*-
"""前台点击防护回归（2026-11-17）。

背景：当天 10:28 薅羊毛批次里 TELEAGENT / ASTUDIO 报「目标窗口被遮挡约 100%」，
失败截图取证发现全屏 CrystalDiskMark 一直压在最上层。自动化按屏幕坐标发点击，
而 `_focus_window` 的 SetForegroundWindow 可能被前台锁/置顶窗拒绝；旧实现不校验
结果，点击仍会落到测速软件上。本套件锁新增的点击硬闸：

[1] `_foreground_click_block_reason`：目标不在前台 / 点击点归属别的窗口 → 给出原因；
[2] `_click_window_local`：被拦时**一个输入都不发**并返回 False，正常时点击一次；
[3] `_hover_window_local`：同闸，被拦时不移动鼠标。

跑法：.venv\\Scripts\\python.exe -m pytest tests\\suite_click_guard_20261117.py -q
"""

import io
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from unittest.mock import patch  # noqa: E402

import game_auto.runtime.foreground_flow_runner as ffr  # noqa: E402

FAILURES = []

TARGET = 100
MODAL = 101
CDM = 900


class FakeWin32Process:
    def __init__(self, pids):
        self.pids = pids

    def GetWindowThreadProcessId(self, hwnd):
        return 0, self.pids.get(hwnd, 0)


def check(label, ok, detail=None):
    mark = "PASS" if ok else "FAIL"
    print(f"  {mark} {label}" + (f"  ← {detail}" if not ok and detail is not None else ""))
    if not ok:
        FAILURES.append(label)


class FakeWin32Gui:
    def __init__(self, foreground, point_owner, rect=(0, 0, 1000, 800)):
        self.foreground = foreground
        self.point_owner = point_owner
        self.rect = rect

    def GetForegroundWindow(self):
        return self.foreground

    def WindowFromPoint(self, _point):
        return self.point_owner

    def GetAncestor(self, hwnd, _flags):
        return hwnd

    def GetWindowText(self, hwnd):
        return {TARGET: "TeleAgent", CDM: "CrystalDiskMark"}.get(hwnd, "")

    def GetWindowRect(self, _hwnd):
        return self.rect


def test_block_reason():
    print("[1] _foreground_click_block_reason")
    cases = [
        ("目标在前台且点击点属于目标", FakeWin32Gui(TARGET, TARGET), ""),
        ("目标进程的模态弹窗也放行", FakeWin32Gui(MODAL, MODAL), ""),
        ("置顶测速软件抢走前台", FakeWin32Gui(CDM, CDM),
         ("不在前台", "crystaldiskmark")),
        ("前台是目标但点击点被别的窗口占住", FakeWin32Gui(TARGET, CDM),
         ("其他窗口", "crystaldiskmark")),
    ]
    pids = FakeWin32Process({TARGET: 500, MODAL: 500, CDM: 600})
    for label, fake, expected in cases:
        with patch.object(ffr, "win32gui", fake), patch.object(ffr, "win32process", pids):
            reason = ffr._foreground_click_block_reason(TARGET, 100, 200)
        if not expected:
            check(label, reason == "", reason)
        else:
            check(label, all(word in reason for word in expected), reason)


def test_click_refused():
    print("[2] _click_window_local 被拦时不发输入")
    fake = FakeWin32Gui(CDM, CDM)
    clicks = []
    with patch.object(ffr, "win32gui", fake), \
            patch.object(ffr, "win32process", FakeWin32Process({TARGET: 500, CDM: 600})), \
            patch.object(ffr, "_focus_window", lambda _h: None), \
            patch.object(ffr, "_pyautogui_click_guarded",
                         lambda *a, **kw: clicks.append((a, kw))):
        ok = ffr._click_window_local(TARGET, 109, 770)
    check("被拦返回 False", ok is False, ok)
    check("被拦时零点击", clicks == [], clicks)

    fake = FakeWin32Gui(TARGET, TARGET)
    clicks = []
    with patch.object(ffr, "win32gui", fake), \
            patch.object(ffr, "win32process", FakeWin32Process({TARGET: 500, CDM: 600})), \
            patch.object(ffr, "_focus_window", lambda _h: None), \
            patch.object(ffr, "_pyautogui_click_guarded",
                         lambda *a, **kw: clicks.append((a, kw))):
        ok = ffr._click_window_local(TARGET, 109, 770)
    check("目标前台时返回 True", ok is True, ok)
    check("目标前台时点击一次且坐标=窗口原点+局部坐标",
          len(clicks) == 1 and clicks[0][0][:2] == (109, 770), clicks)


def test_hover_refused():
    print("[3] _hover_window_local 被拦时不移动鼠标")
    moves = []
    with patch.object(ffr, "win32gui", FakeWin32Gui(CDM, CDM)), \
            patch.object(ffr, "win32process", FakeWin32Process({TARGET: 500, CDM: 600})), \
            patch.object(ffr, "_focus_window", lambda _h: None), \
            patch.object(ffr.pyautogui, "moveTo",
                         lambda *a, **kw: moves.append((a, kw))):
        ok = ffr._hover_window_local(TARGET, 10, 20)
    check("被拦返回 False", ok is False, ok)
    check("被拦时零移动", moves == [], moves)

    with patch.object(ffr, "win32gui", FakeWin32Gui(TARGET, TARGET)), \
            patch.object(ffr, "win32process", FakeWin32Process({TARGET: 500, CDM: 600})), \
            patch.object(ffr, "_focus_window", lambda _h: None), \
            patch.object(ffr.pyautogui, "moveTo",
                         lambda *a, **kw: moves.append((a, kw))):
        ok = ffr._hover_window_local(TARGET, 10, 20)
    check("目标前台时返回 True", ok is True, ok)
    check("目标前台时移动一次", len(moves) == 1, moves)


def main():
    test_block_reason()
    test_click_refused()
    test_hover_refused()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED {len(FAILURES)}: {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
