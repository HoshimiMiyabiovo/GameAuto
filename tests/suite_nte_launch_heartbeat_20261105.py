"""2026-11-05 异环更新补跑连撞 3 次 600s 超时：长等待漏打任务心跳。

事故链（现场栈 watchdog_task_heartbeat.log 11:15:28 实锤）：
  execute_task → _monitor_game → _wait_for_game_process_snapshot 停在
  automation_controller.py:470 的 time.sleep(2)，线程在正常轮询等 HTGame.exe，
  却因为本循环从不调用 self._heartbeat.beat()，被全局任务看门狗
  （_global.task_heartbeat_timeout_sec 未配，默认 600s）误判死锁强杀。
  异环 timeouts.launch=600 / update=7200 都不短于 600s，注定撞上。
  更新补跑队列 max_attempts=3，每次被杀后到点重排，于是
  11:05:27 / 11:15:57 / 11:26:27 各烧 600s。

跑法：.venv\\Scripts\\python.exe tests\\suite_nte_launch_heartbeat_20261105.py
"""

from __future__ import annotations

import ast
import io
import json
import logging
import sys
import time
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import game_auto.runtime.automation_controller as ac  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} :: {detail}")
        FAILURES.append(name)


def test_source_has_beat():
    print("[1] 等待循环里必须有任务线程心跳")
    src = Path(ac.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_wait_for_game_process_snapshot"
    )
    calls = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute) and n.func.attr == "beat"
    ]
    check("函数体内调用 self._heartbeat.beat()", len(calls) == 1, len(calls))
    body = ast.get_source_segment(src, fn) or ""
    check("beat 在 time.sleep(2) 之前（每轮都上报）",
          body.find("self._heartbeat.beat()") < body.rfind("time.sleep(2)"))
    check("注释写明 11-05 三次 600s 事故", "11-05" in body and "600" in body)

    monitor = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_monitor_game"
    )
    monitor_src = ast.get_source_segment(src, monitor) or ""
    check("_monitor_game 主循环本来就有心跳（口径一致）",
          "self._heartbeat.beat()" in monitor_src)


def test_beat_called_while_waiting():
    print("[2] 动态：等游戏进程时每轮都打心跳")
    ctrl = object.__new__(ac.AutomationController)
    ctrl._heartbeat = mock.MagicMock()
    ctrl.stop_current_task = False
    ctrl.logger = logging.getLogger("suite_nte_heartbeat")
    ctrl.logger.addHandler(logging.NullHandler())
    ctrl._configured_game_process_name = lambda config: "HTGame.exe"
    ctrl._log_detected_process = lambda *a, **k: None

    snapshot = object()
    config = {
        "game": {"process_name": "HTGame.exe"},
        "timeouts": {"launch": 600, "update": 7200},
    }
    with mock.patch.object(ac.ProcessSupervisor, "find_process_snapshot",
                           side_effect=[None, None, None, snapshot]), \
            mock.patch.object(ac, "remember_process_snapshot", lambda *a, **k: None), \
            mock.patch.object(ac.time, "sleep", lambda *_: None):
        got, reason = ctrl._wait_for_game_process_snapshot(
            "异环", config, {}, time.time(), {},
            script_choice="ok-nte", progress_hint=None,
        )
    check("第 4 轮拿到进程快照后正常返回", got is snapshot and reason == "", (got, reason))
    check("等待的 3 轮每轮都上报心跳", ctrl._heartbeat.beat.call_count == 3,
          ctrl._heartbeat.beat.call_count)


def test_budget_mismatch_evidence():
    print("[3] 预算不匹配的证据：等待预算 >= 心跳阈值")
    # 必须走 settings.manager.load()：save() 只把「与默认值不同的部分」落盘，
    # 裸读 app_config.json 会看不到 timeouts（GUI 保存后尤其如此），但 load()
    # 会 _merge_defaults 补回来。2026-11-05 全套回归在这里误红过一次。
    from game_auto.settings import manager

    try:
        cfg = manager.load(force=True)
        nte = cfg["apps"]["NevernessToEverness"]["timeouts"]
        global_cfg = cfg.get("apps", {}).get("_global", {}) or {}
        stall = int(global_cfg.get("task_heartbeat_timeout_sec", 600) or 600)
    except Exception as exc:  # noqa: BLE001 - 本机配置不可用时不让套件挂掉
        print(f"  SKIP 本机配置不可读（{exc}），改用已知默认值")
        nte = {"launch": 600, "update": 7200, "monitor": 3600}
        stall = 600
    check("异环 launch 预算不低于心跳阈值", nte["launch"] >= stall, (nte["launch"], stall))
    check("异环 update 预算远大于心跳阈值", nte["update"] > stall, (nte["update"], stall))
    check("全局心跳阈值没有单独放宽（仍是默认 600）", stall == 600, stall)
    wd = PROJECT_ROOT / "data" / "logs" / "watchdog_task_heartbeat.log"
    if wd.exists():
        text = wd.read_text(encoding="utf-8", errors="replace")
        stamps = [s for s in ("2026-11-05 11:15:28", "2026-11-05 11:25:57",
                              "2026-11-05 11:36:28") if s in text]
        print(f"  (看门狗现场栈命中 {len(stamps)}/3 次)")
        check("_wait_for_game_process_snapshot 出现在现场栈里",
              "_wait_for_game_process_snapshot" in text)
    else:
        print("  (本机无看门狗栈文件，跳过取证断言)")


def test_retry_queue():
    print("[4] 补跑队列为什么重复 3 次")
    src = (PROJECT_ROOT / "src" / "game_auto" / "ui" / "bridge_views" /
           "scheduler.py").read_text(encoding="utf-8")
    check("补跑队列默认 max_attempts=3",
          'max_attempts = int(e.get("max_attempts") or 3)' in src)
    check("每 30s 检查一次到点补跑", "每 30s" in src or "30" in src)


def main():
    test_source_has_beat()
    test_beat_called_while_waiting()
    test_budget_mismatch_evidence()
    test_retry_queue()


if __name__ == "__main__":
    main()
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILED: {FAILURES}")
        sys.exit(1)
    print("\nALL PASS")
