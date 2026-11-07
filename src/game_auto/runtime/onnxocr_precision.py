# -*- coding: utf-8 -*-
"""高精度 OCR 的 onnxocr 实现（vendored PP-OCRv6，CUDA）。

与 rapidocr 引擎只差调用约定：``_ocr_scan`` 需要 ``(结果, 耗时)``，结果是
``[[box, text, score], ...]``；``ONNXPaddleOcr.ocr`` 返回 ``[[[box,(text,score)],...]]``。

2026-11-07 实测（RTX 5070，XM 10px 日志面板，窗口 901×610）：v6 全窗命中
「[14:50:22]：游戏数据加载完毕！」。输入保持 RGB：同图 RGB 的「Unity / 账号：」
比 BGR（"Uni ty" / "à"）干净。v5/v4/v3 与 RapidOCR 已于 11-07 全部下线。
"""

import logging
import time
from typing import Optional

import numpy as np

from ..settings.paths import BASE_DIR

ONNXOCR_MODELS_DIR = BASE_DIR / "data" / "models" / "onnx_ocr"
_MODEL_SETS = ("v6",)


class OnnxOcrEngine:
    """把 ONNXPaddleOcr 适配成 ``_ocr_scan`` 认的 engine。"""

    def __init__(self, ocr, backend: str):
        self._ocr = ocr
        self.backend = backend

    def providers(self) -> dict:
        try:
            return {
                "det": self._ocr.text_detector.det_onnx_session.get_providers(),
                "rec": self._ocr.text_recognizer.rec_onnx_session.get_providers(),
            }
        except Exception as exc:
            return {"error": str(exc)}

    def __call__(self, img):
        if not isinstance(img, np.ndarray):
            img = np.asarray(img)
        t0 = time.perf_counter()
        out = self._ocr.ocr(img, det=True, rec=True, cls=False)
        elapsed = time.perf_counter() - t0
        results = []
        for box, (text, score) in (out[0] if out else []):
            results.append([[[float(x), float(y)] for x, y in box], text, float(score)])
        return results, [elapsed]


def build_onnxocr_engine(
    model_set: str, logger: Optional[logging.Logger] = None
) -> Optional[OnnxOcrEngine]:
    """构造 onnxocr 高精度引擎；模型缺失或初始化失败返回 None（调用方回退）。"""
    lg = logger or logging.getLogger("GameAutomation")
    if model_set not in _MODEL_SETS:
        return None
    model_dir = ONNXOCR_MODELS_DIR / f"ppocr{model_set}"
    required = ("det.onnx", "rec.onnx", f"ppocr{model_set}_dict.txt")
    missing = [name for name in required if not (model_dir / name).is_file()]
    if missing:
        lg.info(f"[OCR] onnxocr {model_set} 不可用（{model_dir} 缺 {missing}）")
        return None

    from .ocr_click_tester import _ensure_nvidia_cuda_dlls  # 延迟导入：避免工厂循环依赖

    _ensure_nvidia_cuda_dlls(force=True)
    from onnxocr.onnx_paddleocr import ONNXPaddleOcr

    # ocr_model_name 触发 fork 的 v6 调参：det 960/max、box_thresh 0.45、
    # rec 3,48,320、v6 专用字典；模型路径由 vendored 模型根（data/models/onnx_ocr）解析。
    ocr = ONNXPaddleOcr(use_gpu=True, ocr_model_name="ppocrv6", use_angle_cls=False)
    eng = OnnxOcrEngine(ocr, backend=f"onnxocr_{model_set}")
    lg.info(f"[OCR] onnxocr {model_set} 高精度引擎已加载，EP={eng.providers()}")
    return eng
