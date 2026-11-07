# -*- coding: utf-8 -*-
"""白荆茶憩角色选择页「喝茶」确认兜底回归（2026-11-07）。

事故：13:20 旧代码跑的当帧取证（20261107_132050_BaiJingDaily_午后茶憩.jpg）
显示点首个角色卡后页面根本不跳页——当帧就是角色选择页本身（首位角色卡已打勾、
今日可邀份额 3/3、底部说明文字「每名同调者一天仅能邀请一次…」就挂在这页上），
之前认定的"点角色卡进角色详情页"多半就是它。真正的前进按钮是右下「喝茶」
圆钮，点它才进茶憩场景。

两个硬约束：
  1. 「喝茶」二字是竖排艺术字，当帧 OCR 15 个文字区域里完全没有它——OCR
     候选基本命不中，必须在确认仍停留选择页时按 TEA_SELECT_DRINK_RATIO
     固定比例兜底点击（与休息室「午后茶憩」入口的 OCR 优先/比例兜底同模式）。
  2. 兜底必须先快检 TEA_SELECT_MARKERS 在场才点：nudge 也可能在未知页面上
     被调，盲点比例会把页面点退（11-03/11-04 的老坑）。

改法：`TEA_INVITE_TEXTS` 加「喝茶」（OCR 哪天认出竖排字就直接点）+
`_nudge_into_tea_scene` 末尾加"选择页在场 → 比例点喝茶"兜底。

覆盖：
  [1] 选择页 + OCR 无命中 → 兜底点右下「喝茶」比例，nudge 返回 True
  [2] 不在选择页（未知页面）→ 什么都不点，如实返回 False
  [3] OCR 直接命中「喝茶」→ 点 OCR 位置，比例兜底不触发
  [4] 整链路：nudge 点「喝茶」后进场景 → 茶憩成功（比例兜底与后续场景衔接）
  [5] 「喝茶」通过 _invite_hit_usable（短词、不在排除表）
  [6] 接线静态断言（常量定义 + nudge 引用 + 排除表不误伤）

跑法：.venv\\Scripts\\python.exe tests\\suite_baijing_tea_select_drink_20261107.py
"""

import io
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import game_auto.runtime.baijing_daily as bd  # noqa: E402

LOG = logging.getLogger("tea-drink-test")
LOG.addHandler(logging.NullHandler())
FAILURES = []

DRINK_XY = (1587, 863)  # 「喝茶」圆钮中心（13:20 当帧 960x540 实测 ×2）


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} :: {detail}")
        FAILURES.append(name)


class FakeTime:
    def __init__(self):
        self.t = 0.0

    def time(self):
        self.t += 0.05
        return self.t

    def sleep(self, s):
        self.t += float(s)


class TeaSelectEnv:
    """茶憩链路桩：无特写、_scene_results 恒空；OCR 候选按调用身份分派。"""

    def __init__(self, role_page=False, scene=False, summary=False,
                 invite_hit=None):
        self.role_page = role_page      # TEA_SELECT_MARKERS 等待结果
        self.scene = scene              # TEA_SCENE_MARKERS 等待结果
        self.summary = summary
        self.invite_hit = invite_hit    # nudge 的「邀请/喝茶」OCR 命中（可 None）
        self.clicks = []
        self.ratios = []
        self.failures = []
        self.panel_returns = []
        self._saved = {}

    def install(self):
        self._saved = {k: getattr(bd, k) for k in (
            "_find_row_button", "_pyautogui_click_guarded", "_wait_page_markers",
            "_ocr_best_match", "_click_ratio", "_return_to_task_panel",
            "_record_step_failure", "_ensure_window", "_window_region",
            "_scene_results", "time",
        )}
        bd._find_row_button = lambda *a, **k: (1700, 346)
        bd._pyautogui_click_guarded = lambda x, y, *a, **k: self.clicks.append((x, y))
        bd._wait_page_markers = self._wait
        bd._ocr_best_match = self._ocr
        bd._click_ratio = self._ratio
        bd._return_to_task_panel = lambda *a, **k: self.panel_returns.append(True) or True
        bd._record_step_failure = (
            lambda logger, hwnd, step, detail: self.failures.append((step, detail))
        )
        bd._ensure_window = lambda logger, hwnd: hwnd
        bd._window_region = lambda hwnd: (0, 0, 1920, 1080)
        bd._scene_results = lambda logger, hwnd=None: []   # 无特写
        bd.time = FakeTime()

    def restore(self):
        for k, v in self._saved.items():
            setattr(bd, k, v)

    # ---- 数据缝 ----
    def _wait(self, logger, hwnd, markers, timeout=6.0, stop_check=None, **k):
        if markers == bd.TEA_SELECT_MARKERS:
            return self.role_page
        if markers == bd.TEA_SCENE_MARKERS:
            return self.scene
        if markers == bd.TEA_SUMMARY_MARKERS:
            return self.summary
        return False  # 「是否前往茶憩」确认框等

    def _ocr(self, logger, texts, hwnd, frac_region=None, min_conf=0.5, exclude=()):
        if tuple(texts) == bd.TEA_DRINK_TEXTS:
            return None    # 场景内喝茶按钮轮询：桩不提供（两条候选表共享「喝茶」，按调用身份分派）
        if any(t in bd.REST_ROOM_MARKERS for t in texts):
            return None    # 非休息室
        if any(t in bd.TEA_INVITE_TEXTS for t in texts) and self.invite_hit:
            return dict(self.invite_hit)
        return None

    def _ratio(self, logger, hwnd, ratio, label=""):
        self.ratios.append((tuple(ratio), label))
        return (int(ratio[0] * 1920), int(ratio[1] * 1080))

    def run(self):
        self.install()
        try:
            ok = bd._handle_tea_break_task(LOG, 1)
        finally:
            self.restore()
        return ok


def test_select_page_drink_fallback():
    print("[1] 选择页 + OCR 无命中：兜底点右下「喝茶」比例，nudge 返回 True")
    env = TeaSelectEnv(role_page=True)
    env.install()
    try:
        ok = bd._nudge_into_tea_scene(LOG, 1)
    finally:
        env.restore()
    check("nudge 返回 True", ok is True, ok)
    check("按 TEA_SELECT_DRINK_RATIO 点了「喝茶」",
          (bd.TEA_SELECT_DRINK_RATIO, "角色选择页右下「喝茶」") in env.ratios, env.ratios)
    check("除任务行外没有其它裸点击", env.clicks == [], env.clicks)


def test_unknown_page_no_blind_click():
    print("[2] 不在选择页（未知页面）：什么都不点，如实返回 False")
    env = TeaSelectEnv(role_page=False)
    env.install()
    try:
        ok = bd._nudge_into_tea_scene(LOG, 1)
    finally:
        env.restore()
    check("nudge 返回 False", ok is False, ok)
    check("没有比例点击", env.ratios == [], env.ratios)
    check("没有裸点击", env.clicks == [], env.clicks)


def test_ocr_drink_hit_takes_priority():
    print("[3] OCR 直接命中「喝茶」：点 OCR 位置，比例兜底不触发")
    env = TeaSelectEnv(role_page=True,
                       invite_hit={"text": "喝茶", "confidence": 0.70, "center": DRINK_XY})
    env.install()
    try:
        ok = bd._nudge_into_tea_scene(LOG, 1)
    finally:
        env.restore()
    check("nudge 返回 True", ok is True, ok)
    check("点了 OCR 位置", DRINK_XY in env.clicks, env.clicks)
    check("比例兜底没触发", env.ratios == [], env.ratios)


def test_full_flow_success_after_drink():
    print("[4] 整链路：nudge 点「喝茶」后进场景 → 茶憩成功")
    env = TeaSelectEnv(role_page=True, summary=True)
    # nudge 点完「喝茶」比例后场景就绪（模拟按钮生效）
    orig_ratio = env._ratio

    def _ratio(logger, hwnd, ratio, label=""):
        result = orig_ratio(logger, hwnd, ratio, label)
        if label == "角色选择页右下「喝茶」":
            env.scene = True
        return result
    env._ratio = _ratio
    ok = env.run()
    check("整链路成功", ok is True, ok)
    check("点过首个角色卡和「喝茶」",
          any(label == "首个角色卡" for _, label in env.ratios)
          and any("喝茶" in label for _, label in env.ratios), env.ratios)
    check("没有落失败记录", env.failures == [], env.failures)


def test_drink_text_passes_usable():
    print("[5] 「喝茶」通过 _invite_hit_usable（短词、不在排除表）")
    check("「喝茶」可用", bd._invite_hit_usable(
        {"text": "喝茶", "confidence": 0.70, "center": DRINK_XY}), None)
    check("「喝茶」在 nudge 候选里", "喝茶" in bd.TEA_INVITE_TEXTS, bd.TEA_INVITE_TEXTS)
    check("排除表不误伤「喝茶」",
          not any("喝茶" in x for x in bd.TEA_INVITE_EXCLUDE_TEXTS),
          bd.TEA_INVITE_EXCLUDE_TEXTS)


def test_wiring():
    print("[6] 接线静态断言")
    src = (PROJECT_ROOT / "src/game_auto/runtime/baijing_daily.py").read_text(encoding="utf-8")
    check("TEA_SELECT_DRINK_RATIO 已定义",
          "TEA_SELECT_DRINK_RATIO = (0.827, 0.800)" in src, None)
    check("nudge 里快检选择页后才点「喝茶」",
          "TEA_SELECT_MARKERS, timeout=2.0, stop_check=stop_check" in src and
          'TEA_SELECT_DRINK_RATIO, "角色选择页右下「喝茶」"' in src, None)
    check("失败口径含「喝茶」", "「邀请/开始茶憩/喝茶」推进入口" in src, None)


def main():
    test_select_page_drink_fallback()
    test_unknown_page_no_blind_click()
    test_ocr_drink_hit_takes_priority()
    test_full_flow_success_after_drink()
    test_drink_text_passes_usable()
    test_wiring()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
