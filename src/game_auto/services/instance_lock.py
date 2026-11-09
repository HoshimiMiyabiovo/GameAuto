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

    # 确保锁文件存在。创建失败时 fail-open 放行，但这是单例保护的静默失效
    # （审计 P2）：临时目录不可写往往伴随更大的环境问题，必须 ERROR 留痕，
    # 不允许降级成 debug 后无声继续。
    try:
        lock_path.touch(exist_ok=True)
    except OSError as exc:
        _logger.error(
            "[实例锁] 创建锁文件失败，单实例保护失效，放行启动: %s (%s)", lock_path, exc)
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
    """释放单例锁。

    2026-11-09 审计 P2：不再 unlink 锁文件。旧顺序（解锁 → 删文件）有双实例
    窗口：实例 C 在 A unlink 前已 open 旧文件，A 删除后 C 对孤儿 inode 加锁
    成功，实例 D 再新建文件加锁也成功 → C/D 双实例并发跑任务。锁语义绑
    inode，文件留在临时目录（字节级，无碍）；下次启动直接锁同一文件。
    """
    global _lock_handle
    try:
        if _lock_handle:
            _lock_handle.seek(0)
            try:
                msvcrt.locking(_lock_handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError as exc:
                _logger.debug("[实例锁] 解锁失败（进程退出时会自动释放）: %s", exc)
            _lock_handle.close()
            _lock_handle = None
    except OSError as exc:
        _logger.warning("[实例锁] 释放锁文件失败: %s", exc)

_AUX_HANDLES: dict = {}


def acquire_aux_lock(name: str) -> bool:
    """获取一个命名的辅助互斥锁（msvcrt 文件锁，与主实例锁同目录）。

    返回 True=拿到；False=已被占用（调用方应拒绝执行）。维护执行器
    （generic_workflow_runner / screen_task_runner）用它与生产 GUI 批处理
    互斥——两边都是实点击抢前台，并发会互相把对方的窗口切到前台、点击
    落进对方流程的界面（2026-11-09 审计 P2-5）。句柄进程存活期间保持，
    退出由 OS 回收；atexit 只做解锁不删文件（同 _release_instance_lock 口径）。
    """
    key = str(name)
    if key in _AUX_HANDLES and _AUX_HANDLES[key] is not None:
        return True  # 本进程已持有
    lock_path = get_lock_path().parent / f"game_auto_aux_{key}.lock"
    try:
        lock_path.touch(exist_ok=True)
        handle = open(lock_path, "r+")
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        try:
            if "handle" in dir():
                handle.close()
        except Exception:
            pass
        return False
    _AUX_HANDLES[key] = handle
    return True


def release_aux_lock(name: str) -> None:
    handle = _AUX_HANDLES.pop(str(name), None)
    if handle is None:
        return
    try:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    finally:
        try:
            handle.close()
        except OSError:
            pass
