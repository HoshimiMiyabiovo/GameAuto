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
from types import SimpleNamespace

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


class _Logger:
    """最小 logger 桩：只接文案，不做格式化。"""

    def __init__(self):
        self.lines = []

    def _add(self, msg, *a):
        self.lines.append(str(msg))

    def info(self, msg, *a, **k):
        self._add(msg)

    def warning(self, msg, *a, **k):
        self._add(msg)

    def error(self, msg, *a, **k):
        self._add(msg)

    def debug(self, msg, *a, **k):
        self._add(msg)


def test_launch_normal_exe_uses_createprocess_not_shellexecute():
    """行为锚点：.exe 必须走 CreateProcess 列表参数，不能回退 ShellExecuteW。

    2026-11-26 起 _launch_normal 把启动调用委托给 _start_launcher（有界等待），
    断言从"源码里出现哪个分支"改成"实际调用了哪个通道"，不再锁实现文本。
    """
    print("[3] _launch_normal 接线：exe 走 CreateProcess")
    controller = ac.AutomationController.__new__(ac.AutomationController)
    controller.logger = _Logger()
    controller.stop_current_task = False
    controller.watchdog_aborted_task = False
    # 包装进程发现要真扫进程表，测试里给固定 PID
    controller._find_script_wrapper_process = lambda stem, start: SimpleNamespace(pid=4242)
    calls = {}
    orig_popen = ac.subprocess.Popen
    orig_shell = ac._shell_execute

    def fake_popen(argv, **kwargs):
        calls["argv"] = list(argv)
        calls["kwargs"] = dict(kwargs)
        return SimpleNamespace(pid=4321)

    ac.subprocess.Popen = fake_popen
    ac._shell_execute = lambda *a, **k: calls.setdefault("shell", a)
    try:
        ok, pid = ac.AutomationController._launch_normal(
            controller,
            {"launcher": r"C:\x\BetterGI.exe", "launcher_params": ["--start"], "initial_wait": 0},
            "script",
        )
    finally:
        ac.subprocess.Popen = orig_popen
        ac._shell_execute = orig_shell
    check("启动成功", ok is True, (ok, pid))
    check("走 CreateProcess 列表参数", calls.get("argv") == [r"C:\x\BetterGI.exe", "--start"], calls)
    check("没有回退 ShellExecuteW", "shell" not in calls, calls)
    check("MOTW 清除兜底仍在",
          "clear_motw_marker(" in inspect.getsource(ac.AutomationController._launch_normal))


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
