"""MOTW（Zone.Identifier）自动清除回归：2026-11-16 鸣潮启动阻塞 39 分钟根因。

背景：ShellExecuteW 拉起带 MOTW 标记的可执行文件时，Windows 弹「打开文件 -
安全警告」，无人值守没人点「运行」就一直阻塞，最终返回 SE_ERR_ACCESS_DENIED(5)。
gameauto 是管理员进程，启动前删 ADS 即可绕开，不需要改安全软件配置。

本套件验证 clear_motw_marker 的行为契约（存在/不存在/非 Windows 降级），
并确认 _launch_normal 在 ShellExecuteW 之前确实接了这条清除逻辑。

2026-11-16 改为普通脚本式 check()：原 pytest 风格套件经 test_suites.py
子进程包裹时内层 pytest 的 tmp_path fixture 会撞 finalizer 断言，
且 pytest tmp_path 在项目根留下 pytest-of-admin/。统一与其他 suite 一致。
"""

import inspect
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# 沙箱兼容：tempfile 目录创建改默认权限（0700 受保护 DACL 在沙箱里写不进）
from sandbox_temp import install as _install_sandbox_temp

_install_sandbox_temp()

from game_auto.runtime import automation_controller as ac  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def test_clear_motw_marker_removes_stream():
    print("[1] 带 Zone.Identifier 的文件：清除后 ADS 不存在")
    if os.name != "nt":
        print("  SKIP ADS 是 Windows 特性")
        return
    import ctypes

    with tempfile.TemporaryDirectory(prefix="ga_motw_") as td:
        f = Path(td) / "fake.exe"
        f.write_bytes(b"MZ")
        ads = f"{f}:Zone.Identifier"
        with open(ads, "w", encoding="utf-8") as fh:
            fh.write("[ZoneTransfer]\r\nZoneId=3\r\n")
        check("测试 ADS 已建立", ctypes.windll.kernel32.GetFileAttributesW(ads) != -1)
        check("清除返回 True", ac.clear_motw_marker(str(f)) is True)
        check("ADS 已被删除", ctypes.windll.kernel32.GetFileAttributesW(ads) == -1)
        check("主文件内容未受影响", f.exists() and f.read_bytes() == b"MZ")


def test_clear_motw_marker_no_marker_is_noop():
    print("[2] 没有标记的文件/不存在的路径：返回 False")
    with tempfile.TemporaryDirectory(prefix="ga_motw_") as td:
        f = Path(td) / "plain.exe"
        f.write_bytes(b"MZ")
        check("无标记文件返回 False", ac.clear_motw_marker(str(f)) is False)
        check("不存在路径返回 False", ac.clear_motw_marker(str(Path(td) / "missing.exe")) is False)
        check("空路径返回 False", ac.clear_motw_marker("") is False)


def test_launch_normal_exe_uses_createprocess_not_shellexecute():
    """接线锚点：.exe 必须走 CreateProcess，不能经 ShellExecute 的附件管理器。"""
    print("[3] _launch_normal 接线：exe 走 CreateProcess")
    src = inspect.getsource(ac.AutomationController._launch_normal)
    check("使用 subprocess.Popen", "subprocess.Popen(" in src)
    check("按 .exe 分支", 'launcher.lower().endswith(".exe")' in src)
    check("ShellExecuteW 只在非 exe 分支", src.count("ShellExecuteW(") == 1)
    check("MOTW 清除兜底仍在", "clear_motw_marker(" in src)


def main():
    test_clear_motw_marker_removes_stream()
    test_clear_motw_marker_no_marker_is_noop()
    test_launch_normal_exe_uses_createprocess_not_shellexecute()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
