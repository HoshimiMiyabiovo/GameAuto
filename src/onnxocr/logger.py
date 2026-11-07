"""OnnxOCR 统一日志模块（GameAuto vendored 版：接标准 logging）。

上游 OneDragon 版依赖 one_dragon.utils.log_utils；vendored 后改挂
logging.getLogger("onnxocr.<name>")，保留 {} 占位符调用风格。
"""

import logging
from typing import Any


class _LoggerShim:
    name: str

    def __init__(self, name: str) -> None:
        self.name = name
        self._log = logging.getLogger(f"onnxocr.{name}")

    def _format(self, msg: str) -> str:
        return f"[{self.name}] {msg}"

    def info(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._log.info(self._format(str(msg).format(*args, **kwargs)))

    def debug(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._log.debug(self._format(str(msg).format(*args, **kwargs)))

    def warning(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._log.warning(self._format(str(msg).format(*args, **kwargs)))

    def error(self, msg: str, *args: Any, **kwargs: Any) -> None:
        self._log.error(
            self._format(str(msg).format(*args, **kwargs)),
            exc_info=kwargs.get("exc_info"),
        )


def get_logger(name: str = "OnnxOCR") -> _LoggerShim:
    """获取带模块标识的 logger。"""
    return _LoggerShim(name)