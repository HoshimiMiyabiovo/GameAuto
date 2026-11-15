"""2026-11-14 Qoder 权益中心网络错误页「重新加载」刷新回归。

事故：网络不好时点「权益中心」后弹「活动加载失败 / 请检查网络后重新加载 /
重新加载」错误页，领取/已领取模板永不出现，旧代码干等 20s 判
「点击权益中心后未检测到活动页」→ QoderFlow failed → WoolFlow failed
（11-10/11-11/11-14 三天同死法）。

修法：open_campaign 活动页等待循环里 OCR 检测错误页（LOAD_FAIL_TEXTS），
命中就点「重新加载」按钮（RELOAD_TEXTS 块中心），只点一次并续等 timeout。

跑法：.venv\\Scripts\\python.exe tests/suite_qoder_reload_20261114.py
"""

from __future__ import annotations

import io
import sys
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import game_auto.runtime.qoder_claim as qc

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} :: {detail}")
        FAILURES.append(name)


def test_load_fail_detected_and_reload():
    print("[1] 检测到「活动加载失败」页 → 点「重新加载」")
    err_page = [
        {"text": "活动加载失败", "center": (963, 486), "confidence": 1.0},
        {"text": "请检查网络后重新加载。", "center": (958, 515), "confidence": 1.0},
        {"text": "重新加载", "center": (962, 549), "confidence": 1.0},
    ]
    with mock.patch.object(qc, "_window_rect", return_value=(0, 0, 1920, 1080)), \
         mock.patch("game_auto.runtime.ocr_click_tester.ocr_window_content", return_value=err_page), \
         mock.patch.object(qc, "_click") as click:
        got = qc._dismiss_load_fail_and_reload(1, None)
    check("返回 True（点了刷新）", got is True, got)
    check("点击 1 次", click.call_count == 1, click.call_count)
    if click.call_count:
        args = click.call_args[0]
        check("点的是「重新加载」中心 (962,549)",
              args[0] == (962, 549), args)


def test_no_load_fail_page():
    print("[2] 正常页面 → 不点刷新")
    normal = [
        {"text": "每日 Credits", "center": (900, 300), "confidence": 1.0},
        {"text": "领取", "center": (960, 500), "confidence": 1.0},
    ]
    with mock.patch.object(qc, "_window_rect", return_value=(0, 0, 1920, 1080)), \
         mock.patch("game_auto.runtime.ocr_click_tester.ocr_window_content", return_value=normal), \
         mock.patch.object(qc, "_click") as click:
        got = qc._dismiss_load_fail_and_reload(1, None)
    check("返回 False（没错误页）", got is False, got)
    check("点击 0 次", click.call_count == 0, click.call_count)


def test_load_fail_but_no_reload_button():
    print("[3] 错误页在但「重新加载」按钮没定位到 → 不点、返回 False")
    err_page = [
        {"text": "活动加载失败", "center": (963, 486), "confidence": 1.0},
        {"text": "请检查网络后重新加载。", "center": (958, 515), "confidence": 1.0},
        # 没有「重新加载」块
    ]
    with mock.patch.object(qc, "_window_rect", return_value=(0, 0, 1920, 1080)), \
         mock.patch("game_auto.runtime.ocr_click_tester.ocr_window_content", return_value=err_page), \
         mock.patch.object(qc, "_click") as click:
        got = qc._dismiss_load_fail_and_reload(1, None)
    check("返回 False", got is False, got)
    check("点击 0 次", click.call_count == 0, click.call_count)


def test_open_campaign_reload_only_once():
    print("[4] open_campaign：错误页只点一次刷新，之后正常打开")
    err_page = [
        {"text": "活动加载失败", "center": (963, 486), "confidence": 1.0},
        {"text": "重新加载", "center": (962, 549), "confidence": 1.0},
    ]
    normal_page = [
        {"text": "领取", "center": (960, 500), "confidence": 1.0},
    ]
    ocr_seq = iter([err_page, err_page, normal_page, normal_page])
    visible_seq = iter([False, False, False, True, True])
    with mock.patch.object(qc, "_window_rect", return_value=(0, 0, 1920, 1080)), \
         mock.patch.object(qc, "_ensure_visible", return_value=(True, "")), \
         mock.patch.object(qc, "_claim_button_visible", side_effect=lambda *a, **k: next(visible_seq)), \
         mock.patch.object(qc, "_menu_center", return_value=(500, 600)), \
         mock.patch("game_auto.runtime.ocr_click_tester.ocr_window_content", side_effect=lambda *a, **k: next(ocr_seq)), \
         mock.patch.object(qc, "_click") as click:
        ok, msg = qc.open_campaign(None, 1, timeout_sec=5)
    check("最终打开成功", ok is True, msg)
    # _click 调用：1 次头像 + 1 次权益中心菜单 + 1 次重新加载 = 3 次
    check("点击总数 3（头像/菜单/重新加载）", click.call_count == 3, click.call_count)
    reload_clicks = [c for c in click.call_args_list if c[0][0] == (962, 549)]
    check("「重新加载」只点 1 次", len(reload_clicks) == 1, len(reload_clicks))


def test_open_campaign_no_reload_loop():
    print("[5] open_campaign：错误页持续存在 → 只点 1 次刷新，不无限循环")
    err_page = [
        {"text": "活动加载失败", "center": (963, 486), "confidence": 1.0},
        {"text": "重新加载", "center": (962, 549), "confidence": 1.0},
    ]
    with mock.patch.object(qc, "_window_rect", return_value=(0, 0, 1920, 1080)), \
         mock.patch.object(qc, "_ensure_visible", return_value=(True, "")), \
         mock.patch.object(qc, "_claim_button_visible", return_value=False), \
         mock.patch.object(qc, "_menu_center", return_value=(500, 600)), \
         mock.patch("game_auto.runtime.ocr_click_tester.ocr_window_content", return_value=err_page), \
         mock.patch.object(qc, "_click") as click:
        ok, msg = qc.open_campaign(None, 1, timeout_sec=3)
    check("超时判失败", ok is False, msg)
    reload_clicks = [c for c in click.call_args_list if c[0][0] == (962, 549)]
    check("「重新加载」只点 1 次（不循环）", len(reload_clicks) == 1, len(reload_clicks))


def test_constants():
    print("[6] 词表常量")
    check("LOAD_FAIL_TEXTS 含「活动加载失败」", "活动加载失败" in qc.LOAD_FAIL_TEXTS, qc.LOAD_FAIL_TEXTS)
    check("LOAD_FAIL_TEXTS 含「请检查网络后重新加载」", "请检查网络后重新加载" in qc.LOAD_FAIL_TEXTS, qc.LOAD_FAIL_TEXTS)
    check("RELOAD_TEXTS 含「重新加载」", "重新加载" in qc.RELOAD_TEXTS, qc.RELOAD_TEXTS)


def main():
    test_load_fail_detected_and_reload()
    test_no_load_fail_page()
    test_load_fail_but_no_reload_button()
    test_open_campaign_reload_only_once()
    test_open_campaign_no_reload_loop()
    test_constants()


if __name__ == "__main__":
    main()
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILED: {FAILURES}")
        sys.exit(1)
    print("\nALL PASS")
