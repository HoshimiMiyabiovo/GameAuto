"""无限暖暖 · 奇想日历/晶石兑换/共鸣商店 断链修复回归（2026-11-02）。

背景（11-01 与 11-02 两次实锤）：
- 11-01 12:26 失败记录 #126「搭配赛页未找到左侧『晶石兑换』」，
  失败截图 `20261101_122627_NikkiLauncher_晶石兑换-入口.jpg` 显示**还停在
  奇迹之冠 hub 页** → 说明点「奇迹之冠搭配赛」根本没进去；
- 接着「未回到主界面，跳过共鸣商店」→ 晶石兑换和共鸣商店两个每月清单一件没买；
- 11-02 17:31 失败记录 #141「按 L 后未出现奇想日历」，截图停在主界面 →
  L 是开关，第一次很可能已经打开了（日历正文是艺术字没认出来），再按 L 反而关掉。

修法：
  1. 奇想日历页面判定把标题「奇想日历」也算特征；重试改成「ESC 收起 → 再按 L」，
     不再连按 L（那是开关）。
  2. 点「奇迹之冠搭配赛」最多重试 3 次；实在进不去就退出 hub（左上角返回箭头）。
  3. 搭配赛页左列「晶石兑换」入口轮询 3 次再判失败。
  4. 回主界面失败时先退出 hub 再回一次，别让共鸣商店被整段跳过。

跑法：.venv\\Scripts\\python.exe tests\\suite_nikki_calendar_retry_20261102.py
"""

import logging
import sys
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import game_auto.runtime.nikki_calendar as nc  # noqa: E402

FAILURES = []
LOGGER = logging.getLogger("test_nikki_calendar_retry")
LOGGER.addHandler(logging.NullHandler())

RECT = (0, 0, 1920, 1080)


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def _item(text, center, conf=0.9):
    return {
        "text": text,
        "confidence": conf,
        "center": center,
        "box": (center[0] - 20, center[1] - 12, center[0] + 20, center[1] + 12),
    }


# 奇想日历页：奇迹之冠入口在 CALENDAR_CROWN_REGION 内
CALENDAR_ITEMS = [_item("每日灵感", (400, 300)), _item("奇迹之冠", (1000, 500))]
# 奇迹之冠 hub：搭配赛入口在 HUB_PAIR_REGION 内
HUB_ITEMS = [
    _item("奇迹之冠搭配赛", (400, 620)),
    _item("奇迹之冠争锋赛", (1500, 400)),
]


def test_calendar_page_title():
    print("[1] 奇想日历页面判定")
    check("只认标题也算奇想日历页",
          nc._is_calendar_page([_item("奇想日历", (100, 50))]) is True)
    check("正文特征仍然有效",
          nc._is_calendar_page([_item("每日灵感", (400, 300)), _item("幻境挑战", (1200, 200))]) is True)
    check("主界面不会误判", nc._is_calendar_page([_item("商城", (150, 50))]) is False)


def test_open_calendar_uses_esc_between_l():
    print("[2] 打开奇想日历：认不出就用 ESC 收起，别连按 L")
    keys = []
    with mock.patch.object(nc, "_ocr_full", lambda logger, hwnd, region=None: []), \
         mock.patch.object(nc, "_wait_for", lambda *a, **k: None), \
         mock.patch.object(nc, "_focus", lambda hwnd: None), \
         mock.patch.object(nc, "_press_key", lambda key: keys.append(key)), \
         mock.patch.object(nc, "_press_esc", lambda: keys.append("esc")), \
         mock.patch.object(nc, "_dismiss_continue_screens", lambda *a, **k: 0):
        ok = nc._open_calendar(LOGGER, 1234)
    check("没打开返回 False", ok is False)
    check("L 与 ESC 交替出现", keys == ["l", "esc", "l", "esc", "l", "esc"], keys)


def test_open_calendar_already_open():
    print("[2b] 已经在日历页时不再按 L")
    keys = []
    with mock.patch.object(nc, "_ocr_full", lambda logger, hwnd, region=None: [_item("奇想日历", (100, 50))]), \
         mock.patch.object(nc, "_wait_for", lambda *a, **k: None), \
         mock.patch.object(nc, "_focus", lambda hwnd: None), \
         mock.patch.object(nc, "_press_key", lambda key: keys.append(key)):
        ok = nc._open_calendar(LOGGER, 1234)
    check("直接判定为已打开", ok is True)
    check("没有多按 L", not keys, keys)


def test_pair_click_retried():
    print("[3] 搭配赛点击重试")
    clicks = []
    state = {"n": 0}

    def _fake_wait(logger, hwnd, predicate, timeout, stop_check=None, **kw):
        # 第一次是"等进 hub"（给 hub 特征），之后都是"等搭配赛页"（永远不出现）
        state["n"] += 1
        return HUB_ITEMS if state["n"] == 1 else None

    with mock.patch.object(nc, "_window_rect", lambda hwnd: RECT), \
         mock.patch.object(nc, "_ocr_full", lambda logger, hwnd, region=None: CALENDAR_ITEMS + HUB_ITEMS), \
         mock.patch.object(nc, "_wait_for", _fake_wait), \
         mock.patch.object(nc, "_click", lambda center: clicks.append(center)), \
         mock.patch.object(nc, "_record_failure", lambda *a, **k: None), \
         mock.patch.object(nc, "_leave_crown_hub", lambda *a, **k: True):
        claimed, note = nc._claim_pair_rewards(LOGGER, 1234)
    expected = nc._pair_click_point(HUB_ITEMS[0], RECT)
    pair_clicks = [c for c in clicks if c == expected]
    check("搭配赛点击重试到上限", len(pair_clicks) == nc.CROWN_PAIR_CLICK_ATTEMPTS, pair_clicks)
    check("点击的是文字右侧多边形，不是文字中心",
          expected != HUB_ITEMS[0]["center"], (expected, HUB_ITEMS[0]["center"]))
    check("进不去时提示未就绪", "未就绪" in note, note)


def test_pair_click_succeeds_after_retry():
    print("[3b] 第一次点不进去、第二次成功")
    state = {"n": 0}
    clicks = []

    def _fake_wait(logger, hwnd, predicate, timeout, stop_check=None, **kw):
        state["n"] += 1
        return HUB_ITEMS if state["n"] >= 2 else None

    with mock.patch.object(nc, "_window_rect", lambda hwnd: RECT), \
         mock.patch.object(nc, "_ocr_full", lambda logger, hwnd, region=None: CALENDAR_ITEMS + HUB_ITEMS), \
         mock.patch.object(nc, "_wait_for", _fake_wait), \
         mock.patch.object(nc, "_click", lambda center: clicks.append(center)), \
         mock.patch.object(nc, "_record_failure", lambda *a, **k: None):
        claimed, note = nc._claim_pair_rewards(LOGGER, 1234)
    check("重试后进去了", "未就绪" not in note, note)


def test_exchange_entry_polled():
    print("[4] 晶石兑换入口轮询")
    calls = {"n": 0}

    def _fake_ocr(logger, hwnd, region=None):
        calls["n"] += 1
        # 前两次认不出左列菜单，第三次才渲染出来
        if calls["n"] < 3:
            return [_item("赛事嘉奖", (60, 850))]
        return [_item("赛事嘉奖", (60, 850)), _item("晶石兑换", (120, 780))]

    with mock.patch.object(nc, "_window_rect", lambda hwnd: RECT), \
         mock.patch.object(nc, "_ocr_full", _fake_ocr), \
         mock.patch.object(nc, "_record_failure", lambda *a, **k: None), \
         mock.patch.object(nc, "_dismiss_continue_screens", lambda *a, **k: 0), \
         mock.patch.object(nc, "_click", lambda center: None), \
         mock.patch.object(nc, "_wait_for", lambda *a, **k: [{"text": "本月限购"}]), \
         mock.patch.object(nc, "_buy_plan_items", lambda *a, **k: "已兑换 2 件"):
        out = nc._exchange_crystals(LOGGER, 1234)
    check("入口晚渲染也能找到", "已兑换" in str(out), out)
    check("确实重试了识别", calls["n"] >= 3, calls)


def test_back_to_main_leaves_hub():
    print("[5] 回主界面：ESC 不行就先退出 hub 再回一次")
    calls = {"leave": 0, "return": 0}

    def _fake_return(logger, hwnd, scales, confidences, stop_check=None):
        calls["return"] += 1
        return calls["return"] >= 2

    with mock.patch.object(nc, "_return_to_main", _fake_return), \
         mock.patch.object(nc, "_leave_crown_hub", lambda *a, **k: calls.__setitem__("leave", calls["leave"] + 1) or True):
        ok = nc._back_to_main_for_shop(LOGGER, 1234, [1.0], [0.8])
    check("退出 hub 后回到主界面", ok is True)
    check("退出 hub 被调用过", calls["leave"] == 1, calls)
    check("回主界面被调用两次", calls["return"] == 2, calls)


def test_run_flow_still_tries_resonance_shop():
    print("[6] 晶石兑换失败也要试共鸣商店")
    notes = []

    with mock.patch.object(nc, "_stop", lambda stop_check=None: False), \
         mock.patch.object(nc, "_window_rect", lambda hwnd: RECT), \
         mock.patch.object(nc, "_has_main", lambda *a, **k: True), \
         mock.patch.object(nc, "_dismiss_continue_screens", lambda *a, **k: 0), \
         mock.patch.object(nc, "_open_calendar", lambda *a, **k: True), \
         mock.patch.object(nc, "_claim_pair_rewards", lambda *a, **k: (0, "无可领奖励")), \
         mock.patch.object(nc, "_exchange_crystals", lambda *a, **k: "未找到「晶石兑换」入口"), \
         mock.patch.object(nc, "_back_to_main_for_shop", lambda *a, **k: True), \
         mock.patch.object(nc, "_run_resonance_shop", lambda *a, **k: notes.append("共鸣商店跑了") or "已兑换 4 件"), \
         mock.patch.object(nc, "_ensure_main_or_close", lambda *a, **k: "main"):
        _ok, msg = nc._run_flow_inner(LOGGER, 1234, [1.0], [0.8])
    check("共鸣商店被调用", notes == ["共鸣商店跑了"], notes)
    check("摘要里带共鸣商店的兑换结果", "已兑换 4 件" in msg, msg)


def main():
    test_calendar_page_title()
    test_open_calendar_uses_esc_between_l()
    test_open_calendar_already_open()
    test_pair_click_retried()
    test_pair_click_succeeds_after_retry()
    test_exchange_entry_polled()
    test_back_to_main_leaves_hub()
    test_run_flow_still_tries_resonance_shop()

    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
