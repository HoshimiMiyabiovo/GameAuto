"""白荆关卡战斗「体力不足」修复回归（2026-11-03 物资保障丢 25 分事故）。

事故：11-03 11:40 定向物资保障点「快速战斗」后，没弹「快速战斗设置」，
弹的是带「确认使用」的行动力/体力补充框。旧逻辑在等不到设置窗时按比例
盲点 (0.617,0.799)，再把当前页当结算页点掉，最后 `return _return_to_task_panel(...)`
返回 True —— 上层记「任务已完成」，实际任务行仍是 0/1。当天因此少
「定向物资保障」5 分 + 「完成5次战斗」停在 4/5 的 20 分，活跃度 80 而非 105。

修复：
  1. 等不到设置窗先落证（`_capture_stage_battle_dialog(tag="nodialog")`，截图+OCR 文字）；
  2. 识别「确认使用」→ 点掉（用户确认：限时行动力药可以直接用），再重试「快速战斗」；
  3. 仍无设置窗就回任务面板并返回 False，绝不盲点、绝不谎报完成。
  4. 返回 False 后 `double_battle_pending` 保持 True，后续关卡会用指定次数=2 补场次。

跑法：.venv\\Scripts\\python.exe tests\\suite_baijing_battle_power_20261103.py
"""

import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import game_auto.runtime.baijing_task_handlers as bd  # noqa: E402
import game_auto.runtime.baijing_common as bcommon  # noqa: E402
import game_auto.runtime.baijing_scene as bscene  # noqa: E402
import game_auto.runtime.baijing_tasklist as btasklist  # noqa: E402

LOG = logging.getLogger("baijing-battle-power-test")
FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


class FakeTime:
    def __init__(self):
        self.t = 0.0

    def time(self):
        self.t += 0.05
        return self.t

    def sleep(self, s):
        self.t += float(s)


class BattleEnv:
    """模拟关卡战斗页：控制设置窗/确认使用弹窗是否出现。"""

    def __init__(self, initial_dialog=True, dialog_after_use=True, use_prompt=False, settlement=True, ocr_quick=True):
        self.initial_dialog = initial_dialog  # 第一次点关卡页「快速战斗」后是否弹设置窗
        self.dialog_after_use = dialog_after_use  # 点掉「确认使用」后再点「快速战斗」是否弹窗
        self.use_prompt = use_prompt
        self.settlement = settlement
        self.ocr_quick = ocr_quick  # 关卡页 OCR 能否认出「快速战斗」按钮
        self.dialog_calls = 0
        self.ratios = []
        self.clicks = []
        self.failures = []
        self._saved = {}

    def install(self):
        self._saved = {
            "_find_row_button": bd._find_row_button,
            "_pyautogui_click_guarded": bd._pyautogui_click_guarded,
            "_wait_page_markers": bd._wait_page_markers,
            "_ocr_best_match": bd._ocr_best_match,
            "_click_ratio": bd._click_ratio,
            "_return_to_task_panel": bd._return_to_task_panel,
            "_record_step_failure": bd._record_step_failure,
            "_ensure_window": bd._ensure_window,
            "_stopped": bd._stopped,
            "time": bd.time,
        }
        bd._find_row_button = lambda *a, **k: (1701, 776)
        bd._pyautogui_click_guarded = lambda x, y, *a, **k: self.clicks.append((x, y))
        bd._wait_page_markers = self._wait
        bd._ocr_best_match = self._ocr
        bd._click_ratio = self._ratio
        bd._return_to_task_panel = lambda *a, **k: True
        bd._record_step_failure = lambda logger, hwnd, step, detail: self.failures.append((step, detail))
        bd._ensure_window = lambda logger, hwnd: hwnd
        bd._stopped = lambda stop_check: False
        bd.time = FakeTime()
        # 2026-11-14 拆分：同名桩镜像到符号实际所属模块（common/scene/tasklist）——
        # 各模块函数在自己的模块全局里解析名字，只打 handlers 不生效。
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

    def _wait(self, logger, hwnd, markers, timeout=6.0, stop_check=None, frac_region=None):
        if markers == bd.IMAGING_STAGE_MARKERS:
            return True
        if markers == bd.IMAGING_DIALOG_MARKERS:
            self.dialog_calls += 1
            if self.dialog_calls == 1:
                return self.initial_dialog
            if self.use_prompt and self.dialog_calls == 2:
                # 确认使用后游戏先退回关卡页，5s 内不会直接弹设置窗
                return False
            return self.dialog_after_use
        if markers == bd.IMAGING_SETTLEMENT_MARKERS:
            return self.settlement
        return False

    def _ocr(self, logger, texts, hwnd, frac_region=None, min_conf=0.5, exclude=()):
        if "确认使用" in texts:
            if self.use_prompt:
                return {"text": "确认使用", "confidence": 0.80, "center": (1181, 740)}
            return None
        if "快速战斗" in texts:
            if self.ocr_quick:
                return {"text": "快速战斗", "confidence": 0.78, "center": (1185, 861)}
            return None
        return None

    def _ratio(self, logger, hwnd, ratio, label=""):
        self.ratios.append((tuple(ratio), label))
        return (int(ratio[0] * 1920), int(ratio[1] * 1080))


def run_battle(use_prompt=False, dialog_after_use=True, ocr_quick=True):
    env = BattleEnv(
        initial_dialog=False,
        use_prompt=use_prompt,
        dialog_after_use=dialog_after_use,
        ocr_quick=ocr_quick,
    )
    env.install()
    try:
        ok = bd._handle_stage_battle_task(LOG, 1, "物资保障", "定向物资保障")
    finally:
        env.restore()
    return ok, env


def test_1_no_dialog_no_prompt_fails():
    print("[1] 没设置窗也没「确认使用」：不盲点、不谎报，返回 False 并走失败取证")
    ok, env = run_battle(use_prompt=False)
    check("返回 False（未开打不能算完成）", ok is False, ok)
    check("写了失败记录（flow_failures 证据链）", any("快速战斗" in s for s, _ in env.failures), env.failures)
    check(
        "没有盲点 (0.617,0.799)",
        all(r != bd.IMAGING_DIALOG_OK_BTN for r, _ in env.ratios),
        env.ratios,
    )
    check(
        "没有把当前页当结算页点掉",
        all(r != bd.IMAGING_ANYWHERE_CLICK for r, _ in env.ratios),
        env.ratios,
    )


def test_2_use_power_prompt_then_dialog():
    print("[2] 弹「确认使用」（体力不足用限时药）：点掉后重试快速战斗，走完正常流程")
    ok, env = run_battle(use_prompt=True, dialog_after_use=True)
    check("返回 True（重新进入设置窗并确认）", ok is True, ok)
    check("点过「确认使用」", (1181, 740) in env.clicks, env.clicks)
    check("确认使用后重试了关卡页快速战斗", env.clicks.count((1185, 861)) >= 2, env.clicks)
    check("自愈成功不写失败记录", not env.failures, env.failures)


def test_3_use_power_prompt_still_no_dialog():
    print("[3] 点了「确认使用」仍然没有设置窗：回面板、返回 False 并写失败记录")
    ok, env = run_battle(use_prompt=True, dialog_after_use=False)
    check("返回 False", ok is False, ok)
    check("点过「确认使用」", (1181, 740) in env.clicks, env.clicks)
    check("写了失败记录", any("快速战斗" in s for s, _ in env.failures), env.failures)
    check(
        "没有盲点确认按钮",
        all(r != bd.IMAGING_DIALOG_OK_BTN for r, _ in env.ratios),
        env.ratios,
    )


def test_4_confirm_only_matches_confirm_use():
    print("[4] _confirm_action_power_use 只认「确认使用」，不碰泛化「确认」")
    env = BattleEnv(use_prompt=True)
    env.install()
    try:
        clicked = bd._confirm_action_power_use(LOG, 1)
    finally:
        env.restore()
    check("返回 True", clicked is True, clicked)
    check("点了 (1181,740)", (1181, 740) in env.clicks, env.clicks)
    env2 = BattleEnv(use_prompt=False)
    env2.install()
    try:
        clicked2 = bd._confirm_action_power_use(LOG, 1)
    finally:
        env2.restore()
    check("无弹窗时不点并返回 False", clicked2 is False and not env2.clicks, (clicked2, env2.clicks))


def test_5_quick_battle_ocr_first():
    print("[5] 关卡页「快速战斗」：OCR 命中就点 OCR 坐标，认不出才退回固定比例")
    env = BattleEnv(ocr_quick=True)
    env.install()
    try:
        used_ocr = bd._click_quick_battle_entry(LOG, 1)
    finally:
        env.restore()
    check("OCR 命中返回 True", used_ocr is True, used_ocr)
    check("点了 OCR 中心 (1185,861)", (1185, 861) in env.clicks, env.clicks)
    check("没走比例兜底", not env.ratios, env.ratios)

    env2 = BattleEnv(ocr_quick=False)
    env2.install()
    try:
        used_ocr2 = bd._click_quick_battle_entry(LOG, 1)
    finally:
        env2.restore()
    check("OCR 未命中返回 False", used_ocr2 is False, used_ocr2)
    check(
        "退回固定比例 (0.784,0.893)",
        any(r == bd.IMAGING_QUICK_BATTLE_BTN for r, _ in env2.ratios),
        env2.ratios,
    )


def main():
    test_1_no_dialog_no_prompt_fails()
    test_2_use_power_prompt_then_dialog()
    test_3_use_power_prompt_still_no_dialog()
    test_4_confirm_only_matches_confirm_use()
    test_5_quick_battle_ocr_first()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
