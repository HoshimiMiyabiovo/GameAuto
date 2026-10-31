# -*- coding: utf-8 -*-
"""脚本任务启动前清理回归（2026-10-31）。

背景：ok-nte 是「ok-nte.exe 启动器 + 内置 pythonw」结构，启动前只按进程名
清 ok-nte.exe 会漏掉 pythonw，导致自更新被“仍在运行”挡住、游戏没被拉起。
本套件锁定：
1. 安装目录解析（script_exe_path / launcher / launcher_options 选择）；
2. 按安装目录清理能杀掉 pythonw 子进程，目录外与自身进程不动；
3. execute_task 启动前确实调用统一清理。

跑法：.venv\\Scripts\\python.exe tests\\suite_prelaunch_cleanup_20261031.py
"""

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT.parent))

from game_auto.runtime import automation_controller as ac  # noqa: E402
from game_auto.runtime.process_supervisor import ProcessSupervisor  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


class _Logger:
    def __getattr__(self, _name):
        def _log(*_args, **_kwargs):
            return None
        return _log


class _Proc:
    def __init__(self, pid, name, exe):
        self.info = {"pid": pid, "name": name, "exe": exe}


def _controller():
    ctrl = object.__new__(ac.AutomationController)
    ctrl.logger = _Logger()
    return ctrl


def main():
    ctrl = _controller()

    print("[1] 安装目录解析")
    cfg = {
        "script_process_name": "ok-nte.exe",
        "launcher": "",
        "_discovery": {"script_exe_path": r"E:\x\ok-nte\ok-nte.exe"},
    }
    check("discovery.script_exe_path", ctrl._script_install_roots(cfg) == [r"E:\x\ok-nte"],
          ctrl._script_install_roots(cfg))
    cfg2 = {"launcher_options": [
        {"name": "A", "launcher": r"E:\a\app.exe"},
        {"name": "B", "launcher": r"E:\b\app.exe"},
    ]}
    check("launcher_options 按选择过滤", ctrl._script_install_roots(cfg2, "B") == [r"E:\b"],
          ctrl._script_install_roots(cfg2, "B"))
    check("空配置无根目录", ctrl._script_install_roots({}) == [])
    check("盘符根被拒绝", ctrl._script_install_roots({"launcher": r"E:\app.exe"}) == [],
          ctrl._script_install_roots({"launcher": r"E:\app.exe"}))

    print("[2] 按安装目录清理")
    killed = []
    orig_kill = ProcessSupervisor.safe_kill_process
    orig_iter = ac.psutil.process_iter
    try:
        ProcessSupervisor.safe_kill_process = staticmethod(lambda pid: killed.append(pid))
        fake = [
            _Proc(100, "ok-nte.exe", r"E:\x\ok-nte\ok-nte.exe"),
            _Proc(101, "pythonw.exe", r"E:\x\ok-nte\python\pythonw.exe"),
            _Proc(102, "other.exe", r"E:\y\other.exe"),
            _Proc(103, "pythonw.exe", r"E:\x\ok-nte2\pythonw.exe"),
            _Proc(os.getpid(), "python.exe", r"E:\x\ok-nte\self.exe"),
        ]
        ac.psutil.process_iter = lambda *_a, **_k: iter(fake)
        got = ctrl._kill_processes_under_roots([r"E:\x\ok-nte"], "test",
                                               skip_pids={os.getpid()})
        check("安装目录内进程被清理（含 pythonw）", got == [100, 101], got)
        check("目录外/同前缀目录不动", 102 not in got and 103 not in got, got)
        check("自身进程被跳过", os.getpid() not in got, got)

        killed.clear()
        got_empty = ctrl._kill_processes_under_roots([], "test")
        check("无根目录时不清理", got_empty == [] and not killed, got_empty)
    finally:
        ProcessSupervisor.safe_kill_process = orig_kill
        ac.psutil.process_iter = orig_iter

    print("[3] execute_task 接线")
    source = Path(ac.__file__).read_text(encoding="utf-8")
    check("启动前调用统一清理",
          "self._cleanup_stale_script_before_launch(config, game_display, script_choice)" in source)
    check("清理包含启动器/附加清单",
          "include_game=False, include_script=False" in source)
    check("按安装目录补刀", "_kill_processes_under_roots" in source)
    check("自身与祖先进程跳过", "psutil.Process(os.getpid()).parents()" in source)

    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
