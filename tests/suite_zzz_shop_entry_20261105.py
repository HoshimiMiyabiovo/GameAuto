"""2026-11-05 绝区零商城入口：扫描码 ESC + 固定比例坐标兜底回归。

实机逐项对比（双方提权、UIPI 无阻碍、前台窗口确认为游戏）：
  pyautogui.press/keyDown 的 ESC、F3 全部无效；
  win32api.keybd_event(vk, MapVirtualKey(vk)) 按住 0.15s 立刻开页面。
路径：主世界 → 扫描码 ESC 两次 → 主菜单 → 24HOUR 图标 → 商城
      → 信源清单 (183,573) → 信号残响。

2026-11-06 实机更正：11-05 记的 24HOUR 命中点 (1376,970) 偏右 28px，实际点到
「丽都城募」。11-06 完整买到加密母带×3 + 原装母带×5 的命中点是 (1348,950)
@1920x1080 → 窗口比例 [0.7021, 0.8796]，两份 flow_configs 同步改。**比例按窗口
rect 换算，换分辨率 / 开 NVIDIA DSR 内部渲染倍率都不受影响**。

跑法：.venv\\Scripts\\python.exe tests\\suite_zzz_shop_entry_20261105.py
"""

from __future__ import annotations

import ast
import io
import json
import os
import sys
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import game_auto.runtime.foreground_flow_runner as ff  # noqa: E402
import game_auto.runtime.monthly_exchange as me  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} :: {detail}")
        FAILURES.append(name)


def test_press_escape_scan_code():
    print("[1] _press_escape 必须发带硬件扫描码的按键")
    events = []
    with mock.patch("win32api.keybd_event", side_effect=lambda *a: events.append(a)), \
            mock.patch("win32api.MapVirtualKey", return_value=1), \
            mock.patch.object(ff, "_focus_window", lambda *a: None), \
            mock.patch.object(me.time, "sleep", lambda *_: None):
        me._press_escape(123)
    check("按下+抬起共 2 次", len(events) == 2, events)
    if len(events) == 2:
        down, up = events
        check("按下事件 vk=ESC(27)", down[0] == 27, down)
        check("按下事件带扫描码", down[1] == 1, down)
        check("抬起事件带 KEYEVENTF_KEYUP", bool(up[2] & 0x0002), up)
    tree = ast.parse(Path(me.__file__).read_text(encoding="utf-8"))
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "_press_escape")
    calls = {
        (n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", ""))
        for n in ast.walk(fn) if isinstance(n, ast.Call)
    }
    check("不再调用 pyautogui.press/keyDown（只看真实调用，不算注释）",
          not ({"press", "keyDown"} & calls), sorted(calls))
    src = ast.get_source_segment(Path(me.__file__).read_text(encoding="utf-8"), fn) or ""
    check("finally 保证异常也松开键", "finally:" in src)
    check("按住时长可配置且默认 0.15", me.ESC_HOLD_SEC == 0.15, me.ESC_HOLD_SEC)


def test_shop_entry_priority():
    print("[2] 商城入口定位：模板 > OCR > 固定比例坐标")
    tpl = str(PROJECT_ROOT / "assets" / "templates" / "绝区零-商城入口.png")
    check("实机模板存在", os.path.isfile(tpl), tpl)

    with mock.patch.object(me, "_locate", return_value=(100, 200)), \
            mock.patch.object(me, "_locate_ocr") as ocr, \
            mock.patch.object(me, "_frac_point") as frac:
        got = me._find_shop_entry(mock.MagicMock(), 1, tpl, "商城",
                                  [1.0], [0.8], 1, None, point_frac=[0.7021, 0.8796])
    check("模板命中即返回，不用 OCR/兜底",
          got == (100, 200) and not ocr.called and not frac.called, got)

    with mock.patch.object(me, "_locate", return_value=None), \
            mock.patch.object(me, "_locate_ocr", return_value=(300, 400)), \
            mock.patch.object(me, "_frac_point") as frac:
        got = me._find_shop_entry(mock.MagicMock(), 1, tpl, "商城",
                                  [1.0], [0.8], 1, None, point_frac=[0.7021, 0.8796])
    check("OCR 次之且不用兜底", got == (300, 400) and not frac.called, got)

    with mock.patch.object(me, "_locate", return_value=None), \
            mock.patch.object(me, "_locate_ocr", return_value=None), \
            mock.patch.object(me, "_frac_point", return_value=(1348, 950)) as frac:
        got = me._find_shop_entry(mock.MagicMock(), 1, tpl, "商城",
                                  [1.0], [0.8], 1, None, point_frac=[0.7021, 0.8796])
    check("模板/OCR 全失效才用兜底", got == (1348, 950) and frac.called, got)
    check("兜底走 _frac_point（窗口局部坐标，与其它判据同系）", frac.called)

    with mock.patch.object(me, "_locate", return_value=None), \
            mock.patch.object(me, "_locate_ocr", return_value=None), \
            mock.patch.object(me, "_frac_point") as frac:
        got = me._find_shop_entry(mock.MagicMock(), 1, tpl, "商城",
                                  [1.0], [0.8], 1, None)
    check("不传 point_frac 时行为与旧版一致", got is None and not frac.called, got)


def test_esc_gate_and_config():
    print("[3] 固定坐标只在按过 ESC 之后启用 + 两份配置同步")
    src = Path(me.__file__).read_text(encoding="utf-8")
    check("_open_exchange_page 有 attempt>1 门控",
          "point_frac=entry_point_frac if attempt > 1 else None" in src)
    check("配置键读取正确", 'entry_point_frac = step.get("shop_entry_point_frac")' in src)

    cfg = {}
    for name in ("flow_configs.json", "flow_configs.example.json"):
        cfg[name] = json.loads((PROJECT_ROOT / name).read_text(encoding="utf-8"))
    a, b = cfg["flow_configs.json"], cfg["flow_configs.example.json"]
    check("两份 ZZZ_MONTHLY 配置一致", a["ZZZ_MONTHLY"] == b["ZZZ_MONTHLY"])
    step = a["ZZZ_MONTHLY"]["steps"][2]
    check("步骤3 是信号残响每月兑换", step["name"] == "信号残响每月兑换")
    frac = step.get("shop_entry_point_frac")
    check("兜底坐标已配置（11-06 实机更正）", frac == [0.7021, 0.8796], frac)
    if frac:
        check("比例对应实机 1920x1080 命中点 (1348,950)",
              abs(frac[0] * 1920 - 1348) < 3 and abs(frac[1] * 1080 - 950) < 3,
              (frac[0] * 1920, frac[1] * 1080))
    check("其它流程未被写入该键",
          not any("shop_entry_point_frac" in s
                  for k, v in a.items() if k != "ZZZ_MONTHLY" and isinstance(v, dict)
                  for s in v.get("steps", []) if isinstance(s, dict)))


def test_evidence_files():
    print("[4] 实机取证齐备")
    out = PROJECT_ROOT / "data" / "artifacts"
    menu = out / "zzz_step_1793884843011.jpg"
    shop = out / "zzz_step_1793884868925.jpg"
    signal = out / "zzz_step_1793884886067.jpg"
    # 这些是本机一次性取证，清掉后不该让套件挂掉
    present = [p for p in (menu, shop, signal) if p.exists()]
    print(f"  (本机取证图 {len(present)}/3 张存在)")
    check("单步调试入口脚本存在", (out / "zzz_step.py").exists())


def main():
    test_press_escape_scan_code()
    test_shop_entry_priority()
    test_esc_gate_and_config()
    test_evidence_files()


if __name__ == "__main__":
    main()
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILED: {FAILURES}")
        sys.exit(1)
    print("\nALL PASS")
