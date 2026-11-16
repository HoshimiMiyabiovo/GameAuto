"""心跳看门狗止损只影响当前任务、不连坐整批的回归测试（2026-11-16）。

事故背景：看门狗原先直接把批次级 stop_current_task 置 True，
task_batch_runner 就把被止损的游戏算成 stopped（用户终止）并静默 break，
后面 6 个游戏一个都没跑，也没有统一重试。

本套件全部走行为路径（真调 run_task_batch），只 mock 单任务执行与外部副作用：
  1. 单任务收口函数清掉看门狗标志、本任务判失败、改写"用户终止"字样
  2. 看门狗止损后后续任务照跑，失败项仍进入统一重试
  3. 用户主动停止仍然终止整批（口径不能一起放宽）
  4. 新批次入口会清掉遗留的任务级看门狗标志

跑法：.venv\\Scripts\\python.exe tests\\suite_watchdog_task_scope_20261116.py
"""

import sys

sys.path.insert(0, r"E:\GithubProject\game_auto\src")

from game_auto.runtime import flow_diagnostics  # noqa: E402
from game_auto.runtime import task_batch_runner as tbr  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


class FakeLogger:
    def __init__(self):
        self.lines = []

    def _add(self, level, msg, *args, **kwargs):
        self.lines.append((level, str(msg)))

    def info(self, msg, *a, **k):
        self._add("INFO", msg)

    def warning(self, msg, *a, **k):
        self._add("WARN", msg)

    def error(self, msg, *a, **k):
        self._add("ERROR", msg)

    def debug(self, msg, *a, **k):
        self._add("DEBUG", msg)


class FakeController:
    def __init__(self):
        self.logger = FakeLogger()
        self.stop_current_task = False
        self.watchdog_aborted_task = False

    def _cooldown_before_next_task(self, *a, **k):
        pass


class Patch:
    """临时替换模块属性，退出时按原值恢复。"""

    def __init__(self, module, **kwargs):
        self.module = module
        self.kwargs = kwargs
        self.old = {}

    def __enter__(self):
        for key, value in self.kwargs.items():
            self.old[key] = getattr(self.module, key)
            setattr(self.module, key, value)
        return self

    def __exit__(self, *exc):
        for key, value in self.old.items():
            setattr(self.module, key, value)
        return False


def make_result(app_key, *, success=True, state="success", message="ok", stop_batch=False):
    return tbr.TaskAppResult(
        app_key=app_key,
        script_choice=None,
        effective_script_choice=None,
        display_name=app_key,
        script_name="script",
        label=f"{app_key}(script)",
        success=success,
        message=message,
        duration=1,
        time_str="1秒",
        state=state,
        report_lines=[f"{app_key}: {message}"],
        should_stop_batch=stop_batch,
    )


def common_patches():
    return dict(
        ensure_qbittorrent_running=lambda controller: None,
        run_startup_sign_tasks=lambda apps: [],
        _is_game_script_done_today=lambda app_key: False,
        get_app_display_name=lambda app_key: app_key,
        get_script_display_name=lambda app_key, choice: "script",
        is_standalone_task=lambda app_key: False,
        _append_monthly_report=lambda *a, **k: None,
        _append_weekly_report=lambda *a, **k: None,
        _run_extra_version_cycle_flows=lambda *a, **k: None,
        _sweep_pending_monthly_flows=lambda *a, **k: None,
        _sweep_pending_version_cycle_flows=lambda *a, **k: None,
        _interruptible_sleep=lambda *a, **k: None,
    )


def test_settle_watchdog_abort_unit():
    print("[1] 单任务收口：清标志、判失败、去掉\"用户终止\"字样")
    controller = FakeController()
    controller.watchdog_aborted_task = True
    ok, message = tbr._settle_watchdog_abort(controller, True, "用户终止")
    check("返回失败", ok is False, ok)
    check("文案带看门狗止损", "看门狗止损" in message, message)
    check("不再含用户终止", "用户终止" not in message, message)
    check("任务级标志已清", controller.watchdog_aborted_task is False)
    check("批次级标志未被设置", controller.stop_current_task is False)

    controller.stop_current_task = True  # 用户同时点了停止：不能被看门狗吞掉
    controller.watchdog_aborted_task = True
    tbr._settle_watchdog_abort(controller, False, "启动失败")
    check("用户停止标志保留", controller.stop_current_task is True)


def test_batch_continues_after_watchdog_abort():
    print("[2] 看门狗止损后后续任务照跑，失败项仍进入统一重试")
    controller = FakeController()
    calls = []

    def fake_run_single(controller, app_key, script_choice, **kwargs):
        calls.append(app_key)
        if app_key == "WutheringWaves" and calls.count(app_key) == 1:
            # 模拟看门狗回调 + 真 _run_single_task 末尾的单任务收口
            controller.watchdog_aborted_task = True
            ok, message = tbr._settle_watchdog_abort(controller, False, "用户终止")
            return make_result(app_key, success=ok, state="failed", message=message)
        return make_result(app_key)

    with Patch(tbr, **common_patches(), _run_single_task=fake_run_single):
        result = tbr.run_task_batch(
            controller,
            [("WutheringWaves", None), ("ArknightsEndfield", None), ("InfinityNikki", None)],
        )

    check("三个游戏都进过批次", len(calls) == 4, calls)  # 3 首轮 + 1 次重试
    check("后续任务没被跳过", "ArknightsEndfield" in calls and "InfinityNikki" in calls, calls)
    check("失败项被重试一次", calls.count("WutheringWaves") == 2, calls)
    check("批次停止标志仍为 False", controller.stop_current_task is False)
    check("任务级标志已清", controller.watchdog_aborted_task is False)
    check("日报不再写用户终止", all("用户终止" not in line for line in result.report), result.report)
    check("日报写明看门狗止损", any("看门狗止损" in line for line in result.report), result.report)
    check("重试成功提示在案", any("首次失败后重试成功" in line for line in result.report), result.report)


def test_user_stop_still_stops_batch():
    print("[3] 用户主动停止仍然终止整批")
    controller = FakeController()
    calls = []

    def fake_run_single(controller, app_key, script_choice, **kwargs):
        calls.append(app_key)
        controller.stop_current_task = True
        return make_result(app_key, success=False, state="failed", message="用户终止")

    with Patch(tbr, **common_patches(), _run_single_task=fake_run_single):
        tbr.run_task_batch(
            controller,
            [("WutheringWaves", None), ("ArknightsEndfield", None)],
        )

    check("后续任务没跑", calls == ["WutheringWaves"], calls)
    check("用户停止标志保留", controller.stop_current_task is True)


def test_new_batch_clears_stale_watchdog_flag():
    print("[4] 新批次入口清掉遗留的任务级看门狗标志")
    controller = FakeController()
    controller.watchdog_aborted_task = True  # 上一批异常退出遗留
    calls = []

    def fake_run_single(controller, app_key, script_choice, **kwargs):
        calls.append(app_key)
        return make_result(app_key)

    with Patch(tbr, **common_patches(), _run_single_task=fake_run_single):
        tbr.run_task_batch(controller, [("GenshinImpact", None)])

    check("遗留标志已清", controller.watchdog_aborted_task is False)
    check("任务照常执行", calls == ["GenshinImpact"], calls)


class FakeAttempt:
    success = True
    message = "ok"
    duration = 1
    failure_reason = ""


class FakeTracker:
    def record_script_run(self, **kwargs):
        pass


def test_real_single_task_settles_watchdog_abort():
    print("[5] 真 _run_single_task：看门狗止损收口成本任务失败")
    controller = FakeController()

    def fake_run_ok_script(*args, **kwargs):
        controller.watchdog_aborted_task = True
        return FakeAttempt()

    patches = common_patches()
    patches.update(
        get_saved_script_choice=lambda app_key: None,
        prepare_app_for_runtime=lambda *a, **k: False,
        APPS={"WutheringWaves": {"ok_ww_log_monitor": {"log_dir": "fake"}}},
        run_ok_script_once=fake_run_ok_script,
        _auto_query_daily_progress=lambda *a, **k: None,
        _verify_script_log_result=lambda *a, **k: None,
        get_tracker=lambda *a, **k: FakeTracker(),
    )
    with Patch(tbr, **patches), Patch(flow_diagnostics, record_failure=lambda *a, **k: None):
        result = tbr._run_single_task(controller, "WutheringWaves", None, cj_should_run=False)

    check("本任务判失败", result.state == "failed", result.state)
    check("不带用户终止字样", "用户终止" not in result.message, result.message)
    check("带看门狗止损字样", "看门狗止损" in result.message, result.message)
    check("不请求整批停止", result.should_stop_batch is False, result.should_stop_batch)
    check("任务级标志已清", controller.watchdog_aborted_task is False)
    check("批次级标志未置", controller.stop_current_task is False)


if __name__ == "__main__":
    test_settle_watchdog_abort_unit()
    test_batch_continues_after_watchdog_abort()
    test_user_stop_still_stops_batch()
    test_new_batch_clears_stale_watchdog_flag()
    test_real_single_task_settles_watchdog_abort()
    if FAILURES:
        print(f"\nFAILED {len(FAILURES)}: {FAILURES}")
        sys.exit(1)
    print("\nALL PASS")
