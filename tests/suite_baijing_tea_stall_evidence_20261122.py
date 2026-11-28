# -*- coding: utf-8 -*-
"""白荆茶憩「卡点取证 + 气泡推进 + 冻结判定 + 证据口径」回归（2026-11-22）。

事故链（11-08 12:34，日志 + workflow.sqlite3 双向实锤）：
  12:34:34 点中「再来一杯」（全部日志唯一一次）→ 13s 后弹「选择话题」→ 点第一个
  话题 → 12:34:55 出现「怎么了？」气泡 @(1438,508)，之后连续 14 次点底部对话框
  (0.50,0.92) 画面一字不变；摘要等 45s 是纯等待（_wait_page_markers 不点击），
  当然等不到。旧 has_evidence = scene_seen or summary_seen 让"进场景看到茶憩
  纪念函"提前判成功——11-08 因此没落任何茶憩证据，只落了退出链的 #222。而当天
  daily_progress 里 tea_break 仍是 0/1：点中按钮 ≠ 完成。
  11-09 起每天进茶憩第一帧都是同一句「怎么了？」（会话被游戏持久化），代码只会
  点底部对话框，于是天天卡同一帧。

本套件锁四件事：
  [1] 完成证据只有「茶憩结束」摘要；见到场景不再单独判成功
  [2] 每轮没命中「喝茶」当场落证据；摘要等不到也落证据
  [3] 画面中右侧的孤立气泡（「怎么了？」）点气泡本身，不点底部对话框
  [4] 同一帧 30s 不变 → 判卡死、落证据并返回 False（不再干等两轮+45s）

跑法：.venv/Scripts/python.exe tests/suite_baijing_tea_stall_evidence_20261122.py
"""

import io
import logging
import sys
from pathlib import Path
from unittest import mock  # noqa: F401

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import game_auto.runtime.baijing_task_handlers as bd  # noqa: E402
import game_auto.runtime.baijing_common as bcommon  # noqa: E402
import game_auto.runtime.baijing_scene as bscene  # noqa: E402
import game_auto.runtime.baijing_tasklist as btasklist  # noqa: E402

LOG = logging.getLogger("tea-stall-test")
LOG.addHandler(logging.NullHandler())
FAILURES = []

BUBBLE_XY = (1438, 509)      # 11-08 12:34:55 / 11-09 起每天同一帧「怎么了？」
DIALOGUE_XY = (960, 993)     # 底部对话框（旧实现盲点位置，点它无效）

STALL_FRAME = [
    ("返回", (193, 50)),
    ("茶憩纪念函", (235, 847)),
    ("尤尼", (234, 876)),
    ("尤尼：嗯", (818, 996)),
    ("24919904", (1659, 1068)),
]
BUBBLE_FRAME = STALL_FRAME + [("怎么了？", BUBBLE_XY)]
TOPIC_FRAME = STALL_FRAME + [("选择话题", (1359, 312))]
SUMMARY_FRAME = [("茶憩结束", (960, 500)), ("点击退出", (960, 620))]


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} :: {detail}")
        FAILURES.append(name)


def frame_items(pairs):
    return [
        {"text": t, "confidence": 0.92, "center": c,
         "box": (c[0], c[1], c[0], c[1])}
        for t, c in pairs
    ]


class FakeTime:
    def __init__(self):
        self.t = 0.0

    def time(self):
        self.t += 0.05
        return self.t

    # _TeaProgressTracker（2026-11-28 #533/#534 修复）默认走 time.monotonic
    def monotonic(self):
        return self.time()

    def sleep(self, s):
        self.t += float(s)


class TeaStallEnv:
    """茶憩链路桩：_scene_results 返回可切换的 OCR 帧，其余接口同既有茶憩套件。"""

    def __init__(self, frame_fn=None, summary_wait=False):
        self.frame_fn = frame_fn or (lambda env: STALL_FRAME)
        self.summary_wait = summary_wait
        self.clicks = []
        self.ratios = []
        self.failures = []
        self.panel_returns = []
        self.time = None
        self._saved = {}

    def install(self):
        self._saved = {k: getattr(bd, k) for k in (
            "_find_row_button", "_pyautogui_click_guarded", "_wait_page_markers",
            "_ocr_best_match", "_click_ratio", "_return_to_task_panel",
            "_record_step_failure", "_ensure_window", "_window_region",
            "_scene_results", "time",
        )}
        bd._find_row_button = lambda *a, **k: (1700, 346)
        bd._pyautogui_click_guarded = self._click
        bd._wait_page_markers = self._wait
        bd._ocr_best_match = self._ocr
        bd._click_ratio = self._ratio
        bd._return_to_task_panel = lambda *a, **k: self.panel_returns.append(True) or True
        bd._record_step_failure = (
            lambda logger, hwnd, step, detail: self.failures.append((step, detail))
        )
        bd._ensure_window = lambda logger, hwnd: hwnd
        bd._window_region = lambda hwnd: (0, 0, 1920, 1080)
        bd._scene_results = lambda logger, hwnd=None: frame_items(self.frame_fn(self))
        self.time = FakeTime()
        bd.time = self.time
        for _k in ("_find_row_button", "_pyautogui_click_guarded", "_wait_page_markers",
                   "_ocr_best_match", "_click_ratio", "_return_to_task_panel",
                   "_record_step_failure", "_ensure_window", "_window_region",
                   "_scene_results", "_stopped", "time"):
            for _m in (bcommon, bscene, btasklist):
                if hasattr(_m, _k):
                    if (_m.__name__, _k) not in self._saved:
                        self._saved[(_m.__name__, _k)] = (_m, getattr(_m, _k))
                    setattr(_m, _k, getattr(bd, _k))

    def restore(self):
        for k, v in self._saved.items():
            if isinstance(k, tuple):
                _m, _orig = v
                setattr(_m, k[1], _orig)
            else:
                setattr(bd, k, v)

    # ---- 数据缝 ----
    def _wait(self, logger, hwnd, markers, timeout=6.0, stop_check=None, **k):
        if markers == bd.TEA_SELECT_MARKERS:
            return True          # 有角色选择页，nudge 走「喝茶」兜底
        if markers == bd.TEA_SCENE_MARKERS:
            return True          # 场景等待通过（帧里也有茶憩纪念函）
        if markers == bd.TEA_SUMMARY_MARKERS:
            return self.summary_wait
        return False             # 「是否前往茶憩」确认框等

    def _ocr(self, logger, texts, hwnd, frac_region=None, min_conf=0.5, exclude=()):
        return None              # 全部走 _scene_results 帧

    def _click(self, x, y, *a, **k):
        self.clicks.append((x, y))

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


_STALL_TAILS = ("次要文本甲", "次要文本乙", "次要文本丙")


def _stall_alternating(env):
    """同一场景、三帧轮换：模拟"画面在动但一直没饮料/摘要"，不触发冻结。

    三帧而不是两帧：一轮里 _scene_results 至少被取两次（轮询一次、特写检测
    一次），两帧轮换会被奇偶抵消成同一签名。
    """
    env._flip = (getattr(env, "_flip", 0) + 1) % len(_STALL_TAILS)
    base = list(STALL_FRAME)
    base.append((_STALL_TAILS[env._flip], (1500, 1050)))
    return base


def test_summary_is_the_only_evidence():
    print("[1] 只有「茶憩结束」摘要算完成：摘要到场 → True 且不留失败记录")
    env = TeaStallEnv(frame_fn=lambda e: SUMMARY_FRAME, summary_wait=False)
    ok = env.run()
    check("摘要在场判成功", ok is True, ok)
    check("没有落「未等到摘要」证据",
          not any("未等到「茶憩结束」摘要" in d for _, d in env.failures), env.failures)


def test_scene_alone_is_not_success():
    print("[2] 只有场景（茶憩纪念函）没有摘要/喝茶 → False，每轮当场落证据")
    env = TeaStallEnv(frame_fn=_stall_alternating, summary_wait=False)
    ok = env.run()
    check("场景不算完成，返回 False", ok is False, ok)
    check("第 1 杯没命中就落了证据",
          any("第 1 杯" in d for _, d in env.failures), env.failures)
    check("第 2 杯没命中就落了证据",
          any("第 2 杯" in d for _, d in env.failures), env.failures)
    check("摘要等不到也落了证据",
          any("未等到「茶憩结束」摘要" in d for _, d in env.failures), env.failures)
    check("画面在动，不该误报冻结",
          not any("未变化" in d for _, d in env.failures), env.failures)
    check("失败也交回任务面板", env.panel_returns == [True], env.panel_returns)


def test_bubble_is_clicked():
    print("[3] 中右侧孤立气泡「怎么了？」点气泡本身，而不是底部对话框")
    state = {"bubble_hits": 0}

    tails = ("话题分支甲", "话题分支乙", "话题分支丙")

    def frames(env):
        if state["bubble_hits"] == 0:
            return BUBBLE_FRAME
        env._flip = (getattr(env, "_flip", 0) + 1) % len(tails)
        return TOPIC_FRAME + [(tails[env._flip], (1440, 420))]

    env = TeaStallEnv(frame_fn=frames, summary_wait=True)

    def click(x, y, *a, **k):
        env.clicks.append((x, y))
        if (int(x), int(y)) == BUBBLE_XY:
            state["bubble_hits"] += 1

    env._click = click
    ok = env.run()
    check("气泡被点中（1438,509）", state["bubble_hits"] >= 1, env.clicks[:6])
    check("气泡在场时不先点底部对话框", env.clicks and tuple(env.clicks[0]) != DIALOGUE_XY,
          env.clicks[:6])
    check("最终等到摘要判成功", ok is True, ok)


def test_frozen_frame_fails_fast():
    print("[4] 同一帧 30s 不变 → 判卡死、落证据、快速返回 False")
    env = TeaStallEnv(frame_fn=lambda e: BUBBLE_FRAME, summary_wait=False)
    ok = env.run()
    check("返回 False", ok is False, ok)
    check("落了「画面 30s 未变化」证据",
          any("未变化" in d for _, d in env.failures), env.failures)
    check("冻结后不再等第 2 杯（提前收工）",
          not any("第 2 杯" in d for _, d in env.failures), env.failures)
    check("总耗时远小于两轮+45s（<60s 模拟时间）", env.time.t < 60.0, env.time.t)


def test_bubble_finder_unit():
    print("[5] 气泡识别单元：区域/排除词/纯数字")
    saved = bd._window_region
    bd._window_region = lambda hwnd: (0, 0, 1920, 1080)
    try:
        hit = bd._tea_bubble_hit(1, frame_items(BUBBLE_FRAME))
        check("命中「怎么了？」", bool(hit) and hit["text"] == "怎么了？", hit)
        check("侧栏「茶憩纪念函」不点",
              bd._tea_bubble_hit(1, frame_items([("茶憩纪念函", (235, 847))])) is None)
        check("纯数字不点",
              bd._tea_bubble_hit(1, frame_items([("24919904", (1440, 520))])) is None)
        check("区域外短文本不点",
              bd._tea_bubble_hit(1, frame_items([("怎么了？", (300, 900))])) is None)
        check("长文本不点",
              bd._tea_bubble_hit(1, frame_items([("这一句系统提示文字特别长啊", (1440, 520))])) is None)
    finally:
        bd._window_region = saved


def test_wiring():
    print("[6] 接线静态断言")
    src = (PROJECT_ROOT / "src/game_auto/runtime/baijing_task_handlers.py").read_text(encoding="utf-8")
    check("完成证据只剩摘要", "has_evidence = bool(summary_seen)" in src, None)
    check("场景不再单独算成功", "scene_seen or summary_seen" not in src, None)
    check("30s 冻结阈值与追踪器在用", "TEA_FREEZE_SEC" in src and "_TeaFreezeTracker()" in src, None)
    check("气泡点击接线", "点击茶憩气泡" in src and "_tea_bubble_hit(" in src, None)
    check("摘要等待期继续推进", "_drive_tea_summary(" in src, None)
    check("轮询没命中当场取证", "内未出现「喝茶/再来一杯」" in src, None)


def main():
    test_summary_is_the_only_evidence()
    test_scene_alone_is_not_success()
    test_bubble_is_clicked()
    test_frozen_frame_fails_fast()
    test_bubble_finder_unit()
    test_wiring()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
