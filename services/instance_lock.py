"""进程单例锁。

防止 game_auto 被开多个 Flet 窗口，每个窗口会启动独立的 OK 监控线程、自动化任务和子进程，
导致日志/进程互相覆盖、任务竞争。启动时通过 PID 锁文件检测已有实例并拒绝启动。
"""
from __future__ import annotations

import atexit
import ctypes
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Optional

LOCK_FILENAME = "game_auto.instance.lock"
_logger = logging.getLogger("GameAutoInstanceLock")


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value == STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False


def get_lock_path() -> Path:
    runtime_dir = Path(tempfile.gettempdir()) / "game_auto"
    try:
        runtime_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _logger.warning("[实例锁] 创建运行时目录失败: %s，回退到系统临时目录", exc)
        return Path(tempfile.gettempdir()) / LOCK_FILENAME
    return runtime_dir / LOCK_FILENAME


def _read_existing_pid(lock_path: Path) -> Optional[int]:
    try:
        raw = lock_path.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        return None
    if not raw:
        return None
    try:
        return int(raw.splitlines()[0])
    except ValueError:
        return None


def acquire_instance_lock() -> Optional[int]:
    """尝试获取单例锁。

    返回 ``None`` 表示当前进程获得锁，可以继续启动。
    返回已有实例的 PID 表示检测到重复启动。
    """
    lock_path = get_lock_path()
    current_pid = os.getpid()

    existing_pid = _read_existing_pid(lock_path)
    if existing_pid and existing_pid != current_pid and _is_pid_alive(existing_pid):
        return existing_pid

    try:
        lock_path.write_text(f"{current_pid}\n", encoding="utf-8")
    except OSError as exc:
        _logger.error("[实例锁] 写入锁文件失败: %s", exc)
        return None

    atexit.register(_release_instance_lock, lock_path)
    return None


def _release_instance_lock(lock_path: Path) -> None:
    try:
        existing_pid = _read_existing_pid(lock_path)
        if existing_pid == os.getpid():
            lock_path.unlink(missing_ok=True)
    except OSError as exc:
        _logger.warning("[实例锁] 释放锁文件失败: %s", exc)
