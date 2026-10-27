from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from ..services.utils import read_file_with_timeout

# 默认错误关键字：March7th Assistant 任务失败时会在日志写入 "ERROR | 发生错误 ..."
DEFAULT_ERROR_KEYWORDS = ("发生错误",)


class ScriptErrorLogWatcher:
    """监控脚本日志目录，检测任务启动后新出现的错误关键字行。

    只匹配监控启动之后追加的日志行（启动时记录各文件当前偏移量），
    避免上一次运行遗留的报错行在重试时造成误判。
    """

    def __init__(
        self,
        logger,
        name: str,
        log_dir: str,
        *,
        file_pattern: str = "*.log",
        error_keywords: Optional[List[str]] = None,
        date_format: str = "%Y-%m-%d",
        encoding: str = "utf-8",
        require_date_token: bool = True,
        read_timeout: float = 10.0,
    ):
        self.logger = logger
        self.name = name
        self.log_dir = Path(log_dir)
        self.file_pattern = file_pattern or "*.log"
        self.error_keywords = tuple(k for k in (error_keywords or DEFAULT_ERROR_KEYWORDS) if k)
        self.date_format = date_format or "%Y-%m-%d"
        self.encoding = encoding or "utf-8"
        # require_date_token=False：监控无日期后缀的常驻主日志（如 ok-ef 的
        # ok-script.log）。误判防护靠 _initialize_offsets 记录任务启动时的
        # 文件偏移，只读启动后新增的行。
        self.require_date_token = bool(require_date_token)
        # 读超时：IO 卡住时放弃本轮，绝不死等（见 services.utils.read_file_with_timeout）
        self.read_timeout = max(1.0, float(read_timeout))
        self._offsets: Dict[str, int] = {}
        self._initialize_offsets()

    def _today_token(self) -> str:
        return datetime.now().strftime(self.date_format)

    def _matching_files(self) -> List[Path]:
        if not self.log_dir.exists():
            return []
        if not self.require_date_token:
            return sorted(
                (p for p in self.log_dir.glob(self.file_pattern) if p.is_file()),
                key=lambda item: item.name,
            )
        today_token = self._today_token()
        files = [
            path
            for path in self.log_dir.glob(self.file_pattern)
            if path.is_file() and today_token in path.name
        ]
        return sorted(files, key=lambda item: item.name)

    def _initialize_offsets(self):
        for path in self._matching_files():
            try:
                self._offsets[str(path)] = path.stat().st_size
            except OSError:
                continue

    def poll_new_errors(self) -> Optional[str]:
        """扫描新增日志行，返回第一条命中关键字的行；没有则返回 None。"""
        if not self.error_keywords:
            return None
        for path in self._matching_files():
            key = str(path)
            offset = self._offsets.get(key, 0)
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size < offset:
                # 日志轮转/重建：新文件从头读取
                offset = 0
            if size == offset:
                continue
            chunk, exc = read_file_with_timeout(
                path, offset, self.encoding, self.read_timeout)
            if chunk is None and exc is None:
                # 读超时（疑似盘 IO 卡死）：本轮跳过，offset 不前进去下轮重试
                self.logger.debug(
                    f"[脚本日志监控] 读取超时({self.read_timeout:.0f}s)，本轮跳过: {path.name}")
                continue
            if exc is not None:
                self.logger.debug(f"[脚本日志监控] 读取日志失败 {path.name}: {exc}")
                self._offsets[key] = size
                continue
            self._offsets[key] = size
            for raw_line in chunk.splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                for keyword in self.error_keywords:
                    if keyword in line:
                        return line
        return None


def build_script_error_watchers(config: dict, logger) -> List[ScriptErrorLogWatcher]:
    """从应用配置构建监控器。

    监控项位于 _discovery.monitors 下；顶层同名键（与 discovery 同步的模板位置）
    可作为 abort_on_error / log_dir 的回退来源。
    """
    watchers: List[ScriptErrorLogWatcher] = []
    monitors = (config.get("_discovery") or {}).get("monitors") or {}
    for name, monitor_config in monitors.items():
        if not isinstance(monitor_config, dict):
            continue
        top_level = config.get(name)
        top_level = top_level if isinstance(top_level, dict) else {}
        if not (monitor_config.get("abort_on_error") or top_level.get("abort_on_error")):
            continue
        log_dir = str(monitor_config.get("log_dir", "") or top_level.get("log_dir", "") or "").strip()
        if not log_dir:
            continue
        watchers.append(
            ScriptErrorLogWatcher(
                logger,
                name,
                log_dir,
                file_pattern=str(monitor_config.get("file_pattern", "*.log") or "*.log"),
                error_keywords=(
                    monitor_config.get("error_keywords")
                    or top_level.get("error_keywords")
                    # 兼容旧字段名 error_markers（终末地配置历史命名）
                    or monitor_config.get("error_markers")
                    or top_level.get("error_markers")
                    or None
                ),
                date_format=str(monitor_config.get("date_format", "%Y-%m-%d") or "%Y-%m-%d"),
                encoding=str(monitor_config.get("encoding", "utf-8") or "utf-8"),
                require_date_token=bool(
                    monitor_config.get(
                        "require_date_token",
                        top_level.get("require_date_token", True),
                    )
                ),
                read_timeout=float(
                    monitor_config.get(
                        "read_timeout",
                        top_level.get("read_timeout", 10.0),
                    ) or 10.0
                ),
            )
        )
    return watchers
