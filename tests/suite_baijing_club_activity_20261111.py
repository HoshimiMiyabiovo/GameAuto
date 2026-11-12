# -*- coding: utf-8 -*-
"""白荆回廊俱乐部活动「回声猎区」分支入口回归（2026-11-11）。

背景：俱乐部页右侧「俱乐部活动」卡片出现「回声猎区」（活动重置剩余 12 天）。
本次只加**分支入口**（检测 → 点进活动页 → 快照取证 → 立即退出），活动内容
（刷本/领奖）未实现——活动页布局未实机探明，刻意不做页面判定（页面判定铁律：
无已验证标记集），证据靠快照 jpg。本套件锁分支骨架行为：

[1] 常量守卫：入口词表/检测区域覆盖实测标题坐标 (0.819, 0.370)；
[2] 入口未命中 → 不点击不快照直接跳过（活动结束后卡片可能消失，禁盲点）；
[3] 入口命中 → 点 OCR center（非固定比例）→ 快照 → 复用 _leave_club_page 退出；
[4] 退出失败 → 分支 status=failed，detail 带人工恢复提示（不污染每日任务主状态）；
[5] 接线：claim_club_daily_reward 领完每日任务后调用分支并挂 result["activity"]，
    分支结果不影响主 status；
[6] 用户停止 → 分支 skipped，且 claim 主流程不再调分支。

跑法：.venv\\Scripts\\python.exe -m pytest tests\\suite_baijing_club_activity_20261111.py -q
（经 tests/test_suites.py 子进程跑）
"""

import io
import sys
import unittest.mock as mock
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import game_auto.runtime.baijing_daily as bd  # noqa: E402

FAILURES = []


def check(label, ok, detail=None):
    mark = "PASS" if ok else "FAIL"
    print(f"  {mark} {label}" + (f"  ← {detail}" if not ok and detail is not None else ""))
    if not ok:
        FAILURES.append(label)


def test_constants():
    print("[1] 常量守卫")
    check("入口词表", bd.CLUB_ACTIVITY_ECHO_TEXTS == ("回声猎区",))
    x0, y0, x1, y1 = bd.CLUB_ACTIVITY_DETECT_REGION
    check("检测区域升序合法", x0 < x1 and y0 < y1, bd.CLUB_ACTIVITY_DETECT_REGION)
    # 实测「回声猎区」标题中心 (0.819, 0.370) 必须落在检测区域内（用户 11-11 截图）
    check("实测标题坐标在区域内",
          x0 <= 0.819 <= x1 and y0 <= 0.370 <= y1, bd.CLUB_ACTIVITY_DETECT_REGION)
    # 顶部避开「今日俱乐部奖励已领取」横幅（y≈0.215~0.245）
    check("区域顶部低于横幅带", y0 >= 0.25, y0)
    check("快照前缀", bd.CLUB_ACTIVITY_SNAPSHOT_PREFIX == "club_activity_echo")


def test_entry_miss_skips_without_click():
    print("[2] 入口未命中 → 不点击直接跳过")
    clicked, snapped, left = [], [], []
    with mock.patch.object(bd, "_ocr_best_match", return_value=None), \
         mock.patch.object(bd, "_pyautogui_click_guarded", side_effect=lambda *a: clicked.append(a)), \
         mock.patch.object(bd, "_capture_daily_list_snapshot", side_effect=lambda *a, **k: snapped.append(a)), \
         mock.patch.object(bd, "_leave_club_page", side_effect=lambda *a, **k: left.append(a)):
        r = bd._run_club_activity_echo_zone(logging_logger(), hwnd=1)
    check("status=skipped", r["status"] == "skipped", r)
    check("entered=False", r["entered"] is False)
    check("未点击", not clicked, clicked)
    check("未快照", not snapped, snapped)
    check("未触发退出", not left, left)
    check("detail 说明未检测到", "回声猎区" in r["detail"])


def test_entry_hit_clicks_ocr_center_and_exits():
    print("[3] 入口命中 → 点 OCR center → 快照 → 退出")
    clicked, snapped, left = [], [], []
    hit = {"center": (1572, 400), "text": "回声猎区", "confidence": 0.9}
    with mock.patch.object(bd, "_ocr_best_match", return_value=hit), \
         mock.patch.object(bd, "_pyautogui_click_guarded", side_effect=lambda *a: clicked.append(a)), \
         mock.patch.object(bd, "_capture_daily_list_snapshot",
                           side_effect=lambda *a, **k: snapped.append(a) or "snap.jpg"), \
         mock.patch.object(bd, "_leave_club_page", side_effect=lambda *a, **k: left.append(a) or True), \
         mock.patch.object(bd.time, "sleep", lambda *_: None):
        r = bd._run_club_activity_echo_zone(logging_logger(), hwnd=1)
    check("entered=True", r["entered"] is True)
    check("点击用的是 OCR center（非固定比例）", clicked == [(1572, 400)], clicked)
    check("进页后快照取证", len(snapped) == 1, snapped)
    check("退出走 _leave_club_page", len(left) == 1, left)
    check("returned_main=True", r["returned_main"] is True)
    check("占位分支不报失败", r["status"] == "skipped", r)
    check("detail 标明内容未实现", "未实现" in r["detail"], r["detail"])


def test_exit_failure_marks_failed():
    print("[4] 退出失败 → 分支 failed 且带人工恢复提示")
    with mock.patch.object(bd, "_ocr_best_match",
                           return_value={"center": (1572, 400), "text": "回声猎区", "confidence": 0.9}), \
         mock.patch.object(bd, "_pyautogui_click_guarded", side_effect=lambda *a: None), \
         mock.patch.object(bd, "_capture_daily_list_snapshot", side_effect=lambda *a, **k: None), \
         mock.patch.object(bd, "_leave_club_page", return_value=False), \
         mock.patch.object(bd.time, "sleep", lambda *_: None):
        r = bd._run_club_activity_echo_zone(logging_logger(), hwnd=1)
    check("status=failed", r["status"] == "failed", r)
    check("entered=True", r["entered"] is True)
    check("returned_main=False", r["returned_main"] is False)
    check("detail 带人工恢复提示", "人工恢复" in r["detail"], r["detail"])


def test_user_stop():
    print("[5] 用户停止 → 分支 skipped")
    with mock.patch.object(bd, "_stopped", return_value=True):
        r = bd._run_club_activity_echo_zone(logging_logger(), hwnd=1)
    check("status=skipped detail=用户停止", r["status"] == "skipped" and r["detail"] == "用户停止", r)


def _claim_env(branch_stub, *, stopped=False):
    """claim_club_daily_reward 的全打桩环境：返回 (result, 调用记录)。"""
    calls = {"branch": 0}
    with mock.patch.object(bd, "_stopped", side_effect=lambda c: stopped), \
         mock.patch.object(bd, "_ensure_window", return_value=1), \
         mock.patch.object(bd, "_ensure_main_ui", return_value=True), \
         mock.patch.object(bd, "_enter_club_page", return_value=True), \
         mock.patch.object(bd, "_capture_daily_list_snapshot", return_value=None), \
         mock.patch.object(bd, "_claim_club_daily_task", return_value="purchased"), \
         mock.patch.object(bd, "_run_club_activity_echo_zone",
                           side_effect=lambda *a, **k: (calls.__setitem__("branch", calls["branch"] + 1),
                                                        dict(branch_stub))[1]), \
         mock.patch.object(bd, "_leave_club_page", return_value=True):
        r = bd.claim_club_daily_reward(logging_logger(), hwnd=1)
    return r, calls


def test_wiring_in_claim_club_daily_reward():
    print("[6] 接线：领完每日任务后调分支并挂 result['activity']，主状态不受影响")
    stub = {"status": "skipped", "detail": "stub", "entered": False, "returned_main": None}
    r, calls = _claim_env(stub)
    check("分支被调用 1 次", calls["branch"] == 1, calls)
    check("分支结果挂 activity 键", r.get("activity") == stub, r.get("activity"))
    check("每日任务主状态仍是 success", r["status"] == "success", r)
    check("returned_main 仍是顶层键", r.get("returned_main") is True)
    # 用户停止时分支不再调用
    r2, calls2 = _claim_env(stub, stopped=True)
    check("停止后分支不调用", calls2["branch"] == 0, calls2)
    check("停止时主状态 skipped", r2["status"] == "skipped", r2)


def logging_logger():
    import logging
    return logging.getLogger("suite_baijing_club_activity")


def main():
    test_constants()
    test_entry_miss_skips_without_click()
    test_entry_hit_clicks_ocr_center_and_exits()
    test_exit_failure_marks_failed()
    test_user_stop()
    test_wiring_in_claim_club_daily_reward()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED {len(FAILURES)}: {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
