"""白荆茶憩假成功修复回归（2026-10-25 19:06 事故）。

事故：`_handle_tea_break_task` 末尾 `return _return_to_task_panel(...)`——只要
能回到任务面板就返回 True；`process_task_list` 随即把 tea_state["done"]=True。
19:06 那轮既没看到茶憩场景也没看到结束摘要，本地记成功，游戏内实际仍是 0/1
（19:50 日常列表截图实锤）。最终重扫的 `old.done or st.done` 也无法纠正。

修复：
  1. 茶憩必须有“茶憩场景”或“结束摘要”证据才返回 True；
  2. `process_task_list` 不再乐观写 tea done，最终以任务列表重扫为准；
  3. 点确定后直接进休息室 3D 场景时（19:50 截图），点右下「午后茶憩」立牌卡
     ——2026-10-26 12:50 修正：原先点左下角色头像进入的是「关注」特写，
     茶憩场景永远等不到（见 test_baijing_rest_room_20261026.py）。

跑法：.venv\\Scripts\\python.exe artifacts\\test_baijing_tea_false_positive_20261025.py
"""

import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT.parent))

import game_auto.runtime.baijing_daily as bd  # noqa: E402

LOG = logging.getLogger("tea-false-positive-test")
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
    def __init__(self, scene=True, summary=True):
        self.scene = scene
        self.summary = summary
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
            "_window_region": bd._window_region,
            "time": bd.time,
        }
        bd._find_row_button = lambda *a, **k: (1700, 346)
        bd._pyautogui_click_guarded = lambda x, y, *a, **k: self.clicks.append((x, y))
        bd._wait_page_markers = self._wait
        bd._ocr_best_match = self._ocr
        bd._click_ratio = self._ratio
        bd._return_to_task_panel = lambda *a, **k: True
        bd._record_step_failure = lambda logger, hwnd, step, detail: self.failures.append((step, detail))
        bd._ensure_window = lambda logger, hwnd: hwnd
        bd._window_region = lambda hwnd: (0, 0, 1920, 1080)
        bd.time = FakeTime()

    def restore(self):
        for k, v in self._saved.items():
            setattr(bd, k, v)

    def _wait(self, logger, hwnd, markers, timeout=6.0, stop_check=None, frac_region=None):
        if markers == bd.TEA_SELECT_MARKERS:
            return False          # 模拟直接进休息室，不是角色选择页
        if markers == bd.TEA_SCENE_MARKERS:
            return self.scene
        if markers == bd.TEA_SUMMARY_MARKERS:
            return self.summary
        return False              # 确认框等

    def _ocr(self, logger, texts, hwnd, frac_region=None, min_conf=0.5, exclude=()):
        if any(t in texts for t in bd.REST_ROOM_MARKERS):
            return {"text": "拜访他人", "confidence": 0.80, "center": (1338, 1004)}
        return None

    def _ratio(self, logger, hwnd, ratio, label=""):
        self.ratios.append((tuple(ratio), label))
        return (int(ratio[0] * 1920), int(ratio[1] * 1080))


def run_tea(scene=True, summary=True):
    env = TeaEnv(scene=scene, summary=summary)
    env.install()
    try:
        ok = bd._handle_tea_break_task(LOG, 1)
    finally:
        env.restore()
    return ok, env


def test_1_lounge_fallback():
    print("[1] 直接进休息室：点右下「午后茶憩」立牌卡，不点头像也不点旧角色卡位置")
    ok, env = run_tea(scene=True, summary=True)
    check("返回 True（有场景证据）", ok is True, ok)
    check("点了休息室茶憩入口", (bd.REST_ROOM_TEA_ENTRY, "休息室「午后茶憩」立牌卡") in env.ratios, env.ratios)
    check("未点旧角色选择页位置", all(r != bd.TEA_FIRST_CHARACTER for r, _ in env.ratios), env.ratios)


def test_2_no_evidence_fails():
    print("[2] 既无场景也无摘要：必须返回 False 并落失败记录")
    ok, env = run_tea(scene=False, summary=False)
    check("返回 False", ok is False, ok)
    check("落了一条午后茶憩失败记录", any(s == "午后茶憩" for s, _ in env.failures), env.failures)


def test_3_rescan_authoritative():
    print("[3] process_task_list：茶憩 handler 返回 True 也不能乐观写 done，重扫为准")
    import game_auto.runtime.baijing_daily as m

    def run(rescan_done):
        saved = {
            "scan_daily_task_states": m.scan_daily_task_states,
            "_ensure_task_list_open": m._ensure_task_list_open,
            "_handle_tea_break_task": m._handle_tea_break_task,
            "_claim_rewards_midway": m._claim_rewards_midway,
            "_scroll_row_list_to_top": m._scroll_row_list_to_top,
            "_dismiss_overlay": m._dismiss_overlay,
            "_ensure_window": m._ensure_window,
        }
        calls = {"scan": 0}

        def tea_state():
            return {"done": False, "progress": "0/1", "claimable": False, "activity": 15}

        def scan(*a, **k):
            calls["scan"] += 1
            st = tea_state()
            if calls["scan"] > 1:
                st["done"] = rescan_done
                st["progress"] = "1/1" if rescan_done else "0/1"
            return {"tea_break": st}

        try:
            m.scan_daily_task_states = scan
            m._ensure_task_list_open = lambda *a, **k: True
            m._handle_tea_break_task = lambda *a, **k: True
            m._claim_rewards_midway = lambda *a, **k: 0
            m._scroll_row_list_to_top = lambda *a, **k: True
            m._dismiss_overlay = lambda *a, **k: False
            m._ensure_window = lambda logger, hwnd: hwnd
            summary = m.process_task_list(LOG, 1, max_rounds=0)
        finally:
            for k, v in saved.items():
                setattr(m, k, v)
        return summary["states"]["tea_break"]["done"], calls["scan"]

    done, scans = run(rescan_done=False)
    check("重扫 0/1 → done=False", done is False, done)
    check("确实跑了最终重扫", scans >= 2, scans)
    done, scans = run(rescan_done=True)
    check("重扫 1/1 → done=True", done is True, done)


def test_4_constants():
    print("[4] 休息室特征/坐标来自 12:53 用户截图")
    check("特征含拜访他人", "拜访他人" in bd.REST_ROOM_MARKERS, bd.REST_ROOM_MARKERS)
    check("特征含布置模式", "布置模式" in bd.REST_ROOM_MARKERS, bd.REST_ROOM_MARKERS)
    check("特征不含「午后茶憩」（任务行也有这四个字，会误判）",
          "午后茶憩" not in bd.REST_ROOM_MARKERS, bd.REST_ROOM_MARKERS)
    x, y = bd.REST_ROOM_TEA_ENTRY
    check("茶憩立牌卡在右下", 0.82 <= x <= 0.95 and 0.76 <= y <= 0.90, (x, y))
    bx, by = bd.REST_ROOM_BACK
    check("返回在左上角", bx <= 0.16 and by <= 0.10, (bx, by))


def main():
    test_1_lounge_fallback()
    test_2_no_evidence_fails()
    test_3_rescan_authoritative()
    test_4_constants()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
