"""白荆每日免费礼包重复执行防护回归（2026-10-25）。

事故：同一天第二次跑白荆（force_rerun）时，「每日外勤赠礼」已领过、商品从
货架消失，导航到有猫精选后找不到商品 → 误报 failed 并落失败记录。
修复：buy_daily_free_gift_pack 开头读今日 daily_progress，success/already 直接
返回 already，不再进零售页。

跑法：.venv\\Scripts\\python.exe artifacts\\test_baijing_gift_guard_20261025.py
"""

import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT.parent))

import game_auto.runtime.baijing_daily as bd  # noqa: E402

LOG = logging.getLogger("gift-guard-test")
FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def _run_with(prev_status, ensure_main_ok=False, meta_done=False):
    calls = {"ensure": 0}

    def fake_ensure_main(logger, hwnd, stop_check=None):
        calls["ensure"] += 1
        return ensure_main_ok

    saved = (
        bd._baijing_task_status_today,
        bd._daily_gift_marked_done,
        bd._ensure_main_ui,
        bd._ensure_window,
    )
    bd._baijing_task_status_today = lambda key: prev_status
    bd._daily_gift_marked_done = lambda: meta_done
    bd._ensure_main_ui = fake_ensure_main
    bd._ensure_window = lambda logger, hwnd: 1
    try:
        return bd.buy_daily_free_gift_pack(LOG, 1), calls
    finally:
        (
            bd._baijing_task_status_today,
            bd._daily_gift_marked_done,
            bd._ensure_main_ui,
            bd._ensure_window,
        ) = saved


def main():
    print("[1] 今日已 success → already，且不进入主界面导航")
    res, calls = _run_with("success")
    check("status=already", res.get("status") == "already", res)
    check("detail 标注进度记录", "进度记录" in res.get("detail", ""), res)
    check("未调用 _ensure_main_ui", calls["ensure"] == 0, calls)

    print("[2] 今日已 already → already")
    res, calls = _run_with("already")
    check("status=already", res.get("status") == "already", res)
    check("未调用 _ensure_main_ui", calls["ensure"] == 0, calls)

    print("[3] meta 标记今日已领 → already（即使 daily_progress 被后续失败覆盖）")
    res, calls = _run_with("failed", meta_done=True)
    check("daily_gift=failed 但 meta ok → already", res.get("status") == "already", res)
    check("未调用 _ensure_main_ui", calls["ensure"] == 0, calls)

    print("[4] 今日 failed / 无记录且无 meta → 仍按正常流程尝试（不误判 already）")
    for prev in ("failed", None):
        res, calls = _run_with(prev, ensure_main_ok=False, meta_done=False)
        check(f"prev={prev!r} 不进 already 分支", res.get("status") != "already", res)
        check(f"prev={prev!r} 会调用 _ensure_main_ui", calls["ensure"] == 1, calls)

    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
