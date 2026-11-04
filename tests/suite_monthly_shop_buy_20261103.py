"""月商店「买 1 个就停」修复回归（2026-11-03）。

事故：原神 11-03 08:27 星尘兑换，纠缠之缘/相遇之缘各只买了 1 个（采购前后截图
「本月剩余数量 5 → 4」），日志记「1/5（已达上限或无弹窗）」当成功。崩铁命运的
足迹同样停在 1/2。

根因：兑换成功后游戏弹「获得物品」浮层，浮层上带商品名（原神日志 OCR 命中
「纠缠之缘 @ (960,607)」正是浮层位置）。`_buy_once` 只等兑换确认弹窗消失，没等
浮层散掉 → 下一次循环的定位命中浮层文字，点过去不是商品卡、等不到确认弹窗 →
被判成"已达上限"。

修复：
  1. BOUGHT 后多等 `POST_BUY_SETTLE_SEC`（2.5s），让浮层消失；
  2. 首次点击等不到确认弹窗时，等 `RETRY_SETTLE_SEC`（2s）重新定位再点一次，
     仍无弹窗才返回 NO_POPUP（保留"到上限 = 商品不再可兑"的既有口径）。

跑法：.venv\\Scripts\\python.exe tests\\suite_monthly_shop_buy_20261103.py
"""

import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import game_auto.runtime.monthly_exchange as me  # noqa: E402
import game_auto.runtime.zzz_monthly_shop as zs  # noqa: E402

LOG = logging.getLogger("monthly-shop-buy-test")
FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


class Env:
    """模拟：商品定位固定成功，确认弹窗按队列返回，浮层等待可观测。"""

    def __init__(self, confirm_results=None):
        self.sleeps = []
        self.clicks = []
        self.locate_items = 0
        self.confirm_results = list(confirm_results or [])
        self._saved = {}

    def install(self):
        self._saved = {
            "_locate_item": zs._locate_item,
            "_locate": zs._locate,
            "_wait_gone": zs._wait_gone,
            "sleep": zs.time.sleep,
            "click": None,
        }
        zs._locate_item = self._locate_item
        zs._locate = self._locate_confirm
        zs._wait_gone = lambda *a, **k: True
        zs.time.sleep = lambda s: self.sleeps.append(float(s))
        import game_auto.runtime.foreground_flow_runner as ffr

        self._saved["click"] = ffr._click_window_local
        ffr._click_window_local = lambda hwnd, x, y, clicks=1, **k: self.clicks.append((x, y))

    def restore(self):
        import game_auto.runtime.foreground_flow_runner as ffr

        ffr._click_window_local = self._saved["click"]
        for k in ("_locate_item", "_locate", "_wait_gone"):
            setattr(zs, k, self._saved[k])
        zs.time.sleep = self._saved["sleep"]

    def _locate_item(self, *a, **k):
        self.locate_items += 1
        return (100 + self.locate_items, 100)

    def _locate_confirm(self, *a, **k):
        if self.confirm_results:
            return self.confirm_results.pop(0)
        return (200, 200)


def test_1_bought_waits_for_reward_popup():
    print("[1] 兑换成功后再等 POST_BUY_SETTLE_SEC，让「获得物品」浮层散掉")
    env = Env()
    env.install()
    try:
        result = zs._buy_once(
            LOG, 1, {"name": "纠缠之缘", "ocr_text": "纠缠之缘"},
            "confirm.png", [1.0], [0.8], None, label="星尘兑换",
        )
    finally:
        env.restore()
    check("返回 BOUGHT", result == zs.BOUGHT, result)
    check("点了商品和确认", len(env.clicks) == 2, env.clicks)
    check(
        f"sleep 含 {zs.POST_BUY_SETTLE_SEC}s 浮层等待",
        any(abs(s - zs.POST_BUY_SETTLE_SEC) < 0.01 for s in env.sleeps),
        env.sleeps,
    )


def test_2_retry_after_missing_popup():
    print("[2] 首次点商品没弹确认（浮层遮挡）：等 2s 重新定位再点，第二次弹窗则算买到")
    env = Env(confirm_results=[None, (200, 200)])  # 第一次等确认超时，重试后命中
    env.install()
    try:
        result = zs._buy_once(
            LOG, 1, {"name": "纠缠之缘", "ocr_text": "纠缠之缘"},
            "confirm.png", [1.0], [0.8], None, label="星尘兑换",
        )
    finally:
        env.restore()
    check("返回 BOUGHT", result == zs.BOUGHT, result)
    check("重新定位过一次（共 2 次）", env.locate_items == 2, env.locate_items)
    check("点了两次商品 + 一次确认", len(env.clicks) == 3, env.clicks)
    check(
        f"重试前等了 {zs.RETRY_SETTLE_SEC}s",
        any(abs(s - zs.RETRY_SETTLE_SEC) < 0.01 for s in env.sleeps),
        env.sleeps,
    )


def test_3_still_no_popup_gives_up():
    print("[3] 重试后仍无弹窗：返回 NO_POPUP（按已达上限处理）")
    env = Env(confirm_results=[None, None])
    env.install()
    try:
        result = zs._buy_once(
            LOG, 1, {"name": "星轨专票", "ocr_text": "星轨专票"},
            "confirm.png", [1.0], [0.8], None, label="余烬兑换",
        )
    finally:
        env.restore()
    check("返回 NO_POPUP", result == zs.NO_POPUP, result)


def test_4_partial_keeps_legacy_semantics():
    print("[4] 1/5 仍按「已达上限」处理（用户 2026-11-01 口径），但日志能看出中断")
    env = Env()
    env.install()
    me._buy_once = lambda *a, **k: zs.BOUGHT if not env.confirm_results else env.confirm_results.pop(0)
    env.confirm_results = [zs.BOUGHT, zs.NO_POPUP]
    saved = me._buy_once
    try:
        ok, summaries = me._buy_items(
            LOG, 1, [{"name": "纠缠之缘", "count": 5}],
            "confirm.png", "星尘兑换", [1.0], [0.8], None,
        )
    finally:
        me._buy_once = saved
        env.restore()
    check("all_ok=True（沿用已购口径）", ok is True, ok)
    check("摘要 1/5", any("1/5" in s for s in summaries), summaries)


def test_5_no_popup_with_remaining_is_not_full():
    print("[5] 点商品无弹窗但页面还写着「剩余 4」→ NOT_FULL（用户：4/5 必须算未完成）")
    env = Env(confirm_results=[None, None])
    env.install()
    saved = zs._read_item_remaining
    zs._read_item_remaining = lambda logger, hwnd, center, radius=320, item_text="": 4
    try:
        result = zs._buy_once(
            LOG, 1, {"name": "纠缠之缘", "ocr_text": "纠缠之缘"},
            "confirm.png", [1.0], [0.8], None, label="星尘兑换",
        )
    finally:
        zs._read_item_remaining = saved
        env.restore()
    check("返回 NOT_FULL", result == zs.NOT_FULL, result)


def test_6_not_full_marks_failure():
    print("[6] NOT_FULL 进 _buy_items：all_ok=False，摘要写明未买满")
    env = Env()
    env.install()
    saved = me._buy_once
    me._buy_once = lambda *a, **k: zs.NOT_FULL
    try:
        ok, summaries = me._buy_items(
            LOG, 1, [{"name": "纠缠之缘", "count": 5}],
            "confirm.png", "星尘兑换", [1.0], [0.8], None,
        )
    finally:
        me._buy_once = saved
        env.restore()
    check("all_ok=False", ok is False, ok)
    check("摘要含「未买满」", any("未买满" in s for s in summaries), summaries)


def test_7_remaining_zero_is_still_already():
    print("[7] 剩余读数为 0（真到上限）→ 仍按已达上限，不算失败")
    env = Env(confirm_results=[None, None])
    env.install()
    saved = zs._read_item_remaining
    zs._read_item_remaining = lambda logger, hwnd, center, radius=320, item_text="": 0
    try:
        result = zs._buy_once(
            LOG, 1, {"name": "星轨专票", "ocr_text": "星轨专票"},
            "confirm.png", [1.0], [0.8], None, label="余烬兑换",
        )
    finally:
        zs._read_item_remaining = saved
        env.restore()
    check("返回 NO_POPUP", result == zs.NO_POPUP, result)


def main():
    test_1_bought_waits_for_reward_popup()
    test_2_retry_after_missing_popup()
    test_3_still_no_popup_gives_up()
    test_4_partial_keeps_legacy_semantics()
    test_5_no_popup_with_remaining_is_not_full()
    test_6_not_full_marks_failure()
    test_7_remaining_zero_is_still_already()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
