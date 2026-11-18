# -*- coding: utf-8 -*-
"""鸣潮启动器「启动器版本更新」模态框回归（2026-11-18 #431）。

事故：11:37 WuwaLauncher 启动官方启动器后，弹出独立顶层窗口
``krupdatereminderdlg``（launcher_main.exe 同 PID），标题「启动器版本更新」，
按钮「退出 / 更新」。旧逻辑两处失守：

1. ``ocr_click_tester._window_obscured_ratio`` 把同 PID 的模态窗算作遮挡，
   采样比例 100% → OCR 防护拒绝识别 → 步骤 1 的「更新」永远扫不到。
2. 步骤 1 用 legacy 子串匹配，即使 OCR 放行也会先命中标题「启动器版本更新」
   （点标题无效），之后「进入游戏」点击被模态框吞掉，步骤 4 空等 40 分钟。

本套件锁：
[1] 遮挡判据：同 PID 顶层弹窗不计遮挡；跨进程窗口仍计；最小化仍算 100%。
[2] exact 匹配：只有正文「更新」命中，标题「启动器版本更新」不命中。
[3] 两份 flow_configs 的 WuwaLauncher 步骤 1 接线：ocr_match_mode=exact。

跑法：.venv\\Scripts\\python.exe -m pytest tests\\suite_wuwa_launcher_update_modal_20261118.py -q
"""

import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import game_auto.runtime.ocr_click_tester as oct_  # noqa: E402

FAILURES = []


def check(label, ok, detail=None):
    mark = "PASS" if ok else "FAIL"
    print(f"  {mark} {label}" + (f"  ← {detail}" if not ok and detail is not None else ""))
    if not ok:
        FAILURES.append(label)


class FakeWin32Gui:
    def __init__(self, rect=(0, 0, 100, 100), iconic=False):
        self.rect = rect
        self.iconic = iconic

    def IsWindow(self, hwnd):
        return True

    def IsIconic(self, hwnd):
        return self.iconic

    def GetWindowRect(self, hwnd):
        return self.rect

    def GetWindowText(self, hwnd):
        return "鸣潮"


class FakeWin32Process:
    def __init__(self, pid_by_hwnd):
        self.pid_by_hwnd = pid_by_hwnd

    def GetWindowThreadProcessId(self, hwnd):
        return 0, self.pid_by_hwnd.get(hwnd, 0)


class FakeUser32:
    def __init__(self, hits):
        self.hits = hits

    def GetAncestor(self, hwnd, _flag):
        return hwnd

    def WindowFromPoint(self, pt):
        return self.hits.get((pt.x, pt.y), 0)


class FakeShcore:
    def SetProcessDpiAwareness(self, _value):
        return 0


def _ratio(hits, pid_by_hwnd, *, iconic=False):
    fake_windll = SimpleNamespace(user32=FakeUser32(hits), shcore=FakeShcore())
    with mock.patch.object(oct_, "win32gui", FakeWin32Gui(iconic=iconic)), \
         mock.patch.object(oct_, "win32process", FakeWin32Process(pid_by_hwnd)), \
         mock.patch.object(oct_.ctypes, "windll", fake_windll):
        return oct_._window_obscured_ratio(100, samples=2)


def test_obscure_ratio_same_pid_modal():
    print("[1] 遮挡判据：同 PID 弹窗不算遮挡，跨进程仍算")
    # samples=2，窗口 (0,0,100,100) → 采样点 (33,33)/(66,33)/(33,66)/(66,66)
    pts = [(33, 33), (66, 33), (33, 66), (66, 66)]

    ratio = _ratio({p: 200 for p in pts}, {100: 42, 200: 42})
    check("同 PID 模态窗全部覆盖 → 0% 遮挡", ratio == 0.0, ratio)

    half = {pts[0]: 200, pts[1]: 200, pts[2]: 300, pts[3]: 300}
    ratio = _ratio(half, {100: 42, 200: 42, 300: 99})
    check("同 PID 弹窗 + 跨进程窗口各半 → 50% 遮挡", ratio == 0.5, ratio)

    ratio = _ratio({p: 300 for p in pts}, {100: 42, 300: 99})
    check("全是跨进程窗口 → 100% 遮挡（防护仍生效）", ratio == 1.0, ratio)

    ratio = _ratio({p: 200 for p in pts}, {100: 42, 200: 42}, iconic=True)
    check("目标最小化 → 仍判 100%", ratio == 1.0, ratio)


def test_exact_mode_skips_modal_title():
    print("[2] exact 匹配：只点「更新」按钮，不点「启动器版本更新」标题")
    candidates = ["更新"]
    results = [
        {"text": "启动器版本更新", "confidence": 0.99, "center": (700, 315)},
        {"text": "优化了部分功能", "confidence": 0.98, "center": (700, 380)},
        {"text": "更新", "confidence": 0.97, "center": (779, 474)},
    ]
    exact, meta = oct_._rank_ocr_candidates(results, candidates, "exact", 0.5)
    check("exact 只命中按钮正文「更新」",
          exact is not None and exact["text"] == "更新",
          exact and exact["text"])
    title_item = next(
        (item for item in meta["items"] if item["text"] == "启动器版本更新"), None
    )
    check("exact 下标题「启动器版本更新」不参与匹配",
          title_item is not None and title_item["status"] == "not_target",
          title_item and title_item["status"])


def test_flow_configs_wired():
    print("[3] 两份 flow_configs 的 WuwaLauncher 步骤 1 接线")
    found = 0
    for name in ("flow_configs.example.json", "flow_configs.json"):
        path = PROJECT_ROOT / name
        if not path.exists():
            print(f"  SKIP {name} 不存在（干净 clone / CI 隐藏本机配置）")
            continue
        found += 1
        cfg = json.loads(path.read_text(encoding="utf-8"))
        steps = cfg["WuwaLauncher"]["steps"]
        step = next((s for s in steps if s.get("name") == "游戏更新"), None)
        check(f"{name}: 存在「游戏更新」步骤", step is not None)
        if not step:
            continue
        check(f"{name}: ocr_match_mode=exact", step.get("ocr_match_mode") == "exact",
              step.get("ocr_match_mode"))
        check(f"{name}: target_text 仍为「更新」", step.get("target_text") == "更新",
              step.get("target_text"))
    check("至少校验到一份配置", found >= 1, found)


def main():
    test_obscure_ratio_same_pid_modal()
    test_exact_mode_skips_modal_title()
    test_flow_configs_wired()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED {len(FAILURES)}: {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
