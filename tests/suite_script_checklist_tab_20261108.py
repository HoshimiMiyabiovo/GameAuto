"""清单同步 `_click_tab` 标签匹配归一化回归（2026-11-08）。

背景：XM 步骤 6「同步限时活动勾选」2026-11-08 首跑失败「未找到标签页
Unity」——onnxocr v6 把 "Unity" 稳定拆成 "Uni ty"（11-07 10:37 conf 0.78 /
11-08 08:17 conf 0.96 两天取证一致），`_click_tab` 原始子串匹配
`"unity" in "uni ty"` 落空。当帧取证里其余标签全部命中（Flash 1.00 等）。

断言（mock `_ocr_scan`，点击打桩 `_pyautogui_click_guarded`）：
  1. 实锤复现：OCR 给 "Uni ty" 也能命中并点击（2026-11-08 事故帧坐标）
  2. 精确文本 "Unity" 仍命中（无回归）
  3. 多个候选取 y 最小（标签栏在窗口顶部，列表正文里的同名串不误点）
  4. 无匹配 → False 且不点击
  5. 静态：匹配走 `_norm_text` 归一化（防退回原始子串匹配）

跑法：.venv\\Scripts\\python.exe -m pytest tests/suite_script_checklist_tab_20261108.py -q
"""

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from game_auto.runtime import foreground_flow_runner as ffr  # noqa: E402
from game_auto.runtime import script_checklist as scl  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}", flush=True)
    else:
        print(f"  FAIL {name} {detail}", flush=True)
        FAILURES.append(name)


# 2026-11-08 08:17 事故帧的标签栏 OCR（截取相关区域），"Uni ty" 为实锤原文
ACCIDENT_FRAME_RESULTS = [
    {"text": "XXM Seer脱机日常", "confidence": 0.96, "center": [576.0, 255.0], "box": [0, 0, 10, 10]},
    {"text": "Flash", "confidence": 1.00, "center": [716.0, 283.0], "box": [0, 0, 10, 10]},
    {"text": "Uni ty", "confidence": 0.96, "center": [765.0, 284.0], "box": [0, 0, 10, 10]},
    {"text": "精灵因子", "confidence": 1.00, "center": [821.0, 284.0], "box": [0, 0, 10, 10]},
    {"text": "巅峰圣战", "confidence": 1.00, "center": [879.0, 283.0], "box": [0, 0, 10, 10]},
    {"text": "扩展中心", "confidence": 1.00, "center": [958.0, 280.0], "box": [0, 0, 10, 10]},
]


def _patch_env(results):
    """打桩 _ocr_scan（按 06-robustness 惯例）与点击/睡眠，返回点击记录列表。"""
    clicks = []

    def fake_ocr_scan(logger, hwnd=None, **kwargs):
        return results, None, (0, 0, 1200, 800)

    def fake_click(x, y, **kwargs):
        clicks.append((x, y))

    scl._ocr_scan = fake_ocr_scan
    ffr._pyautogui_click_guarded = fake_click
    scl.time.sleep = lambda *_a, **_k: None
    return clicks


def test_ocr_split_repro():
    print("[1] 实锤复现：'Uni ty' 命中并按事故帧坐标点击")
    clicks = _patch_env(list(ACCIDENT_FRAME_RESULTS))
    ok = scl._click_tab(__import__("logging").getLogger("test"), 1234, "Unity")
    check("匹配成功", ok is True)
    check("点击了 Unity 标签位置 (765, 284)", clicks == [(765, 284)], clicks)


def test_exact_text_still_hits():
    print("[2] 精确文本 'Unity' 仍命中（无回归）")
    clicks = _patch_env([{"text": "Unity", "confidence": 0.99, "center": [760.0, 280.0], "box": [0, 0, 10, 10]}])
    ok = scl._click_tab(__import__("logging").getLogger("test"), 1234, "Unity")
    check("匹配成功", ok is True)
    check("点击 (760, 280)", clicks == [(760, 280)], clicks)


def test_topmost_y_wins():
    print("[3] 多候选取 y 最小（正文里的同名串不误点）")
    results = list(ACCIDENT_FRAME_RESULTS) + [
        {"text": "unity模式说明", "confidence": 0.90, "center": [400.0, 500.0], "box": [0, 0, 10, 10]},
    ]
    clicks = _patch_env(results)
    ok = scl._click_tab(__import__("logging").getLogger("test"), 1234, "Unity")
    check("匹配成功", ok is True)
    check("点了顶部标签 (765, 284) 而非正文 (400, 500)", clicks == [(765, 284)], clicks)


def test_no_match_fails_without_click():
    print("[4] 无匹配 → False 且不点击")
    clicks = _patch_env([{"text": "Flash", "confidence": 1.00, "center": [716.0, 283.0], "box": [0, 0, 10, 10]}])
    ok = scl._click_tab(__import__("logging").getLogger("test"), 1234, "Unity")
    check("返回 False", ok is False)
    check("没有点击", clicks == [], clicks)


def test_normalized_match_in_source():
    print("[5] 静态：匹配走 _norm_text 归一化（防退回原始子串匹配）")
    src = inspect.getsource(scl._click_tab)
    check("实现包含 _norm_text 归一化", "_norm_text" in src)


def main():
    test_ocr_split_repro()
    test_exact_text_still_hits()
    test_topmost_y_wins()
    test_no_match_fails_without_click()
    test_normalized_match_in_source()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
