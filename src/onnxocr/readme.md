# 说明

复制自 https://github.com/jingsongliujing/OnnxOCR
基于 Apache 2.0 License 开源协议。
请注意：OnnxOCR 项目采用 Apache 2.0 协议开源，本项目在引用和修改其代码时遵循该协议，特此声明。

更新时间 2025.06.13

改动说明

- 将模型、字体、文本放入 assets/models/onnx_ocr 中
- 支持 PP-OCRv6 的使用，保留了报错截图及 v5 模型的后向兼容

## GameAuto vendored 改动（2026-11-07）

来源：本机 OneDragon 安装 `D:\down\ZZZ\ZenlessZoneZero-OneDragon\src\onnxocr`
（上游自 OnnxOCR，OneDragon 维护 fork：PP-OCRv5 + v6 字典/尺寸归一化）。

为脱离 OneDragon 运行时，只改 3 处 self-contained 接缝：

- `logger.py`：`one_dragon.utils.log_utils` → 标准 `logging`（`onnxocr.<name>`），保留 `{}` 占位风格。
- `predict_base.py`：`one_dragon.utils.gpu_executor` 的单线程 executor → DirectML 时加全局锁（CUDA/CPU 的 ORT run 本身线程安全）。
- `onnx_paddleocr.py`：v6 默认参数里的模型根目录改为 `<仓库根>/data/models/onnx_ocr`（`ONNXOCR_MODEL_ROOT` 可覆）；推理异常走标准日志；删掉依赖 one_dragon 的 `__debug()` 入口。
- `inference_engine.py`：CUDA EP 用 `HEURISTIC` 卷积算法搜索 + `kSameAsRequested` arena（对齐本项目 v4 实测，上游 `DEFAULT` 在动态 shape 下刷屏 Conv fallback 警告）；`log_severity_level=3` 只留 Error。

模型不入库（`data/` 已 gitignore），本机从 OneDragon 的
`assets/models/onnx_ocr/{ppocrv5,ppocrv6}/` 拷贝 `det.onnx` / `rec.onnx` / `cls.onnx` / 字典。
