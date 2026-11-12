"""2026-11-11：恶名狩猎交回 OneDragon 一条龙，本项目每周流程停用回归。

只锁两件事：调度层在 enabled=false 时零成本跳过（不调用 runner、不进报告、
不写周标记），以及 enabled=true 时原有周常行为不受影响。
"""

import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from game_auto.runtime import task_batch_runner as tbr  # noqa: E402
from game_auto.runtime import foreground_flow_runner as ffr  # noqa: E402

LOG = logging.getLogger("weekly_disabled")
FAILS = []


def check(name, cond, extra=""):
    if cond:
        print(f"  PASS {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL {name} {extra}")


def _controller():
    return SimpleNamespace(stop_current_task=False, logger=LOG)


def test_disabled_short_circuit():
    print("[1] enabled=false 调度层短路")
    original_loader = ffr._load_flow_config
    original_runners = tbr._WEEKLY_RUNNERS
    original_done = tbr.is_weekly_flow_done
    calls = []

    ffr._load_flow_config = lambda key: {"enabled": False}
    tbr._WEEKLY_RUNNERS = {
        "ZZZ_NOTORIOUS_HUNT": lambda logger: (calls.append("runner"), (True, "不该跑到"))[1]
    }
    tbr.is_weekly_flow_done = lambda key: calls.append("is_done") or False
    try:
        out = tbr._run_pending_weekly_flow(_controller(), "ZenlessZoneZero")
    finally:
        ffr._load_flow_config = original_loader
        tbr._WEEKLY_RUNNERS = original_runners
        tbr.is_weekly_flow_done = original_done

    check("返回 None（报告里不出现 ❌）", out is None, out)
    check("没有调用执行入口", "runner" not in calls, calls)
    check("连周标记都不用读", "is_done" not in calls, calls)


def test_enabled_still_runs():
    print("[2] enabled=true 时周常照常执行（不误伤其它流程）")
    original_loader = ffr._load_flow_config
    original_runners = tbr._WEEKLY_RUNNERS
    original_done = tbr.is_weekly_flow_done
    calls = []

    ffr._load_flow_config = lambda key: {"enabled": True}
    tbr._WEEKLY_RUNNERS = {
        "ZZZ_NOTORIOUS_HUNT": lambda logger: (calls.append("runner"), (True, "本周已打"))[1]
    }
    tbr.is_weekly_flow_done = lambda key: False
    try:
        out = tbr._run_pending_weekly_flow(_controller(), "ZenlessZoneZero")
    finally:
        ffr._load_flow_config = original_loader
        tbr._WEEKLY_RUNNERS = original_runners
        tbr.is_weekly_flow_done = original_done

    check("返回报告行", isinstance(out, tuple) and out[1] is True, out)
    check("执行入口被调用", "runner" in calls, calls)
    check("报告内容正常", isinstance(out, tuple) and "本周已打" in out[0], out)


def test_config_files():
    print("[3] 两份流程配置默认禁用")
    for name in ("flow_configs.json", "flow_configs.example.json"):
        cfg = json.loads((PROJECT_ROOT / name).read_text(encoding="utf-8"))
        section = cfg.get("ZZZ_NOTORIOUS_HUNT", {})
        check(f"{name} enabled=false", section.get("enabled") is False, section.get("enabled"))
        check(f"{name} 留了停用原因", "_comment_disabled" in section)


def test_source_shape():
    print("[4] 源码静态断言")
    src = (PROJECT_ROOT / "src/game_auto/runtime/task_batch_runner.py").read_text(encoding="utf-8")
    check("短路在 is_weekly_flow_done 之前",
          src.index("流程已禁用，跳过（本周由外部脚本负责）") < src.index("if is_weekly_flow_done(flow_key):"))


def main():
    test_disabled_short_circuit()
    test_enabled_still_runs()
    test_config_files()
    test_source_shape()
    print(f"\n{'FAILED: ' + ', '.join(FAILS) if FAILS else 'ALL PASS'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
