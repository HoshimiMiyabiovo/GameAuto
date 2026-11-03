"""茶憩「点角色卡后进详情页」补脚回归（2026-11-03 新增）。

事故：11-03 11:34:24 点了首个角色卡，之后 23 秒画面稳定在 20 个文字区域
（不是茶憩场景），老逻辑继续点底部对话框 (0.50,0.92)，反而把页面退回休息室，
两轮「喝茶」轮询全空，任务判定未完成（活跃度少 15 分）。

修复：点角色卡后先 8 秒快检场景；没进就用 OCR 找「邀请/开始茶憩/一同品茶/邀约」
这类明确入口点一次（不碰泛化的「确认」，同页有退出确认）；两样都没有就返回
False，上层再给 10 秒兜底，仍不进则落 flow_failures 并跳过盲点推进。

跑法：.venv\\Scripts\\python.exe tests\\suite_baijing_tea_invite_20261103.py
"""

import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import game_auto.runtime.baijing_daily as bd  # noqa: E402

LOG = logging.getLogger("baijing-tea-invite-test")
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


class TeaEnv:
    def __init__(self, scene=False, invite=None):
        self.scene = scene
        self.invite = invite
        self.clicks = []
        self._saved = {}

    def install(self):
        self._saved = {
            "_wait_page_markers": bd._wait_page_markers,
            "_ocr_best_match": bd._ocr_best_match,
            "_pyautogui_click_guarded": bd._pyautogui_click_guarded,
            "_stopped": bd._stopped,
            "time": bd.time,
        }
        bd._wait_page_markers = lambda logger, hwnd, markers, timeout=6.0, stop_check=None, frac_region=None: (
            self.scene if markers == bd.TEA_SCENE_MARKERS else False
        )
        bd._ocr_best_match = self._ocr
        bd._pyautogui_click_guarded = lambda x, y, *a, **k: self.clicks.append((x, y))
        bd._stopped = lambda stop_check: False
        bd.time = FakeTime()

    def restore(self):
        for k, v in self._saved.items():
            setattr(bd, k, v)

    def _ocr(self, logger, texts, hwnd, frac_region=None, min_conf=0.5, exclude=()):
        if any(t in texts for t in bd.TEA_INVITE_TEXTS):
            return self.invite
        return None


def run(scene, invite):
    env = TeaEnv(scene=scene, invite=invite)
    env.install()
    try:
        ok = bd._nudge_into_tea_scene(LOG, 1)
    finally:
        env.restore()
    return ok, env


def test_1_scene_immediately():
    print("[1] 点角色卡后直接进茶憩场景：不额外点击")
    ok, env = run(scene=True, invite=None)
    check("返回 True", ok is True, ok)
    check("没点任何东西", not env.clicks, env.clicks)


def test_2_invite_found():
    print("[2] 停在角色详情页：OCR 找到「邀请」→ 点它")
    hit = {"text": "邀请", "confidence": 0.72, "center": (1560, 940)}
    ok, env = run(scene=False, invite=hit)
    check("返回 True", ok is True, ok)
    check("点了邀请按钮", (1560, 940) in env.clicks, env.clicks)


def test_3_nothing_found():
    print("[3] 没场景也没邀请入口：不盲点，返回 False 交给上层兜底")
    ok, env = run(scene=False, invite=None)
    check("返回 False", ok is False, ok)
    check("没点任何东西", not env.clicks, env.clicks)


def test_4_candidates_exclude_confirm():
    print("[4] 候选词只含邀请语义，不含泛化「确认」")
    check("含邀请", "邀请" in bd.TEA_INVITE_TEXTS, bd.TEA_INVITE_TEXTS)
    check("含开始茶憩", "开始茶憩" in bd.TEA_INVITE_TEXTS, bd.TEA_INVITE_TEXTS)
    check("不含「确认」", "确认" not in bd.TEA_INVITE_TEXTS, bd.TEA_INVITE_TEXTS)


def main():
    test_1_scene_immediately()
    test_2_invite_found()
    test_3_nothing_found()
    test_4_candidates_exclude_confirm()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
