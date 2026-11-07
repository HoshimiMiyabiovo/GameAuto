# -*- coding: utf-8 -*-
"""白荆茶憩链路「关注特写」自愈回归（2026-11-07）。

事故：11-06 09:15 茶憩链路停在「关注特写」镜头里干等茶憩纪念函直到失败——
特写帧右上「退出关注」(0.80) 明晃晃挂着没人点（flow_failures #196，取证截图
20261106_091513_BaiJingDaily_午后茶憩_ocr.jpg），之后两杯喝茶的盲点对话全打在
特写里。2026-11-07 用户指路：「那不是有个退出关注？点那个 X 就关掉了」。

改法：`_exit_focus_if_present`（口径与登记表「关注特写」规则一致，右上角区域
限定）+ `_retry_tea_entry_after_focus`（退出特写只会回到休息室全景，必须按
「午后茶憩立牌卡 → 角色卡」重找一次入口）。接线四处：nudge 开头、场景等待
失败后、nudge 失败兜底、喝茶轮询循环（特写里不盲点对话框）。

覆盖：
  [1] 场景等待超时时帧在特写 → 点退出关注 → 重走休息室入口 → 茶憩成功
  [2] 特写退不掉 → 诚实失败落证据，且绝不盲点对话框
  [3] 无特写 → 行为与旧版完全一致（回归）
  [4] 「退出关注」只认右上角区域（FOCUS_MODE_REGION，别处的同名词不点）
  [5] 四处接线静态断言

跑法：.venv\\Scripts\\python.exe tests\\suite_baijing_tea_focus_20261107.py
"""

import io
import logging
import sys
from pathlib import Path
from unittest import mock  # noqa: F401  (保持与其它套件一致的导入面)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import game_auto.runtime.baijing_daily as bd  # noqa: E402

LOG = logging.getLogger("tea-focus-test")
LOG.addHandler(logging.NullHandler())
FAILURES = []

FOCUS_XY = (1764, 152)      # 11-06 09:15 取证帧「退出关注」位置（conf 0.80）
TEA_ENTRY_XY = (1720, 948)  # 休息室「午后茶憩」文字（11-06 09:12 实点 conf 0.77）


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


class TeaFocusEnv:
    """茶憩链路桩：_scene_results 供特写检测，其余接口与 false_positive 套件同缝。"""

    def __init__(self, focus=False, scene=False, summary=False, role_page=False,
                 in_rest=True, tea_entry_hit=None, invite_hit=None):
        self.focus = focus              # True → _scene_results 返回特写帧
        self.scene = scene              # TEA_SCENE_MARKERS 等待结果
        self.summary = summary
        self.role_page = role_page      # TEA_SELECT_MARKERS 等待结果
        self.in_rest = in_rest          # _in_rest_room 结果
        self.tea_entry_hit = tea_entry_hit
        self.invite_hit = invite_hit
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
        bd._scene_results = self._scene_results
        bd.time = FakeTime()

    def restore(self):
        for k, v in self._saved.items():
            setattr(bd, k, v)

    # ---- 数据缝 ----
    def _scene_results(self, logger, hwnd=None):
        if not self.focus:
            return []
        return [{"text": "退出关注", "confidence": 0.80, "center": FOCUS_XY,
                 "box": (FOCUS_XY[0], FOCUS_XY[1], FOCUS_XY[0], FOCUS_XY[1])}]

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
            # 场景内喝茶按钮轮询：桩不提供（2026-11-07 起「喝茶」同时是 nudge 的
            # 候选，两条候选表共享成员，必须按调用身份分派，否则轮询会误命中
            # 下面的 invite_hit 把道具名点掉）
            return None
        if any(t in bd.REST_ROOM_MARKERS for t in texts):
            if self.in_rest:
                return {"text": "拜访他人", "confidence": 0.80, "center": (1338, 1004)}
            return None
        if "午后茶憩" in texts and self.tea_entry_hit:
            return dict(self.tea_entry_hit)
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


def test_focus_recovered():
    print("[1] 场景等待超时时帧在特写：点退出关注 → 重走休息室入口 → 茶憩成功")
    env = TeaFocusEnv(focus=True, scene=False, summary=True, role_page=False, in_rest=True)
    # 退出关注被点之后：休息室「午后茶憩」文字可见、角色选择页出现、场景就绪
    def _after_focus():
        env.focus = False
        env.tea_entry_hit = {"text": "午后茶憩", "confidence": 0.77, "center": TEA_ENTRY_XY}
        env.role_page = True
        env.scene = True
    orig_scene_results = env._scene_results

    def _scene_results(logger, hwnd=None):
        hit = orig_scene_results(logger, hwnd)
        if hit and env.focus:
            _after_focus()
        return hit
    env._scene_results = _scene_results

    ok = env.run()
    check("退出特写并重进后茶憩成功", ok is True, ok)
    check("先点「退出关注」再点「午后茶憩」入口（clicks[0] 是任务行「前往」）",
          env.clicks[1:3] == [FOCUS_XY, TEA_ENTRY_XY], env.clicks[:3])
    check("重走进场后点了首个角色卡",
          any(label == "首个角色卡" for _, label in env.ratios), env.ratios)
    check("没有落失败记录", env.failures == [], env.failures)


def test_focus_stuck_fails_honestly():
    print("[2] 特写退不掉：诚实失败落证据，绝不盲点对话框")
    env = TeaFocusEnv(focus=True, scene=False, summary=False, role_page=False, in_rest=True)
    ok = env.run()
    check("返回 False", ok is False, ok)
    check("落了「未检测到茶憩场景/结束摘要」证据",
          any("未检测到茶憩场景/结束摘要" in d for _, d in env.failures), env.failures)
    check("点过「退出关注」", FOCUS_XY in env.clicks, env.clicks)
    dialogue = [label for _, label in env.ratios if "对话框" in label]
    check("特写在场时不盲点对话框", dialogue == [], dialogue)
    check("交回了任务面板", env.panel_returns == [True], env.panel_returns)


def test_no_focus_unchanged():
    print("[3] 无特写：行为与旧版一致（回归）+ 场景未确认零盲点")
    env = TeaFocusEnv(focus=False, scene=False, summary=False, role_page=False, in_rest=True)
    ok = env.run()
    check("无特写时同样诚实失败", ok is False, ok)
    check("不点「退出关注」", FOCUS_XY not in env.clicks, env.clicks)
    check("失败信息口径不变",
          any("未检测到茶憩场景/结束摘要" in d for _, d in env.failures), env.failures)
    dialogue = [label for _, label in env.ratios if "对话框" in label]
    check("场景未确认时绝不盲点对话框（11-06 根因收口）", dialogue == [], dialogue)


def test_focus_region_limited():
    print("[4] 「退出关注」只认右上角区域")
    env = TeaFocusEnv()
    env.install()
    try:
        # 同样的文字出现在左下角（区域外）→ 不得命中
        bd._scene_results = lambda logger, hwnd=None: [
            {"text": "退出关注", "confidence": 0.80, "center": (200, 900),
             "box": (200, 900, 200, 900)}]
        hit = bd._exit_focus_if_present(LOG, 1)
        check("区域外的「退出关注」不点", hit is False and env.clicks == [], (hit, env.clicks))
        # 右上角区域内 → 命中并点击
        bd._scene_results = lambda logger, hwnd=None: [
            {"text": "退出关注", "confidence": 0.80, "center": FOCUS_XY,
             "box": (FOCUS_XY[0], FOCUS_XY[1], FOCUS_XY[0], FOCUS_XY[1])}]
        hit = bd._exit_focus_if_present(LOG, 1)
        check("右上角的「退出关注」点掉", hit is True and env.clicks == [FOCUS_XY], (hit, env.clicks))
    finally:
        env.restore()


def test_wiring():
    print("[5] 接线静态断言")
    src = (PROJECT_ROOT / "src/game_auto/runtime/baijing_daily.py").read_text(encoding="utf-8")
    check("nudge 开头先退特写",
          src.count("_exit_focus_if_present(logger, hwnd, stop_check=stop_check)") >= 4, None)
    check("场景等待失败后重走休息室入口",
          "scene_seen = _retry_tea_entry_after_focus(" in src, None)
    check("nudge 失败兜底也带特写恢复",
          "_exit_focus_if_present(logger, hwnd, stop_check=stop_check)\n                and _retry_tea_entry_after_focus(" in src, None)
    check("喝茶轮询里特写优先于盲点对话框",
          src.find("_exit_focus_if_present") < src.find("TEA_DIALOGUE_CLICK") or
          "_exit_focus_if_present(logger, hwnd, stop_check=stop_check):\n                continue    # 特写镜头里不盲点对话框" in src, None)


def test_invite_item_excluded():
    print("[6] 邀请候选排除「××邀请函」道具名（11-06 误点定向邀请函实录）")
    check("「定向邀请函」不可当按钮", not bd._invite_hit_usable(
        {"text": "定向邀请函", "confidence": 0.82, "center": (462, 664)}), None)
    check("「通用邀请函」不可当按钮", not bd._invite_hit_usable(
        {"text": "通用邀请函", "confidence": 0.80, "center": (462, 664)}), None)
    check("「邀请」按钮可用", bd._invite_hit_usable(
        {"text": "邀请", "confidence": 0.70, "center": (462, 664)}), None)

    # 整链路：详情页上只有道具名（没有真按钮）→ 不点击、如实失败
    env = TeaFocusEnv(focus=False, scene=False, summary=False, role_page=True,
                      in_rest=False,
                      invite_hit={"text": "定向邀请函", "confidence": 0.82, "center": (462, 664)})
    ok = env.run()
    check("只有道具名可点时不点它", (462, 664) not in env.clicks, env.clicks)
    check("链路如实失败", ok is False, ok)


def main():
    test_focus_recovered()
    test_focus_stuck_fails_honestly()
    test_no_focus_unchanged()
    test_focus_region_limited()
    test_wiring()
    test_invite_item_excluded()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
