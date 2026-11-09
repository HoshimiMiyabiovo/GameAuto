# -*- coding: utf-8 -*-
"""绝区零游戏内按键/开菜单回归（2026-11-07）。

事故一：12:09 ZZZ_LOGISTICS_MONTHLY 步骤3「打开功能菜单」模板命中右下 ··· 按钮
(1777,967) 并点击（按住 0.06s），confirm_type:none 无验证默认成功——主菜单根本
没开出来，步骤4 在主世界空找「后勤商店」4 轮失败（#209）。···点击是 10-21 设计
时写下的路径，后勤链路 11-06 拆分前从没实机跑过，今天是第一次执行。
事故二：12:15 ZZZ_NOTORIOUS_HUNT `_press_key` 用 pyautogui.press——11-05 实机
已证明瞬时虚拟键事件被 Unity 吞掉，F2 按三次快捷手册都没开（#210）。

改法：
- runner 新增通用 `press_key` action（press_key_scancode 扫描码，可选
  verify_texts 按下后等 OCR 见到页面特征才算成功、没见重按）；
- ZZZ_LOGISTICS 步骤2 换成 `{"action":"press_key","key":"oem3"}`（用户
  2026-11-09 拍板：后勤商店入口在「手机」里，大世界按 ·/~ 键即 VK_OEM_3 打开；
  ESC 功能菜单 →「丽都城募」活动页是错误链路，已整步删除）；
- zzz_notorious_hunt 的 `_press_key`/`_hold_key` 全部换 press_key_scancode。

覆盖：
  [1] press_key 无验证：按 N 次透传 key
  [2] press_key 带 verify_texts：OCR 见到才成功 / 重按 / 耗尽失败
  [3] 恶名狩猎 _press_key/_hold_key 走扫描码
  [4] 两份配置：后勤步骤3 = press_key esc；恶名狩猎源码静态断言
  [5] runner 静态：_BUILTIN_ACTIONS 注册 press_key

跑法：.venv\\Scripts\\python.exe tests\\suite_zzz_menu_key_20261107.py
"""

import io
import logging
import sys
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import game_auto.runtime.foreground_flow_runner as ff  # noqa: E402
import game_auto.runtime.zzz_notorious_hunt as nh  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} :: {detail}")
        FAILURES.append(name)


def _logger():
    logger = logging.getLogger("test_zzz_menu_key")
    logger.setLevel(logging.CRITICAL)
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


def _dispatch(step):
    return ff._dispatch_step_action(
        logger=_logger(), flow_key="ZZZ_LOGISTICS_MONTHLY", step=step,
        name=step.get("name", "打开功能菜单"), step_no=3, action=step["action"],
        hwnd=7, pid=4321, scales=[1.0], confidences=[0.8], stop_check=None,
    )


def test_press_key_plain():
    print("[1] press_key 无验证：扫描码按 N 次")
    presses = []
    with mock.patch.object(ff, "press_key_scancode",
                           lambda key, hold_sec=0.15: presses.append(key)), \
            mock.patch.object(ff, "_focus_window", lambda *a, **k: None), \
            mock.patch.object(ff.time, "sleep", lambda s: None):
        ok, msg, hwnd, pid = _dispatch(
            {"action": "press_key", "key": "esc", "presses": 1})
    check("返回成功", ok is True, msg)
    check("扫描码按了一次 esc", presses == ["esc"], presses)

    presses.clear()
    with mock.patch.object(ff, "press_key_scancode",
                           lambda key, hold_sec=0.15: presses.append(key)), \
            mock.patch.object(ff, "_focus_window", lambda *a, **k: None), \
            mock.patch.object(ff.time, "sleep", lambda s: None):
        ok, msg, hwnd, pid = _dispatch(
            {"action": "press_key", "key": "esc", "presses": 2,
             "press_interval_sec": 1.0})
    check("presses=2 按两次", ok is True and presses == ["esc", "esc"], (ok, presses))


def test_press_key_verified():
    print("[2] press_key 带 verify_texts：见到特征才算成功")
    # 2a. 第一次按下后 OCR 就见到 → 成功
    with mock.patch.object(ff, "press_key_scancode", lambda *a, **k: None), \
            mock.patch.object(ff, "_focus_window", lambda *a, **k: None), \
            mock.patch.object(ff, "_ocr_locate_in_window", lambda *a, **k: (100, 100)), \
            mock.patch.object(ff.time, "sleep", lambda s: None):
        ok, msg, *_ = _dispatch(
            {"action": "press_key", "key": "esc", "verify_texts": ["后勤商店"],
             "verify_timeout_sec": 3})
    check("OCR 见到特征 → 成功", ok is True, msg)

    # 2b. 一直见不到 → 重按到耗尽，返回失败
    presses = []
    with mock.patch.object(ff, "press_key_scancode",
                           lambda key, hold_sec=0.15: presses.append(key)), \
            mock.patch.object(ff, "_focus_window", lambda *a, **k: None), \
            mock.patch.object(ff, "_ocr_locate_in_window",
                              lambda *a, **k: None), \
            mock.patch.object(ff.time, "sleep", lambda s: None):
        ok, msg, *_ = _dispatch(
            {"action": "press_key", "key": "esc", "verify_texts": ["后勤商店"],
             "verify_timeout_sec": 1, "max_attempts": 3})
    check("重按 3 轮后失败", ok is False, msg)
    check("共按了 3 次", presses == ["esc"] * 3, presses)
    check("失败信息带步骤名", "打开功能菜单" in msg, msg)


def test_notorious_scancode():
    print("[3] 恶名狩猎按键走扫描码")
    calls = []
    with mock.patch.object(ff, "press_key_scancode",
                           lambda key, hold_sec=0.15: calls.append((key, hold_sec))), \
            mock.patch.object(ff, "_focus_window", lambda *a, **k: None):
        nh._press_key(1, "f2")
        nh._hold_key(1, "w", 2.0)
    check("_press_key → 扫描码 f2", calls and calls[0][0] == "f2", calls)
    check("_hold_key → 扫描码 w 按住 2s",
          len(calls) == 2 and calls[1] == ("w", 2.0), calls)

    # 战斗引擎（zzz_battle_assist）普攻/技能/切人/闪避也必须走扫描码
    battle_calls = []
    with mock.patch.object(ff, "press_key_scancode",
                           lambda key, hold_sec=0.15: battle_calls.append((key, hold_sec))):
        from game_auto.runtime.zzz_battle_assist import ZzzBattleAssist
        ZzzBattleAssist._press("shift", 0.05)
        ZzzBattleAssist._press("q", 0.03)
    check("战斗 _press → 扫描码（shift 闪避键）",
          battle_calls[:1] == [("shift", 0.05)] and battle_calls[1][0] == "q", battle_calls)
    check("press_key_scancode 支持 shift（闪避键不抛 ValueError）",
          ff._scancode_vk("shift") == 0x10, ff._scancode_vk("shift"))

    # do_hold 键盘长按：down 事件带扫描码（重复 down + 一次 up）
    import win32con
    events = []
    with mock.patch.object(ff, "_focus_window", lambda *a, **k: None), \
            mock.patch("win32api.MapVirtualKey", lambda vk, flag: 0x2A), \
            mock.patch("win32api.keybd_event",
                       lambda vk, scan, flags, extra: events.append((vk, scan, flags))), \
            mock.patch.object(nh.time, "sleep", lambda s: None):
        from game_auto.runtime.zzz_battle_assist import ZzzBattleAssist
        # seconds=0：down → 立即 break → up，共一次 down + 一次 up
        assist = ZzzBattleAssist(1, model_path="Z:/nonexistent.onnx")
        assist.do_hold("shift", 0)
    downs = [e for e in events if e[2] == 0]
    ups = [e for e in events if e[2] == win32con.KEYEVENTF_KEYUP]
    check("do_hold 键盘长按发扫描码 down/up", bool(downs) and bool(ups), events[:3])


def test_configs_and_wiring():
    print("[4][5] 配置与静态接线")
    for name in ("flow_configs.json", "flow_configs.example.json"):
        import json
        cfg = json.loads((PROJECT_ROOT / name).read_text(encoding="utf-8"))
        steps = cfg["ZZZ_LOGISTICS_MONTHLY"]["steps"]
        s2 = steps[1]
        check(f"{name} 后勤步骤2 = press_key oem3（· 打开手机）",
              s2.get("action") == "press_key" and s2.get("key") == "oem3", s2)
        check(f"{name} 手机是 toggle：max_attempts=1 + verify_texts 见到后勤商店",
              s2.get("max_attempts") == 1
              and s2.get("verify_texts") == ["后勤商店"], s2)
        check(f"{name} 已删丽都城募中间层",
              not any(s.get("target_text") == "丽都城募" for s in steps),
              [s.get("name") for s in steps])
        check(f"{name} 后勤流程 5 步且末步采购",
              len(steps) == 5 and steps[-1]["action"] == "zzz_shop_buy",
              [s["name"] for s in steps])
    check("_scancode_vk 支持 oem3（· 键 = VK_OEM_3）",
          ff._scancode_vk("oem3") == 0xC0, ff._scancode_vk("oem3"))
    src = Path(ff.__file__).read_text(encoding="utf-8")
    check("_BUILTIN_ACTIONS 注册 press_key", '"press_key"' in src, None)
    src_nh = Path(nh.__file__).read_text(encoding="utf-8")
    check("恶名狩猎 _press_key 用扫描码",
          "press_key_scancode(key)" in src_nh and "pyautogui.press(" not in src_nh, None)
    check("恶名狩猎 _hold_key 用扫描码",
          "press_key_scancode(key, hold_sec=" in src_nh, None)


def main():
    test_press_key_plain()
    test_press_key_verified()
    test_notorious_scancode()
    test_configs_and_wiring()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
