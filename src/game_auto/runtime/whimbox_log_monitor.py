from __future__ import annotations

import os
import re
import threading
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

from .process_supervisor import ProcessSupervisor


WRAPPER_PROCESS_NAMES = {
    "cmd.exe",
    "conhost.exe",
    "explorer.exe",
    "powershell.exe",
    "pwsh.exe",
    "python.exe",
    "pythonw.exe",
}


def _is_wrapper_process_name(name: object) -> bool:
    return str(name or "").strip().lower() in WRAPPER_PROCESS_NAMES


def _resolve_exact_game_process_name(config: dict) -> str:
    game_cfg = config.get("game") if isinstance(config.get("game"), dict) else {}
    discovery = config.get("_discovery") if isinstance(config.get("_discovery"), dict) else {}

    candidates = [
        game_cfg.get("process_name"),
        os.path.basename(str(game_cfg.get("exe_path", "") or "").strip()),
        discovery.get("game_process_name"),
        os.path.basename(str(discovery.get("game_exe_path", "") or "").strip()),
    ]
    for candidate in candidates:
        name = str(candidate or "").strip()
        if name and not _is_wrapper_process_name(name):
            return name
    return ""


class FileCompletionMonitor:
    RESULT_HEADER_MARKER = "一条龙任务完成: 任务结果如下："
    LOG_LINE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?\s+\|")

    def __init__(
        self,
        logger,
        log_dir: str,
        file_pattern: str,
        completion_markers: Iterable[str],
        *,
        poll_interval: float = 2.0,
        encoding: str = "utf-8",
        on_complete: Optional[Callable[["FileCompletionMonitor"], None]] = None,
    ):
        self.logger = logger
        self.log_dir = Path(log_dir)
        self.file_pattern = file_pattern
        self.completion_markers = [str(marker) for marker in completion_markers if str(marker).strip()]
        self.poll_interval = max(0.5, float(poll_interval))
        self.encoding = encoding or "utf-8"
        self.on_complete = on_complete

        self.stop_event = threading.Event()
        self.completion_event = threading.Event()
        self.task_summary_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._offsets: dict[str, int] = {}
        self._capture_result_block = False
        self._completion_callback_invoked = False

        self.matched_file = ""
        self.matched_marker = ""
        self.matched_line = ""
        self.task_results: Dict[str, List[str]] = {
            "completed": [],
            "skipped": [],
            "failed": [],
        }

    def start(self) -> "FileCompletionMonitor":
        if self._thread and self._thread.is_alive():
            return self
        self._offsets = self._snapshot_offsets()
        self.logger.info(
            f"[CJ流程] 开始监控奇想盒日志完成标记: {self.log_dir}\\{self.file_pattern}"
        )
        self._thread = threading.Thread(target=self._watch_loop, name="WhimboxLogMonitor", daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout: float = 2.0):
        self.stop_event.set()
        if self._thread and self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout=timeout)

    def has_task_results(self) -> bool:
        return any(self.task_results.values())

    def wait_for_task_results(self, timeout: float = 0.0) -> bool:
        if self.has_task_results():
            return True
        if timeout <= 0:
            return False
        return self.task_summary_event.wait(timeout)

    def _matching_files(self) -> list[Path]:
        if not self.log_dir.exists():
            return []
        files = []
        for path in self.log_dir.glob(self.file_pattern):
            if not path.is_file():
                continue
            files.append(path)
        return sorted(files, key=lambda item: (item.stat().st_mtime, item.name))

    def _snapshot_offsets(self) -> dict[str, int]:
        offsets: dict[str, int] = {}
        for path in self._matching_files():
            try:
                offsets[str(path)] = path.stat().st_size
            except OSError:
                continue
        return offsets

    def _maybe_invoke_completion_callback(self):
        if self._completion_callback_invoked or self.on_complete is None:
            return
        self._completion_callback_invoked = True
        try:
            self.on_complete(self)
        except Exception as exc:
            self.logger.error(f"[CJ流程] 奇想盒完成回调执行失败: {str(exc)}")

    def _parse_task_status_line(self, line: str) -> Optional[tuple[str, str]]:
        candidates = (
            ("✅", "completed", ("已完成",)),
            ("⏭️", "skipped", ("已跳过",)),
            ("⏭", "skipped", ("已跳过",)),
            ("❌", "failed", ("未完成", "失败", "已失败")),
        )
        for prefix, bucket, suffixes in candidates:
            if not line.startswith(prefix):
                continue
            task_name = line[len(prefix):].strip()
            for suffix in suffixes:
                if task_name.endswith(suffix):
                    task_name = task_name[: -len(suffix)].strip()
                    break
            task_name = task_name.strip("-:： ")
            if not task_name:
                return None
            return bucket, task_name
        return None

    def _record_task_result(self, bucket: str, task_name: str):
        if task_name in self.task_results[bucket]:
            return
        self.task_results[bucket].append(task_name)
        self.task_summary_event.set()

    def _process_line(self, path: Path, line: str):
        if self.RESULT_HEADER_MARKER in line:
            self._capture_result_block = True

        marker = next((item for item in self.completion_markers if item in line), None)
        if marker and not self.completion_event.is_set():
            self.matched_file = str(path)
            self.matched_marker = marker
            self.matched_line = line
            self.completion_event.set()
            self.logger.info(f"[CJ流程] 检测到奇想盒完成标记: {marker} ({path.name})")
            self._maybe_invoke_completion_callback()

        if not self._capture_result_block:
            return

        parsed = self._parse_task_status_line(line)
        if parsed is not None:
            bucket, task_name = parsed
            self._record_task_result(bucket, task_name)
            return

        if self.LOG_LINE_PATTERN.match(line) and self.has_task_results():
            self._capture_result_block = False

    def _watch_loop(self):
        missing_dir_warned = False
        while not self.stop_event.is_set():
            if not self.log_dir.exists():
                if not missing_dir_warned:
                    self.logger.warning(f"[CJ流程] 奇想盒日志目录不存在，继续等待: {self.log_dir}")
                    missing_dir_warned = True
                self.stop_event.wait(self.poll_interval)
                continue

            for path in self._matching_files():
                file_key = str(path)
                previous_offset = self._offsets.get(file_key, 0)
                try:
                    current_size = path.stat().st_size
                except OSError:
                    continue

                if current_size < previous_offset:
                    previous_offset = 0

                if current_size == previous_offset:
                    self._offsets[file_key] = previous_offset
                    continue

                try:
                    with open(path, "r", encoding=self.encoding, errors="ignore") as handle:
                        handle.seek(previous_offset)
                        chunk = handle.read()
                except OSError as exc:
                    self.logger.debug(f"[CJ流程] 读取奇想盒日志失败 {path}: {str(exc)}")
                    continue

                self._offsets[file_key] = current_size
                if not chunk:
                    continue

                for raw_line in chunk.splitlines():
                    line = raw_line.strip()
                    if not line:
                        continue
                    self._process_line(path, line)

            self.stop_event.wait(self.poll_interval)


def format_whimbox_task_summary(monitor: Optional[FileCompletionMonitor]) -> List[str]:
    if monitor is None:
        return []

    completed = monitor.task_results.get("completed", [])
    skipped = monitor.task_results.get("skipped", [])
    failed = monitor.task_results.get("failed", [])
    if not completed and not skipped and not failed:
        if monitor.completion_event.is_set():
            return ["  奇想盒任务结果：已检测到一条龙结束，但还没从日志里解析到明细。"]
        return []

    lines = [
        f"  奇想盒任务结果：已完成 {len(completed)} 项，已跳过 {len(skipped)} 项，未完成 {len(failed)} 项",
    ]
    if completed:
        lines.append(f"  已完成：{'、'.join(completed)}")
    if skipped:
        lines.append(f"  已跳过：{'、'.join(skipped)}")
    if failed:
        lines.append(f"  未完成：{'、'.join(failed)}")
    return lines


def start_infinity_nikki_log_monitor(
    config: dict,
    logger,
    *,
    stop_game_on_completion: bool = True,
) -> Optional[FileCompletionMonitor]:
    monitor_config = config.get("whimbox_log_monitor") or {}
    log_dir = str(monitor_config.get("log_dir", "") or "").strip()
    markers = monitor_config.get("completion_markers") or []
    if not log_dir or not markers:
        logger.warning("[CJ流程] 未配置奇想盒日志监控，跳过日志完成触发")
        return None

    def _stop_game_after_completion(monitor: FileCompletionMonitor):
        game_process_name = _resolve_exact_game_process_name(config)
        if not stop_game_on_completion:
            logger.info("[CJ流程] 检测到奇想盒完成标记，但当前批次未启用重聚前台流程，跳过自动结束暖暖")
            return
        if not game_process_name:
            logger.warning("[CJ流程] 未解析到暖暖精确游戏进程名，无法根据日志完成自动结束游戏")
            return

        logger.info(
            f"[CJ流程] 奇想盒完成回调触发，准备按精确进程名结束暖暖: {game_process_name} "
            f"(来源: {Path(monitor.matched_file).name if monitor.matched_file else '未知日志'})"
        )
        matched_procs = ProcessSupervisor.find_processes(game_process_name, exact_name=True)
        if not matched_procs:
            logger.info("[CJ流程] 检测到奇想盒完成标记，但暖暖游戏进程已退出")
            return

        logger.info(f"[CJ流程] 奇想盒一条龙已完成，终止暖暖游戏进程以切换到下一步，共命中 {len(matched_procs)} 个进程")
        for proc in matched_procs:
            ProcessSupervisor.safe_kill_process(proc.pid)

    def _handle_complete(monitor: FileCompletionMonitor):
        worker = threading.Thread(
            target=_stop_game_after_completion,
            args=(monitor,),
            name="WhimboxCompletionStopGame",
            daemon=True,
        )
        worker.start()

    return FileCompletionMonitor(
        logger,
        log_dir=log_dir,
        file_pattern=str(monitor_config.get("file_pattern", "whimbox-*.log") or "whimbox-*.log"),
        completion_markers=markers,
        poll_interval=float(monitor_config.get("poll_interval", 2.0) or 2.0),
        encoding=str(monitor_config.get("encoding", "utf-8") or "utf-8"),
        on_complete=_handle_complete,
    ).start()