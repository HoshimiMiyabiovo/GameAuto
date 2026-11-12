"""白荆回廊记忆烙痕强化·「等级提升」页签回归（2026-11-11）。

10:15 取证帧 #271：点「强化烙痕」后弹窗**默认停在「特质提升」页**，画面只有
「特质变动 / 特质升级 / 消耗材料 / 当前选择」，没有「指定等级 / 重置 / +」。
旧流程 MEMORY_DIALOG_MARKERS 看到页签标题「等级提升」就以为强化窗就绪，
接着按「重置」偏移找「+」→ 报「未找到 + 按钮（重置=None）」。
用户口径：「其实只要切到等级提升页面就好了」。

覆盖：
[1] helper 单测：已在目标页不点击 / 特质页切页签 / 比例兜底 / 切后无证据不放行
[2] 主流程状态机：特质页起始 → 自动切页签 → 重置 / +1 / 确认 → 成功
[3] 静态：区域语义 (x0,y0,x1,y1)、接线顺序、失败文案

跑法：.venv\\Scripts\\python.exe tests\\suite_baijing_memory_level_tab_20261111.py
"""

import io
import logging
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from PIL import Image  # noqa: E402

from game_auto.runtime import baijing_daily as bd  # noqa: E402

LOG = logging.getLogger("baijing_memory_tab_test")
LOG.addHandler(logging.NullHandler())
FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}", flush=True)
    else:
        print(f"  FAIL {name} — {detail}", flush=True)
        FAILURES.append(name)


def _rows(*pairs):
    return [
        {"text": t, "confidence": 0.95, "center": (x, y), "box": [x - 40, y - 15, x + 40, y + 15]}
        for t, x, y in pairs
    ]


# 状态行坐标 @1919x1079。enhance_trait = 11-11 10:15 真实取证帧转录；
# enhance_level = 10-30 用户定标帧（docs/refs/baijing_memory_enhance_20261030.png）。
STATE_ROWS = {
    "list": _rows(("只看红点", 400, 90), ("排序与筛选", 700, 90), ("按等级", 900, 90)),
    "detail": _rows(("特质提升", 700, 210), ("烙痕等级", 700, 260),
                    ("返回", 60, 50), ("强化烙痕", 1500, 950)),
    "enhance_trait": _rows(
        ("等级提升", 447, 206), ("特质提升", 758, 205),
        ("特质变动", 400, 297), ("消耗材料", 1286, 296),
        ("特质升级：", 403, 450), ("契文·燎夜余尘", 1292, 642),
        ("当前选择", 1308, 733), ("取 消", 735, 863), ("确 认", 1173, 863),
    ),
    "enhance_level": _rows(
        ("等级提升", 447, 206), ("特质提升", 758, 205),
        ("指定等级", 900, 620), ("重置", 1189, 669), ("最大", 1529, 670),
        ("强化费用", 700, 700), ("确 认", 1173, 860), ("取 消", 741, 860),
    ),
    "anim": _rows(("点击屏幕继续", 960, 900)),
}


class Machine:
    """点击推进状态机：每次点击取下一个状态。"""

    def __init__(self, state, transitions):
        self.state = state
        self.trans = list(transitions)
        self.clicks = []

    def click(self, x, y):
        self.clicks.append((int(x), int(y)))
        if self.trans:
            self.state = self.trans.pop(0)

    def grab(self, region=None, **kwargs):
        w = int(region[2]) if region else 13
        h = int(region[3]) if region else 13
        return Image.new("RGB", (max(1, w), max(1, h)), (200, 200, 200))


def _harness(transitions, start="list"):
    machine = Machine(start, transitions)
    failures = []
    patches = [
        mock.patch.object(bd, "ocr_window_content",
                          side_effect=lambda logger, hwnd, **k: list(STATE_ROWS[machine.state])),
        mock.patch.object(bd, "grab_screen", side_effect=machine.grab),
        mock.patch.object(bd, "_pyautogui_click_guarded", side_effect=machine.click),
        mock.patch.object(bd, "_ensure_window", side_effect=lambda logger, hwnd: hwnd),
        mock.patch.object(bd, "_window_region", side_effect=lambda hwnd: (0, 0, 1919, 1079)),
        mock.patch.object(bd, "_template_center", side_effect=lambda *a, **k: None),
        mock.patch.object(bd, "_find_row_button", side_effect=lambda *a, **k: (100, 300)),
        mock.patch.object(bd, "_record_step_failure",
                          side_effect=lambda logger, hwnd, step, msg: failures.append((step, msg))),
        mock.patch.object(bd, "_return_to_task_panel", side_effect=lambda *a, **k: True),
        mock.patch.object(bd.time, "sleep", lambda *_: None),
    ]
    for p in patches:
        p.start()
    try:
        result = bd._handle_memory_exp_task(LOG, 1)
    finally:
        for p in reversed(patches):
            p.stop()
    return result, machine, failures


def _helper(ocr_side_effect):
    """单独驱动 _ensure_memory_level_tab，返回 (结果, 点击列表)。"""
    clicks = []
    patches = [
        mock.patch.object(bd, "_ocr_best_match", side_effect=ocr_side_effect),
        mock.patch.object(bd, "_pyautogui_click_guarded",
                          side_effect=lambda x, y: clicks.append((int(x), int(y)))),
        mock.patch.object(bd, "_ratio_point", side_effect=lambda logger, hwnd, ratio: (447, 206)),
        mock.patch.object(bd, "_stopped", side_effect=lambda *a, **k: False),
        mock.patch.object(bd.time, "sleep", lambda *_: None),
    ]
    for p in patches:
        p.start()
    try:
        ok = bd._ensure_memory_level_tab(LOG, 1)
    finally:
        for p in reversed(patches):
            p.stop()
    return ok, clicks


def _hit(text, xy):
    return {"text": text, "center": xy, "confidence": 0.95}


def test_helper():
    print("[1] 页签切换 helper")

    # 已在等级提升页：直接放行，零点击
    calls = []

    def already(logger, markers, hwnd, **kw):
        calls.append(tuple(markers))
        return _hit("指定等级", (900, 620))

    ok, clicks = _helper(already)
    check("已在等级提升页 → True", ok is True)
    check("已在目标页不点击", clicks == [], clicks)
    check("已在目标页只查一次", len(calls) == 1, calls)

    # 特质提升页：找页签文字 → 点它 → 复验通过
    seq = iter([
        None,                                   # 首次查等级页标记：无
        _hit("等级提升", (447, 206)),           # 页签 OCR 命中
        _hit("指定等级", (900, 620)),           # 切后复验
    ])
    ok, clicks = _helper(lambda logger, markers, hwnd, **kw: next(seq, None))
    check("特质页切页签 → True", ok is True)
    check("点的是「等级提升」页签坐标", clicks == [(447, 206)], clicks)

    # 页签 OCR 也没认出来 → 比例兜底
    seq2 = iter([None, None, _hit("重置", (1189, 669))])
    ok, clicks = _helper(lambda logger, markers, hwnd, **kw: next(seq2, None))
    check("OCR 全丢 → 比例兜底仍 True", ok is True)
    check("兜底点击落在页签", clicks == [(447, 206)], clicks)

    # 切完仍看不到等级页独占内容 → 不放行（不许盲点 +）
    ok, clicks = _helper(lambda logger, markers, hwnd, **kw: None)
    check("切后无证据 → False", ok is False)
    check("切后无证据只点一次（不反复点）", len(clicks) == 1, clicks)


def test_main_flow():
    print("[2] 主流程：特质页起始也能完成强化")
    trans = ["list", "detail", "enhance_trait", "enhance_level", "enhance_level",
             "enhance_level", "anim", "detail"]
    result, machine, failures = _harness(trans)
    check("任务成功", result is True, result)
    check("切了「等级提升」页签", (447, 206) in machine.clicks, machine.clicks)
    check("重置被点击", (1189, 669) in machine.clicks, machine.clicks)
    check("「+」按重置锚点推算后点击", (1424, 669) in machine.clicks, machine.clicks)
    check("确认被点击", (1173, 860) in machine.clicks, machine.clicks)
    check("没有失败取证", failures == [], failures)
    check("点击数 8", len(machine.clicks) == 8, machine.clicks)
    # 旧行为回归护栏：切页签必须发生在重置之前
    if (447, 206) in machine.clicks and (1189, 669) in machine.clicks:
        check("先切页签再点重置",
              machine.clicks.index((447, 206)) < machine.clicks.index((1189, 669)),
              machine.clicks)


def test_static():
    print("[3] 静态守卫")
    x0, y0, x1, y1 = bd.MEMORY_LEVEL_TAB_REGION
    check("区域是 (x0,y0,x1,y1) 且 x1>x0/y1>y0",
          0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1, bd.MEMORY_LEVEL_TAB_REGION)
    tab_x, tab_y = 447 / 1919, 206 / 1079
    check("取证帧页签坐标落在区域内",
          x0 <= tab_x <= x1 and y0 <= tab_y <= y1, (tab_x, tab_y))
    src = (Path(__file__).resolve().parent.parent
           / "src/game_auto/runtime/baijing_daily.py").read_text(encoding="utf-8")
    check("主流程已调用 helper", "_ensure_memory_level_tab(logger, hwnd" in src)
    check("helper 在重置之前",
          src.index("if not _ensure_memory_level_tab(logger, hwnd")
          < src.index('reset = _ocr_best_match(\n            logger, ("重置",)'))
    check("失败文案点明页签", "未切到「等级提升」页签" in src)
    check("等级页标记含 指定等级/重置/最大",
          all(m in bd.MEMORY_LEVEL_TAB_MARKERS for m in ("指定等级", "重置", "最大")))
    check("特质页状态行不含等级页标记（用真实取证帧）",
          not any(any(m in r["text"] for m in bd.MEMORY_LEVEL_TAB_MARKERS)
                  for r in STATE_ROWS["enhance_trait"]))


def main():
    test_helper()
    test_main_flow()
    test_static()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
