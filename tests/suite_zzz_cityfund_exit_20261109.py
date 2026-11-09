# -*- coding: utf-8 -*-
"""ZZZ 丽都城募活动页退出 回归（2026-11-09 22:28 实跑取证）。

背景：ZZZ_LOGISTICS_MONTHLY 旧链路（ESC 功能菜单 → 点「丽都城募」活动横幅 →
期望活动页里有「后勤商店」）11-09 22:28 实跑停在 NEW ERIDU CITY FUND 活动页，
步骤「点击后勤商店」全窗 6 轮未匹配。取证帧 OCR：顶部页签组
选择奖励/兑换商店/等级回馈/成长任务/城募赠礼，左上红色返回箭头图标。
用户拍板：活动页点返回图标即可返回（ESC 也行）。场景登记表新增规则 2.5
认领该页并退出，词表与功能菜单（MENU_MARKERS）无交集防串扰。

覆盖：
  [1] 活动页认领 → 点左上返回图标 → 页签词消失复核 → 下一轮认出大世界
  [2] 返回图标点不掉 → ESC 兜底退出
  [3] 词表隔离：功能菜单帧/大世界帧不触发活动页规则
  [4] 接线静态：规则在 unknown 计数前、CITY_FUND_MARKERS 有取证出处

跑法：.venv\\Scripts\\python.exe -m pytest tests/suite_zzz_cityfund_exit_20261109.py -q
"""

import io
import sys
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from game_auto.runtime import flow_diagnostics as fd  # noqa: E402
from game_auto.runtime import foreground_flow_runner as ffr  # noqa: E402
from game_auto.runtime import zzz_scene as zs  # noqa: E402

FAILURES = []
LOG = __import__("logging").getLogger("zzz_cityfund_test")
LOG.addHandler(__import__("logging").NullHandler())


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}", flush=True)
    else:
        print(f"  FAIL {name} — {detail}", flush=True)
        FAILURES.append(name)


def _rows(*pairs):
    return [
        {"text": t, "confidence": c, "center": (x, y), "box": [x - 40, y - 15, x + 40, y - 15 + 30]}
        for t, c, x, y in pairs
    ]


# 11-09 22:28 取证帧 OCR 逐行转录（conf 照抄 _collect_failure_evidence 输出）
CITY_FUND_ROWS = _rows(
    ("NEW!", 1.00, 1651, 20),
    ("选择奖励", 1.00, 305, 50), ("兑换商店", 1.00, 528, 50),
    ("等级回馈", 1.00, 1283, 50), ("成长任务", 1.00, 1505, 51), ("城募赠礼", 1.00, 1727, 51),
    ("N.C.C.F.", 0.91, 181, 146), ("NEW ERIDU • CITY FUND", 0.97, 182, 186),
    ("Lv.37 38 39 40 41 4145", 0.97, 1130, 170), ("本期剩余20天", 1.00, 172, 1028),
)
ZZZ_WORLD_ROWS = _rows(("星期四", 0.95, 1800, 80))
ZZZ_MENU_ROWS = _rows(
    ("更多", 1.00, 100, 900), ("助战编队", 1.00, 200, 900), ("邮件", 1.00, 300, 900),
    ("丽都城募", 1.00, 400, 900), ("调频", 1.00, 500, 900),
)


class CityFundMachine:
    """状态机：city_fund → （点返回图标或 ESC）→ world。

    back_click_works=True 时返回图标点击生效；False 时图标点不掉，必须靠 ESC 兜底。
    """

    def __init__(self, back_click_works=True):
        self.state = "city_fund"
        self.back_click_works = back_click_works
        self.clicks = []
        self.escapes = []
        self.ocr_calls = 0

    def rows(self, logger, hwnd=None, **kwargs):
        self.ocr_calls += 1
        return {"city_fund": CITY_FUND_ROWS, "world": ZZZ_WORLD_ROWS}[self.state]

    def template(self, hwnd, template_path, scales, confidences, use_color=False):
        return None  # 本场景不走模板通道

    def click(self, x, y):
        self.clicks.append((int(x), int(y)))
        if self.state == "city_fund" and self.back_click_works:
            self.state = "world"

    def escape(self, key, **kwargs):
        self.escapes.append(key)
        if self.state == "city_fund":
            self.state = "world"


def _harness(machine):
    failures = []
    patches = [
        mock.patch.object(zs, "ocr_window_content", side_effect=machine.rows),
        mock.patch.object(zs, "_process_age_sec", return_value=99999.0),
        mock.patch.object(ffr, "_locate_template_in_window", side_effect=machine.template),
        mock.patch.object(ffr, "_pyautogui_click_guarded", side_effect=machine.click),
        mock.patch.object(ffr, "press_key_scancode", side_effect=machine.escape),
        mock.patch.object(fd, "record_failure",
                          side_effect=lambda logger, fk, step, err, **k: failures.append((fk, step, err))),
        mock.patch.object(zs.time, "sleep", lambda *_: None),
        mock.patch("game_auto.runtime.ocr_click_tester._ensure_window_region",
                   lambda hwnd: (0, 0, 1920, 1080)),
    ]
    for p in patches:
        p.start()
    try:
        ok, msg = zs.ensure_main_world(LOG, hwnd=12345, stop_check=lambda: False,
                                       scales=[1.0], confidences=[0.88],
                                       flow_key="ZZZ_LOGISTICS_MONTHLY",
                                       context="回放测试")
    finally:
        mock.patch.stopall()
    return ok, msg, failures


def test_city_fund_back_icon():
    print("[1] 活动页 → 点左上返回图标 → 大世界")
    m = CityFundMachine(back_click_works=True)
    ok, msg, failures = _harness(m)
    check("回大世界", ok is True and msg == "大世界", f"ok={ok} msg={msg}")
    # 窗口 1920x1080，返回图标点击区 = 宽12% 高12% 的中心：(115, 64)
    check("点击坐标在左上返回图标区", m.clicks and m.clicks[0] == (115, 64), m.clicks)
    check("走返回图标、未误按 ESC", m.escapes == [], m.escapes)
    check("零失败取证", not failures, failures)


def test_city_fund_esc_fallback():
    print("[2] 返回图标点不掉 → ESC 兜底")
    m = CityFundMachine(back_click_works=False)
    ok, msg, failures = _harness(m)
    check("ESC 兜底回大世界", ok is True and msg == "大世界", f"ok={ok} msg={msg}")
    check("点过返回图标一次", len(m.clicks) == 1, m.clicks)
    check("按过 ESC", len(m.escapes) == 1, m.escapes)
    check("零失败取证", not failures, failures)


def test_marker_isolation():
    print("[3] 词表隔离")
    cf_hits = zs._count_markers([r["text"] for r in ZZZ_MENU_ROWS], zs.CITY_FUND_MARKERS)
    menu_hits = zs._count_markers([r["text"] for r in ZZZ_MENU_ROWS], zs.MENU_MARKERS)
    check("功能菜单帧不触发活动页规则", cf_hits == 0 and menu_hits >= 2,
          f"cityfund={cf_hits} menu={menu_hits}")
    world_hits = zs._count_markers([r["text"] for r in ZZZ_WORLD_ROWS], zs.CITY_FUND_MARKERS)
    check("大世界帧不触发活动页规则", world_hits == 0, world_hits)


def test_wiring_static():
    print("[4] 接线静态")
    zs_src = (PROJECT_ROOT / "src" / "game_auto" / "runtime" / "zzz_scene.py").read_text(encoding="utf-8")
    check("活动页规则在 unknown 计数前",
          zs_src.index("_count_markers(texts, CITY_FUND_MARKERS) >= 2")
          < zs_src.index("unknown_streak += 1"))
    check("活动页词表有取证出处（选择奖励/兑换商店/城募赠礼）",
          all(w in zs.CITY_FUND_MARKERS for w in ("选择奖励", "兑换商店", "城募赠礼")))
    check("返回图标点击区按窗口比例（非绝对坐标）",
          "_CITY_FUND_BACK_RATIO" in zs_src and "1920 * 0.12" not in zs_src)
    check("退出后 OCR 复核页签消失才认退出",
          "_count_markers(texts, CITY_FUND_MARKERS) < 2" in zs_src)


def main():
    test_city_fund_back_icon()
    test_city_fund_esc_fallback()
    test_marker_isolation()
    test_wiring_static()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
