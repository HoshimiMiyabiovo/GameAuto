# -*- coding: utf-8 -*-
"""ok-ww module runner 空闲自杀（2026-11-28）。

现场：GUI 被强杀/崩溃时 `aboutToQuit` 不跑，`shutdown_module_runtime` 的孤儿扫杀
也跟着没执行，常驻 headless runner 变成无窗口的 python 孤儿挂一整晚（PID 8448，
19:53 停任务后还挂着）。补丁：runner 自己判断「没有控制端连着 + 没有任务在跑 +
空闲超过 IDLE_EXIT_SEC」就收摊；有任务时永不触发。
"""

import importlib.util
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUNNER = PROJECT_ROOT / "tools" / "okww_module" / "runner.py"


def _load_runner():
    spec = importlib.util.spec_from_file_location("okww_runner_probe", RUNNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make(module):
    return module.ModuleRunner(
        okww_root=str(PROJECT_ROOT / "data" / "okww"),
        host="127.0.0.1",
        port=0,
        token="",
        ready_file=str(PROJECT_ROOT / ".temp" / "ready.json"),
        log_file=str(PROJECT_ROOT / ".temp" / "runner.log"),
        humanize_config="",
    )


def test_patch_anchors():
    src = RUNNER.read_text(encoding="utf-8", errors="replace")
    assert "IDLE_EXIT_SEC = max(15.0" in src
    assert "def _idle_exit_due(self)" in src
    assert "self._client_gone_at = time.monotonic()" in src
    assert "if self._idle_exit_due():" in src


def test_idle_exit_default_is_two_minutes():
    module = _load_runner()
    assert module.IDLE_EXIT_SEC >= 60


def test_idle_runner_exits_when_client_never_came():
    module = _load_runner()
    runner = _make(module)
    runner._started_monotonic = time.monotonic() - module.IDLE_EXIT_SEC - 1
    assert runner._idle_exit_due() is True


def test_recently_disconnected_client_exits_after_grace():
    module = _load_runner()
    runner = _make(module)
    runner._client_gone_at = time.monotonic() - module.IDLE_EXIT_SEC - 1
    assert runner._idle_exit_due() is True
    runner._client_gone_at = time.monotonic()
    assert runner._idle_exit_due() is False


def test_connected_client_blocks_exit():
    module = _load_runner()
    runner = _make(module)
    runner._client_gone_at = time.monotonic() - module.IDLE_EXIT_SEC - 1
    runner._active_client = object()
    assert runner._idle_exit_due() is False


def test_running_task_blocks_exit():
    module = _load_runner()
    runner = _make(module)
    runner._client_gone_at = time.monotonic() - module.IDLE_EXIT_SEC - 1
    runner._current_task = object()
    assert runner._idle_exit_due() is False
