# -*- coding: utf-8 -*-
"""签到链路韧性回归（2026-11-21）。

背景：08:11 四家签到全部成功；17:30 重复执行的启动前签到撞上"Windows 系统代理
指到 127.0.0.1:7897 但代理软件没启动"，urllib 得到 WinError 10061，日报里
genshin/honkai_sr/wuwa/endfield 的签到状态被失败轮覆盖。修复两件事：
  1. 启动前签到先探测本机代理端口，不通直接提示"系统代理未启动"；
  2. 同一天已有 success/already 的签到状态，禁止被后续 failed 覆盖
     （tracker 与 app_config 的 last_sign_* 双写路径都要保护）。

跑法：.venv\\Scripts\\python.exe tests\\suite_sign_resilience_20261121.py
"""

import shutil
import socket
import sys
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

# 沙箱兼容：tempfile 目录创建改默认权限（0700 受保护 DACL 在沙箱里写不进）
from sandbox_temp import install as _install_sandbox_temp

_install_sandbox_temp()

import game_auto.services.daily_progress_tracker as dpt  # noqa: E402
import game_auto.services.network_preflight as npf  # noqa: E402
import game_auto.services.startup_sign_runner as ssr  # noqa: E402
from game_auto.storage.workflow_db import WorkflowDB  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def _closed_port():
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def _listening_socket():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    return sock


def test_proxy_probe():
    print("[1] 系统代理端口探测")
    dead_port = _closed_port()
    with patch.object(npf, "getproxies",
                      return_value={"https": f"http://127.0.0.1:{dead_port}/"}):
        msg = npf.probe_system_proxy(timeout=0.3)
    check("本机代理端口拒绝 → 返回提示", bool(msg), msg)
    check("提示含具体端口", msg is not None and str(dead_port) in msg, msg)
    check("提示含未启动", msg is not None and "未启动" in msg, msg)

    live = _listening_socket()
    try:
        live_port = live.getsockname()[1]
        with patch.object(npf, "getproxies",
                          return_value={"https": f"127.0.0.1:{live_port}"}):
            msg = npf.probe_system_proxy(timeout=0.3)
        check("本机代理端口可连 → None", msg is None, msg)
    finally:
        live.close()

    with patch.object(npf, "getproxies",
                      return_value={"https": "http://10.255.255.1:8080"}):
        msg = npf.probe_system_proxy(timeout=0.3)
    check("非本机代理不探测/不误报", msg is None, msg)

    with patch.object(npf, "getproxies", return_value={}):
        msg = npf.probe_system_proxy(timeout=0.3)
    check("未配置代理 → None", msg is None, msg)


def test_proxy_guard_cached():
    print("[2] 同一批次只探测一次")
    dead_port = _closed_port()
    calls = []

    def fake_getproxies():
        calls.append(1)
        return {"https": f"127.0.0.1:{dead_port}"}

    guard = npf.make_proxy_guard()
    raised = 0
    with patch.object(npf, "getproxies", side_effect=fake_getproxies):
        for _ in range(3):
            try:
                guard()
            except npf.SystemProxyUnavailable:
                raised += 1
    check("guard 三次调用都提示", raised == 3, raised)
    check("guard 只探测一次", len(calls) == 1, len(calls))


def _find_task(tracker, game_key, task_key):
    snapshot = tracker.load_today()
    for game in snapshot.games:
        if game.game_key != game_key:
            continue
        for task in game.tasks:
            if task.task_key == task_key:
                return task
    return None


def check_tracker_protection(tmp_dir):
    print("[3] tracker 签到状态防降级")
    tracker = dpt.DailyProgressTracker(WorkflowDB(str(tmp_dir / "sign_guard.sqlite3")))

    tracker.record_sign_result("genshin", "原神", "米游社", "success", "摩拉x8000", uid="1")
    tracker.record_sign_result("genshin", "原神", "米游社", "failed", "系统代理未启动", uid="1")
    task = _find_task(tracker, "genshin", "sign_米游社")
    check("success 不被后续 failed 覆盖", task is not None and task.status == "success",
          getattr(task, "status", None))
    check("保留成功文案", task is not None and task.message == "摩拉x8000",
          getattr(task, "message", None))

    tracker.record_sign_result("honkai_sr", "崩铁", "米游社", "failed", "first")
    tracker.record_sign_result("honkai_sr", "崩铁", "米游社", "failed", "second")
    task = _find_task(tracker, "honkai_sr", "sign_米游社")
    check("failed 可被后续 failed 更新",
          task is not None and task.message == "second", getattr(task, "message", None))

    tracker.record_sign_result("honkai_sr", "崩铁", "米游社", "success", "ok")
    task = _find_task(tracker, "honkai_sr", "sign_米游社")
    check("failed → success 允许", task is not None and task.status == "success",
          getattr(task, "status", None))

    tracker.record_sign_result("wuwa", "鸣潮", "库街区", "already", "今天已经签到过")
    tracker.record_sign_result("wuwa", "鸣潮", "库街区", "failed", "系统代理未启动")
    task = _find_task(tracker, "wuwa", "sign_库街区")
    check("already 不被后续 failed 覆盖", task is not None and task.status == "already",
          getattr(task, "status", None))

    tracker.record_script_run("genshin", "原神", "BetterGI", "failed", "首轮失败")
    tracker.record_script_run("genshin", "原神", "BetterGI", "success", "重试成功")
    task = _find_task(tracker, "genshin", "script_BetterGI")
    check("脚本任务仍保持后写覆盖", task is not None and task.status == "success",
          getattr(task, "status", None))


def test_config_protection():
    print("[4] app_config last_sign_* 防降级")
    today = time.strftime("%Y-%m-%d")
    yesterday = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")

    cfg = {"wuwa_kujiequ": {
        "last_sign_date": today,
        "last_sign_status": "success",
        "last_sign_message": "签到成功",
    }}
    with patch.object(ssr.cm, "update_config", side_effect=lambda mutate: mutate(cfg)):
        ssr._write_sign_status_to_config("wuwa_kujiequ", "failed", "系统代理未启动")
    section = cfg["wuwa_kujiequ"]
    check("当日 success 不被 failed 覆盖", section["last_sign_status"] == "success", section)
    check("保留成功文案", section["last_sign_message"] == "签到成功", section)

    cfg = {"skendfield_sign": {
        "last_sign_date": yesterday,
        "last_sign_status": "success",
        "last_sign_message": "昨天签到成功",
    }}
    with patch.object(ssr.cm, "update_config", side_effect=lambda mutate: mutate(cfg)):
        ssr._write_sign_status_to_config("skendfield_sign", "failed", "系统代理未启动")
    section = cfg["skendfield_sign"]
    check("昨天 success 允许今日 failed", section["last_sign_status"] == "failed", section)
    check("日期更新为今天", section["last_sign_date"] == today, section)

    cfg = {"tajiduo_sign": {
        "last_sign_date": today,
        "last_sign_status": "failed",
        "last_sign_message": "旧失败",
    }}
    with patch.object(ssr.cm, "update_config", side_effect=lambda mutate: mutate(cfg)):
        ssr._write_sign_status_to_config("tajiduo_sign", "success", "签到成功")
    section = cfg["tajiduo_sign"]
    check("当日 failed → success 允许", section["last_sign_status"] == "success", section)


def test_bridge_status_protection():
    print("[4b] DailyView 手动签到状态防降级")
    from game_auto.settings import manager as cfg_manager
    from game_auto.ui.bridge_views.daily import DailyView

    today = time.strftime("%Y-%m-%d")
    cfg = {"wuwa_kujiequ": {
        "last_sign_date": today,
        "last_sign_status": "success",
        "last_sign_message": "签到成功",
    }}
    view = DailyView.__new__(DailyView)
    with patch.object(cfg_manager, "update_config",
                      side_effect=lambda mutate: mutate(cfg)):
        view._update_sign_status("wuwa_kujiequ", "failed", "系统代理未启动")
    section = cfg["wuwa_kujiequ"]
    check("Bridge: 当日 success 不被 failed 覆盖",
          section["last_sign_status"] == "success", section)
    check("Bridge: 保留成功文案",
          section["last_sign_message"] == "签到成功", section)


class _FakeTracker:
    def __init__(self):
        self.records = []

    def record_sign_result(self, *args, **kwargs):
        self.records.append((args, kwargs))


def test_startup_proxy_blocked():
    print("[5] 启动前签到遇到死代理：不发请求、提示明确")
    dead_port = _closed_port()
    tracker = _FakeTracker()
    config = {"mihoyo_sign": {
        "startup_sign": True,
        "cookie": "cookie_token=test-cookie;",
        "enabled_games": {"genshin": True},
    }}
    called = {"sign_games": 0}

    def fake_sign_games(*args, **kwargs):
        called["sign_games"] += 1
        return {"games": []}

    with patch.object(npf, "getproxies",
                      return_value={"https": f"127.0.0.1:{dead_port}"}), \
            patch.object(ssr.cm, "load", return_value=config), \
            patch.object(ssr, "get_tracker", return_value=tracker), \
            patch.object(ssr, "sign_games", side_effect=fake_sign_games):
        lines = ssr.run_startup_sign_tasks(["GenshinImpact"])

    check("代理未启动时不发起签到请求", called["sign_games"] == 0, called)
    check("返回行含系统代理提示",
          any("系统代理" in line and "未启动" in line for line in lines), lines)
    check("tracker 记录 failed 且带明确提示",
          any(args[3] == "failed" and "系统代理" in args[4]
              for args, _ in tracker.records),
          tracker.records)


def test_startup_proxy_ok():
    print("[6] 代理正常时签到照常执行")
    live = _listening_socket()
    tracker = _FakeTracker()
    config = {"mihoyo_sign": {
        "startup_sign": True,
        "cookie": "cookie_token=test-cookie;",
        "enabled_games": {"genshin": True},
    }}
    called = {"sign_games": 0}

    def fake_sign_games(*args, **kwargs):
        called["sign_games"] += 1
        return {"games": []}

    try:
        live_port = live.getsockname()[1]
        with patch.object(npf, "getproxies",
                          return_value={"https": f"127.0.0.1:{live_port}"}), \
                patch.object(ssr.cm, "load", return_value=config), \
                patch.object(ssr, "get_tracker", return_value=tracker), \
                patch.object(ssr, "sign_games", side_effect=fake_sign_games):
            ssr.run_startup_sign_tasks(["GenshinImpact"])
    finally:
        live.close()
    check("端口可连 → sign_games 被调用", called["sign_games"] == 1, called)


def main():
    # 临时库放 .temp（gitignore），别放 data/cache——那是生产数据目录，
    # record_task 的"测试进程不写生产库"守卫会把它当生产路径拦掉（2026-11-22）
    tmp_dir = Path(tempfile.mkdtemp(
        prefix="sign_resilience_",
        dir=str(PROJECT_ROOT / ".temp"),
    ))
    try:
        test_proxy_probe()
        test_proxy_guard_cached()
        check_tracker_protection(tmp_dir)
        test_config_protection()
        test_bridge_status_protection()
        test_startup_proxy_blocked()
        test_startup_proxy_ok()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
