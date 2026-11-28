# -*- coding: utf-8 -*-
"""ok-ww module runner 任务护栏：焦点守护 + 日志停滞判死（2026-11-28）。

现场两台事叠在一起：
1. 游戏热更重启换了 hwnd 后，ok-ww 只在交互线程启动时置顶过一次，之后没人再切
   前台；它的输入层有前台门禁（非前台直接跳过点击/按键），真走到输入阶段会满屏
   `window in background, not clickable` 原地走不动。
2. 热更重启后 ok-ww 卡在 `start_device()`（`start_device end` 永不出现），23 分钟
   零日志、无任何超时，直到 GUI 断线才被 TaskDisabledException 踢出来。

补丁（只改我们的 runner，不动上游）：
- 焦点守护：任务在跑且游戏窗口不是前台 → 定期 `bring_to_front()`，失败再用
  AttachThreadInput 版兜底；
- 停滞判死：任务在跑但 ok-ww 日志连续 STALL_LOG_SEC 无新增 → faulthandler dump
  全线程栈留现场，再 `stop_current_task()`，任务按失败返回（批次可继续）。
"""

import importlib.util
import time
import types
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUNNER = PROJECT_ROOT / "tools" / "okww_module" / "runner.py"

_OPEN_HANDLES = []


@pytest.fixture(autouse=True)
def _close_handles():
    yield
    while _OPEN_HANDLES:
        try:
            _OPEN_HANDLES.pop().close()
        except Exception:  # noqa: BLE001 - 清理失败不影响测试结论
            pass


def _load_runner():
    spec = importlib.util.spec_from_file_location("okww_runner_guard_probe", RUNNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make(module, root: Path):
    return module.ModuleRunner(
        okww_root=str(root),
        host="127.0.0.1",
        port=0,
        token="",
        ready_file=str(root / "ready.json"),
        log_file=str(root / "runner.log"),
        humanize_config="",
    )


class _FakeTask:
    def __init__(self, name="DailyTask", running=True):
        self.name = name
        self.running = running


class _FakeHwndWindow:
    def __init__(self, hwnd=1234, foreground=False):
        self.hwnd = hwnd
        self.foreground = foreground
        self.bring_calls = 0

    def is_foreground(self):
        return self.foreground

    def bring_to_front(self):
        self.bring_calls += 1
        self.foreground = True
        return True


def _wire(runner, root: Path, task, hwnd_window=None, stopped=None):
    executor = types.SimpleNamespace(
        stop_current_task=lambda: stopped.append(1) if stopped is not None else None
    )
    runner.ok = types.SimpleNamespace(
        task_executor=executor,
        device_manager=types.SimpleNamespace(hwnd_window=hwnd_window),
    )
    runner._current_task = task
    runner._task_started_at = time.monotonic()
    handle = open(root / "stall_dump.log", "a", encoding="utf-8")
    _OPEN_HANDLES.append(handle)
    runner._log_handle = handle


def test_guard_symbols_exist():
    module = _load_runner()
    assert hasattr(module, "STALL_LOG_SEC")
    assert hasattr(module, "FOCUS_RETRY_SEC")
    assert hasattr(module, "_pick_okww_log")
    assert hasattr(module, "_file_signature")
    assert hasattr(module, "ModuleRunner")
    for name in ("_guard_running_task", "_ensure_game_foreground", "_check_log_stall"):
        assert hasattr(module.ModuleRunner, name), name


def test_guard_thresholds_sane():
    module = _load_runner()
    assert 60 <= module.STALL_LOG_SEC <= 600
    assert 5 <= module.FOCUS_RETRY_SEC <= 60


def test_pick_okww_log_prefers_live_file(tmp_path):
    module = _load_runner()
    logs = tmp_path / "logs"
    logs.mkdir()
    live = logs / "ok-script.log"
    live.write_text("live\n", encoding="utf-8")
    (logs / "ok-script.2026-11-27.log").write_text("old\n", encoding="utf-8")
    assert module._pick_okww_log(tmp_path) == live


def test_pick_okww_log_falls_back_to_newest_rotated(tmp_path):
    module = _load_runner()
    logs = tmp_path / "logs"
    logs.mkdir()
    older = logs / "ok-script.2026-11-26.log"
    newer = logs / "ok-script.2026-11-27.log"
    older.write_text("a\n", encoding="utf-8")
    newer.write_text("b\n", encoding="utf-8")
    assert module._pick_okww_log(tmp_path) == newer
    assert module._pick_okww_log(tmp_path / "missing") is None


def test_file_signature_tracks_growth(tmp_path):
    module = _load_runner()
    path = tmp_path / "ok-script.log"
    assert module._file_signature(path) is None
    path.write_text("one\n", encoding="utf-8")
    first = module._file_signature(path)
    assert first is not None and first[1] > 0
    path.write_text("one\ntwo\n", encoding="utf-8")
    second = module._file_signature(path)
    assert second is not None and second[1] > first[1]


def test_stall_triggers_after_quiet_window(tmp_path):
    module = _load_runner()
    root = tmp_path / "okww"
    logs = root / "logs"
    logs.mkdir(parents=True)
    (logs / "ok-script.log").write_text("line\n", encoding="utf-8")
    runner = _make(module, root)
    stopped = []
    _wire(runner, tmp_path, _FakeTask(), stopped=stopped)

    runner._check_log_stall()  # 建立基线
    assert not stopped and runner._stall_reason == ""

    runner._stall_since = time.monotonic() - module.STALL_LOG_SEC - 1
    runner._check_log_stall()
    assert stopped, "停滞应触发 stop_current_task"
    assert "无新增" in runner._stall_reason
    dump_text = (tmp_path / "stall_dump.log").read_text(encoding="utf-8", errors="replace")
    # 单线程测试进程里 faulthandler 输出 "Current thread"；runner 实跑是多线程，
    # all_threads=True 会把卡住的任务线程一起 dump（"Thread 0x..."）。
    assert "thread" in dump_text.lower(), "停滞时必须落全线程栈现场"


def test_log_growth_resets_stall_timer(tmp_path):
    module = _load_runner()
    root = tmp_path / "okww"
    logs = root / "logs"
    logs.mkdir(parents=True)
    live = logs / "ok-script.log"
    live.write_text("line\n", encoding="utf-8")
    runner = _make(module, root)
    stopped = []
    _wire(runner, tmp_path, _FakeTask(), stopped=stopped)

    runner._check_log_stall()
    runner._stall_since = time.monotonic() - module.STALL_LOG_SEC - 1
    live.write_text("line\nnext\n", encoding="utf-8")  # 有新日志 → 计时重置
    runner._check_log_stall()
    assert not stopped
    assert runner._stall_since is not None
    assert time.monotonic() - runner._stall_since < 5


def test_focus_guard_reactivates_background_window(tmp_path):
    module = _load_runner()
    runner = _make(module, tmp_path)
    hwnd_window = _FakeHwndWindow(foreground=False)
    _wire(runner, tmp_path, _FakeTask(), hwnd_window=hwnd_window)

    runner._ensure_game_foreground()
    assert hwnd_window.bring_calls == 1

    # 节流：立刻再调不会重复动作（此时已被切为前台，双重短路）
    hwnd_window.foreground = False
    runner._ensure_game_foreground()
    assert hwnd_window.bring_calls == 1


def test_focus_guard_skips_foreground_window(tmp_path):
    module = _load_runner()
    runner = _make(module, tmp_path)
    hwnd_window = _FakeHwndWindow(foreground=True)
    _wire(runner, tmp_path, _FakeTask(), hwnd_window=hwnd_window)
    runner._ensure_game_foreground()
    assert hwnd_window.bring_calls == 0


def test_guard_resets_when_task_not_running(tmp_path):
    module = _load_runner()
    root = tmp_path / "okww"
    (root / "logs").mkdir(parents=True)
    runner = _make(module, root)
    stopped = []
    _wire(runner, tmp_path, _FakeTask(running=False), stopped=stopped)
    runner._stall_signature = ("x", 1, 1.0)
    runner._stall_since = time.monotonic() - 9999
    runner._guard_running_task()
    assert runner._stall_signature is None and runner._stall_since is None
    assert not stopped
