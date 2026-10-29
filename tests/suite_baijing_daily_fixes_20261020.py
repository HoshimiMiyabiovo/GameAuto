"""白荆回廊日常流程 2026-10-20 修复回归测试。

纯 mock，不连游戏。覆盖：
  1. _find_row_button：从列表底部/中段也能足量回顶，并逐屏覆盖到底部行
  2. _claim_leaf_vein_rewards：OCR 未命中「叶脉联结」tab 时走比例兜底
  3. _claim_club_daily_task：无「领取」按钮 → no_claim；点过「领取」仍未完成 → failed
  4. _on_memory_detail_page：模板失配时用 OCR「强化烙痕」兜底

跑法：.venv\\Scripts\\python.exe artifacts\\test_baijing_daily_fixes_20261020.py
"""

import sys

sys.path.insert(0, r"E:\GithubProject")

import game_auto.runtime.baijing_daily as bd  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


class NoSleepTime:
    @staticmethod
    def sleep(_s):
        pass

    @staticmethod
    def time():
        return 0.0


def _save_attrs(names):
    return {name: getattr(bd, name) for name in names}


def _restore_attrs(saved):
    for name, value in saved.items():
        setattr(bd, name, value)


class RowHarness:
    """用虚拟 screen 号模拟任务列表滚动。"""

    def __init__(self, rows_by_screen, start_screen=0, bottom=4):
        self.rows = rows_by_screen
        self.screen = start_screen
        self.bottom = bottom
        self.scrolls = 0
        self.saved = {}

    def install(self):
        self.saved = _save_attrs(
            (
                "_find_row_button_once",
                "_scroll_row_list_to_top",
                "_scroll_list_with_verify",
                "_ensure_window",
                "_abs_region",
            )
        )
        bd._find_row_button_once = (
            lambda logger, hwnd, kw, btn: self.rows.get(self.screen, {}).get(kw)
        )
        bd._scroll_row_list_to_top = lambda logger, hwnd: self._to_top()
        bd._scroll_list_with_verify = lambda logger, hwnd, notches: self._down()
        bd._ensure_window = lambda logger, hwnd: hwnd
        bd._abs_region = lambda hwnd, frac: (0, 0, 1000, 800)

    def restore(self):
        _restore_attrs(self.saved)

    def _to_top(self):
        self.screen = 0
        return True

    def _down(self):
        if self.screen < self.bottom:
            self.screen += 1
            self.scrolls += 1
            return True
        return False


def test_find_row_from_bottom():
    print("[1] 从列表底部找顶部第 3 行（研发室场景）")
    h = RowHarness({0: {"研发室": (100, 200)}}, start_screen=4, bottom=4)
    h.install()
    try:
        pos = bd._find_row_button(bd.logging.getLogger("test"), 12345, "研发室", "前往")
    finally:
        h.restore()
    check("找到研发室", pos == (100, 200), pos)
    check("已回滚到顶部", h.screen == 0, h.screen)


def test_find_row_bottom_of_list():
    print("[2] 从顶部逐屏找到列表底部行（光刻协议场景）")
    h = RowHarness({4: {"光刻协议": (900, 700)}}, start_screen=0, bottom=4)
    h.install()
    try:
        pos = bd._find_row_button(bd.logging.getLogger("test"), 12345, "光刻协议", "前往")
    finally:
        h.restore()
    check("找到光刻协议", pos == (900, 700), pos)
    check("确实向下扫了 4 屏", h.scrolls == 4, h.scrolls)


def test_find_row_missing_terminates():
    print("[3] 行不存在时完整扫描后终止，不死循环")
    h = RowHarness({}, start_screen=0, bottom=4)
    h.install()
    try:
        pos = bd._find_row_button(bd.logging.getLogger("test"), 12345, "不存在的行", "前往")
    finally:
        h.restore()
    check("返回 None", pos is None, pos)
    check("最多两轮扫描（<=10 屏）", h.scrolls <= 10, h.scrolls)


class LeafHarness:
    def __init__(self, tab_hit):
        self.tab_hit = tab_hit
        self.clicks = []
        self.saved = {}

    def install(self):
        self.saved = _save_attrs(
            (
                "_ocr_best_match",
                "_click_ratio",
                "_pyautogui_click_guarded",
                "_wait_page_markers",
                "_ensure_window",
                "_window_region",
                "_dismiss_overlay",
                # 2026-10-25 起叶脉入口先确认面板在屏，不 mock 会在冻结时间下死循环
                "_ensure_task_list_open",
                "time",
            )
        )

        def fake_ocr(logger, texts, hwnd, frac_region=None, min_conf=0.5, exclude=()):
            if tuple(texts) == tuple(bd.LEAF_TAB_TEXTS):
                return self.tab_hit
            if tuple(texts) == ("任务总览",):
                return {"text": "任务总览", "center": (700, 850), "confidence": 0.9}
            if tuple(texts) == ("全部领取",):
                return {"text": "全部领取", "center": (1360, 860), "confidence": 0.9}
            return None

        bd._ocr_best_match = fake_ocr
        bd._click_ratio = lambda logger, hwnd, ratio, label="": self.clicks.append(
            ("ratio", ratio)
        )
        bd._pyautogui_click_guarded = lambda x, y, *a, **k: self.clicks.append(
            ("click", (x, y))
        )
        bd._wait_page_markers = lambda *a, **k: True
        bd._ensure_window = lambda logger, hwnd: hwnd
        bd._window_region = lambda hwnd: (0, 0, 1919, 1079)
        bd._dismiss_overlay = lambda *a, **k: False
        bd._ensure_task_list_open = lambda *a, **k: True
        bd.time = NoSleepTime

    def restore(self):
        _restore_attrs(self.saved)


def test_leaf_ocr_hit():
    print("[4] 叶脉 tab OCR 命中时不走比例兜底")
    h = LeafHarness({"text": "叶脉联结", "center": (208, 596), "confidence": 0.7})
    h.install()
    try:
        ok = bd._claim_leaf_vein_rewards(bd.logging.getLogger("test"), 12345)
    finally:
        h.restore()
    check("返回 True", ok is True, ok)
    check("点击 OCR 命中位置", ("click", (208, 596)) in h.clicks, h.clicks)
    check("未走比例兜底", ("ratio", bd.LEAF_TAB_FALLBACK) not in h.clicks, h.clicks)


def test_leaf_fallback():
    print("[5] 叶脉 tab OCR 未命中时按实测比例兜底")
    h = LeafHarness(None)
    h.install()
    try:
        ok = bd._claim_leaf_vein_rewards(bd.logging.getLogger("test"), 12345)
    finally:
        h.restore()
    check("返回 True", ok is True, ok)
    check("走了比例兜底", ("ratio", bd.LEAF_TAB_FALLBACK) in h.clicks, h.clicks)


class ClubHarness:
    def __init__(self, claim_hit):
        self.claim_hit = claim_hit
        self.clicks = []
        self.saved = {}

    def install(self):
        self.saved = _save_attrs(
            (
                "_ocr_best_match",
                "_pyautogui_click_guarded",
                "_click_ratio",
                "_dismiss_overlay",
                "time",
            )
        )

        def fake_ocr(logger, texts, hwnd, frac_region=None, min_conf=0.5, exclude=()):
            if tuple(texts) == tuple(bd.CLUB_DONE_TEXTS):
                return None
            if tuple(texts) == tuple(bd.CLUB_DAILY_TASK_TEXTS):
                return {"text": "俱乐部每日任务", "center": (1066, 318), "confidence": 0.9}
            if tuple(texts) == tuple(bd.CLUB_CLAIM_TEXTS):
                return self.claim_hit
            return None

        bd._ocr_best_match = fake_ocr
        bd._pyautogui_click_guarded = lambda x, y, *a, **k: self.clicks.append((x, y))
        bd._click_ratio = lambda logger, hwnd, ratio, label="": self.clicks.append(
            ("ratio", ratio)
        )
        bd._dismiss_overlay = lambda *a, **k: False
        bd.time = NoSleepTime

    def restore(self):
        _restore_attrs(self.saved)


def test_club_no_claim_button():
    print("[6] 俱乐部卡片无「领取」按钮 → no_claim（不记 failed）")
    h = ClubHarness(None)
    h.install()
    try:
        state = bd._claim_club_daily_task(bd.logging.getLogger("test"), 12345)
    finally:
        h.restore()
    check("state=no_claim", state == "no_claim", state)


def test_club_claim_button_failed():
    print("[7] 点过「领取」仍未出现「今日任务已完成」→ failed")
    h = ClubHarness({"text": "领取", "center": (1100, 450), "confidence": 0.9})
    h.install()
    try:
        state = bd._claim_club_daily_task(bd.logging.getLogger("test"), 12345)
    finally:
        h.restore()
    check("state=failed", state == "failed", state)
    check("确实点过「领取」", (1100, 450) in h.clicks, h.clicks)


def test_memory_ocr_fallback():
    print("[8] 烙痕详情页模板失配时 OCR 兜底")
    saved = _save_attrs(("_template_center", "_ocr_best_match"))
    try:
        bd._template_center = lambda *a, **k: None
        bd._ocr_best_match = lambda *a, **k: {
            "text": "强化烙痕", "center": (1600, 1000), "confidence": 0.8
        }
        check("OCR 命中视为详情页", bd._on_memory_detail_page(bd.logging.getLogger("test"), 1) is True)
        bd._template_center = lambda *a, **k: (1600, 1000)
        bd._ocr_best_match = lambda *a, **k: None
        check("模板命中仍视为详情页", bd._on_memory_detail_page(bd.logging.getLogger("test"), 1) is True)
        bd._template_center = lambda *a, **k: None
        bd._ocr_best_match = lambda *a, **k: None
        check("两者都不中判否", bd._on_memory_detail_page(bd.logging.getLogger("test"), 1) is False)
    finally:
        _restore_attrs(saved)


def test_ensure_daily_tab_retries():
    print("[9] 任务面板默认停在叶脉联结时，点日常后重试识别累计活跃度")
    saved = _save_attrs(("_ocr_best_match", "_pyautogui_click_guarded", "_ensure_window", "time"))
    state = {"checks": 0}
    clicks = []

    def fake_ocr(logger, texts, hwnd, **kwargs):
        if tuple(texts) == ("累计活跃度",):
            state["checks"] += 1
            if state["checks"] >= 3:
                return {"text": "累计活跃度", "center": (585, 356), "confidence": 0.8}
            return None
        if tuple(texts) == ("日常",):
            return {"text": "日常", "center": (207, 385), "confidence": 0.7}
        return None

    try:
        bd._ocr_best_match = fake_ocr
        bd._pyautogui_click_guarded = lambda x, y, *a, **k: clicks.append((x, y))
        bd._ensure_window = lambda logger, hwnd: hwnd
        bd.time = NoSleepTime
        ok = bd._ensure_daily_tab(bd.logging.getLogger("test"), 12345)
    finally:
        _restore_attrs(saved)
    check("重试后返回 True", ok is True, ok)
    check("点了「日常」tab", (207, 385) in clicks, clicks)
    check("检查了至少 3 次", state["checks"] >= 3, state["checks"])


def test_friend_row_path():
    print("[10] 友情勋章：任务行「前往」进入好友页")
    saved = _save_attrs(
        ("_find_row_button", "_pyautogui_click_guarded", "_wait_page_markers", "_do_friend_medal_on_page")
    )
    calls = []
    try:
        bd._find_row_button = lambda *a, **k: (1700, 560)
        bd._pyautogui_click_guarded = lambda x, y, *a, **k: calls.append((x, y))
        bd._wait_page_markers = lambda *a, **k: True
        bd._do_friend_medal_on_page = lambda *a, **k: True
        ok = bd._handle_friend_medal_task(bd.logging.getLogger("test"), 12345)
    finally:
        _restore_attrs(saved)
    check("返回 True", ok is True, ok)
    check("点击了行内前往", (1700, 560) in calls, calls)


def test_friend_main_ui_fallback():
    print("[11] 友情勋章：行内前往失败 → 主界面好友入口")
    saved = _save_attrs(
        (
            "_find_row_button",
            "close_task_list",
            "_enter_friend_page_from_main",
            "_do_friend_medal_on_page",
            "_wait_page_markers",
        )
    )
    try:
        bd._find_row_button = lambda *a, **k: None
        bd._wait_page_markers = lambda *a, **k: False
        bd.close_task_list = lambda *a, **k: True
        bd._enter_friend_page_from_main = lambda *a, **k: True
        bd._do_friend_medal_on_page = lambda *a, **k: True
        ok = bd._handle_friend_medal_task(bd.logging.getLogger("test"), 12345)
    finally:
        _restore_attrs(saved)
    check("返回 True", ok is True, ok)


def test_friend_gift_click():
    print("[12] 好友页点击「一键领取/赠送」并处理弹窗")
    saved = _save_attrs(
        ("_ocr_best_match", "_pyautogui_click_guarded", "_handle_friend_gift_popup", "_return_to_task_panel", "time")
    )
    calls = []
    try:
        bd._ocr_best_match = lambda logger, texts, hwnd, **k: {
            "text": "一键领取/赠送", "center": (1704, 58), "confidence": 0.9
        }
        bd._pyautogui_click_guarded = lambda x, y, *a, **k: calls.append((x, y))
        bd._handle_friend_gift_popup = lambda *a, **k: True
        bd._return_to_task_panel = lambda *a, **k: True
        bd.time = NoSleepTime
        ok = bd._do_friend_medal_on_page(bd.logging.getLogger("test"), 12345)
    finally:
        _restore_attrs(saved)
    check("返回 True", ok is True, ok)
    check("点击了一键领取/赠送", (1704, 58) in calls, calls)


def main():
    test_find_row_from_bottom()
    test_find_row_bottom_of_list()
    test_find_row_missing_terminates()
    test_leaf_ocr_hit()
    test_leaf_fallback()
    test_club_no_claim_button()
    test_club_claim_button_failed()
    test_memory_ocr_fallback()
    test_ensure_daily_tab_retries()
    test_friend_row_path()
    test_friend_main_ui_fallback()
    test_friend_gift_click()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
