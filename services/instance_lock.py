"""进程单例锁。

防止 game_auto 被开多个窗口，每个窗口会启动独立的 OK 监控线程、自动化任务和子进程，
导致日志/进程互相覆盖、任务竞争。启动时通过文件锁检测已有实例并拒绝启动。

使用 msvcrt.locking 实现文件级独占锁：进程退出（包括崩溃/被杀）时操作系统自动释放锁，
不依赖 atexit，避免崩溃后锁残留导致无法重新启动。
"""
from __future__ import annotations

import atexit
import ctypes
import logging
import msvcrt
import os
from pathlib import Path
from typing import Optional

LOCK_FILENAME = "game_auto.instance.lock"
_logger = logging.getLogger("GameAutoInstanceLock")

# 保持文件句柄，进程退出时操作系统自动关闭并释放锁
_lock_handle = None


def get_lock_path() -> Path:
    """返回锁文件路径。"""
    import tempfile
    runtime_dir = Path(tempfile.gettempdir()) / "game_auto"
    try:
        runtime_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _logger.warning("[实例锁] 创建运行时目录失败: %s，回退到系统临时目录", exc)
        return Path(tempfile.gettempdir()) / LOCK_FILENAME
    return runtime_dir / LOCK_FILENAME


def _read_existing_pid(lock_path: Path) -> Optional[int]:
    """读取锁文件中的 PID。"""
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
    返回已有实例的 PID（或 -1 如果读取不到 PID）表示检测到重复启动。
    """
    global _lock_handle
    lock_path = get_lock_path()
    current_pid = os.getpid()

    # 确保锁文件存在
    try:
        lock_path.touch(exist_ok=True)
    except OSError as exc:
        _logger.error("[实例锁] 创建锁文件失败: %s", exc)
        return None

    # 读取已有 PID（用于错误消息）
    existing_pid = _read_existing_pid(lock_path)

    # 尝试打开文件并获取独占锁
    try:
        _lock_handle = open(lock_path, "r+")
        _lock_handle.seek(0)
        msvcrt.locking(_lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        # 文件锁被占用，说明有其他实例在运行
        if _lock_handle:
            _lock_handle.close()
            _lock_handle = None
        _logger.warning("[实例锁] 检测到已有实例运行 (PID=%s)", existing_pid or "未知")
        return existing_pid if existing_pid else -1

    # 获取锁成功，写入当前 PID
    try:
        _lock_handle.seek(0)
        _lock_handle.truncate()
        _lock_handle.write(f"{current_pid}\n")
        _lock_handle.flush()
    except OSError as exc:
        _logger.warning("[实例锁] 写入 PID 失败: %s", exc)

    atexit.register(_release_instance_lock, lock_path)
    _logger.debug("[实例锁] 已获取锁 PID=%d", current_pid)
    return None


def _release_instance_lock(lock_path: Path) -> None:
    """释放单例锁。"""
    global _lock_handle
    try:
        if _lock_handle:
            _lock_handle.seek(0)
            try:
                msvcrt.locking(_lock_handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            _lock_handle.close()
            _lock_handle = None
        existing_pid = _read_existing_pid(lock_path)
        if existing_pid == os.getpid():
            lock_path.unlink(missing_ok=True)
    except OSError as exc:
        _logger.warning("[实例锁] 释放锁文件失败: %s", exc)
