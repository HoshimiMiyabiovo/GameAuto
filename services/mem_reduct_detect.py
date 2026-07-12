"""检测 Mem Reduct 是否在运行，并查询其当前清理阈值。

仅检测，不修改任何系统配置 / 注册表 / 进程优先级，避免越权。

Mem Reduct 会在后台按计划或触发器清理工作集，可能造成 game_auto 及其子进程
（OK 监控、subprocess 启动器、Edge cookie 读取）被换出 / 卡顿。
"""
from __future__ import annotations

import ctypes
import logging
import os
import sys
from typing import Optional, Tuple

LOGGER = logging.getLogger("GameAutoMemReductDetect")

_MEM_REDUCT_PROCESS_NAMES = ("memreduct.exe", "mem_reduct.exe")
_POLL_INTERVAL_BEFORE_START = 5 * 60  # 启动后 5 分钟内定期轮询
_POLL_INTERVAL_AFTER_START = 15 * 60  # 5 分钟后改为 15 分钟一次


def _enum_processes() -> list[Tuple[int, str]]:
    """返回 (pid, image_name_lower) 列表。Windows 实现。"""
    if sys.platform != "win32":
        return []

    Psapi = ctypes.WinDLL("Psapi.dll")
    Kernel32 = ctypes.Windll.kernel32

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    MAX_PATH = 260

    class PROCESSENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", ctypes.c_ulong),
            ("cntUsage", ctypes.c_ulong),
            ("th32ProcessID", ctypes.c_ulong),
            ("th32DefaultHeapID", ctypes.c_ulong),
            ("th32ModuleID", ctypes.c_ulong),
            ("cntThreads", ctypes.c_ulong),
            ("th32ParentProcessID", ctypes.c_ulong),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", ctypes.c_ulong),
            ("szExeFile", ctypes.c_char * MAX_PATH),
        ]

    snapshot = Kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        return []
    try:
        entry = PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
        results: list[Tuple[int, str]] = []
        if not Kernel32.Process32First(snapshot, ctypes.byref(entry)):
            return results
        while True:
            try:
                name = entry.szExeFile.decode("ascii", errors="ignore").lower()
            except Exception:
                name = ""
            if name:
                results.append((int(entry.th32ProcessID), name))
            if not Kernel32.Process32Next(snapshot, ctypes.byref(entry)):
                break
        return results
    finally:
        Kernel32.CloseHandle(snapshot)


def _read_memory_load_percent(pid: int) -> Optional[int]:
    """查询系统内存负载百分比（不依赖 psutil）。"""
    if sys.platform != "win32":
        return None
    try:
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]
        Kernel32 = ctypes.Windll.kernel32
        status = MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(status)
        if Kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return int(status.dwMemoryLoad)
    except Exception as exc:
        LOGGER.debug("[MemReduct] GlobalMemoryStatusEx 失败: %s", exc)
    return None


def find_mem_reduct_pids() -> list[int]:
    pids: list[int] = []
    for pid, name in _enum_processes():
        if name in _MEM_REDUCT_PROCESS_NAMES:
            pids.append(pid)
    return pids


def _raise_our_process_priority_above_normal() -> bool:
    """把 game_auto 进程优先级提到 ABOVE_NORMAL，让 Mem Reduct 默认清理更倾向不动它。

    不会修改任何外部进程 / 系统设置。失败则忽略。
    """
    if sys.platform != "win32":
        return False
    try:
        ABOVE_NORMAL_PRIORITY_CLASS = 0x00008000
        handle = ctypes.windll.kernel32.OpenProcess(0x0080, False, os.getpid())
        if not handle:
            return False
        try:
            ctypes.windll.kernel32.SetPriorityClass(handle, ABOVE_NORMAL_PRIORITY_CLASS)
            return True
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    except Exception as exc:
        LOGGER.debug("[MemReduct] 提升进程优先级失败: %s", exc)
        return False


def warn_once_at_startup(logger: logging.Logger) -> Optional[list[int]]:
    """在 Flet 启动时调用：检测到 Mem Reduct 跑着就打一条 warn，不弹窗。"""
    try:
        pids = find_mem_reduct_pids()
    except Exception as exc:
        logger.debug("[MemReduct] 枚举进程失败: %s", exc)
        return None

    if not pids:
        logger.info("[MemReduct] 未检测到 Mem Reduct 运行")
        return None

    mem_load = _read_memory_load_percent(os.getpid())
    load_text = f"系统内存负载 {mem_load}%" if mem_load is not None else "系统内存负载未知"

    logger.warning(
        "[MemReduct] 检测到 Mem Reduct 在运行 (PID=%s)，%s。"
        "为避免 OK 监控 / 子进程 / Flet UI 被清内存卡顿，建议在 Mem Reduct 设置中"
        "将 game_auto.exe 加入 Skip list。",
        ",".join(str(p) for p in pids),
        load_text,
    )

    boosted = _raise_our_process_priority_above_normal()
    if boosted:
        logger.info("[MemReduct] 已将当前进程优先级临时提升为 ABOVE_NORMAL（仅本进程，进程退出后失效）")
    else:
        logger.info("[MemReduct] 提升进程优先级失败或被拒绝，继续以原优先级运行")

    return pids
