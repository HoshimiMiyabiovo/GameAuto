# -*- coding: utf-8 -*-
"""MiniMax 签到按钮 OCR 撞词回归（2026-10-31）。

现象：目标「签到得」子串命中了卡片宣传语「连续签到得更多积分」，点到了文案
而不是按钮，导致没有 signin/claim。
本套件锁定：
1. ocr_find_any_text 支持 exclude_texts，且排除后能选中按钮；
2. 不排除时确实会撞上宣传语（记录原始 bug）；
3. flow_configs 的步骤4带排除词与按钮候选；
4. 流程运行器把 ocr_exclude_texts 传给 OCR 定位。

跑法：.venv\\Scripts\\python.exe tests\\suite_minimax_ocr_exclude_20261031.py
"""

import json
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT.parent))

from game_auto.runtime import ocr_click_tester as oct  # noqa: E402
from game_auto.runtime import foreground_flow_runner as ffr  # noqa: E402

FAILURES = []
LOGGER = logging.getLogger("test.minimax.ocr")


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def _results():
    return [
        {"text": "连续签到得更多积分", "confidence": 0.85, "center": (582.0, 748.0)},
        {"text": "签到得 ⊙ 400", "confidence": 0.90, "center": (330.0, 900.0)},
    ]


def main():
    # 2026-10-31 起 ocr_find_any_text 走 _ocr_scan（需要拿当帧截图做候选标注），
    # 旧测试桩从 ocr_window_content 迁到这里。
    original = oct._ocr_scan
    try:
        oct._ocr_scan = lambda *_a, **_k: (_results(), None, None)

        print("[1] 原始撞词与排除词")
        hit = oct.ocr_find_any_text(["签到得"], LOGGER)
        check("不排除时撞宣传语（原始 bug）",
              hit is not None and hit["text"] == "连续签到得更多积分", hit and hit["text"])
        hit = oct.ocr_find_any_text(["签到得"], LOGGER, exclude_texts=["更多积分"])
        check("排除后选中按钮",
              hit is not None and hit["text"] == "签到得 ⊙ 400", hit and hit["text"])
        check("排除后坐标来自按钮",
              hit is not None and hit["center"] == (330.0, 900.0), hit and hit["center"])

        hit = oct.ocr_find_any_text(
            ["签到得⊙400", "签到得 400", "签到得"], LOGGER, exclude_texts=["更多积分"])
        check("多候选时优先具体按钮文案",
              hit is not None and hit["center"] == (330.0, 900.0), hit and hit["center"])

        hit = oct.ocr_find_any_text(["签到得"], LOGGER, exclude_texts=["更多积分", "签到"])
        check("全部被排除时返回 None", hit is None, hit)
    finally:
        oct._ocr_scan = original

    print("[2] 配置与接线")
    for name in ("flow_configs.json", "flow_configs.example.json"):
        cfg = json.loads((PROJECT_ROOT / name).read_text(encoding="utf-8"))
        step = next((s for s in (cfg.get("MINIMAX") or {}).get("steps") or []
                     if s.get("name") == "点击签到得按钮"), None)
        check(f"{name} 步骤4存在", step is not None)
        if step:
            check(f"{name} 排除「更多积分」", "更多积分" in (step.get("ocr_exclude_texts") or []),
                  step.get("ocr_exclude_texts"))
            targets = step.get("target_text")
            check(f"{name} 目标含按钮文案",
                  isinstance(targets, list) and "签到得" in targets, targets)

    source = Path(ffr.__file__).read_text(encoding="utf-8")
    check("流程读取 ocr_exclude_texts", "step.get(\"ocr_exclude_texts\")" in source)
    check("流程把排除词传给 OCR", "exclude_texts=step_exclude_texts" in source)
    check("定位函数支持 exclude_texts", "exclude_texts: Optional[List[str]] = None" in source)

    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
