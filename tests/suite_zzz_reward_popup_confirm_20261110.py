# -*- coding: utf-8 -*-
"""ZZZ 获得弹窗自带确认按钮 → CONFIRM_GONE_FAIL 误判回归（2026-11-10 实跑事故）。

21:22 后勤商店补跑：音擎残芯实际买到了（采购后截图是「获得 音擎残芯×1」弹窗），
但弹窗自己带「确认」按钮，「确认按钮是否消失」复核把它当「购买弹窗没关」→
CONFIRM_GONE_FAIL 提前返回，跳过只在 gone 之后执行的关浮层逻辑 → 浮层盖住
商店，后续商品全 0。08:48 首轮同死法。

修法：not-gone 分支先识别「获得」奖励弹窗——是就按买成功放行到关浮层 +
购买证据复核；真·购买确认没关才报 CONFIRM_GONE_FAIL。关浮层的确认/空白
分派由 suite_nte_reward_popup_20261105 [6] 覆盖，本套件锁判定分支。

证据帧 OCR 转录（21:22:30 当帧）：获得(958,422) / 音擎残芯(958,625) / 确认(977,730)。

覆盖：
  [1] not-gone + 获得弹窗（真帧转录）→ BOUGHT，且浮层被点确认关掉
  [2] not-gone + 无获得弹窗 → 仍 CONFIRM_GONE_FAIL（真失败不放过）
  [3] gone=True 旧路径不变（不做弹窗识别）
  [4] 放行不绕过防假成功：purchase_verify 签名不变仍 PURCHASE_UNVERIFIED
  [5] 接线静态：not-gone 分支先识别再返回失败码

跑法：.venv\\Scripts\\python.exe tests\\suite_zzz_reward_popup_confirm_20261110.py
"""

from __future__ import annotations

import io
import sys
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import game_auto.runtime.foreground_flow_runner as ff  # noqa: E402
import game_auto.runtime.zzz_monthly_shop as zs  # noqa: E402

FAILURES = []

# 2026-11-10 21:22:30 采购后当帧 OCR 转录（屏幕绝对坐标）
REAL_FRAME_POPUP = [
    {"text": "获得", "center": (958, 422), "confidence": 0.99},
    {"text": "音擎残芯", "center": (958, 625), "confidence": 1.0},
    {"text": "确认", "center": (977, 730), "confidence": 1.0},
]


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name}  {detail}")
        FAILURES.append(name)


def _env(ocr_seq, gone=False, verify_sig=None):
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(mock.patch.object(ff, "_window_rect", return_value=(0, 0, 1920, 1080)))
    stack.enter_context(mock.patch(
        "game_auto.runtime.ocr_click_tester.ocr_window_content", side_effect=ocr_seq))
    click = stack.enter_context(mock.patch.object(ff, "_click_window_local"))
    stack.enter_context(mock.patch.object(zs, "_locate_item", return_value=(500, 400)))
    stack.enter_context(mock.patch.object(zs, "_locate_confirm_button", return_value=((960, 760), "template")))
    stack.enter_context(mock.patch.object(zs, "_wait_gone", return_value=gone))
    stack.enter_context(mock.patch.object(zs.time, "sleep", lambda *_: None))
    if verify_sig is not None:
        stack.enter_context(mock.patch.object(zs, "_read_verify_signature", side_effect=verify_sig))
    return stack, click


def _buy(step=None):
    logger = mock.MagicMock()
    return zs._buy_once(logger, 1, {"name": "音擎残芯"}, "tpl", [1.0], [0.8], None,
                        label="商店采购", step=step)


def test_not_gone_reward_popup_bought():
    print("[1] not-gone + 获得弹窗（真帧转录）→ BOUGHT 并点确认关浮层")
    stack, click = _env([REAL_FRAME_POPUP, REAL_FRAME_POPUP, []])
    with stack:
        got = _buy()
    check("返回 BOUGHT", got == zs.BOUGHT, got)
    calls = [c[0] for c in click.call_args_list]
    check("最后一击点弹窗确认 (977,730)", bool(calls) and calls[-1][1:3] == (977, 730), calls)
    check("商品/确认/关浮层共 3 击", len(calls) == 3, calls)


def test_not_gone_real_fail_still_blocked():
    print("[2] not-gone + 无获得弹窗 → 仍 CONFIRM_GONE_FAIL")
    shop_page = [{"text": "兑换确认", "center": (960, 300)}, {"text": "确认", "center": (960, 760)}]
    stack, click = _env([shop_page])
    with stack:
        got = _buy()
    check("返回 CONFIRM_GONE_FAIL", got == zs.CONFIRM_GONE_FAIL, got)
    check("没有后续关浮层点击", click.call_count == 2, click.call_count)


def test_gone_true_old_path():
    print("[3] gone=True 旧路径不做弹窗识别")
    stack, click = _env([[]], gone=True)
    with stack:
        got = _buy()
    check("返回 BOUGHT", got == zs.BOUGHT, got)
    check("OCR 只被关浮层读了一次", click.call_count == 2, click.call_count)


def test_verify_guard_not_bypassed():
    print("[4] 放行后 purchase_verify 签名不变仍判假成功")
    stack, click = _env([REAL_FRAME_POPUP, REAL_FRAME_POPUP, []], verify_sig=[(5,), (5,)])
    with stack:
        got = _buy(step={"purchase_verify": {"labels": ["本月库存"]}})
    check("返回 PURCHASE_UNVERIFIED", got == zs.PURCHASE_UNVERIFIED, got)


def test_wiring_static():
    print("[5] 接线静态：先识别奖励弹窗再返回失败码")
    check("helper 已定义", callable(getattr(zs, "_reward_popup_present", None)))
    src = Path(zs.__file__).read_text(encoding="utf-8")
    buy_src = src.split("def _buy_once(", 1)[1].split("\ndef ", 1)[0]
    i_detect = buy_src.find("_reward_popup_present(logger, hwnd)")
    i_fail = buy_src.find("return CONFIRM_GONE_FAIL")
    check("not-gone 分支先识别后返回", 0 <= i_detect < i_fail, (i_detect, i_fail))


def main():
    test_not_gone_reward_popup_bought()
    test_not_gone_real_fail_still_blocked()
    test_gone_true_old_path()
    test_verify_guard_not_bypassed()
    test_wiring_static()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())