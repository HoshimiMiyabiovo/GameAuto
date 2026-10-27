"""GUI 卡死看门狗。

主线程通过 :meth:`HangWatchdog.beat` 上报心跳（由 QTimer 驱动，2s 一次）。
一旦主线程被阻塞 —— QML 实例化卡住、GIL 被后台线程的 heavy import/网络等待
长时间占用、同步 IO 等 —— 心跳就会停止，看门狗线程随即把**全部线程**的
Python 栈 dump 到 ``logs/watchdog_main_thread.log``（settings.WATCHDOG_MAIN_LOG_FILE），
用于事后定位"到底卡在哪一行"。任务线程卡死走另一个文件
``logs/watchdog_task_heartbeat.log``（task_heartbeat_watchdog），两类硬故障不混在一起。

设计要点：
- 看门狗自身是 daemon 线程，只做计时与 dump，不参与 Qt 事件循环，
  因此主线程完全冻结时它仍能工作。
- 首次心跳到达后才进入监控（``_armed``），避免启动阶段（engine.load 等
  尚未进入事件循环）被误判为卡死。
- 两次 dump 之间设冷却，避免持续卡死时把日志刷爆；文件超过 5MB 时归档为
  带时间戳的副本，不删除历史现场。
"""
from __future__ import annotations

import faulthandler
import logging
import threading
import time
from pathlib import Path
from typing import Optional

# dump 文件超过该大小时归档为带时间戳的副本，避免单个文件无限增长且不丢历史
MAX_DUMP_BYTES = 5 * 1024 * 1024

_logger = logging.getLogger("GameAutomation.Watchdog")


class HangWatchdog:
    """主线程心跳监控 + 卡死时全线程栈转储。"""

    def __init__(
        self,
        dump_path: Path,
        heartbeat_timeout: float = 15.0,
        check_interval: float = 5.0,
        cooldown: float = 60.0,
        logger: Optional[logging.Logger] = None,
    ):
        self.dump_path = Path(dump_path)
        self.heartbeat_timeout = heartbeat_timeout
        self.check_interval = check_interval
        self.cooldown = cooldown
        self._logger = logger or _logger
        self._last_beat = time.monotonic()
        self._last_dump_at = 0.0
        self._armed = False
        self._dump_count = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ── 主线程调用 ──
    def beat(self) -> None:
        """上报心跳。首次调用同时激活监控。"""
        self._last_beat = time.monotonic()
        self._armed = True

    def start(self) -> None:
        """启动看门狗线程（可重复调用，已启动则忽略）。"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="HangWatchdog", daemon=True
        )
        self._thread.start()
        self._logger.debug("[卡死看门狗] 已启动，输出: %s", self.dump_path)

    def stop(self) -> None:
        """停止监控（进程退出时调用）。"""
        self._stop.set()

    # ── 看门狗线程 ──
    def _loop(self) -> None:
        while not self._stop.wait(self.check_interval):
            if not self._armed:
                continue
            stall = time.monotonic() - self._last_beat
            if stall < self.heartbeat_timeout:
                continue
            now = time.monotonic()
            if now - self._last_dump_at < self.cooldown:
                continue
            self._last_dump_at = now
            self._dump(stall)

    def _dump(self, stall: float) -> None:
        self._dump_count += 1
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
                except OSError:
                    pass
            with open(self.dump_path, "a", encoding="utf-8") as fh:
                fh.write(
                    f"\n{'=' * 72}\n"
                    f"[卡死看门狗] 主线程已 {stall:.1f}s 无心跳"
                    f"（阈值 {self.heartbeat_timeout:.0f}s）"
                    f"｜第 {self._dump_count} 次采样"
                    f"｜{time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"{'=' * 72}\n"
                )
                faulthandler.dump_traceback(fh, all_threads=True)
            self._logger.warning(
                "[卡死看门狗] 主线程已 %.1fs 无响应，线程栈已写入 %s",
                stall,
                self.dump_path,
            )
        except Exception as exc:  # 转储失败绝不能影响主流程
            self._logger.warning("[卡死看门狗] 转储失败: %s", exc)
