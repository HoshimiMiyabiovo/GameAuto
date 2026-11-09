# -*- coding: utf-8 -*-
"""暖暖副本退出链路回归（2026-11-09 用户拍板接手并行任务）。

实机序列（用户指认）：右上门形 Esc 图标 → 按 ESC → 弹「确认返回」
（标题「确认返回」、正文「是否要返回书外?」、按钮「取消/确认」）→ 点「确认」
→ 回主界面。11-09 20:19 取证帧（ref_nikki_instance_map_20261109.jpg）即停在
副本独立地图：聊天气泡主界面模板不出现、旧逻辑死等 240s 超时。

覆盖：
  [1] 门形图标触发全链：ESC → 确认返回 → 精确点确认 → 主界面收口
  [2] OCR 读到右上 Esc 标签（图标模板失配）同样触发全链
  [3] 弹窗已在场：不重复按 ESC；标题置信度高于按钮也只点按钮（精确匹配）
  [4] ESC 后无弹窗：诚实超时、冷却限制 ESC 次数、零盲点
  [5] 主世界即使门形图标也在：_has_main 优先，零 ESC
  [6] 模板资产 + 取证帧回匹配（缺帧 SKIP）
  [7] 接线静态：右上区域限定 / 精确匹配 / 冷却

跑法：.venv\\Scripts\\python.exe -m pytest tests/suite_nikki_instance_exit_20261109.py -q
"""

import io
import sys
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from game_auto.runtime import foreground_flow_runner as ffr  # noqa: E402
from game_auto.runtime import nikki_launcher as nl  # noqa: E402

FAILURES = []
LOG = __import__("logging").getLogger("nikki_instance_exit_test")
LOG.addHandler(__import__("logging").NullHandler())

BUTTON = (1050, 600)
TITLE = (960, 400)
CANCEL = (870, 600)


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}", flush=True)
    else:
        print(f"  FAIL {name} — {detail}", flush=True)
        FAILURES.append(name)


def _rows(*pairs):
    return [
        {"text": t, "confidence": c, "center": (x, y), "box": [x - 40, y - 15, x + 40, y + 15]}
        for t, c, x, y in pairs
    ]


INSTANCE_ROWS = _rows(("ESC", 1.0, 1830, 95), ("CapsLock", 1.0, 1830, 230),
                      ("UID 106947376", 1.0, 90, 1070))
# 标题置信度故意高于按钮：子串匹配会点中标题，精确匹配必须点按钮
DIALOG_ROWS = _rows(("确认返回", 0.99, *TITLE), ("是否要返回书外?", 0.95, 960, 480),
                    ("取消", 0.95, *CANCEL), ("确认", 0.90, *BUTTON))
WORLD_ROWS = _rows(("聊天", 0.9, 100, 1000))


class FakeTime:
    def __init__(self):
        self.t = 1000.0

    def time(self):
        return self.t

    def sleep(self, sec):
        self.t += sec


class Machine:
    def __init__(self, start, icon_hit=True, dialog_opens=True):
        self.state = start
        self.icon_hit = icon_hit
        self.dialog_opens = dialog_opens
        self.clicks = []
        self.escapes = []

    def rows(self, logger, hwnd, region=None):
        return {"instance": INSTANCE_ROWS, "dialog": DIALOG_ROWS, "world": WORLD_ROWS}[self.state]

    def template(self, template_path, region, scales, confidences, use_color=False):
        if "副本退出图标" in template_path:
            return (1855, 47) if (self.icon_hit and self.state == "instance") else None
        if "暖暖-主界面" in template_path:
            return (300, 900) if self.state == "world" else None
        return None

    def click(self, center):
        x, y = center
        self.clicks.append((int(x), int(y)))
        if self.state == "dialog" and (int(x), int(y)) == BUTTON:
            self.state = "world"

    def esc(self):
        self.escapes.append("esc")
        if self.state == "instance" and self.dialog_opens:
            self.state = "dialog"


def _harness(machine, clock=None, timeout=30):
    clock = clock or FakeTime()
    patches = [
        mock.patch.object(nl, "_window_alive", return_value=True),
        mock.patch.object(nl, "_focus", lambda hwnd: None),
        mock.patch.object(nl, "_window_rect", return_value=(0, 0, 1920, 1080)),
        mock.patch.object(nl, "_ocr", side_effect=machine.rows),
        mock.patch.object(nl, "_click", side_effect=machine.click),
        mock.patch.object(nl, "_press_esc", side_effect=machine.esc),
        mock.patch.object(ffr, "_locate_template_on_screen", side_effect=machine.template),
        mock.patch.object(nl, "time", clock),
    ]
    for p in patches:
        p.start()
    try:
        phase, msg = nl._wait_main(LOG, 1, [1.0], [0.88],
                                   {"loading_timeout_sec": timeout}, None)
    finally:
        for p in reversed(patches):
            p.stop()
    return phase, msg


def test_icon_trigger_full_chain():
    print("[1] 门形图标触发全链")
    machine = Machine("instance")
    phase, msg = _harness(machine)
    check("回主界面收口 ok", phase == "ok", (phase, msg))
    check("按了一次 ESC", machine.escapes == ["esc"], machine.escapes)
    check("只点了确认按钮", machine.clicks == [BUTTON], machine.clicks)


def test_esc_label_trigger():
    print("[2] 图标模板失配、右上 Esc 标签触发")
    machine = Machine("instance", icon_hit=False)
    phase, msg = _harness(machine)
    check("回主界面收口 ok", phase == "ok", (phase, msg))
    check("按了一次 ESC", machine.escapes == ["esc"], machine.escapes)
    check("点了确认且没点标题/取消", BUTTON in machine.clicks
          and TITLE not in machine.clicks and CANCEL not in machine.clicks, machine.clicks)


def test_dialog_already_open():
    print("[3] 弹窗已在场：不重复 ESC、标题高置信也不点标题")
    machine = Machine("dialog", icon_hit=False)
    phase, msg = _harness(machine)
    check("回主界面收口 ok", phase == "ok", (phase, msg))
    check("没有按 ESC", machine.escapes == [], machine.escapes)
    check("点了确认按钮", BUTTON in machine.clicks, machine.clicks)
    check("没点标题/取消", TITLE not in machine.clicks and CANCEL not in machine.clicks,
          machine.clicks)


def test_no_dialog_honest_timeout():
    print("[4] ESC 后无弹窗：诚实超时 + 冷却限次 + 零盲点")
    machine = Machine("instance", dialog_opens=False)
    phase, msg = _harness(machine, clock=FakeTime(), timeout=20)
    check("超时失败不假成功", phase == "timeout", (phase, msg))
    check("ESC 次数被冷却限制", 1 <= len(machine.escapes) <= 4, machine.escapes)
    check("零盲点", machine.clicks == [], machine.clicks)


def test_main_world_priority():
    print("[5] 主世界优先：门形图标也在也不按 ESC")
    machine = Machine("world")
    phase, msg = _harness(machine)
    check("直接 ok", phase == "ok", (phase, msg))
    check("零 ESC 零点击", machine.escapes == [] and machine.clicks == [],
          (machine.escapes, machine.clicks))


def test_template_asset_and_real_frame():
    print("[6] 模板资产 + 取证帧回匹配")
    tpl_path = Path(nl.INSTANCE_EXIT_TEMPLATE)
    check("门形图标模板入库", tpl_path.is_file(), str(tpl_path))
    frame = PROJECT_ROOT / "data" / "artifacts" / "flow_failures" / \
        "20261109_201943_NikkiLauncher_进入无限暖暖主界面.jpg"
    if not frame.is_file():
        print("  SKIP 取证原图不在（14 天清理），跳过回匹配")
        return
    from PIL import Image
    tpl = Image.open(tpl_path)
    pt = ffr._match_template_cv2_once(Image.open(frame), tpl, [0.8])
    check("取证帧右上命中门形图标", pt is not None and abs(pt[0] - 1855) <= 4
          and abs(pt[1] - 47) <= 4, pt)


def test_wiring_static():
    print("[7] 接线静态")
    src = (PROJECT_ROOT / "src" / "game_auto" / "runtime" / "nikki_launcher.py").read_text(encoding="utf-8")
    check("图标区域限定右上 10%", "int(width * 0.90)" in src and "int(width * 0.10)" in src)
    check("确认按钮精确匹配", "_pick_exact(items, INSTANCE_CONFIRM_BUTTON" in src)
    check("退出序列有冷却", "last_exit" in src and "time.time() - last_exit >= 6" in src)
    check("弹窗词表有实机出处", "确认返回" in nl.INSTANCE_CONFIRM_TEXTS
          and "返回书外" in nl.INSTANCE_CONFIRM_TEXTS)


def main():
    test_icon_trigger_full_chain()
    test_esc_label_trigger()
    test_dialog_already_open()
    test_no_dialog_honest_timeout()
    test_main_world_priority()
    test_template_asset_and_real_frame()
    test_wiring_static()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())