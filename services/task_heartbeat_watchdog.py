"""任务线程心跳看门狗。

背景（2026-10-15 蓝屏复盘）：GUI 主线程看门狗（hang_watchdog）只监控
主线程心跳，而真正跑任务的 execute_task 线程如果卡死（盘 IO 挂起、
驱动事故、死锁），主线程 QTimer 照常心跳 —— 看门狗完全无感，
表现为"日志停在某时刻、界面显示任务进行中、实际已死"。

本组件补上这个洞：任务线程在 _monitor_game 循环每轮调用 beat()，
超过 stall_timeout 没有心跳即判定任务线程卡死：
  1. faulthandler.dump_traceback 全线程栈写 watchdog_task_heartbeat.log（留现场）
  2. logger.error 记录
  3. 执行止损回调（置停止标志 + 杀进程树），让任务批次继续走流程

注意：心跳超时只代表"任务线程没有进展"，不代表进程已死；
止损动作（杀进程树）由调用方提供的回调执行，本组件不直接杀进程。
栈文件与主线程卡死的 watchdog_main_thread.log 分开，避免两类硬故障混在一起
事后无法区分；超过 MAX_DUMP_BYTES 时归档为带时间戳的副本，不删除历史现场。
"""
from __future__ import annotations

import faulthandler
import logging
import threading
import time
from pathlib import Path
from typing import Callable, Optional

# dump 文件超过该大小时归档为带时间戳的副本，避免单个文件无限增长且不丢历史
MAX_DUMP_BYTES = 5 * 1024 * 1024


class TaskHeartbeatWatchdog:
    """任务线程心跳监控（单任务周期：start -> beat... -> stop）。"""

    def __init__(
        self,
        logger: logging.Logger,
        dump_path: Path,
        *,
        stall_timeout: float = 600.0,
        check_interval: float = 30.0,
    ):
        self.logger = logger
        self.dump_path = Path(dump_path)
        self.stall_timeout = max(30.0, float(stall_timeout))
        self.check_interval = max(5.0, float(check_interval))
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_beat = 0.0
        self._task_key = ""
        self._task_label = ""
        self._stall_callback: Optional[Callable[[str, str], None]] = None
        self._fired = False

    # ── 任务线程调用 ──
    def beat(self) -> None:
        """上报心跳（任务线程每轮循环调用）。"""
        with self._lock:
            self._last_beat = time.monotonic()

    def start(
        self,
        task_key: str,
        on_stall: Callable[[str, str], None],
        *,
        task_label: str = "",
    ) -> None:
        """开始监控一个任务。on_stall(task_key, task_label) 在看门狗线程执行。"""
        self.stop()
        with self._lock:
            self._task_key = str(task_key or "")
            self._task_label = str(task_label or task_key or "")
            self._stall_callback = on_stall
            self._fired = False
            self._last_beat = time.monotonic()
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop, name="TaskHeartbeatWatchdog", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """结束监控（任务结束时调用，幂等）。"""
        self._stop_event.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    # ── 看门狗线程 ──
    def _loop(self) -> None:
        while not self._stop_event.wait(self.check_interval):
            with self._lock:
                last_beat = self._last_beat
                fired = self._fired
                task_key = self._task_key
                task_label = self._task_label
                callback = self._stall_callback
            if fired:
                continue
            stall = time.monotonic() - last_beat
            if stall < self.stall_timeout:
                continue
            with self._lock:
                if self._fired:
                    continue
                self._fired = True
            self._on_stall(task_key, task_label, stall, callback)

    def _on_stall(
        self,
        task_key: str,
        task_label: str,
        stall: float,
        callback: Optional[Callable[[str, str], None]],
    ) -> None:
        self.logger.error(
            "[任务看门狗] %s 任务线程已 %.0f 秒无心跳（阈值 %.0f 秒），"
            "判定任务线程卡死，全线程栈已写入 %s",
            task_label or task_key or "?",
            stall,
            self.stall_timeout,
            self.dump_path,
        )
        try:
            self.dump_path.parent.mkdir(parents=True, exist_ok=True)
            if (
                self.dump_path.exists()
                and self.dump_path.stat().st_size > MAX_DUMP_BYTES
            ):
                archive = self.dump_path.with_name(
                    f"{self.dump_path.stem}_{time.strftime('%Y%m%d_%H%M%S')}{self.dump_path.suffix}"
                )
                try:
                    self.dump_path.replace(archive)
                except OSError as exc:
                    self.logger.debug("[任务看门狗] 归档旧 dump 失败: %s", exc)
            with open(self.dump_path, "a", encoding="utf-8") as fh:
                fh.write(
                    f"\n{'=' * 72}\n"
                    f"[任务看门狗] {task_label or task_key} 任务线程 {stall:.0f}s 无心跳"
                    f"｜{time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"{'=' * 72}\n"
                )
                faulthandler.dump_traceback(fh, all_threads=True)
        except Exception as exc:  # 转储失败绝不能影响看门狗本身
            self.logger.warning("[任务看门狗] 线程栈转储失败: %s", exc)
        if callback is not None:
            try:
                callback(task_key, task_label)
            except Exception as exc:
                self.logger.warning("[任务看门狗] 止损回调执行失败: %s", exc)
