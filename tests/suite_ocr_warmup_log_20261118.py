# -*- coding: utf-8 -*-
"""OCR 预热 + 文件日志兜底回归（2026-11-18）。

背景（11-18 实锤）：薅羊毛 TELEAGENT 步骤 1 的首次 OCR 撞上冷加载，22:30:47 →
22:52:53 卡了 22 分钟（历史同机 3-6s），用户看到的是「应用启动成功但脚本没反应」；
同一时段 `data/logs/auto_runner.log` 停在 21:16 —— AutomationController 是懒加载的，
薅羊毛走 `_run_wool_backend → run_wool_background → run_flow` 不经过 controller，
`_init_logger` 从未执行，整段运行日志只存在于 WebUI 内存缓冲。

本套件锁：
[1] `ensure_file_log_handler`：首次挂载 / 二次幂等 / 已有 QueueHandler 跳过 /
    禁用开关（测试环境）跳过；
[2] `_get_onnxocr_engine` 并发只构建一次（预热线程与步骤线程会同时到达）；
[3] `warmup_onnxocr_engine`：成功返回 True、引擎不可用返回 False、异常吞掉不抛出；
[4] 薅羊毛批次入口确实启动预热（接线锚点）；
[5] GUI / WebUI 启动边界接线（函数名锚点）。

跑法：.venv\\Scripts\\python.exe -m pytest tests\\suite_ocr_warmup_log_20261118.py -q
"""

import io
import logging
import queue
import shutil
import sys
import tempfile
import threading
import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import game_auto.runtime.background_template_runner as btr  # noqa: E402
import game_auto.runtime.ocr_click_tester as oct  # noqa: E402
import game_auto.services.logging_setup as ls  # noqa: E402

FAILURES = []


def check(label, ok, detail=None):
    mark = "PASS" if ok else "FAIL"
    print(f"  {mark} {label}" + (f"  ← {detail}" if not ok and detail is not None else ""))
    if not ok:
        FAILURES.append(label)


def test_file_log_handler():
    print("[1] ensure_file_log_handler")
    # 项目约定：套件临时目录钉 data/cache，不走系统 TEMP。本机实测 pytest 默认
    # basetemp（C:\Users\admin\AppData\Local\Temp\pytest-of-admin）会 PermissionError。
    cache = PROJECT_ROOT / "data" / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    tmp_path = Path(tempfile.mkdtemp(prefix="ocr_warmup_log_", dir=str(cache)))
    try:
        _check_file_log_handler(tmp_path)
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)


def _check_file_log_handler(tmp_path):
    log = logging.getLogger("TestFileLog")
    log.setLevel(logging.INFO)
    log.handlers[:] = []
    target = tmp_path / "auto_runner.log"

    from logging.handlers import QueueHandler

    with ExitStack() as st:
        st.enter_context(patch.object(ls, "log_file_disabled", return_value=False))
        st.enter_context(patch("game_auto.settings.paths.LOG_FILE", target))
        st.enter_context(
            patch("game_auto.settings.manager.get_log_config", return_value={"backup_count": 0})
        )
        h1 = ls.ensure_file_log_handler(log)
        check("首次挂载返回 FileHandler", isinstance(h1, logging.FileHandler), type(h1))
        h2 = ls.ensure_file_log_handler(log)
        check("二次调用幂等返回 None", h2 is None)
        file_handlers = [h for h in log.handlers if isinstance(h, logging.FileHandler)]
        check("只挂一个 FileHandler", len(file_handlers) == 1, log.handlers)
        log.warning("hello-file-log")
        for h in file_handlers:
            h.flush()
        text = target.read_text(encoding="utf-8") if target.is_file() else ""
        check("日志真正落盘", "hello-file-log" in text, text[-200:])
        for h in list(log.handlers):
            h.close()
            log.removeHandler(h)

    log_q = logging.getLogger("TestQueueLog")
    log_q.handlers[:] = []
    log_q.addHandler(QueueHandler(queue.Queue()))
    with patch.object(ls, "log_file_disabled", return_value=False):
        check("已有 QueueHandler（controller 已接管）跳过", ls.ensure_file_log_handler(log_q) is None)
    log_q.handlers[:] = []

    log_off = logging.getLogger("TestDisabledLog")
    log_off.handlers[:] = []
    with patch.object(ls, "log_file_disabled", return_value=True):
        check("禁用开关（测试进程）跳过", ls.ensure_file_log_handler(log_off) is None)
    check("禁用时未挂任何 handler", not log_off.handlers, log_off.handlers)


def test_engine_single_build():
    print("[2] _get_onnxocr_engine 并发构建只一次")
    getter = getattr(oct, "_get_onnxocr_engine", None)
    if not callable(getter):
        print("  SKIP 环境缺 onnxocr/onnxruntime")
        return

    calls = []
    build_lock = threading.Lock()

    class Stub:
        def providers(self):
            return {"det": ["CPUExecutionProvider"], "rec": ["CPUExecutionProvider"]}

    def fake_build(model_set, logger=None):
        with build_lock:
            calls.append(model_set)
        time.sleep(0.15)
        return Stub()

    old = oct._onnxocr_engine
    oct._onnxocr_engine = None
    got = []
    try:
        with patch("game_auto.runtime.onnxocr_precision.build_onnxocr_engine", fake_build):
            threads = [
                threading.Thread(
                    target=lambda: got.append(getter(logging.getLogger("t"))),
                )
                for _ in range(4)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        check("并发 4 次只构建一次", len(calls) == 1, calls)
        check("所有调用拿到同一实例", len({id(x) for x in got}) == 1, [id(x) for x in got])
    finally:
        oct._onnxocr_engine = old


def test_warmup():
    print("[3] warmup_onnxocr_engine")
    fn = getattr(oct, "warmup_onnxocr_engine", None)
    check("warmup 函数存在", callable(fn))
    if not callable(fn):
        return
    lg = logging.getLogger("TestWarmup")
    with patch.object(oct, "_get_onnxocr_engine", lambda logger=None: object()):
        check("预热成功返回 True", fn(lg) is True)
    with patch.object(oct, "_get_onnxocr_engine", lambda logger=None: None):
        check("引擎不可用返回 False", fn(lg) is False)
    with patch.object(oct, "_get_onnxocr_engine", side_effect=RuntimeError("boom")):
        check("预热异常吞掉返回 False", fn(lg) is False)


def test_wool_warmup_wiring():
    print("[4] 薅羊毛批次入口启动预热")
    lg = logging.getLogger("TestWool")
    calls = []
    with patch.object(btr, "_start_ocr_warmup", side_effect=lambda logger: calls.append(logger)), \
            patch("game_auto.runtime.foreground_flow_runner._load_flow_config",
                  return_value={"enabled": True}), \
            patch.object(btr, "run_flow", return_value=(True, "ok")), \
            patch.object(btr, "_close_wool_flow", return_value=False):
        ok, msg = btr.run_wool_flow_background(lg, ["MINIMAX"])
    check("批前预热被调用一次", len(calls) == 1, calls)
    check("子流程照常执行", ok is True and "MINIMAX ✅" in msg, msg)


def test_startup_wiring():
    print("[5] GUI / WebUI 启动边界接线")
    import inspect

    from game_auto.app import gui
    from game_auto.ui.webui import state

    check(
        "gui.main 调 ensure_file_log_handler",
        "ensure_file_log_handler" in inspect.getsource(gui.main),
    )
    check(
        "webui ensure_log_handler 调 ensure_file_log_handler",
        "ensure_file_log_handler" in inspect.getsource(state.ensure_log_handler),
    )


def main():
    test_file_log_handler()
    test_engine_single_build()
    test_warmup()
    test_wool_warmup_wiring()
    test_startup_wiring()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED {len(FAILURES)}: {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
