# -*- coding: utf-8 -*-
"""onnxocr 高精度引擎（PP-OCRv6，vendored）回归（2026-11-07）。

锁定：
1. vendored 包可导入、无 one_dragon 残留；
2. v6 适配层符合 _ocr_scan 约定（[[box, text, score], ...]），XM 窗口截图命中
   「加载完」完整行（GPU；无 CUDA 的机器跳过 EP 硬断言）；
3. 统一精度工厂只留 v6 单档（v3/v4/v5 与 RapidOCR 已于 2026-11-07 全部下线）；
4. runner 两处 ocr_precision 接线走 _get_precision_engine。

跑法：.venv\\Scripts\\python.exe tests\\suite_onnxocr_precision_20261107.py
"""

import io
import logging
import os
import re
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

FAILURES = []
REF = PROJECT_ROOT / "data" / "artifacts" / "ref_xm_window_20261107.png"
WINDOW = (508, 240, 901, 610)
MODELS = PROJECT_ROOT / "data" / "models" / "onnx_ocr"
VENDOR = PROJECT_ROOT / "src" / "onnxocr"


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} :: {detail}")
        FAILURES.append(name)


def _logger():
    logger = logging.getLogger("test_onnxocr_precision")
    logger.setLevel(logging.CRITICAL)
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


def _window_img():
    import numpy as np
    from PIL import Image

    l, t, w, h = WINDOW
    return np.asarray(
        Image.open(str(REF)).convert("RGB").crop((l, t, l + w, t + h))
    )


def _check_format(results):
    ok = True
    for item in results or []:
        if len(item) != 3:
            return False
        box, text, score = item
        if not isinstance(text, str) or not isinstance(score, float):
            return False
        if len(box) != 4 or any(len(p) != 2 for p in box):
            return False
    return ok


def test_vendor_package():
    print("[1] vendored 包与 self-contained")
    check("包目录存在", VENDOR.is_dir(), VENDOR)
    sources = sorted(VENDOR.glob("*.py"))
    check("源码文件齐全（>=15）", len(sources) >= 15, len(sources))
    residual = [
        p.name for p in sources
        if re.search(r"(?m)^\s*(from|import)\s+one_dragon\b", p.read_text(encoding="utf-8"))
    ]
    check("无 one_dragon import 残留", not residual, residual)
    missing = [
        str(d.relative_to(MODELS))
        for d in (MODELS / "ppocrv6",)
        for f in ("det.onnx", "rec.onnx", f"{d.name}_dict.txt")
        if not (d / f).is_file()
    ]
    check("v6 模型与字典已就位（缺失则后续 e2e 跳过）", not missing, missing)
    return not missing


def test_v6_end_to_end():
    print("[2][3] v6 适配层与 XM 窗口端到端")
    if not REF.is_file():
        print("  SKIP 缺回归截图", REF)
        return
    from game_auto.runtime.onnxocr_precision import build_onnxocr_engine

    try:
        import onnxruntime as ort
        has_cuda = "CUDAExecutionProvider" in ort.get_available_providers()
    except Exception:
        has_cuda = False
    img = _window_img()

    ms = "v6"
    eng = build_onnxocr_engine(ms, _logger())
    check(f"{ms} 引擎可构建", eng is not None, "模型缺失或初始化失败")
    if eng is None:
        return
    if has_cuda:
        ep = eng.providers()
        check(f"{ms} det/rec 都用 CUDA",
              "CUDAExecutionProvider" in ep.get("det", [])
              and "CUDAExecutionProvider" in ep.get("rec", []), ep)
    eng(img)  # 预热
    t0 = time.perf_counter()
    results, elapse = eng(img)
    cost = time.perf_counter() - t0
    check(f"{ms} 返回 (结果, 耗时) 且耗时为正",
          isinstance(elapse, list) and elapse and elapse[0] > 0, elapse)
    check(f"{ms} 结果符合 rapidocr 约定", _check_format(results),
          (results or [])[:2])
    check(f"{ms} 单次窗口扫描 < 10s（防回归到 CPU 慢路径）", cost < 10, f"{cost:.1f}s")
    texts = [t for _, t, _ in (results or [])]
    check("v6 全窗命中「加载完」（回归锁）",
          any("加载完" in t for t in texts), texts[:6])
    check("v6 识别到完整行（含「游戏数据加载」）",
          any("游戏数据加载" in t for t in texts), texts[:6])


def test_precision_factory():
    print("[4] 统一精度工厂选择")
    import game_auto.runtime.ocr_click_tester as oct

    if not (MODELS / "ppocrv6" / "det.onnx").is_file():
        print("  SKIP 缺 v6 模型，工厂会回退 v4")
        return

    def _reset():
        oct._onnxocr_engine = None
        oct._precision_engine = None
        oct._precision_engine_resolved = False

    old = os.environ.pop("GAMEAUTO_OCR_PRECISION", None)
    try:
        _reset()
        eng = oct._get_precision_engine(_logger())
        check("默认选 onnxocr_v6（唯一引擎）",
              getattr(eng, "backend", "") == "onnxocr_v6",
              type(eng).__name__)
    finally:
        if old is not None:
            os.environ["GAMEAUTO_OCR_PRECISION"] = old
        _reset()


def test_runner_wiring():
    print("[5] runner 接线与静态约定")
    import game_auto.runtime.foreground_flow_runner as ff

    src = Path(ff.__file__).read_text(encoding="utf-8")
    check("步骤循环用统一工厂", "step_engine = _get_precision_engine(logger)" in src, None)
    check("失败取证用统一工厂", "prec_engine = _get_precision_engine(logger)" in src, None)
    ocr_src = (PROJECT_ROOT / "src/game_auto/runtime/ocr_click_tester.py").read_text(
        encoding="utf-8")
    check("RapidOCR 主引擎已删除", "_get_rapidocr_engine" not in ocr_src, None)
    check("旧 v4/v5 工厂已删除",
          "_get_rapidocr_precision_engine" not in ocr_src
          and "\"v5\"" not in ocr_src, None)
    check("默认引擎是 onnxocr v6", "_get_onnxocr_engine()" in ocr_src, None)
    adapter = PROJECT_ROOT / "src/game_auto/runtime/onnxocr_precision.py"
    check("适配层存在且声明数据格式", adapter.is_file()
          and "rapidocr" in adapter.read_text(encoding="utf-8"), adapter)


def main():
    models_ok = test_vendor_package()
    if models_ok:
        test_v6_end_to_end()
    else:
        print("[2][3] SKIP 模型缺失")
    test_precision_factory()
    test_runner_wiring()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED {len(FAILURES)}: {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
