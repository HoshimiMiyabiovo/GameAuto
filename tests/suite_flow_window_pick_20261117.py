# -*- coding: utf-8 -*-
"""流程窗口选择回归（2026-11-17）。

背景：薅羊毛集合里 QODER 步骤 1「打开权益中心」连失败两轮。实锤证据是日志里的
搜索区域 ``region=(1768, 928, 51, 79)`` —— 选中的窗口是 **Qoder Desktop Pet**
（右下角 51×79 的置顶小窗），不是 Qoder 主窗口；在那个小区域里找
``qoder-权益中心.png``，模板图比整个区域还大，报
"needle dimension(s) exceed the haystack image or region dimensions"，重试 16 次
全败。同批 TELEAGENT / AStudio 也报「目标窗口被遮挡约 100%」，根子相同：
窗口选择的旧排序把「是否前台窗口」放在第一位，置顶小窗长期占据前台就被稳定
选中，面积（第三顺位）救不回来。

本套件锁：
[1] 尺寸门槛纯函数：够格判定 / 全是小窗时原样回退（迷你模式应用仍需可用）；
[2] `_choose_best_window_for_flow` 场景：11-17 事故复现（宠物窗在前台也不得
    胜出）/ 先尺寸后关键词（含关键词的小窗不得挤出不含关键词的主窗）/ 双大窗
    取面积大者 / 只有小窗时仍返回小窗 / 平局时既有句柄优先；
[3] `_find_window_by_filters` 同源防线：跨进程找窗口时候选同样先过尺寸门槛。

跑法：.venv\\Scripts\\python.exe -m pytest tests\\suite_flow_window_pick_20261117.py -q
（经 tests/test_suites.py 子进程跑；直接文件调用有 stdout 包装 capture 老毛病）
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


def check(label, ok, detail=None):
    mark = "PASS" if ok else "FAIL"
    print(f"  {mark} {label}" + (f"  ← {detail}" if not ok and detail is not None else ""))
    if not ok:
        FAILURES.append(label)


class FakeWin32Gui:
    """最小 win32gui 桩：只服务窗口枚举/标题/前台查询。"""

    def __init__(self, windows, foreground=None):
        # windows: {hwnd: {"title": str, "rect": (l, t, r, b) | None,
        #                  "visible": bool, "iconic": bool}}
        self.windows = windows
        self.foreground = foreground

    def IsWindowVisible(self, hwnd):
        return bool(self.windows.get(hwnd, {}).get("visible", False))

    def IsIconic(self, hwnd):
        return bool(self.windows.get(hwnd, {}).get("iconic", False))

    def GetWindowRect(self, hwnd):
        rect = self.windows[hwnd]["rect"]
        if rect is None:
            raise RuntimeError("GetWindowRect on dead hwnd")
        return rect

    def GetWindowText(self, hwnd):
        return self.windows[hwnd].get("title", "")

    def GetForegroundWindow(self):
        return self.foreground

    def EnumWindows(self, callback, _lparam):
        for hwnd in self.windows:
            callback(hwnd, None)


class FakeWin32Process:
    def __init__(self, pid_by_hwnd):
        self.pid_by_hwnd = pid_by_hwnd

    def GetWindowThreadProcessId(self, hwnd):
        return 0, self.pid_by_hwnd.get(hwnd, 0)


def _patch_env(windows, pid_by_hwnd, foreground=None):
    gui = FakeWin32Gui(windows, foreground=foreground)
    proc = FakeWin32Process(pid_by_hwnd)
    return patch.object(ffr, "win32gui", gui), patch.object(ffr, "win32process", proc)


def _win(title, rect, pid, visible=True, iconic=False):
    return {"title": title, "rect": rect, "visible": visible, "iconic": iconic}


def test_size_gate_pure():
    print("[1] 尺寸门槛纯函数")
    # 注意：_window_rect 的契约是 (left, top, width, height)
    rects = {
        1: (0, 0, 1200, 800),      # 正常主窗口
        2: (1768, 928, 51, 79),    # Qoder Desktop Pet 51×79（11-17 实锤）
        3: (0, 0, 199, 800),       # 宽不足
        4: (0, 0, 800, 119),       # 高不足
    }
    with patch.object(ffr, "_window_rect", side_effect=lambda h: rects.get(h)):
        check("主窗口够格", ffr._is_full_size_flow_window(1) is True)
        check("51×79 桌面宠物不够格", ffr._is_full_size_flow_window(2) is False)
        check("宽 199 不够格", ffr._is_full_size_flow_window(3) is False)
        check("高 119 不够格", ffr._is_full_size_flow_window(4) is False)

        kept = ffr._filter_flow_window_candidates([1, 2, 3, 4])
        check("混合候选只留大窗", kept == [1], kept)
        check("全是小窗 → 原样回退（迷你模式应用仍可用）",
              ffr._filter_flow_window_candidates([2, 3, 4]) == [2, 3, 4])
        check("空候选返回空", ffr._filter_flow_window_candidates([]) == [])

    # 最小化窗口 _window_rect 为 None，但可 SW_RESTORE，必须仍算够格
    with patch.object(ffr, "_window_rect", side_effect=lambda h: None):
        check("最小化窗口仍算够格（可 SW_RESTORE）", ffr._is_full_size_flow_window(9) is True)


def test_choose_best_window():
    print("[2] _choose_best_window_for_flow 场景")
    # 11-17 事故复现：主窗 + 置顶宠物窗，且宠物窗是当前前台
    windows = {
        100: _win("Qoder", (0, 0, 1280, 800), pid=500),
        200: _win("Qoder Desktop Pet", (1768, 928, 1819, 1007), pid=500),
    }
    pid_by_hwnd = {100: 500, 200: 500}
    gui_patch, proc_patch = _patch_env(windows, pid_by_hwnd, foreground=200)
    with gui_patch, proc_patch:
        picked = ffr._choose_best_window_for_flow(500)
        check("宠物窗是前台也不得胜出（选主窗 100）", picked == 100, picked)

    # 先尺寸后关键词：含关键词的小窗不得挤出不含关键词的主窗
    windows = {
        100: _win("Qoder 主界面", (0, 0, 1280, 800), pid=500),
        200: _win("Qoder Desktop Pet", (1768, 928, 1819, 1007), pid=500),
    }
    gui_patch, proc_patch = _patch_env(windows, pid_by_hwnd, foreground=None)
    with gui_patch, proc_patch:
        picked = ffr._choose_best_window_for_flow(500, title_keywords=["Qoder Desktop Pet"])
        check("关键词只命中宠物窗时仍选主窗（尺寸优先于关键词过滤）",
              picked == 100, picked)

    # 双大窗：面积大者胜，即便前台是小的那个
    windows = {
        100: _win("AStudio", (0, 0, 900, 600), pid=600),
        200: _win("AStudio", (0, 0, 1920, 1080), pid=600),
    }
    gui_patch, proc_patch = _patch_env(windows, {100: 600, 200: 600}, foreground=100)
    with gui_patch, proc_patch:
        picked = ffr._choose_best_window_for_flow(600)
        check("双大窗取面积大者（前台小窗不得胜出）", picked == 200, picked)

    # 只有小窗：软回退，仍返回那个小窗而不是 None
    windows = {200: _win("Qoder Desktop Pet", (1768, 928, 1819, 1007), pid=500)}
    gui_patch, proc_patch = _patch_env(windows, {200: 500}, foreground=200)
    with gui_patch, proc_patch:
        picked = ffr._choose_best_window_for_flow(500)
        check("只有小窗时仍返回它（不因门槛把候选清空）", picked == 200, picked)

    # 关键词过滤仍然生效：两个大窗，只有一个是目标
    windows = {
        100: _win("Trae SOLO", (0, 0, 1280, 800), pid=700),
        200: _win("Trae 后台", (0, 0, 1280, 800), pid=700),
    }
    gui_patch, proc_patch = _patch_env(windows, {100: 700, 200: 700}, foreground=200)
    with gui_patch, proc_patch:
        picked = ffr._choose_best_window_for_flow(700, title_keywords=["solo"])
        check("同尺寸下标题关键词优先", picked == 100, picked)

    # 面积相同的平局：前台优先于既有句柄（用户切到同进程另一个窗口要能被检测到，
    # _refresh_flow_window 靠 candidate != current_hwnd 判"窗口切换"）
    windows = {
        100: _win("App", (0, 0, 1280, 800), pid=800),
        200: _win("App", (0, 0, 1280, 800), pid=800),
    }
    gui_patch, proc_patch = _patch_env(windows, {100: 800, 200: 800}, foreground=200)
    with gui_patch, proc_patch:
        picked = ffr._choose_best_window_for_flow(800, current_hwnd=100)
        check("平局时前台优先（不误吞窗口切换）", picked == 200, picked)

    # 都不是前台时，既有句柄胜出，避免同尺寸多窗口导致句柄抖动
    gui_patch, proc_patch = _patch_env(windows, {100: 800, 200: 800}, foreground=None)
    with gui_patch, proc_patch:
        picked = ffr._choose_best_window_for_flow(800, current_hwnd=100)
        check("都非前台时既有句柄优先（句柄不抖动）", picked == 100, picked)


def test_find_window_by_filters():
    print("[3] _find_window_by_filters 同源防线")
    windows = {
        100: _win("TeleAgent 星辰超级智能体", (0, 0, 1280, 800), pid=900),
        200: _win("TeleAgent 星辰超级智能体", (1768, 928, 1819, 1007), pid=900),
    }
    gui_patch, proc_patch = _patch_env(windows, {100: 900, 200: 900}, foreground=200)
    with gui_patch, proc_patch, \
            patch.object(ffr, "_process_matches_filters", return_value=True), \
            patch.object(ffr, "_process_name_of", return_value="teleagent.exe"), \
            patch.object(ffr, "_is_window_owned", return_value=True):
        got = ffr._find_window_by_filters(title_keywords=["teleagent"])
        check("跨进程找窗口同样先过尺寸门槛（选主窗 100）",
              got is not None and got[0] == 100, got)


def main():
    test_size_gate_pure()
    test_choose_best_window()
    test_find_window_by_filters()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED {len(FAILURES)}: {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
