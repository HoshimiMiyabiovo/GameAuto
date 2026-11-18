# -*- coding: utf-8 -*-
"""GameAutomation 日志落盘兜底（不依赖 AutomationController 懒加载）。

背景（2026-11-18 实锤）：QML GUI 的 AutomationController 是**懒加载**的
（``ui/bridge.py::_get_controller`` 首次正式任务才构造），而薅羊毛集合走
``bridge_views/automation._run_wool_backend → run_wool_background → run_flow``，
整条路径不经过 controller。于是 ``AutomationController._init_logger`` 从未执行：
``data/logs/auto_runner.log`` 停在上一轮 GUI 任务（11-18 21:16），22:30 的整段
运行日志只存在于 WebUI 内存缓冲（页面能看、文件查不到），事故复盘时没有落盘证据。

调用约定：GUI / WebUI 启动边界各调一次，幂等：
- logger 上已有 ``FileHandler`` → 跳过；
- 已有 ``QueueHandler``（AutomationController 已接管，文件写盘在 QueueListener 里）
  → 跳过，不能重复写；
- 测试进程（``GAMEAUTO_LOG_FILE_DISABLED`` / ``PYTEST_CURRENT_TEST``）→ 跳过，
  保持 06-robustness「测试不许写生产日志」的约定。
"""

from __future__ import annotations

import logging
import os
from logging.handlers import TimedRotatingFileHandler
from typing import Optional

_FORMAT = (
    "[%(asctime)s.%(msecs)03d] [%(levelname)-8s] "
    "%(name)s:%(funcName)s:%(lineno)d - %(message)s"
)
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# 标记自己挂的 handler，便于排查与测试（不改任何现有 handler 行为）
_HANDLER_FLAG = "_gameauto_file_handler"


def log_file_disabled() -> bool:
    """测试进程禁止写生产日志（与 AutomationController._init_logger 同口径）。"""
    return (
        os.environ.get("GAMEAUTO_LOG_FILE_DISABLED", "").strip().lower()
        in ("1", "true", "yes")
        or bool(os.environ.get("PYTEST_CURRENT_TEST"))
    )


def _has_queue_handler(logger: logging.Logger) -> bool:
    from logging.handlers import QueueHandler

    return any(isinstance(h, QueueHandler) for h in logger.handlers)


def ensure_file_log_handler(
    logger: Optional[logging.Logger] = None,
) -> Optional[logging.Handler]:
    """确保 GameAutomation 日志有文件落盘；返回新挂的 handler 或 None（已接管/禁用）。

    只挂 ``GameAutomation`` 这一层：子 logger（GameAutomation.Bridge /
    GameAutomation.WebUI / ...）都会向它传播，不需要在 root 重复挂。
    """
    lg = logger or logging.getLogger("GameAutomation")
    if log_file_disabled():
        return None
    if any(isinstance(h, logging.FileHandler) for h in lg.handlers):
        return None
    if _has_queue_handler(lg):
        return None

    try:
        from ..settings import paths as _paths
        from ..settings.manager import get_log_config

        log_path = _paths.LOG_FILE
        backup_count = max(0, int(get_log_config().get("backup_count", 0) or 0))
        os.makedirs(os.path.dirname(str(log_path)), exist_ok=True)
        handler = TimedRotatingFileHandler(
            str(log_path),
            when="midnight",
            interval=1,
            backupCount=backup_count,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATE_FORMAT))
        setattr(handler, _HANDLER_FLAG, True)
        lg.addHandler(handler)
        lg.info("[日志] 文件日志已挂载: %s", log_path)
        return handler
    except Exception:
        # 日志基础设施自身失败不能影响 GUI 启动（与 06-robustness 的静默例外同源）
        return None
