"""启动调用有界化回归（2026-11-26）。

事故：无限暖暖的 run_one_dragon.bat 走 ShellExecuteW 拉起，被系统弹窗挂住，
任务线程 09:33:26 一直卡到 12:38:03 才返回 5（拒绝访问）；心跳看门狗只能杀
游戏进程，打断不了这个 Win32 调用——整批后面的白荆/薅羊毛/CJ 与 10:01 的
定时任务一个都没跑。

本套件锁四件事（全部行为路径，不锁源码文本）：
  1. .bat/.cmd 走 cmd /c、.py 走解释器、.exe 直连、.lnk 才回退 shell；
  2. _call_with_timeout 对永久阻塞的调用按超时返回，不拖住调用线程；
  3. _launch_normal 遇到卡死的启动调用会在超时后判失败返回，批次可继续；
  4. _launch_special 走同一套有界启动，并保持「不起控制台」的 hidden 语义。

跑法：.venv\\Scripts\\python.exe tests\\suite_launch_timeout_20261126.py
"""

import sys
import time
import threading
import subprocess
from types import SimpleNamespace

sys.path.insert(0, r"E:\GithubProject\game_auto\src")

from game_auto.runtime import automation_controller as ac  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


class FakeLogger:
    """接住 logger.* 的 %-参数，失败用例里能直接看文案。"""

    def __init__(self):
        self.records = []

    def _add(self, level, msg, *args):
        text = str(msg)
        if args:
            try:
                text = text % args
            except Exception:
                text = f"{text} {args}"
        self.records.append((level, text))

    def info(self, msg, *a, **k):
        self._add("INFO", msg, *a)

    def warning(self, msg, *a, **k):
        self._add("WARN", msg, *a)

    def error(self, msg, *a, **k):
        self._add("ERROR", msg, *a)

    def debug(self, msg, *a, **k):
        self._add("DEBUG", msg, *a)

    def text(self):
        return "\n".join(t for _, t in self.records)


def make_controller():
    """用 __new__ 绕过重构造：只补 _launch_normal 真正会碰到的属性。

    _watchdog_beat / _task_abort_requested 本身就对桩实例做了防御式读取，
    与 suite_wuwa_window_wait 等既有桩套件口径一致。
    """
    controller = ac.AutomationController.__new__(ac.AutomationController)
    controller.logger = FakeLogger()
    controller.stop_current_task = False
    controller.watchdog_aborted_task = False
    # 包装进程发现要真扫进程表，测试里给固定 PID，避免撞上本机其它 cmd.exe
    controller._find_script_wrapper_process = lambda stem, start_time: SimpleNamespace(pid=4242)
    return controller


class Patch:
    """临时替换模块/类属性，退出时按原值恢复。"""

    def __init__(self, target, **kwargs):
        self.target = target
        self.kwargs = kwargs
        self.old = {}

    def __enter__(self):
        for key, value in self.kwargs.items():
            self.old[key] = getattr(self.target, key)
            setattr(self.target, key, value)
        return self

    def __exit__(self, *exc):
        for key, value in self.old.items():
            setattr(self.target, key, value)
        return False


def test_build_launcher_command():
    print("[1] 启动器分派：bat/cmd 走 cmd /c、py 走解释器、exe 直连、lnk 回退 shell")
    bat = r"C:\tmp a\run_one_dragon.bat"
    kind, argv, flags = ac._build_launcher_command(bat, ["--foo bar", "-e"])
    check("bat 走 cmd /c", kind == "process" and argv[1] == "/c" and argv[2] == bat, argv)
    check("bat 参数原样保留", argv[3:] == ["--foo bar", "-e"], argv)
    check("bat 单独开控制台", flags == ac.CREATE_NEW_CONSOLE, flags)
    _, _, hidden_flags = ac._build_launcher_command(bat, [], hidden=True)
    check("hidden 时不起控制台", hidden_flags == subprocess.CREATE_NO_WINDOW, hidden_flags)

    exe = r"C:\tools\ok-nte.exe"
    kind, argv, flags = ac._build_launcher_command(exe, ["-t", "2"])
    check("exe 直连 CreateProcess",
          kind == "process" and argv == [exe, "-t", "2"] and flags == 0, (kind, argv, flags))
    _, _, exe_hidden = ac._build_launcher_command(exe, [], hidden=True)
    check("exe hidden 沿用 CREATE_NO_WINDOW",
          exe_hidden == subprocess.CREATE_NO_WINDOW, exe_hidden)

    py = r"C:\tools\run.py"
    kind, argv, _ = ac._build_launcher_command(py, ["--x"])
    check("py 走当前解释器",
          kind == "process" and argv[1] == py and argv[0].lower().endswith(".exe"), argv)

    kind, argv, _ = ac._build_launcher_command(r"C:\tools\launch.lnk", [])
    check("lnk 才回退 shell 关联", kind == "shell" and argv == [], (kind, argv))

    kind, argv, _ = ac._build_launcher_command(bat, "-e")
    check("字符串参数不被拆成字符", kind == "process" and argv[3:] == ["-e"], argv)


def test_call_with_timeout():
    print("[2] 有界调用：永久阻塞按超时返回，快速调用/异常正常带回")
    blocker = threading.Event()
    started = time.time()
    finished, value, error = ac._call_with_timeout(lambda: blocker.wait(5), 0.2)
    elapsed = time.time() - started
    check("阻塞调用判超时", finished is False and value is None, (finished, value))
    check("超时不拖住调用方", elapsed < 1.5, f"{elapsed:.2f}s")
    blocker.set()

    finished, value, error = ac._call_with_timeout(lambda: 42, 1.0)
    check("快速调用拿到返回值", finished and value == 42 and error is None, (finished, value, error))

    def boom():
        raise ValueError("launcher broken")

    finished, value, error = ac._call_with_timeout(boom, 1.0)
    check("异常带回而不是抛出", finished and isinstance(error, ValueError), error)


def test_launch_normal_timeout_keeps_batch_alive():
    print("[3] 卡死的启动调用：超时判失败返回，不把任务线程钉住")
    controller = make_controller()
    blocker = threading.Event()

    def blocking_popen(*args, **kwargs):
        blocker.wait(5)
        return SimpleNamespace(pid=1)

    started = time.time()
    with Patch(ac, LAUNCH_CALL_TIMEOUT_SEC=0.3), Patch(ac.subprocess, Popen=blocking_popen):
        ok, pid = ac.AutomationController._launch_normal(
            controller,
            {"launcher": r"C:\x\run_one_dragon.bat", "launcher_params": []},
            "script",
        )
    elapsed = time.time() - started
    blocker.set()
    check("启动判失败", ok is False and pid is None, (ok, pid))
    check("超时后立刻返回", elapsed < 3.0, f"{elapsed:.2f}s")
    check("日志写明启动调用未返回", "未返回" in controller.logger.text(), controller.logger.text())
    check("日志写明按启动失败处理", "按启动失败处理" in controller.logger.text(), controller.logger.text())


def test_launch_normal_bat_uses_cmd():
    print("[4] .bat 启动：传给 Popen 的是 cmd /c 列表，并拿到包装进程 PID")
    controller = make_controller()
    captured = {}

    def fake_popen(argv, **kwargs):
        captured["argv"] = list(argv)
        captured["kwargs"] = dict(kwargs)
        return SimpleNamespace(pid=999)

    bat = r"C:\whimbox\run_one_dragon.bat"
    with Patch(ac, LAUNCH_CALL_TIMEOUT_SEC=2.0), Patch(ac.subprocess, Popen=fake_popen):
        ok, pid = ac.AutomationController._launch_normal(
            controller, {"launcher": bat, "launcher_params": ["-e"], "initial_wait": 0}, "script"
        )
    argv = captured.get("argv") or []
    comspec = __import__("os").environ.get("COMSPEC") or "cmd.exe"
    check("启动成功", ok is True, (ok, pid))
    check("走的是 cmd /c", argv[:3] == [comspec, "/c", bat], argv)
    check("参数跟在批处理后面", argv[3:] == ["-e"], argv)
    check("开了独立控制台", (captured.get("kwargs") or {}).get("creationflags") == ac.CREATE_NEW_CONSOLE, captured)
    check("工作目录取启动器目录", (captured.get("kwargs") or {}).get("cwd") == r"C:\whimbox", captured)
    check("脚本包装进程 PID 回传", pid == 4242, pid)


def test_shell_fallback_still_bounded():
    print("[5] 只剩 shell 关联的目标（.lnk）也受超时/返回码保护")
    controller = make_controller()
    lnk = r"C:\x\launch.lnk"

    def slow_shell(*a, **k):
        time.sleep(5)
        return 42

    started = time.time()
    with Patch(ac, LAUNCH_CALL_TIMEOUT_SEC=0.3, _shell_execute=slow_shell):
        ok, pid = ac.AutomationController._launch_normal(
            controller, {"launcher": lnk, "launcher_params": []}, "script"
        )
    elapsed = time.time() - started
    check("shell 卡死同样判失败", ok is False and pid is None, (ok, pid))
    check("shell 卡死不拖住调用方", elapsed < 3.0, f"{elapsed:.2f}s")

    with Patch(ac, LAUNCH_CALL_TIMEOUT_SEC=1.0, _shell_execute=lambda *a, **k: 5):
        ok, _ = ac.AutomationController._launch_normal(
            controller, {"launcher": lnk, "launcher_params": []}, "script"
        )
    check("ShellExecuteW 返回码 5 判失败", ok is False, ok)
    check("日志带错误码", "错误码: 5" in controller.logger.text(), controller.logger.text())

    with Patch(ac, LAUNCH_CALL_TIMEOUT_SEC=1.0, _shell_execute=lambda *a, **k: 42):
        ok, _ = ac.AutomationController._launch_normal(
            controller, {"launcher": lnk, "launcher_params": []}, "script"
        )
    check("ShellExecuteW 成功码判成功", ok is True, ok)


def test_launch_special_uses_bounded_starter():
    print("[6] special 启动器走同一套有界启动，并保持 hidden 语义")
    controller = make_controller()
    calls = {}

    def fake_start(self, launcher, params, exe_dir, script_type="", hidden=False):
        calls.update(launcher=launcher, params=list(params), hidden=hidden, script_type=script_type)
        return False

    with Patch(ac.AutomationController, _start_launcher=fake_start):
        ok, pid = ac.AutomationController._launch_special(
            controller, {"launcher": r"C:\x\special.bat", "launcher_params": ["/q"]}
        )
    check("special 走 _start_launcher", bool(calls), calls)
    check("special 保持 hidden", calls.get("hidden") is True, calls)
    check("special 参数透传", calls.get("params") == ["/q"], calls)
    check("启动失败即返回失败", ok is False and pid is None, (ok, pid))


if __name__ == "__main__":
    test_build_launcher_command()
    test_call_with_timeout()
    test_launch_normal_timeout_keeps_batch_alive()
    test_launch_normal_bat_uses_cmd()
    test_shell_fallback_still_bounded()
    test_launch_special_uses_bounded_starter()
    if FAILURES:
        print(f"\nFAILED {len(FAILURES)}: {FAILURES}")
        sys.exit(1)
    print("\nALL PASS")
