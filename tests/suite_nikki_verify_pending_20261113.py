"""2026-11-13：无限暖暖「朝夕心愿 pending=假成功」回归。

背景：用户拍板口径（2026-10-06）暖暖复核以朝夕心愿为关键判定，但
GameDailyRecord.completion_state() 只裁决 success/failed 二态，pending 会
掉到计数口径返回 "pending"；_verify_script_log_result 又只在 == "failed" 时
改判，导致 11-13 朝夕心愿 100/500（游戏更新弹窗打断、whimbox 无汇总块）被
判成功、不进统一重试。本套件锁住新增的第三态判定与其它游戏不受影响。
"""

import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from game_auto.runtime import task_batch_runner as tbr  # noqa: E402

LOG = logging.getLogger("nikki_verify_pending")
FAILS = []


def check(name, cond, extra=""):
    if cond:
        print(f"  PASS {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL {name} {extra}")


def _task(name, status, current=None, total=None):
    extra = {} if current is None else {"current": current, "total": total}
    return {"task_name": name, "status": status, "message": "", "extra": extra}


def _data(key_status="pending", key_cur=100, key_total=500, *, include_key=True,
          game_key="nikki", is_all_success=True):
    tasks = [_task("每周幻境", "success", 2, 2), _task("星海拾光", "pending", 0, 500)]
    if include_key:
        tasks.insert(0, _task("朝夕心愿", key_status, key_cur, key_total))
    return {
        "game_key": game_key,
        "game_name": "无限暖暖",
        "tasks": tasks,
        "errors": [],
        "is_all_success": is_all_success,
        "has_failed": False,
    }


def _profile(data, game_key="nikki"):
    return SimpleNamespace(
        game_key=game_key,
        app_key="InfinityNikki",
        daily_parser="x:y",
        verify_scripts=(),
        latest_daily_log=lambda: data,
        augment_verify=lambda *a, **k: False,
        daily_tool="whimbox",
    )


def verify(data, game_key="nikki", app_key="InfinityNikki"):
    with patch("game_auto.games.get_profile", return_value=_profile(data, game_key)):
        return tbr._verify_script_log_result(app_key, "whimbox", "无限暖暖", LOG)


def test_pending_is_failure():
    print("[1] 11-13 真实形态：朝夕心愿 pending 100/500")
    got = verify(_data())
    check("改判失败（返回非空）", isinstance(got, str) and bool(got), got)
    check("失败原因点名朝夕心愿", isinstance(got, str) and "朝夕心愿未完成" in got, got)
    check("失败原因带 100/500", isinstance(got, str) and "100/500" in got, got)
    check("失败原因带 pending 状态", isinstance(got, str) and "pending" in got, got)


def test_completed_states_pass():
    print("[2] 关键任务已完成/已跳过时放行")
    for status, cur in (("success", 500), ("already", 500), ("skipped", 500)):
        got = verify(_data(status, cur))
        check(f"{status} -> 保持原判", got is None, got)


def test_failed_and_other_states():
    print("[3] 其它未完成态")
    got = verify(_data("failed", 100))
    check("failed -> 改判", isinstance(got, str) and "朝夕心愿" in got, got)
    got = verify(_data("", 0))
    check("空状态 -> 改判", isinstance(got, str) and "朝夕心愿" in got, got)
    got = verify(_data("unknown", 0))
    check("unknown -> 改判", isinstance(got, str) and "朝夕心愿" in got, got)


def test_no_key_task():
    print("[4] 解析不到关键任务时不干预（沿用原口径）")
    got = verify(_data(include_key=True))
    ok_when_present = isinstance(got, str)
    got_absent = verify(_data(include_key=False))
    check("有关键任务时会改判", ok_when_present, got)
    check("无关键任务时保持原判", got_absent is None, got_absent)
    failed_others = _data(include_key=False)
    failed_others["tasks"].append(_task("星海拾光", "failed", 0, 500))
    failed_others["has_failed"] = True
    failed_others["is_all_success"] = False
    check("无关键任务但有失败项仍走原逻辑", isinstance(verify(failed_others), str))


def test_other_games_untouched():
    print("[5] 其它游戏不受影响（领取类不可重入）")
    zzz = {
        "game_key": "zzz",
        "game_name": "绝区零",
        "tasks": [_task("活跃度奖励", "pending"), _task("邮件", "success")],
        "errors": [], "is_all_success": True, "has_failed": False,
    }
    with patch("game_auto.games.get_profile", return_value=_profile(zzz, "zzz")):
        got = tbr._verify_script_log_result("ZenlessZoneZero", "OneDragon 一条龙（直连）",
                                            "绝区零", LOG)
    check("zzz 关键任务 pending -> 保持原判", got is None, got)

    nte = {
        "game_key": "nte", "game_name": "异环",
        "tasks": [_task("日常领取", "pending")],
        "errors": [], "is_all_success": True, "has_failed": False,
    }
    with patch("game_auto.games.get_profile", return_value=_profile(nte, "nte")):
        got = tbr._verify_script_log_result("NevernessToEverness", "ok-nte", "异环", LOG)
    check("nte 关键任务 pending -> 保持原判", got is None, got)


def test_source_shape():
    print("[6] 源码静态断言")
    src = (PROJECT_ROOT / "src/game_auto/runtime/task_batch_runner.py").read_text(encoding="utf-8")
    check("导入了 KEY_STATUS_TASKS", "KEY_STATUS_TASKS, DailyTaskRecord, GameDailyRecord" in src)
    check("新分支只对 nikki", 'if profile.game_key == "nikki":' in src)
    check("放行集合含 skipped", '("success", "already", "skipped")' in src)
    check("失败文案点名朝夕心愿", "朝夕心愿未完成" in src)
    check("统一重试仍只收 failed", 'r.state == "failed"' in src)


def main():
    test_pending_is_failure()
    test_completed_states_pass()
    test_failed_and_other_states()
    test_no_key_task()
    test_other_games_untouched()
    test_source_shape()
    print(f"\n{'FAILED: ' + ', '.join(FAILS) if FAILS else 'ALL PASS'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
