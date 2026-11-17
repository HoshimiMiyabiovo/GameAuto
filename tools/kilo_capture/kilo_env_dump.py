# -*- coding: utf-8 -*-
"""Kilo 内核环境变量提权导出（须管理员运行）。

背景：Kilo Desktop 内核 kilo.exe（serve 模式）跑在比普通用户高的完整性级别，
普通权限 OpenProcess 一律 err 5，读不到它的环境变量。本地 API 的 Basic 密码
`KILO_SERVER_PASSWORD` 恰好只在内核环境里（app.asar 启动内核时塞进去的；
客户端自己的进程里没有——2026-11-17 实测）。内核重启会轮换密码与端口，
所以每次抓取前若 kilo_fetch 报密码过期，用管理员重跑本脚本一次。

用法（管理员 PowerShell / 提权 cmd）：
  .venv\\Scripts\\python.exe tools\\kilo_capture\\kilo_env_dump.py
  # 或显式指定 pid：python tools\\kilo_capture\\kilo_env_dump.py 21612

产物：data/kilo_capture/kernel_env.json
  {"pid": <内核pid>, "port": <监听端口>, "captured_at": <ISO时间>,
   "env": {"KILO_SERVER_PASSWORD": "...", ...全部 KILO*/TOKEN* 条目}}
kilo_fetch 会按 pid 校验新鲜度（内核重启后旧文件自动失效并提示重跑）。
原理：NtQueryInformationProcess 拿 PEB → ProcessParameters → Environment 块
（UTF-16LE，\\x00 分隔），与验证脚本 _tmp_kilo_env.py 同源。
"""
from __future__ import annotations

import ctypes
import datetime
import json
import os
import sys

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
ntdll = ctypes.WinDLL("ntdll", use_last_error=True)


class PBI(ctypes.Structure):
    # x64 PROCESS_BASIC_INFORMATION 真实布局（ExitStatus 在 0x00，PEB 在 0x08）
    _fields_ = [("ExitStatus", ctypes.c_void_p),
                ("PebBaseAddress", ctypes.c_void_p),
                ("AffinityMask", ctypes.c_void_p),
                ("BasePriority", ctypes.c_void_p),
                ("UniqueProcessId", ctypes.c_void_p),
                ("InheritedFromUniqueProcessId", ctypes.c_void_p)]


def read_mem(handle, addr, size):
    buf = ctypes.create_string_buffer(size)
    got = ctypes.c_size_t(0)
    ok = k32.ReadProcessMemory(handle, ctypes.c_void_p(addr), buf, size, ctypes.byref(got))
    if not ok:
        return None
    return buf.raw[:got.value]


def read_env_entries(handle):
    """PEB → ProcessParameters → Environment 块 → 字符串条目列表。"""
    pbi = PBI()
    st = ntdll.NtQueryInformationProcess(handle, 0, ctypes.byref(pbi), ctypes.sizeof(pbi), None)
    if st != 0:
        raise OSError(f"NtQueryInformationProcess 失败 {hex(st & 0xFFFFFFFF)}")
    pp = read_mem(handle, (pbi.PebBaseAddress or 0) + 0x20, 8)
    if not pp:
        raise OSError("读 ProcessParameters 失败")
    proc_params = int.from_bytes(pp, "little")
    env_ptr = read_mem(handle, proc_params + 0x80, 8)
    env_size = read_mem(handle, proc_params + 0x3F0, 8)
    if not env_ptr or not env_size:
        raise OSError("读 Environment 指针失败")
    env_addr = int.from_bytes(env_ptr, "little")
    size = int.from_bytes(env_size, "little")
    if size <= 0 or size > 64 * 1024 * 1024:
        size = 256 * 1024  # EnvironmentSize 异常时的兜底读长
    data = read_mem(handle, env_addr, size)
    if not data:
        raise OSError("读 Environment 失败")
    text = data.decode("utf-16-le", "replace")
    return [e for e in text.split("\x00") if e.strip()]


def find_kernel_pid():
    """自动发现 serve 模式内核 pid（与 kilo_fetch.find_kernel 同口径）。

    坑：内核是提权进程，process_iter 读它 cmdline 会 AccessDenied 被吞成空，
    cmdline 过滤常态失效 → 兜底反查 127.0.0.1 监听者名字恰为 kilo.exe 的。
    """
    try:
        import psutil
    except ImportError:
        return None
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        if (proc.info.get("name") or "").lower() != "kilo.exe":
            continue
        if "serve" in " ".join(proc.info.get("cmdline") or []):
            return proc.pid
    for conn in psutil.net_connections(kind="inet"):
        if not (conn.status == psutil.CONN_LISTEN and conn.laddr
                and conn.laddr.ip == "127.0.0.1"):
            continue
        try:
            if (psutil.Process(conn.pid).name() or "").lower() == "kilo.exe":
                return conn.pid
        except Exception:  # noqa: BLE001 - 监听者已退出/权限不足则跳过
            continue
    return None


def kernel_port(pid):
    """内核 127.0.0.1 监听端口（port=0 时由系统分配，须实测）。"""
    try:
        import psutil
    except ImportError:
        return None
    for conn in psutil.net_connections(kind="inet"):
        if (conn.status == psutil.CONN_LISTEN and conn.pid == pid
                and conn.laddr and conn.laddr.ip == "127.0.0.1"):
            return conn.laddr.port
    return None


def main():
    if not ctypes.windll.shell32.IsUserAnAdmin():
        print("⚠ 当前非管理员权限——OpenProcess 大概率被拒（err 5）。")
        print("  请用管理员 PowerShell 运行本脚本（UAC 提权）。")
    pid = int(sys.argv[1]) if len(sys.argv) > 1 else (find_kernel_pid() or 0)
    if not pid:
        raise SystemExit("未发现 kilo.exe 内核（serve 模式）——先打开 Kilo Desktop，"
                         "或显式传 pid：python kilo_env_dump.py <pid>")
    handle = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not handle:
        raise SystemExit(f"OpenProcess({pid}) 失败 err={ctypes.get_last_error()}"
                         "——目标完整性级别更高，须以管理员运行本脚本。")
    try:
        entries = read_env_entries(handle)
    finally:
        k32.CloseHandle(handle)

    env = {}
    for entry in entries:
        name, _, value = entry.partition("=")
        if name.upper().startswith(("KILO", "KILOCODE", "XDG_")) or "TOKEN" in name.upper():
            env[name] = value
    if "KILO_SERVER_PASSWORD" not in env:
        raise SystemExit(f"环境块里没有 KILO_SERVER_PASSWORD（共读到 {len(entries)} 条）"
                         "——内核版本可能已更换鉴权机制。")

    out = {
        "pid": pid,
        "port": kernel_port(pid),
        "captured_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "env": env,
    }
    root = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "data", "kilo_capture")
    os.makedirs(root, exist_ok=True)
    path = os.path.join(root, "kernel_env.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    print(f"已写 {path}")
    print(f"pid={pid} port={out['port']} 环境条目 {len(env)}（含 KILO_SERVER_PASSWORD）")
    print("现在可以普通权限跑: .venv\\Scripts\\python.exe tools\\kilo_capture\\kilo_fetch.py")


if __name__ == "__main__":
    main()
