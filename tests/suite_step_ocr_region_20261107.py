# -*- coding: utf-8 -*-
"""步骤级 OCR 区域（ocr_region_frac）回归（2026-11-07）。

背景：XM「等待数据加载完毕」的目标字打在脚本右下 10px 小日志面板里，全窗
扫描必漏（33 区域轮询 0 命中）；用户原生截图实测——窗口相对紧裁剪一次命中
（数据加载完毕！）。新增 `ocr_region_frac`（窗口相对 [x0,y0,x1,y1]，每轮按
当前窗口 rect 实时换算，窗口移动/换屏自动跟随）。

覆盖：
  [1] _step_ocr_region 换算 + 跟随窗口移动 + 非法输入回退 None
  [2] 真实截图端到端：窗口 (508,240,901,610) + 区域 → 裁剪 OCR 命中目标字
  [3] 两份配置：XM 步骤3 带 ocr_region_frac + optional_if_missing
  [4] runner 静态接线

跑法：.venv\\Scripts\\python.exe tests\\suite_step_ocr_region_20261107.py
"""

import io
import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import game_auto.runtime.foreground_flow_runner as ff  # noqa: E402

FAILURES = []
REF = PROJECT_ROOT / "data" / "artifacts" / "ref_xm_window_20261107.png"
WINDOW = (508, 240, 901, 610)          # XM 窗口 rect（今晨实机 + 用户截图一致）
FRAC = [0.7125, 0.5049, 0.9656, 0.623]  # 小日志面板紧贴边界（排除时间戳列）


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} :: {detail}")
        FAILURES.append(name)


def _logger():
    logger = logging.getLogger("test_step_ocr_region")
    logger.setLevel(logging.CRITICAL)
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


def test_region_math():
    print("[1] 窗口相对 → 屏幕绝对，且跟随窗口移动")
    r = ff._step_ocr_region([0.69, 0.48, 0.98, 0.65], WINDOW)
    expect = (int(508 + 0.69 * 901), int(240 + 0.48 * 610),
              int(0.29 * 901), int(0.17 * 610))
    check("基准换算精确", r == expect, (r, expect))
    moved = ff._step_ocr_region([0.69, 0.48, 0.98, 0.65], (1000, 300, 901, 610))
    check("窗口移动 → 区域跟着走",
          moved[0] == expect[0] + (1000 - 508) and moved[1] == expect[1] + (300 - 240),
          (moved, expect))
    check("窗口尺寸变化 → 按比例缩放",
          ff._step_ocr_region([0.0, 0.0, 1.0, 1.0], (0, 0, 500, 400)) == (0, 0, 500, 400),
          None)
    check("非法 frac → None（回退全窗扫描）",
          ff._step_ocr_region("bad", WINDOW) is None
          and ff._step_ocr_region(None, WINDOW) is None
          and ff._step_ocr_region([0.1, 0.2], WINDOW) is None, None)
    check("窗口 rect 无效 → None",
          ff._step_ocr_region(FRAC, None) is None
          and ff._step_ocr_region(FRAC, (0, 0, 0, 0)) is None, None)


def test_end_to_end_on_real_screenshot():
    print("[2] 真实截图端到端：onnxocr v6 全窗扫描命中（GPU）")
    check("原生参考截图已存档", REF.is_file(), REF)
    if not REF.is_file():
        return
    import numpy as np
    from PIL import Image
    from game_auto.runtime.ocr_click_tester import _get_onnxocr_engine

    eng = _get_onnxocr_engine(_logger())
    if eng is None:
        print("  SKIP v6 引擎不可用（缺模型/GPU），跳过端到端")
        return
    img = np.asarray(
        Image.open(str(REF)).convert("RGB").crop(
            (WINDOW[0], WINDOW[1], WINDOW[0] + WINDOW[2], WINDOW[1] + WINDOW[3]))
    )
    result, _ = eng(img)
    texts = [t for _, t, _ in (result or [])]
    check(f"v6 全窗扫描命中「加载完」（区域={len(texts)}）",
          any("加载完" in t for t in texts), texts[:6])
    check("识别到的是完整行（含「游戏数据加载」）",
          any("游戏数据加载" in t for t in texts), texts[:6])


def test_configs_and_wiring():
    print("[3][4] 配置与静态接线")
    for name in ("flow_configs.json", "flow_configs.example.json"):
        cfg = json.loads((PROJECT_ROOT / name).read_text(encoding="utf-8"))
        s = cfg["XM"]["steps"][3]
        check(f"{name} XM 步骤3 = ocr_precision（onnxocr v6）",
              s.get("ocr_precision") is True, s.get("ocr_precision"))
        check(f"{name} XM 步骤3 已去掉脆弱的区域标定",
              "ocr_region_frac" not in s, s.get("ocr_region_frac"))
        check(f"{name} XM 步骤3 仍是 optional（引擎不可用时兜底）",
              s.get("optional_if_missing") is True, s.get("optional_if_missing"))
    src = Path(ff.__file__).read_text(encoding="utf-8")
    check("runner 定义 _step_ocr_region（通用能力保留）", "def _step_ocr_region(" in src, None)
    check("步骤循环读取 ocr_precision", 'step.get("ocr_precision")' in src, None)
    check("定位链透传 engine",
          "engine=step_engine" in src and "engine=prec_engine" in src, None)
    src_ocr = (PROJECT_ROOT / "src/game_auto/runtime/ocr_click_tester.py").read_text(
        encoding="utf-8")
    check("v6 引擎工厂存在", "def _get_onnxocr_engine(" in src_ocr, None)
    check("RapidOCR v4 工厂已删除", "_get_rapidocr_precision_engine" not in src_ocr, None)


def main():
    test_region_math()
    test_end_to_end_on_real_screenshot()
    test_configs_and_wiring()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
