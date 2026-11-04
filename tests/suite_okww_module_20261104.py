"""2026-11-04 ok-ww 模块化 runner + 战斗输入节流回归。

覆盖：
  1. humanize 配置解析/夹取/按队伍覆盖；
  2. 只对战斗中的中心左键做节流，UI/技能/切人不碰；
  3. 5 CPS + 抖动后的实际点击间隔；
  4. 真实 runner 的 ping/list_tasks/shutdown 协议（缺 ok-ww 安装时跳过）。

跑法：.venv\\Scripts\\python.exe tests\\suite_okww_module_20261104.py
"""

import io
import json
import sys
import time
import types
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "tools" / "okww_module"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import humanize  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class FakeTask:
    _gameauto_combat_depth = 1
    chars = []


def test_settings():
    print("[1] humanize 配置解析")
    settings = humanize.settings_from_dict(
        {
            "target_cps": 99,
            "jitter": 5,
            "micro_pause_sec": [0.2, 0.1],
            "teams": {"A__B__C": {"target_cps": 7}},
        }
    )
    check("target_cps 夹到 12", settings.target_cps == 12.0, settings.target_cps)
    check("jitter 夹到 0.9", settings.jitter == 0.9, settings.jitter)
    check("micro pause 自动交换上下限",
          settings.micro_pause_min == 0.1 and settings.micro_pause_max == 0.2)
    team = settings.for_team("a__b__c")
    check("队伍覆盖大小写不敏感", team.target_cps == 7.0, team.target_cps)
    check("队伍覆盖不污染全局", settings.target_cps == 12.0)


def test_should_throttle():
    print("[2] 只节流战斗中的中心左键")
    settings = humanize.settings_from_dict({"target_cps": 5, "jitter": 0, "micro_pause_chance": 0})
    throttle = humanize.CombatThrottle(settings)
    base = {
        "x": -1,
        "y": -1,
        "name": None,
        "interval": -1,
        "move": False,
        "key": "left",
    }
    task = FakeTask()
    check("战斗中中心左键要节流", throttle.should_throttle(task, **base) is True)
    check("非战斗不节流",
          throttle.should_throttle(task, **{**base, "x": -1}) is True)
    task._gameauto_combat_depth = 0
    check("depth=0 不节流", throttle.should_throttle(task, **base) is False)
    task._gameauto_combat_depth = 1
    check("显式坐标不节流", throttle.should_throttle(task, **{**base, "x": 100, "y": 200}) is False)
    check("点名元素不节流", throttle.should_throttle(task, **{**base, "name": "button"}) is False)
    check("右键不节流", throttle.should_throttle(task, **{**base, "key": "right"}) is False)
    check("move 不节流", throttle.should_throttle(task, **{**base, "move": True}) is False)
    check("调用方已要求 >= 0.2s 间隔时不重复节流",
          throttle.should_throttle(task, **{**base, "interval": 0.2}) is False)
    check("调用方 0.1s 间隔仍要节流",
          throttle.should_throttle(task, **{**base, "interval": 0.1}) is True)


def test_click_rate():
    print("[3] 5 CPS 节流后的实际点击节奏")
    clock = FakeClock()
    original_time = humanize.time
    humanize.time = clock
    try:
        settings = humanize.settings_from_dict(
            {"target_cps": 5, "jitter": 0, "micro_pause_chance": 0}
        )
        throttle = humanize.CombatThrottle(settings)
        task = FakeTask()
        for _ in range(21):
            throttle.wait_for_turn(task)
    finally:
        humanize.time = original_time
    check("21 次点击", throttle.clicks == 21, throttle.clicks)
    check("20 个间隔 * 0.2s = 4.0s", abs(clock.now - 4.0) < 0.001, clock.now)
    check("没有额外 micro pause", throttle.micro_pauses == 0)


def test_team_override():
    print("[4] 按当前队伍覆盖 CPS")
    class A:
        pass

    class B:
        pass

    class C:
        pass

    settings = humanize.settings_from_dict(
        {"target_cps": 5, "teams": {"A__B__C": {"target_cps": 10}}}
    )
    throttle = humanize.CombatThrottle(settings)
    task = FakeTask()
    task.chars = [A(), B(), C()]
    resolved = throttle._resolve_team(task)
    check("识别队伍键并覆盖到 10 CPS", resolved.target_cps == 10.0, resolved.target_cps)
    check("10 CPS 基础间隔 0.1s", abs(resolved.base_interval - 0.1) < 0.001, resolved.base_interval)


def test_controller_module_branch():
    print("[5] controller 模块分支：启动 session → 等结果 → 收尾")
    from game_auto.runtime import automation_controller as ac
    from game_auto.runtime.okww_module_session import ModuleTaskResult

    check(
        "module_mode 只对 ok-ww 生效",
        ac.AutomationController._module_mode_active(
            {"script_name": "ok-ww", "module_mode": {"enabled": True}}
        )
        is True
        and ac.AutomationController._module_mode_active(
            {"script_name": "ok-ef", "module_mode": {"enabled": True}}
        )
        is False,
    )

    class FakeSession:
        pid = 12345

        def __init__(self):
            self.stopped = False

        def start(self, stop_check=None):
            return self

        def run_task(self, task, *, timeout, close_game):
            time.sleep(0.05)
            return ModuleTaskResult(task=task, success=True, duration=0.05)

        def stop_task(self):
            self.stopped = True

        def stop(self):
            self.stopped = True

        def is_alive(self):
            return True

        def log_tail(self, lines=40):
            return ""

    # The import is local to _start_module_session; patch the class method on
    # the actual module object.
    from game_auto.runtime import okww_module_session as oms

    original_method = oms.OkwwModuleSession.from_app_config
    oms.OkwwModuleSession.from_app_config = classmethod(lambda cls, *a, **k: FakeSession())
    controller = None
    try:
        controller = ac.AutomationController()
        controller._heartbeat = types.SimpleNamespace(
            beat=lambda: None, start=lambda *a, **k: None, stop=lambda: None
        )
        controller._click_game_window_after_launch = lambda *a, **k: None
        config = {
            "game": {"process_name": "Wuthering Waves.exe"},
            "timeouts": {"monitor": 30},
            "initial_wait": 0,
            "post_launch_click_game": False,
            "module_mode": {
                "enabled": True,
                "task": "DailyTask",
                "timeout": 30,
                "close_game": False,
            },
        }
        ok, pid = controller._start_module_session(config, "鸣潮")
        check("session 启动成功", ok is True and pid == 12345, (ok, pid))
        assert controller._module_task_thread is not None
        controller._module_task_thread.join(5)
        result, duration, reason = controller._monitor_module_task(
            "WutheringWaves", config, None, runtime_processes={}
        )
        check("模块任务判成功", result is True, result)
        check("结束原因 module_done", reason == "module_done", reason)
        check("结果已写入 controller", controller._module_task_result is not None)
    finally:
        oms.OkwwModuleSession.from_app_config = original_method
        try:
            controller._module_session = None
        except Exception:
            pass


def test_runner_protocol():
    print("[6] 真实 runner 协议（无 ok-ww 安装时跳过）")
    config_path = PROJECT_ROOT / "app_config.json"
    if not config_path.is_file():
        print("  SKIP app_config.json 不存在")
        return
    try:
        app_config = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"  SKIP app_config.json 读取失败: {exc}")
        return

    wuwa_config = (app_config.get("apps") or {}).get("WutheringWaves") or {}
    discovery = wuwa_config.get("_discovery") or {}
    script_exe = Path(str(discovery.get("script_exe_path") or ""))
    runtime_root = PROJECT_ROOT / "data" / "okww" / "src"
    if not script_exe.is_file() or not (runtime_root / "main.py").is_file():
        print("  SKIP 未找到 ok-ww 安装或 data/okww/src")
        return

    from game_auto.runtime.okww_module_session import OkwwModuleSession

    module_config = dict(wuwa_config)
    module_config["module_mode"] = {
        "enabled": True,
        "runtime_root": str(runtime_root),
        "humanize_config": str(PROJECT_ROOT / "data" / "okww" / "humanize.json"),
        "startup_timeout": 120,
    }
    session = OkwwModuleSession.from_app_config(module_config)
    try:
        session.start()
        pong = session.request("ping", timeout=10)
        check("ping 返回 pong", pong.get("pong") is True, pong)
        tasks = session.list_tasks()
        names = [item["class"] for item in tasks.get("onetime", [])]
        check("任务列表包含 DailyTask", "DailyTask" in names, names)
        check("runner 版本是 v3.7.17", session.ready.get("version") == "v3.7.17",
              session.ready.get("version"))
    finally:
        session.stop()


def main():
    test_settings()
    test_should_throttle()
    test_click_rate()
    test_team_override()
    test_controller_module_branch()
    test_runner_protocol()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
