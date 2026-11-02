# -*- coding: utf-8 -*-
"""崩铁「自塑尘脂」每月合成 回归（2026-11-01）。

锁定这条流程的判据与安全边界：
1. 「本月还可合成 0」时**一次都不点**（本月已合成完不能白点、不能点坏界面）；
2. N > 0 时按 N 递减计数，减到 0 结束；按钮「材料不足」「已达上限」都算正常结束；
3. 点了但 N 连续两次不变 → 判定卡住并**失败**（不无限点）；
4. 底部「合成」按钮的 OCR 必须限定在按钮区域，顶部「材料合成」标题不能当按钮；
5. 「本月还可合成」的两种 OCR 排版（冒号连行 / 数字被切开）都能解析；
6. 背包已打开时不再按 B（按了会把背包关掉）；
7. `_dispatch_step_action` 已接 `hsr_resin_craft`，失败要带着原因冒泡；
8. 配置里 HSR_MONTHLY 有这一步，模板文件真实存在且能自匹配。

跑法：.venv\\Scripts\\python.exe tests\\suite_hsr_resin_craft_20261101.py
"""

import json
import logging
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT.parent))

import game_auto.runtime.foreground_flow_runner as ffr  # noqa: E402
import game_auto.runtime.hsr_resin_craft as hrc  # noqa: E402

FAILURES = []
LOG = logging.getLogger("suite")


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name}  {detail}")
        FAILURES.append(name)


WIN_W, WIN_H = 1920, 1080


class FakeScreen:
    """假游戏画面：OCR 文本 + 按钮状态 + 点击副作用。"""

    def __init__(self):
        self.texts = []
        self.remaining = None     # None = 页面上没有「本月还可合成」这一行
        self.button = None        # ready / limit / no_material / None
        self.clicks = []
        self.presses = []
        self.on_click = None
        self.on_press = None

    # --- 底层替身 ---
    def ocr(self, logger, hwnd):
        items = list(self.texts)
        if self.remaining is not None:
            items.append(
                {"text": f"本月还可合成：{self.remaining}", "confidence": 0.9, "center": (1200, 547)}
            )
        if self.button == "ready":
            items.append({"text": "合成", "confidence": 0.92, "center": (1102, 870)})
        elif self.button == "limit":
            items.append({"text": "已达上限", "confidence": 0.93, "center": (1102, 870)})
        elif self.button == "no_material":
            items.append({"text": "材料不足", "confidence": 0.91, "center": (1102, 870)})
        return items

    def click(self, hwnd, x, y, clicks=1, interval=0.2, background=False):
        self.clicks.append((int(x), int(y)))
        if self.on_click:
            self.on_click()

    def press(self, hwnd, key):
        self.presses.append(key)
        if self.on_press:
            self.on_press()

    def locate_none(self, hwnd, template, scales, confidences, timeout_sec, stop_check=None, **kw):
        return None


def base_step(**over):
    step = {
        "log_label": "自塑尘脂",
        "bag_open_key": "b",
        "tab_text": "贵重物",
        "tab_point_frac": [0.705, 0.057],
        "item_text": "自塑尘脂",
        "item_point_frac": [0.401, 0.192],
        "source_text": "万能合成机",
        "source_point_frac": [0.825, 0.830],
        "craft_button_region_frac": [0.25, 0.70, 0.76, 0.90],
        "craft_button_point_frac": [0.574, 0.806],
        "max_rounds": 10,
    }
    step.update(over)
    return step


def ready_screen(screen):
    """让前四步（背包/分类/物料/合成界面）全部命中「已就绪」。"""
    screen.texts = [
        {"text": "背包", "confidence": 0.9, "center": (90, 40)},
        {"text": "贵重物", "confidence": 0.9, "center": (1353, 62)},
        {"text": "稀有度", "confidence": 0.9, "center": (200, 1010)},
        {"text": "自塑尘脂", "confidence": 0.9, "center": (1200, 300)},
        {"text": "获得途径", "confidence": 0.9, "center": (1200, 830)},
        {"text": "所需材料", "confidence": 0.8, "center": (1200, 700)},
    ]


def patch_env(stack, screen, locate=None):
    stack.enter_context(patch.object(hrc, "_texts", screen.ocr))
    stack.enter_context(patch.object(hrc, "_locate", locate or screen.locate_none))
    stack.enter_context(patch.object(hrc, "_locate_ocr", lambda *a, **k: None))
    stack.enter_context(patch.object(hrc, "_press_key", screen.press))
    stack.enter_context(patch.object(hrc, "_sleep", lambda *a, **k: None))
    stack.enter_context(patch.object(ffr, "_click_window_local", screen.click))
    stack.enter_context(patch.object(ffr, "_window_rect", lambda hwnd: (0, 0, WIN_W, WIN_H)))


def case_parse_remaining():
    print("[1] 「本月还可合成」两种 OCR 排版")
    screen = FakeScreen()
    with ExitStack() as stack:
        patch_env(stack, screen)
        screen.texts = [{"text": "本月还可合成：3", "confidence": 0.9, "center": (1200, 547)}]
        n, raw = hrc._read_remaining(LOG, 1)
        check("冒号连行 -> 3", n == 3, (n, raw))

        screen.texts = [
            {"text": "本月还可合成", "confidence": 0.9, "center": (1150, 547)},
            {"text": "5", "confidence": 0.85, "center": (1230, 550)},
        ]
        n, raw = hrc._read_remaining(LOG, 1)
        check("数字被切到右侧 -> 5", n == 5, (n, raw))

        screen.texts = []
        n, raw = hrc._read_remaining(LOG, 1)
        check("没有该行 -> None", n is None, (n, raw))


def case_button_region():
    print("[2] 底部按钮区域限定：顶部「材料合成」不算按钮")
    screen = FakeScreen()
    with ExitStack() as stack:
        patch_env(stack, screen)
        screen.texts = [
            {"text": "材料合成", "confidence": 0.95, "center": (300, 45)},
            {"text": "合成", "confidence": 0.95, "center": (640, 45)},
        ]
        state, center = hrc._button_state(LOG, 1, base_step(), [1.0], [0.8], None)
        check("只见顶部标题 -> unknown", state == "unknown", (state, center))

        screen.texts.append({"text": "合成", "confidence": 0.93, "center": (1102, 870)})
        state, center = hrc._button_state(LOG, 1, base_step(), [1.0], [0.8], None)
        check("底部按钮 -> ready", state == "ready", (state, center))
        check("按钮坐标换算成窗口局部", center == (1102, 870), center)


def case_limit_zero():
    print("[3] 本月还可合成 0 -> 一次都不点")
    screen = FakeScreen()
    ready_screen(screen)
    screen.remaining = 0
    screen.button = "limit"
    with ExitStack() as stack:
        patch_env(stack, screen)
        ok, msg = hrc.craft_self_modeling_resin(LOG, 1, base_step(), scales=[1.0], confidences=[0.8])
    check("成功返回", ok is True, msg)
    check("摘要写明已合成完", "已合成完" in msg, msg)
    check("零点击", len(screen.clicks) == 0, screen.clicks)
    check("不按 B（背包已开）", "b" not in screen.presses, screen.presses)


def case_craft_until_zero():
    print("[4] N 从 2 递减到 0，点 2 次")
    screen = FakeScreen()
    ready_screen(screen)
    screen.remaining = 2
    screen.button = "ready"

    def on_click():
        screen.remaining -= 1

    screen.on_click = on_click
    with ExitStack() as stack:
        patch_env(stack, screen)
        ok, msg = hrc.craft_self_modeling_resin(LOG, 1, base_step(), scales=[1.0], confidences=[0.8])
    check("成功返回", ok is True, msg)
    check("合成 2 次", "本次合成 2 次" in msg, msg)
    check("剩余归零", screen.remaining == 0, screen.remaining)
    check("点了 2 次按钮", len(screen.clicks) == 2, screen.clicks)


def case_no_material():
    print("[5] 材料不足 -> 正常结束、不点")
    screen = FakeScreen()
    ready_screen(screen)
    screen.remaining = None
    screen.button = "no_material"
    with ExitStack() as stack:
        patch_env(stack, screen)
        ok, msg = hrc.craft_self_modeling_resin(LOG, 1, base_step(), scales=[1.0], confidences=[0.8])
    check("成功返回", ok is True, msg)
    check("摘要写明材料不足", "遗器残骸不足" in msg, msg)
    check("零点击", len(screen.clicks) == 0, screen.clicks)


def case_limit_template():
    print("[6] OCR 认不出时靠「已达上限」模板收尾")
    screen = FakeScreen()
    ready_screen(screen)
    screen.remaining = None
    screen.button = None

    def locate_limit(hwnd, template, scales, confidences, timeout_sec, stop_check=None, **kw):
        if "已达上限" in str(template):
            return (1102, 870)
        return None

    with ExitStack() as stack:
        patch_env(stack, screen, locate=locate_limit)
        ok, msg = hrc.craft_self_modeling_resin(
            LOG,
            1,
            base_step(limit_template="模板\\崩铁-自塑尘脂-已达上限.png"),
            scales=[1.0],
            confidences=[0.8],
        )
    check("成功返回", ok is True, msg)
    check("摘要写已达上限", "已达本月上限" in msg, msg)
    check("零点击", len(screen.clicks) == 0, screen.clicks)


def case_stuck():
    print("[7] 点了计数不动 -> 连续两次判定失败")
    screen = FakeScreen()
    ready_screen(screen)
    screen.remaining = 3
    screen.button = "ready"
    with ExitStack() as stack:
        patch_env(stack, screen)
        ok, msg = hrc.craft_self_modeling_resin(LOG, 1, base_step(), scales=[1.0], confidences=[0.8])
    check("判失败", ok is False, msg)
    check("说明停在 3", "停在 3" in msg, msg)
    check("只点了两次就停", len(screen.clicks) == 2, screen.clicks)


def case_stop():
    print("[8] 用户停止")
    screen = FakeScreen()
    ready_screen(screen)
    screen.remaining = 5
    screen.button = "ready"
    with ExitStack() as stack:
        patch_env(stack, screen)
        ok, msg = hrc.craft_self_modeling_resin(
            LOG, 1, base_step(), scales=[1.0], confidences=[0.8], stop_check=lambda: True
        )
    check("判失败", ok is False, msg)
    check("原因含用户停止", "用户停止" in msg, msg)


def case_open_bag_by_key():
    print("[9] 背包没开时按 B，开了以后不重复按")
    screen = FakeScreen()
    screen.texts = []
    screen.remaining = 0
    screen.button = "limit"

    def after_press():
        screen.texts = [
            {"text": "背包", "confidence": 0.9, "center": (90, 40)},
            {"text": "贵重物", "confidence": 0.9, "center": (1353, 62)},
            {"text": "稀有度", "confidence": 0.9, "center": (200, 1010)},
            {"text": "自塑尘脂", "confidence": 0.9, "center": (1200, 300)},
            {"text": "获得途径", "confidence": 0.9, "center": (1200, 830)},
        ]

    screen.on_press = after_press
    with ExitStack() as stack:
        patch_env(stack, screen)
        ok, msg = hrc.craft_self_modeling_resin(LOG, 1, base_step(), scales=[1.0], confidences=[0.8])
    check("成功返回", ok is True, msg)
    check("按了一次 b", screen.presses.count("b") == 1, screen.presses)


def case_fail_returns_to_main():
    print("[10] 前面步骤失败也会退回主界面")
    screen = FakeScreen()
    calls = []
    with ExitStack() as stack:
        patch_env(stack, screen)
        stack.enter_context(patch.object(hrc, "_open_bag", lambda *a, **k: (False, "未能打开背包")))
        stack.enter_context(
            patch.object(hrc, "_leave_to_main", lambda *a, **k: calls.append(1) or True)
        )
        ok, msg = hrc.craft_self_modeling_resin(LOG, 1, base_step(), scales=[1.0], confidences=[0.8])
    check("判失败", ok is False, msg)
    check("原因保留", "未能打开背包" in msg, msg)
    check("调了一次回主界面", len(calls) == 1, calls)


def case_fail_exit_unconfirmed():
    print("[11] 回主界面没确认上时要在摘要里写明")
    screen = FakeScreen()
    with ExitStack() as stack:
        patch_env(stack, screen)
        stack.enter_context(patch.object(hrc, "_open_bag", lambda *a, **k: (False, "未能打开背包")))
        stack.enter_context(patch.object(hrc, "_leave_to_main", lambda *a, **k: False))
        ok, msg = hrc.craft_self_modeling_resin(LOG, 1, base_step(), scales=[1.0], confidences=[0.8])
    check("判失败", ok is False, msg)
    check("摘要标注未确认返回", "未确认返回主界面" in msg, msg)


def case_dispatch():
    print("[12] _dispatch_step_action 已接 hsr_resin_craft")
    check("已登记为内建动作", "hsr_resin_craft" in ffr._BUILTIN_ACTIONS, ffr._BUILTIN_ACTIONS)
    step = base_step()
    kwargs = dict(
        logger=LOG,
        flow_key="HSR_MONTHLY",
        step=step,
        name="自塑尘脂每月合成",
        step_no=3,
        action="hsr_resin_craft",
        hwnd=1,
        pid=2,
        scales=[1.0],
        confidences=[0.8],
        stop_check=None,
    )
    with patch.object(hrc, "craft_self_modeling_resin", lambda *a, **k: (True, "已达本月上限（本次合成 0 次）")):
        ok, msg, hwnd, pid = ffr._dispatch_step_action(**kwargs)
    check("成功时放行", ok is True, msg)

    with patch.object(hrc, "craft_self_modeling_resin", lambda *a, **k: (False, "未能打开背包")):
        ok, msg, hwnd, pid = ffr._dispatch_step_action(**kwargs)
    check("失败时冒泡", ok is False, msg)
    check("失败信息带步骤名与原因", "自塑尘脂每月合成" in msg and "未能打开背包" in msg, msg)


def case_config():
    print("[13] 配置与模板")
    path = PROJECT_ROOT / "flow_configs.example.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    steps = data["HSR_MONTHLY"]["steps"]
    hit = [s for s in steps if s.get("action") == "hsr_resin_craft"]
    check("HSR_MONTHLY 含该步骤", len(hit) == 1, [s.get("name") for s in steps])
    if hit:
        step = hit[0]
        for key in ("tab_point_frac", "item_point_frac", "source_point_frac", "craft_button_point_frac"):
            check(f"{key} 是 [fx, fy]", isinstance(step.get(key), list) and len(step[key]) == 2, step.get(key))
        for key in ("limit_template", "exit_marker_template"):
            tpl = PROJECT_ROOT / step[key]
            check(f"{key} 文件存在", tpl.is_file(), tpl)
        check("max_rounds 有上限", isinstance(step.get("max_rounds"), int) and step["max_rounds"] <= 100, step.get("max_rounds"))


def case_template_self_match():
    print("[14] 「已达上限」模板自匹配")
    ref = PROJECT_ROOT / "data/artifacts/ref_starrail_resin_synth_20261101.png"
    tpl = PROJECT_ROOT / "模板/崩铁-自塑尘脂-已达上限.png"
    if not (ref.is_file() and tpl.is_file()):
        print("  SKIP 缺少参考截图")
        return
    try:
        import cv2
        import numpy as np
    except ImportError:
        print("  SKIP 无 cv2")
        return

    def imread_u(p):
        return cv2.imdecode(np.fromfile(str(p), dtype=np.uint8), cv2.IMREAD_COLOR)

    screen = imread_u(ref)
    template = imread_u(tpl)
    res = cv2.matchTemplate(screen, template, cv2.TM_CCOEFF_NORMED)
    _mn, mx, _mnl, mxl = cv2.minMaxLoc(res)
    check("峰值 ≥0.99", mx >= 0.99, round(float(mx), 4))
    check("位置在按钮条 (666,847)", mxl == (666, 847), mxl)


def main():
    logging.basicConfig(level=logging.CRITICAL)
    print("=" * 60)
    case_parse_remaining()
    case_button_region()
    case_limit_zero()
    case_craft_until_zero()
    case_no_material()
    case_limit_template()
    case_stuck()
    case_stop()
    case_open_bag_by_key()
    case_fail_returns_to_main()
    case_fail_exit_unconfirmed()
    case_dispatch()
    case_config()
    case_template_self_match()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} 项 → {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
