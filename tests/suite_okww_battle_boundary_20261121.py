# -*- coding: utf-8 -*-
"""内置 ok-ww working 树补丁：战斗边界与宏脱战探针（2026-11-21）。

ok-ww 自带的战斗判定只有"在战斗 / 不在战斗"两态：配队脚本（自定义角色代码）
一整轮 30-60 秒，期间还把判定关掉（skip_combat_check），框架看不见这一场什么时候
结束，于是下一场从上一轮中段接着跑、启动轴永远轮不到（用户实机：每次结束战斗再打
下一场，不是从头开始，而是从上一次不知道是什么时候开始）。补丁三处：
1. CombatCheck：脱战够久再进战斗就算新一场，重建角色实例（自定义类 __init__ 重跑）；
2. CombatCheck：宏期间借执行器节拍做脱战探针，目标锁+血条一起消失 4 秒掐断这一轮；
3. TaskExecutor.check_enabled 接探针（一条龙里真正在打的是嵌套跑的 DomainTask，
   current_task 只是 DailyTask，所以还要扫 onetime_tasks/trigger_tasks）、
   BaseChar.send_liberation_key 把演出窗口告诉判定。

补丁只活在 data/okww 的 working 树（gitignore 内），重装 ok-ww 会丢，所以用锚点锁住。
"""

import ast
import builtins
import types
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WORKING = (
    PROJECT_ROOT
    / "data"
    / "okww"
    / "ok-ww-v3.8.3"
    / "data"
    / "apps"
    / "ok-ww"
    / "working"
)


def _read(rel: str) -> str:
    path = WORKING / rel
    if not path.is_file():
        pytest.skip(f"内置 ok-ww working 树不在本机：{path}")
    return path.read_text(encoding="utf-8", errors="replace")


def test_new_battle_rebuilds_team():
    """新一场重建角色实例：自定义脚本的 __init__ 重跑，启动轴才会重新起跑。"""
    src = _read("src/combat/CombatCheck.py")
    assert "BATTLE_REBUILD_GAP = 3.0" in src
    assert "def _begin_new_battle" in src
    assert "self.combat_id += 1" in src
    assert "def _team_readable" in src

    entry = src.split("if self._begin_new_battle() and self._team_readable():", 1)
    assert len(entry) == 2, "进战斗分支没有接新战斗重建"
    body = entry[1][:800]
    assert "self.chars = [None, None, None]" in body
    # 重读后必须再验一次读屏：三人认不全时 apply_team_char_classes 不挂自定义类，
    # 带着残缺队伍开打 = 下一场退回内置轮换
    assert "self.load_chars() and self._team_readable()" in body
    # 读屏失败要退回旧实例，不能把队伍读成 None 继续打
    assert "self.chars = previous_chars" in body

    # 深塔强制进战斗同样走新战斗判定，同样要能回退
    base = _read("src/task/BaseCombatTask.py")
    forced = base.split("def force_abyss_combat", 1)[1][:600]
    assert "_begin_new_battle" in forced
    assert "self.chars = previous_chars" in forced
    assert "self.load_chars() and self._team_readable()" in forced


def test_macro_combat_probe():
    """宏期间唯一的脱战出口：目标锁+血条一起消失 4 秒就掐断这一轮。"""
    src = _read("src/combat/CombatCheck.py")
    assert "def on_executor_tick" in src
    assert "COMBAT_LOST_TIME = 4.0" in src
    probe = src.split("def on_executor_tick", 1)[1][:2200]
    assert "raise NotInCombatException" in probe
    assert "self.has_target() or self.check_health_bar()" in probe
    # 掐断前先把框架状态收回"不在战斗"，否则 sleep_check 会在战斗循环外再抛一次
    assert "self.reset_to_false(reason='macro combat probe out of combat')" in probe
    # 探针读目标时不能走 has_target 的 ESC 纠错分支（那是会发按键的）
    assert "not getattr(self, '_combat_probe_running', False)" in src.split(
        "def has_target", 1)[1][:2600]
    # 三重免判：非宏期间、解放演出窗口、深塔重开流程
    assert "skip_combat_check" in probe
    assert "_liberation_sent_until" in probe
    assert "_combat_probe_suspended" in probe
    # 重入保护：探针自己读屏会再触发 check_enabled
    assert "_combat_probe_running" in probe


def test_executor_and_liberation_wiring():
    """执行器把节拍接给任务；发解放键时把演出窗口告诉战斗判定。"""
    executor = _read("ok/task/TaskExecutor.py")
    tick = executor.split("def check_enabled", 1)[1][:1600]
    assert "on_executor_tick" in tick
    # 一条龙（DailyTask）里战斗实例是嵌套跑的，current_task 只指向最外层
    assert "for nested_task in" in tick and "onetime_tasks" in tick

    base_char = _read("src/char/BaseChar.py")
    send = base_char.split("def send_liberation_key", 1)[1][:900]
    assert "mark_liberation_sent" in send

    combat = _read("src/combat/CombatCheck.py")
    assert "def mark_liberation_sent" in combat
    assert "LIBERATION_BLIND_TIME = 7.0" in combat
    check = combat.split("def do_check_in_combat", 1)[1][:200]
    assert "_liberation_sent_until" in check


class _TickTask:
    _enabled = True

    def __init__(self, skip=False):
        self.skip_combat_check = skip
        self.calls = 0

    def on_executor_tick(self):
        self.calls += 1


class _TickExecutor:
    """只喂 check_enabled 用到的那几根线，字段名与真实 TaskExecutor 一致。"""

    paused = False
    debug_mode = False
    current_job = None
    _exclusive_owner = None

    def __init__(self, current_task, onetime, trigger):
        self.current_task = current_task
        self.onetime_tasks = onetime
        self.trigger_tasks = trigger

    def blocked_by_pause(self):
        return False

    def sleep(self, sec):
        pass

    def is_executor_thread(self):
        return True


def _load_executor_check_enabled():
    tree = ast.parse(_read("ok/task/TaskExecutor.py"))
    cls = next(n for n in tree.body
               if isinstance(n, ast.ClassDef) and n.name == "TaskExecutor")
    fn = next(n for n in cls.body
              if isinstance(n, ast.FunctionDef) and n.name == "check_enabled")
    module = ast.Module(body=[fn], type_ignores=[])
    ast.fix_missing_locations(module)
    code = compile(module, "<TaskExecutor.check_enabled>", "exec")
    namespace = {"logger": _Logger(), "TaskDisabledException": _TaskDisabled}
    exec(code, namespace)
    return namespace["check_enabled"]


def test_nested_combat_task_gets_executor_tick():
    """一条龙：current_task 是 DailyTask，真正在打的 DomainTask 也必须收到节拍。

    ok-ww 的嵌套任务走 run_task_by_class，不改 executor.current_task；配队宏的
    skip_combat_check 挂在 DomainTask 实例上 —— 只 tick current_task 时，宏跑在
    time.sleep 里的那几十秒探针一次都不执行，等于没打这个补丁。
    """
    check_enabled = _load_executor_check_enabled()
    daily = _TickTask()
    domain = _TickTask(skip=True)
    idle = _TickTask()
    trigger = _TickTask(skip=True)
    executor = _TickExecutor(daily, [domain, idle], [trigger, daily])

    check_enabled(executor, check_pause=False)

    assert daily.calls == 1, "外层任务照旧要收到一次节拍"
    assert domain.calls == 1, "嵌套的 DomainTask 没收到节拍，宏期间探针等于没有"
    assert trigger.calls == 1, "触发任务同样要覆盖"
    assert idle.calls == 0, "没开 skip_combat_check 的任务不该付探针代价"


def test_patch_backups_present():
    """改前备份在 working 树里：出问题能原地回滚，也说明这是本项目的补丁。"""
    missing = [
        rel
        for rel in (
            "src/combat/CombatCheck.py.bak_20261121",
            "src/char/BaseChar.py.bak_20261121",
            "src/task/BaseCombatTask.py.bak_20261121",
            "ok/task/TaskExecutor.py.bak_20261121",
        )
        if not (WORKING / rel).is_file()
    ]
    if not WORKING.is_dir():
        pytest.skip("内置 ok-ww working 树不在本机")
    assert not missing, f"缺改前备份：{missing}"


# ---------------------------------------------------------------------------
# 行为桩测：不启动游戏、不 import ok-ww 的重依赖，只把方法体抽出来跑
# ---------------------------------------------------------------------------

class _Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def time(self):
        return self.now


class _Logger:
    def __init__(self):
        self.lines = []

    def debug(self, msg):
        self.lines.append(("debug", msg))

    def info(self, msg):
        self.lines.append(("info", msg))


class _TaskDisabled(Exception):
    pass


class _NotInCombat(Exception):
    pass


class _Probe:
    BATTLE_REBUILD_GAP = 3.0
    COMBAT_PROBE_INTERVAL = 0.7
    COMBAT_LOST_TIME = 4.0
    LIBERATION_BLIND_TIME = 7.0

    def __init__(self):
        self._combat_probe_running = False
        self.skip_combat_check = False
        self._in_liberation = False
        self._combat_probe_suspended = False
        self._in_combat = False
        self._liberation_sent_until = 0.0
        self._combat_probe_at = 0.0
        self._combat_lost_since = 0.0
        self.target = False
        self.health = False
        self.target_calls = 0
        self.reset_calls = 0
        self.reset_reason = None
        # _begin_new_battle 用
        self.combat_id = 0
        self._last_in_combat_at = 0.0
        self.log_lines = []

    def has_target(self):
        self.target_calls += 1
        return self.target

    def check_health_bar(self):
        return self.health

    def reset_to_false(self, reason=""):
        self.reset_calls += 1
        self.reset_reason = reason
        self._in_combat = False
        return False

    def tr(self, text):
        return text

    def log_info(self, text):
        self.log_lines.append(text)


def _load_method(name):
    """从 working 树的 CombatCheck.py 抽出单个方法，配上可控时钟/日志桩。"""
    tree = ast.parse(_read("src/combat/CombatCheck.py"))
    cls = next(node for node in tree.body
               if isinstance(node, ast.ClassDef) and node.name == "CombatCheck")
    fn = next(node for node in cls.body
              if isinstance(node, ast.FunctionDef) and node.name == name)
    module = ast.Module(body=[fn], type_ignores=[])
    ast.fix_missing_locations(module)
    code = compile(module, f"<CombatCheck.{name}>", "exec")

    real_import = builtins.__import__

    def fake_import(module_name, globals=None, locals=None, fromlist=(), level=0):
        if module_name == "src.task.BaseCombatTask":
            return types.SimpleNamespace(NotInCombatException=_NotInCombat)
        return real_import(module_name, globals, locals, fromlist, level)

    clock = _Clock()
    logger = _Logger()
    namespace = {
        "time": clock,
        "logger": logger,
        "TaskDisabledException": _TaskDisabled,
        "__builtins__": dict(vars(builtins)),
    }
    namespace["__builtins__"]["__import__"] = fake_import
    exec(code, namespace)
    return namespace[name], clock, logger


def test_probe_raises_after_combat_lost_and_resets_framework_state():
    tick, clock, _ = _load_method("on_executor_tick")
    probe = _Probe()
    probe.skip_combat_check = True
    probe._in_combat = True

    clock.now = 1000.0
    tick(probe)  # 第一次探针：开始计"目标/血条都没了"
    assert probe._combat_lost_since == 1000.0
    assert probe.reset_calls == 0

    clock.now = 1003.5
    tick(probe)  # 3.5s 还没到 4s
    assert probe.reset_calls == 0

    clock.now = 1004.3  # 距上一次探针也超过 0.7s 间隔
    with pytest.raises(_NotInCombat):
        tick(probe)
    # 掐断前必须先把 _in_combat 收回 False，否则任务里的 sleep_check 会在
    # combat_once 之外再抛一次 NotInCombatException
    assert probe.reset_calls == 1
    assert probe.reset_reason == "macro combat probe out of combat"
    assert probe._in_combat is False


def test_probe_does_not_fire_when_target_or_health_visible():
    tick, clock, _ = _load_method("on_executor_tick")
    probe = _Probe()
    probe.skip_combat_check = True
    probe._in_combat = True

    clock.now = 2000.0
    probe.target = True
    tick(probe)
    assert probe._combat_lost_since == 0.0

    clock.now = 2001.0
    probe.target = False
    probe.health = True
    tick(probe)
    assert probe._combat_lost_since == 0.0


def test_probe_liberation_blind_window_and_suspend():
    tick, clock, _ = _load_method("on_executor_tick")
    probe = _Probe()
    probe.skip_combat_check = True
    probe._in_combat = True
    probe._liberation_sent_until = 3005.0

    clock.now = 3000.0
    tick(probe)
    assert probe._combat_lost_since == 0.0

    clock.now = 3004.9  # 还在解放演出免判窗口
    tick(probe)
    assert probe._combat_lost_since == 0.0

    clock.now = 3005.1  # 窗口结束，开始重新计时
    tick(probe)
    assert probe._combat_lost_since == 3005.1

    clock.now = 3009.0
    tick(probe)
    assert probe.reset_calls == 0

    clock.now = 3009.8
    with pytest.raises(_NotInCombat):
        tick(probe)

    # 深塔重开挑战：整段时间闭嘴
    probe2 = _Probe()
    probe2.skip_combat_check = True
    probe2._in_combat = True
    probe2._combat_probe_suspended = True
    clock.now = 4000.0
    tick(probe2)
    clock.now = 4010.0
    tick(probe2)
    assert probe2.reset_calls == 0
    assert probe2._combat_lost_since == 0.0


def test_probe_skips_when_macro_flag_off_or_probe_reentrant():
    tick, _, _ = _load_method("on_executor_tick")
    probe = _Probe()
    probe._in_combat = True
    tick(probe)
    assert probe.target_calls == 0  # skip_combat_check=False：不读屏

    probe.skip_combat_check = True
    probe._combat_probe_running = True
    tick(probe)
    assert probe.target_calls == 0  # 重入保护：探针自己读屏时不再进


def test_begin_new_battle_uses_gap():
    begin, clock, _ = _load_method("_begin_new_battle")
    probe = _Probe()

    clock.now = 1000.0
    assert begin(probe) is True  # 从没进过战斗 -> 第一场必然是新场
    assert probe.combat_id == 1
    # 首场不能报 (time.time() - 0) 的假间隔
    assert any("new battle #1 (first battle)" in line for line in probe.log_lines)

    probe._last_in_combat_at = 1000.0
    clock.now = 1001.0
    assert begin(probe) is False  # 1s 内再进 = 战斗判定抖动，还是同一场
    assert probe.combat_id == 2

    probe._last_in_combat_at = 1000.0
    clock.now = 1005.0
    assert begin(probe) is True  # 脱战 5s 后进 = 下一场
    assert probe.combat_id == 3
    assert any("new battle #3 after 5.0s" in line for line in probe.log_lines)


def main() -> int:
    """CI 包装层是 `python suite.py` + 退出码，不认 pytest 的用例收集。

    没有这个入口时断言失败也返回 0——2026-11-21 实测的假通过。本机没有 working
    树时打印 SKIP，绝不伪装成"验证过"。
    """
    tests = [
        test_new_battle_rebuilds_team,
        test_macro_combat_probe,
        test_executor_and_liberation_wiring,
        test_nested_combat_task_gets_executor_tick,
        test_patch_backups_present,
        test_probe_raises_after_combat_lost_and_resets_framework_state,
        test_probe_does_not_fire_when_target_or_health_visible,
        test_probe_liberation_blind_window_and_suspend,
        test_probe_skips_when_macro_flag_off_or_probe_reentrant,
        test_begin_new_battle_uses_gap,
    ]
    failures = []
    skipped = []
    for test in tests:
        try:
            test()
        except pytest.skip.Exception as exc:  # working 树不在本机
            skipped.append(test.__name__)
            print(f"SKIP {test.__name__}: {exc}")
        except AssertionError as exc:
            failures.append(test.__name__)
            print(f"FAIL {test.__name__}: {exc}")
        else:
            print(f"PASS {test.__name__}")
    print("-" * 60)
    print(
        f"{len(tests)} 项：{len(tests) - len(failures) - len(skipped)} PASS / "
        f"{len(failures)} FAIL / {len(skipped)} SKIP"
    )
    if failures:
        print(f"FAILED: {', '.join(failures)}")
        return 1
    if skipped and len(skipped) == len(tests):
        print("SKIPPED: 本机没有内置 ok-ww working 树，未验证补丁")
        return 0
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
