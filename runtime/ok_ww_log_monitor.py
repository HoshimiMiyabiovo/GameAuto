from __future__ import annotations

import ctypes
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

from PIL import Image

from .foreground_flow_runner import (
    _find_window_by_filters,
    _locate_template_on_screen,
    _locate_with_ocr_fallback,
    _ocr_locate_in_window,
    _window_rect,
)
from .process_supervisor import ProcessSupervisor
from ..settings.paths import TEMPLATES_DIR


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


def _resolve_exact_process_name(config: dict, role: str) -> str:
    discovery = config.get("_discovery") if isinstance(config.get("_discovery"), dict) else {}

    if role == "game":
        game_cfg = config.get("game") if isinstance(config.get("game"), dict) else {}
        candidates = [
            game_cfg.get("process_name"),
            os.path.basename(str(game_cfg.get("exe_path", "") or "").strip()),
            discovery.get("game_process_name"),
            os.path.basename(str(discovery.get("game_exe_path", "") or "").strip()),
        ]
    else:
        candidates = [
            config.get("script_process_name"),
            os.path.basename(str(config.get("launcher", "") or "").strip()),
            discovery.get("script_process_name"),
            os.path.basename(str(discovery.get("script_exe_path", "") or "").strip()),
        ]

    for candidate in candidates:
        name = str(candidate or "").strip()
        if name and not _is_wrapper_process_name(name):
            return name
    return ""


def _resolve_template_path(template_value: object, default_name: str) -> str:
    template_path = str(template_value or "").strip()
    if template_path:
        return template_path
    return str(TEMPLATES_DIR / default_name)


def _window_background_click(hwnd: int, x_local: int, y_local: int) -> bool:
    rect = _window_rect(hwnd)
    if not rect:
        return False
    _, _, width, height = rect
    if x_local < 0 or y_local < 0 or x_local > width or y_local > height:
        return False

    lparam = ((int(y_local) & 0xFFFF) << 16) | (int(x_local) & 0xFFFF)
    user32 = ctypes.windll.user32
    user32.PostMessageW(hwnd, 0x0200, 0, lparam)
    user32.PostMessageW(hwnd, 0x0201, 0x0001, lparam)
    time.sleep(0.05)
    user32.PostMessageW(hwnd, 0x0202, 0, lparam)
    return True


def _locate_window_target(
    logger,
    hwnd: int,
    template_path: str,
    target_text: object,
    *,
    timeout_sec: float,
):
    scales = (1.0, 0.96, 1.04)
    confidences = (0.92, 0.88, 0.84)
    end = time.monotonic() + max(1.0, float(timeout_sec))

    while time.monotonic() < end:
        center, method = _locate_with_ocr_fallback(
            logger=logger,
            hwnd=hwnd,
            template_path=template_path,
            target_text=target_text,
            scales=scales,
            confidences=confidences,
            enable_ocr=True,
        )
        if center is not None:
            return center, method or "template"
        time.sleep(0.4)

    return None, None


def _build_row_search_region(hwnd: int, anchor_local: tuple[int, int], row_search_height: int):
    rect = _window_rect(hwnd)
    if not rect:
        return None
    left, top, width, height = rect
    half_height = max(int(row_search_height // 2), 40)
    row_top = max(top, top + int(anchor_local[1]) - half_height)
    row_bottom = min(top + height, top + int(anchor_local[1]) + half_height)
    return (left, row_top, width, max(1, row_bottom - row_top))


def _locate_row_start_button(
    logger,
    hwnd: int,
    daily_center_local: tuple[int, int],
    *,
    start_template: str,
    start_target_text: object,
    row_search_height: int,
    timeout_sec: float,
):
    rect = _window_rect(hwnd)
    if not rect:
        return None, None
    left, top, _, _ = rect
    region = _build_row_search_region(hwnd, daily_center_local, row_search_height)
    if not region:
        return None, None

    scales = (1.0, 0.96, 1.04)
    confidences = (0.92, 0.88, 0.84)
    end = time.monotonic() + max(1.0, float(timeout_sec))

    while time.monotonic() < end:
        screen_center = _locate_template_on_screen(start_template, region, scales, confidences)
        if screen_center:
            return (screen_center[0] - left, screen_center[1] - top), "template"

        ocr_center = _ocr_locate_in_window(
            logger,
            hwnd,
            start_target_text,
            search_region=region,
        )
        if ocr_center is not None:
            return ocr_center, "ocr"

        time.sleep(0.4)

    return None, None


def _run_nte_ocr_click(
    logger,
    config: dict,
    monitor_config: dict,
    *,
    reason: str = "",
    hwnd_override: Optional[int] = None,
) -> bool:
    fallback_cfg = monitor_config.get("ui_stall_fallback") or {}
    if not isinstance(fallback_cfg, dict) or not fallback_cfg.get("enabled", False):
        return False

    # 游戏进程已在运行则跳过OCR流程，避免误杀
    game_process_name = _resolve_exact_process_name(config, "game")
    if game_process_name and ProcessSupervisor.find_processes(game_process_name, exact_name=True):
        logger.info(f"[NTE启动] 游戏进程 {game_process_name} 已在运行，跳过 OCR 点击流程")
        return True

    daily_weekly_template = _resolve_template_path(fallback_cfg.get("daily_weekly_template"), "ok-nte 日常周常.png")
    daily_weekly_target_text = fallback_cfg.get("daily_weekly_target_text") or "日常周常"
    daily_task_template = _resolve_template_path(fallback_cfg.get("daily_task_template"), "ok-nte 日常任务-2.png")
    daily_task_target_text = fallback_cfg.get("daily_task_target_text") or "日常任务"
    start_template = _resolve_template_path(fallback_cfg.get("start_template"), "ok-nte 开始.png")
    start_target_text = fallback_cfg.get("start_target_text") or "开始"
    search_timeout_sec = max(3.0, float(fallback_cfg.get("search_timeout_sec", 12) or 12))
    post_task_click_wait_sec = max(0.5, float(fallback_cfg.get("post_task_click_wait_sec", 1.5) or 1.5))
    row_search_height = max(60, int(fallback_cfg.get("row_search_height", 120) or 120))
    configured_window_names = [
        str(name).strip()
        for name in (fallback_cfg.get("window_process_names") or [])
        if str(name).strip()
    ]
    script_process_name = _resolve_exact_process_name(config, "script")
    window_find_timeout_sec = max(5.0, float(fallback_cfg.get("window_find_timeout_sec", 30) or 30))
    title_keywords = [str(k).strip() for k in (fallback_cfg.get("title_keywords") or ["ok-nte"]) if str(k).strip()]

    if hwnd_override is not None:
        hwnd = hwnd_override
        logger.info(f"[NTE启动] 使用预置窗口 (HWND: {hwnd})，{reason}")
    else:
        window_process_names = list(configured_window_names)
        if script_process_name and script_process_name not in window_process_names:
            window_process_names.append(script_process_name)

        # 等待 ok-nte 窗口出现（启动后窗口可能需要时间创建）
        # ok-nte 实际以 pythonw.exe 运行，先按进程名找，再按窗口标题兜底
        hwnd = None
        wait_end = time.monotonic() + window_find_timeout_sec
        while time.monotonic() < wait_end:
            fallback = _find_window_by_filters(process_names=window_process_names or None)
            if fallback:
                hwnd, _ = fallback
                break
            # 按标题关键词兜底（ok-nte 实际进程是 pythonw.exe）
            fallback = _find_window_by_filters(title_keywords=title_keywords)
            if fallback:
                hwnd, _ = fallback
                logger.info(f"[NTE启动] 通过标题关键词匹配到 ok-nte 窗口 (HWND: {hwnd})")
                break
            logger.info(f"[NTE启动] 等待 ok-nte 窗口出现...")
            time.sleep(2.0)

        if hwnd is None:
            logger.warning(f"[NTE启动] {reason}，但未找到 ok-nte 可见窗口 (已等待 {window_find_timeout_sec:.0f}秒)")
            return False

    prefix = "[NTE启动]" if reason else "[OK监控]"
    if reason:
        logger.info(f"{prefix} {reason}，执行 OCR 点击启动流程 (HWND: {hwnd})")

    # 第一步：点击"日常周常"进入模块
    daily_weekly_center, daily_weekly_method = _locate_window_target(
        logger,
        hwnd,
        daily_weekly_template,
        daily_weekly_target_text,
        timeout_sec=search_timeout_sec,
    )
    if daily_weekly_center is None:
        logger.warning(f'{prefix} 未找到"日常周常"入口')
        return False
    if not _window_background_click(hwnd, *daily_weekly_center):
        logger.warning(f'{prefix} 后台点击"日常周常"入口失败')
        return False
    logger.info(f'{prefix} 已点击"日常周常" [方式: {daily_weekly_method}] @ {daily_weekly_center}')
    time.sleep(post_task_click_wait_sec)

    # 第二步：找"日常任务"，重试等待页面加载
    daily_task_center = None
    daily_task_method = None
    for attempt in range(1, 4):
        daily_task_center, daily_task_method = _locate_window_target(
            logger,
            hwnd,
            daily_task_template,
            daily_task_target_text,
            timeout_sec=search_timeout_sec,
        )
        if daily_task_center is not None:
            break
        if attempt < 3:
            logger.info(f'{prefix} 第{attempt}次未找到"日常任务"，等待页面加载后重试...')
            time.sleep(post_task_click_wait_sec)
    if daily_task_center is None:
        logger.warning(f'{prefix} 重试3次后仍未找到"日常任务"')
        return False
    logger.info(f'{prefix} 已找到"日常任务" [方式: {daily_task_method}] @ {daily_task_center}')

    # 第三步：点击"日常任务"同一行的"开始"按钮
    start_center, start_method = _locate_row_start_button(
        logger,
        hwnd,
        daily_task_center,
        start_template=start_template,
        start_target_text=start_target_text,
        row_search_height=row_search_height,
        timeout_sec=search_timeout_sec,
    )
    if start_center is None:
        logger.warning(f'{prefix} 未找到"日常任务"同一行的"开始"按钮')
        return False
    if not _window_background_click(hwnd, *start_center):
        logger.warning(f'{prefix} 后台点击"开始"按钮失败')
        return False
    logger.info(f'{prefix} 已点击"日常任务"同行"开始"按钮 [方式: {start_method}] @ {start_center}')
    return True


def _build_ui_stall_fallback(config: dict, logger, monitor_config: dict):
    fallback_cfg = monitor_config.get("ui_stall_fallback") or {}
    if not isinstance(fallback_cfg, dict) or not fallback_cfg.get("enabled", False):
        return None

    def _run_fallback(_monitor: "OkScriptErrorMonitor", stall_reason: str) -> bool:
        return _run_nte_ocr_click(
            logger,
            config,
            monitor_config,
            reason=f"{stall_reason}，尝试执行 ok-nte 界面兜底",
        )

    return _run_fallback


def run_nte_post_launch_ocr_click(config: dict, logger) -> bool:
    monitor_config = _get_monitor_config(config)
    return _run_nte_ocr_click(
        logger,
        config,
        monitor_config,
        reason="启动 ok-nte 后 OCR 点击启动任务",
    )


def _get_monitor_config(config: dict, monitor_config_key: str = "ok_script_log_monitor") -> dict:
    if monitor_config_key and config.get(monitor_config_key):
        return config.get(monitor_config_key) or {}
    if config.get("ok_script_log_monitor"):
        return config.get("ok_script_log_monitor") or {}
    if config.get("ok_ww_log_monitor"):
        return config.get("ok_ww_log_monitor") or {}
    return {}


@dataclass
class OkScriptAttemptResult:
    success: bool
    message: str
    duration: int
    restart_requested: bool
    error_detected: bool = False
    failure_reason: str = ""
    error_file: str = ""
    error_line: str = ""


WutheringWavesAttemptResult = OkScriptAttemptResult


class OkScriptErrorMonitor:
    def __init__(
        self,
        logger,
        log_dir: str,
        file_pattern: str,
        error_markers: Iterable[str],
        *,
        script_name: str,
        stall_after_error_sec: float = 180.0,
        stall_without_update_sec: float = 180.0,
        poll_interval: float = 2.0,
        encoding: str = "utf-8",
        on_restart_requested: Optional[Callable[["OkScriptErrorMonitor"], None]] = None,
        on_stall_fallback: Optional[Callable[["OkScriptErrorMonitor", str], bool]] = None,
        completion_markers: Optional[Iterable[str]] = None,
        ignore_error_patterns: Optional[Iterable[str]] = None,
    ):
        self.logger = logger
        self.log_dir = Path(log_dir)
        self.file_pattern = file_pattern
        self.error_markers = [str(marker) for marker in error_markers if str(marker).strip()]
        self.ignore_error_patterns = [str(p) for p in (ignore_error_patterns or []) if str(p).strip()]
        self.script_name = script_name or "ok-script"
        self.stall_after_error_sec = max(1.0, float(stall_after_error_sec))
        self.stall_without_update_sec = max(1.0, float(stall_without_update_sec))
        self.poll_interval = max(0.5, float(poll_interval))
        self.encoding = encoding or "utf-8"
        self.on_restart_requested = on_restart_requested
        self.on_stall_fallback = on_stall_fallback
        self.completion_markers = [str(marker) for marker in (completion_markers or []) if str(marker).strip()]

        self.stop_event = threading.Event()
        self.error_detected_event = threading.Event()
        self.restart_requested_event = threading.Event()
        self.completed_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._offsets: dict[str, int] = {}
        self.started_at = 0.0
        self.last_activity_at = 0.0
        self.saw_matching_file = False

        self.first_error_file = ""
        self.first_error_line = ""
        self.last_error_line = ""
        self.failure_reason = ""
        self.stall_fallback_attempted = False

    def start(self) -> "OkScriptErrorMonitor":
        if self._thread and self._thread.is_alive():
            return self
        self.started_at = time.monotonic()
        self._offsets = self._snapshot_offsets()
        self.saw_matching_file = bool(self._offsets)
        self.last_activity_at = self.started_at if self.saw_matching_file else 0.0
        self.logger.info(f"[OK监控] 开始监控 {self.script_name} 日志: {self.log_dir}\\{self.file_pattern}")
        self._thread = threading.Thread(target=self._watch_loop, name=f"OkScriptLogMonitor-{self.script_name}", daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout: float = 2.0):
        self.stop_event.set()
        if self._thread and self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout=timeout)

    def should_retry(self) -> bool:
        return self.restart_requested_event.is_set()

    def _matching_files(self) -> list[Path]:
        if not self.log_dir.exists():
            return []
        files = [path for path in self.log_dir.glob(self.file_pattern) if path.is_file()]
        return sorted(files, key=lambda item: (item.stat().st_mtime, item.name))

    def _snapshot_offsets(self) -> dict[str, int]:
        offsets: dict[str, int] = {}
        for path in self._matching_files():
            try:
                offsets[str(path)] = path.stat().st_size
            except OSError:
                continue
        return offsets

    def _mark_error(self, path: Path, line: str):
        now = time.monotonic()
        if not self.error_detected_event.is_set():
            self.first_error_file = str(path)
            self.first_error_line = line
            self.logger.warning(f"[OK监控] 检测到 {self.script_name} 错误日志: {path.name}")
        self.error_detected_event.set()
        self.last_error_line = line
        self.last_activity_at = now

    def _process_line(self, path: Path, line: str):
        if not line:
            return
        self.last_activity_at = time.monotonic()

        if self.completion_markers:
            completion_marker = next((m for m in self.completion_markers if m in line), None)
            if completion_marker:
                self.completed_event.set()
                self.logger.info(f"[OK监控] {self.script_name} 检测到完成标记: {line[:200]}")
                return

        marker = next((item for item in self.error_markers if item in line), None)
        if marker:
            ignored = any(pat in line for pat in self.ignore_error_patterns)
            if ignored:
                self.logger.debug(f"[OK监控] 忽略已知非致命错误: {line[:200]}")
            else:
                self._mark_error(path, line)

    def _request_restart(self, reason: str):
        if self.restart_requested_event.is_set():
            return
        self.failure_reason = reason
        self.restart_requested_event.set()
        self.logger.warning(f"[OK监控] {reason}，准备终止脚本和游戏以便重试")
        if self.on_restart_requested is not None:
            try:
                self.on_restart_requested(self)
            except Exception as exc:
                self.logger.error(f"[OK监控] 执行重试回调失败: {str(exc)}")

    def _maybe_run_stall_fallback(self, reason: str) -> bool:
        if self.stall_fallback_attempted or self.on_stall_fallback is None:
            return False

        self.stall_fallback_attempted = True
        try:
            handled = bool(self.on_stall_fallback(self, reason))
        except Exception as exc:
            self.logger.error(f"[OK监控] 执行界面兜底失败: {str(exc)}")
            return False

        if handled:
            self.last_activity_at = time.monotonic()
            self.logger.info("[OK监控] 界面兜底已执行，继续等待 ok 脚本恢复日志输出")
            return True
        return False

    def _maybe_trigger_restart(self):
        if self.restart_requested_event.is_set() or self.completed_event.is_set():
            return

        now = time.monotonic()
        if self.error_detected_event.is_set() and self.last_activity_at:
            idle_seconds = now - self.last_activity_at
            if idle_seconds >= self.stall_after_error_sec:
                reason = f"{self.script_name} 日志报错后停滞约 {int(idle_seconds)} 秒"
                if self.first_error_line:
                    reason = f"{reason}；首条异常：{self.first_error_line}"
                if self._maybe_run_stall_fallback(reason):
                    return
                self._request_restart(reason)
            return

        # 已移除 stall_without_update_sec 逻辑：脚本正常运行时不一定持续输出日志，
        # 仅在检测到错误且停滞时才触发兜底/重启（见上方 error_detected_event 分支）

    def _watch_loop(self):
        missing_dir_warned = False
        last_heartbeat_at = 0.0
        heartbeat_interval = 300.0
        while not self.stop_event.is_set():
            if not self.log_dir.exists():
                if not missing_dir_warned:
                    self.logger.warning(f"[OK监控] {self.script_name} 日志目录不存在，继续等待: {self.log_dir}")
                    missing_dir_warned = True
                self.stop_event.wait(self.poll_interval)
                continue

            files = self._matching_files()
            if files:
                self.saw_matching_file = True

            for path in files:
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
                    self.logger.debug(f"[OK监控] 读取 {self.script_name} 日志失败 {path}: {str(exc)}")
                    continue

                self._offsets[file_key] = current_size
                if not chunk:
                    continue

                for raw_line in chunk.splitlines():
                    self._process_line(path, raw_line.strip())

            self._maybe_trigger_restart()

            now = time.monotonic()
            if now - last_heartbeat_at >= heartbeat_interval:
                elapsed = int(now - self.started_at)
                state = (
                    "completed" if self.completed_event.is_set() else
                    "error" if self.error_detected_event.is_set() else
                    "running"
                )
                self.logger.info(
                    f"[OK监控] 心跳 {self.script_name} state={state} elapsed={elapsed}s "
                    f"files={len(files)} last_error={self.last_error_line[:120] if self.last_error_line else '-'}"
                )
                last_heartbeat_at = now

            self.stop_event.wait(self.poll_interval)

        self.logger.info(
            f"[OK监控] {self.script_name} 监控线程退出 "
            f"completed={self.completed_event.is_set()} "
            f"error={self.error_detected_event.is_set()} "
            f"restart={self.restart_requested_event.is_set()}"
        )


def start_ok_script_log_monitor(
    config: dict,
    logger,
    *,
    monitor_config_key: str = "ok_script_log_monitor",
) -> Optional[OkScriptErrorMonitor]:
    monitor_config = _get_monitor_config(config, monitor_config_key)
    log_dir = str(monitor_config.get("log_dir", "") or "").strip()
    if not log_dir:
        logger.warning("[OK监控] 未配置 ok 系列日志目录，跳过日志监控")
        return None

    script_name = str(config.get("script_name", os.path.basename(str(config.get("launcher", "") or ""))) or "ok-script")
    game_process_name = _resolve_exact_process_name(config, "game")
    script_process_name = _resolve_exact_process_name(config, "script")
    cleanup_processes = [str(name).strip() for name in (config.get("cleanup_processes") or []) if str(name).strip()]
    ui_stall_fallback = _build_ui_stall_fallback(config, logger, monitor_config)

    process_names = []
    for name in [script_process_name, game_process_name, *cleanup_processes]:
        if name and name not in process_names:
            process_names.append(name)

    def _kill_named_processes(name: str):
        if not name:
            return
        for proc in ProcessSupervisor.find_processes(name, exact_name=True):
            try:
                proc_name = proc.name()
            except Exception:
                proc_name = name
            logger.info(f"[OK监控] 终止进程: {proc_name} (PID: {proc.pid})")
            ProcessSupervisor.safe_kill_process(proc.pid)

    def _handle_restart_requested(_monitor: OkScriptErrorMonitor):
        # 游戏进程已在运行则跳过重启，避免误杀
        if game_process_name and ProcessSupervisor.find_processes(game_process_name, exact_name=True):
            logger.info(f"[OK监控] 游戏进程 {game_process_name} 已在运行，跳过重启杀进程")
            return
        for process_name in process_names:
            _kill_named_processes(process_name)

    return OkScriptErrorMonitor(
        logger,
        log_dir=log_dir,
        file_pattern=str(monitor_config.get("file_pattern", "app.*") or "app.*"),
        error_markers=monitor_config.get("error_markers") or [],
        ignore_error_patterns=monitor_config.get("ignore_error_patterns") or [],
        completion_markers=monitor_config.get("completion_markers"),
        script_name=script_name,
        stall_after_error_sec=float(monitor_config.get("stall_after_error_sec", 180) or 180),
        stall_without_update_sec=float(monitor_config.get("stall_without_update_sec", 180) or 180),
        poll_interval=float(monitor_config.get("poll_interval", 2.0) or 2.0),
        encoding=str(monitor_config.get("encoding", "utf-8") or "utf-8"),
        on_restart_requested=_handle_restart_requested,
        on_stall_fallback=ui_stall_fallback,
    ).start()


def run_ok_script_with_retry(
    controller,
    config: dict,
    script_choice: Optional[str] = None,
    *,
    app_key: str,
    before_retry: Optional[Callable[[int, float, OkScriptAttemptResult], None]] = None,
    monitor_config_key: str = "ok_script_log_monitor",
) -> list[OkScriptAttemptResult]:
    monitor_config = _get_monitor_config(config, monitor_config_key)
    max_retries = max(0, int(monitor_config.get("max_retries", 1) or 1))
    restart_delay = max(0.0, float(monitor_config.get("restart_delay_sec", 10) or 10))
    script_name = str(config.get("script_name", app_key) or app_key)
    display_name = str(config.get("display_name", app_key) or app_key)
    attempts: list[OkScriptAttemptResult] = []

    for attempt_index in range(max_retries + 1):
        monitor = start_ok_script_log_monitor(config, controller.logger, monitor_config_key=monitor_config_key)
        try:
            success, message, duration = controller.execute_task(app_key, script_choice)
        finally:
            if monitor is not None:
                monitor.stop()

        attempt = OkScriptAttemptResult(
            success=success,
            message=message,
            duration=duration,
            restart_requested=bool(monitor and monitor.should_retry()),
            error_detected=bool(monitor and monitor.error_detected_event.is_set()),
            failure_reason=monitor.failure_reason if monitor else "",
            error_file=monitor.first_error_file if monitor else "",
            error_line=monitor.first_error_line if monitor else "",
        )
        if not attempt.failure_reason and attempt.error_detected and attempt.error_line:
            attempt.failure_reason = f"{script_name} 日志检测到异常；首条异常：{attempt.error_line}"
        attempts.append(attempt)

        should_retry = (
            (attempt.restart_requested or (attempt.error_detected and not attempt.success and not (monitor and monitor.completed_event.is_set())))
            and attempt_index < max_retries
            and not controller.stop_current_task
        )
        if not should_retry:
            break

        controller.logger.warning(
            f"[OK监控] 检测到 {script_name} 日志异常，{int(restart_delay)} 秒后自动重试{display_name}任务 (第 {attempt_index + 2} 次尝试)"
        )
        if before_retry is not None:
            before_retry(attempt_index + 1, restart_delay, attempt)
        time.sleep(restart_delay)

    return attempts


def start_wuthering_waves_log_monitor(config: dict, logger) -> Optional[OkScriptErrorMonitor]:
    return start_ok_script_log_monitor(config, logger, monitor_config_key="ok_ww_log_monitor")


def run_wuthering_waves_with_retry(
    controller,
    config: dict,
    script_choice: Optional[str] = None,
    *,
    app_key: str = "WutheringWaves",
    before_retry: Optional[Callable[[int, float, WutheringWavesAttemptResult], None]] = None,
) -> list[WutheringWavesAttemptResult]:
    return run_ok_script_with_retry(
        controller,
        config,
        script_choice=script_choice,
        app_key=app_key,
        before_retry=before_retry,
        monitor_config_key="ok_ww_log_monitor",
    )