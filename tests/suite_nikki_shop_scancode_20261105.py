"""2026-11-05 无限暖暖「商店又没跑」回归 + 归因更正。

事故：08:32 NikkiLauncher 步骤2 打不开奇想日历（点右上角图标 + 按 L 两次
全失败，OCR 区域数全程 17~20，成功时是 33），`_run_flow_inner` 却返回
True「已记录，继续后续任务」→ `[NikkiLauncher] 流程执行成功`，实际晶石兑换 /
共鸣商店整段没执行；task_meta 里本月两个 nikki 商店标记一条都没有。

归因更正（22:40 复核历史日志）：暖暖是 **UE5**，`press_key_scancode` 只是加固，
**不是**日历打不开的根因 —— 11-01 12:26:10 用 `pyautogui.press('l')` 第 2 下
就打开了日历（18→33 区域）并进到奇迹之冠 hub（用户记得的「点文字不进、
改点水晶碎片」就是那次之后修的 `_pair_click_point`，此后再没实机跑到过）。
真正的退化是 11-03 起「点图标优先」：11-04 被顶栏连读里的「奇想日历」误判
成已在日历页；11-05 把连读文本「奇迹之旅活动大厅 奇想日历 无限之心」
@(1502,103) 的中心当图标标签，算出 (1503,65) 点错位置。

商城（共鸣商店）：今天整段没执行（日历失败提前 return）；11-04 曾单独跑到，
按 H 三次 OCR 恒定 17 区域、失败截图仍是主世界 3D HUD，11-01～11-05
「已进入商城」0 次。

23:11 用户指出主世界鼠标被 UE5 锁在屏幕中心、按 Alt 才有光标；顶栏放大图
`data/artifacts/nikki_topbar_ref_20261105.png` 实证每个图标都有官方快捷键
（商城 H / 奇想日历 L / 共鸣 O / 任务 U / 换装 C）。

**23:28（用户拍板）**：别再折腾鼠标，`_open_calendar` / `_open_shop` 改回
**只按 L / H 三次**；`_focus_and_press` 每轮先用 `_ensure_game_foreground`
（PID 校验 + AttachThreadInput）抢前台。图标/Alt 点击 helper 保留但**未接线**，
回归 [5][7][8] 锁其行为，[6][7][9] 锁纯键盘三轮与前台校验。

**23:50 更正（用户追问证据）**：「H/L 发给了非前台窗口」**没有直接证据** ——
两次 `SetForegroundWindow 失败` 在 08:49:34 启动器阶段（游戏窗口 08:50:09
才就绪），08:50:40「窗口不可用」之后 OCR 已正常、08:52:16 报已进入主界面；
`record_failure` 的 `window_title` 记的是**目标**窗口，截图是按目标 hwnd
矩形裁剪的全屏图，画面干净无遮挡，反而**不支持**「被别的窗口盖住」。
现有证据只能证明「按 H 三次画面没变、OCR 恒定 17 区域」。现已补上取证：
`_foreground_snapshot()` 每轮记录真实前台窗口 hwnd/pid/exe/title，
三次全失败时 `_record_key_no_response` 落**整屏**快照（hwnd=None）。
回归 [10] 锁快照字段与「不裁剪到游戏窗口」。

跑法：.venv\\Scripts\\python.exe tests\\suite_nikki_shop_scancode_20261105.py
"""

from __future__ import annotations

import ast
import io
import logging
import sys
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import game_auto.runtime.foreground_flow_runner as ff  # noqa: E402
import game_auto.runtime.nikki_calendar as nc  # noqa: E402
import game_auto.runtime.nikki_launcher as nl  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} :: {detail}")
        FAILURES.append(name)


def _source(module):
    return Path(module.__file__).read_text(encoding="utf-8")


def test_scancode_helper():
    print("[1] press_key_scancode 发带扫描码的按下/抬起")
    for key, vk in (("l", 76), ("h", 72), ("esc", 27), ("escape", 27), ("f3", 114)):
        events = []
        with mock.patch("win32api.keybd_event", side_effect=lambda *a: events.append(a)), \
                mock.patch("win32api.MapVirtualKey", return_value=1), \
                mock.patch.object(ff.time, "sleep", lambda *_: None):
            ff.press_key_scancode(key)
        ok = (len(events) == 2 and events[0][0] == vk and events[0][1] == 1
              and events[0][2] == 0 and bool(events[1][2] & 0x0002))
        check(f"{key} → vk={vk} 按下+抬起且带扫描码", ok, events)

    events = []
    with mock.patch("win32api.keybd_event", side_effect=lambda *a: events.append(a)), \
            mock.patch("win32api.MapVirtualKey", return_value=1), \
            mock.patch.object(ff.time, "sleep", side_effect=RuntimeError("boom")):
        try:
            ff.press_key_scancode("l")
        except RuntimeError:
            pass
    check("sleep 抛异常也 finally 松开键", len(events) == 2, events)

    with mock.patch("win32api.keybd_event"), mock.patch("win32api.MapVirtualKey", return_value=1):
        try:
            ff.press_key_scancode("ctrl+alt+delete")
            raised = False
        except ValueError:
            raised = True
    check("无法映射的键明确报错，不静默", raised)


def test_nikki_uses_helper():
    print("[2] 暖暖的 L / H / ESC 全部改走公共函数")
    calls = []
    with mock.patch.object(ff, "press_key_scancode", side_effect=lambda *a, **k: calls.append(a)):
        nc._press_key("l")
        nc._press_key("h")
        nl._press_esc()
    check("两处按键 + ESC 都调用 press_key_scancode",
          calls == [("l",), ("h",), ("esc",)], calls)
    for module, label in ((nc, "nikki_calendar"), (nl, "nikki_launcher")):
        # 只看真实调用，不算 docstring/注释里说明旧实现的字样
        press_calls = [
            n for n in ast.walk(ast.parse(_source(module)))
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "press"
        ]
        check(f"{label} 不再调用 pyautogui.press", press_calls == [], len(press_calls))

    nc_src = _source(nc)
    check("_open_calendar 仍通过 _focus_and_press 按 L",
          '_focus_and_press(hwnd, "l")' in nc_src)
    check("_open_shop 仍通过 _focus_and_press 按 H",
          '_focus_and_press(hwnd, "h")' in nc_src)
    check("_focus_and_press 保留修饰键释放（防 L/H 被当组合键）",
          "_release_modifier_keys()" in nc_src)


def test_calendar_failure_is_visible():
    print("[3] 日历打不开时不能冒充「商店跑完了」")
    src = _source(nc)
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_run_flow_inner")
    body = ast.get_source_segment(src, fn) or ""
    check("失败文案写明晶石兑换/共鸣商店未执行",
          "本月晶石兑换/共鸣商店未执行" in body, body[:200])
    # 函数里另一个分支（未识别主界面）用的是 logger.warning，只看日历失败这一段
    branch = body.split("if not _open_calendar", 1)[-1]
    check("日历失败分支用 logger.error 而不是 warning 盖过去",
          'logger.error("[无限暖暖·奇想日历] %s", message)' in branch
          and "奇想日历未打开（已记录，继续后续任务）" not in branch)
    check("保留 return True 容错（prelaunch 返回 False 会毁掉整个暖暖日常）",
          "return True, message" in body)
    check("旧的模糊文案已移除", "奇想日历未打开（已记录，继续后续任务）" not in body)


def test_monthly_meta_evidence():
    print("[4] 月度门控与今日取证")
    src = _source(nc)
    check("晶石兑换月度键", "monthly_sequence:nikki_crystal_exchange" in src)
    check("共鸣商店月度键", "monthly_sequence:nikki_resonance_shop" in src)
    out = PROJECT_ROOT / "data" / "artifacts"
    check("今天奇想日历失败截图在",
          (out / "flow_failures" / "20261105_083255_NikkiLauncher_奇想日历-打开.jpg").exists())


def test_calendar_icon_point():
    print("[5] 顶栏连读文本不能当「奇想日历」图标标签")
    rect = (0, 0, 1920, 1080)
    # 08:32:55 失败记录里的真实 OCR：整排图标被连读，中心 (1502,103) 不是日历图标
    joined = [{"text": "奇迹之旅活动大厅 奇想日历 无限之心", "center": (1502, 103)}]
    with mock.patch.object(nc, "_norm", side_effect=lambda s: str(s).replace(" ", "")):
        check("11-05 实盘连读 → None（交回固定比例）",
              nc._calendar_icon_point(joined, rect) is None)
        single = [{"text": "奇想日历", "center": (1540, 103)}]
        check("独立标签 → 上移 0.035 窗高",
              nc._calendar_icon_point(single, rect) == (1540, 65),
              nc._calendar_icon_point(single, rect))
        short = [{"text": "奇想日历 L", "center": (1540, 103)}]
        check("标签带键位提示仍可识别", nc._calendar_icon_point(short, rect) == (1540, 65))
        off = [{"text": "奇想日历", "center": (200, 900)}]
        check("页面内同名文字（不在右上区）不算图标",
              nc._calendar_icon_point(off, rect) is None)
    check("固定比例兜底仍是 11-01 定标点", nc.CALENDAR_ICON_POINT == (0.802, 0.061))
    check("图标轮次是第 2 轮（L 优先）", nc.CALENDAR_ICON_ATTEMPT == 2)


def test_calendar_attempt_order():
    print("[6] 日历：只按 L 三次，不点图标")
    calls = []
    with mock.patch.object(nc, "_ocr_full", return_value=[]), \
            mock.patch.object(nc, "_is_calendar_page", return_value=False), \
            mock.patch.object(nc, "_wait_for", return_value=None), \
            mock.patch.object(nc, "_dismiss_continue_screens"), \
            mock.patch.object(nc, "_is_main_world", return_value=True), \
            mock.patch.object(nc, "_window_rect", return_value=(0, 0, 1920, 1080)), \
            mock.patch.object(nc, "_focus"), \
            mock.patch.object(nc, "_focus_and_press", side_effect=lambda *a: calls.append("L")), \
            mock.patch.object(nc, "_calendar_icon_point", return_value=(1540, 66)), \
            mock.patch.object(nc, "_click_with_alt", side_effect=lambda *a, **k: calls.append("icon")), \
            mock.patch.object(nc, "_click", side_effect=lambda *a: calls.append("icon")), \
            mock.patch.object(nc, "_click_ratio", side_effect=lambda *a, **k: calls.append("icon-ratio")), \
            mock.patch.object(nc.time, "sleep", lambda *_: None), \
            mock.patch.object(nc, "_record_key_no_response") as noresp:
        result = nc._open_calendar(logging.getLogger("test"), 1)
    check("三轮全失败返回 False", result is False)
    check("三次都是 L", calls == ["L", "L", "L"], calls)
    check("全程不碰鼠标（主世界光标被锁）", not any("icon" in c for c in calls), calls)
    check("三次无反应会落全屏取证", noresp.called)


def test_shop_entry_fallback():
    print("[7] 商城：只按 H 三次，不点图标；图标定位 helper 仍可用")
    rect = (0, 0, 1920, 1080)
    with mock.patch.object(nc, "_norm", side_effect=lambda s: str(s).replace(" ", "")):
        check("顶栏独立「商城」标签 → 上移后 (1222,65)",
              nc._shop_icon_point([{"text": "商城", "center": (1222, 103)}], rect) == (1222, 65),
              nc._shop_icon_point([{"text": "商城", "center": (1222, 103)}], rect))
        check("商城页左上「商城」标题不算顶栏图标",
              nc._shop_icon_point([{"text": "商城", "center": (100, 50)}], rect) is None)
        check("连读「商城 共鸣」不产生点击坐标",
              nc._shop_icon_point([{"text": "商城 共鸣", "center": (1300, 103)}], rect) is None)
    check("商城图标固定比例 = 1222/1920, 66/1080", nc.SHOP_ICON_POINT == (0.637, 0.061))
    check("图标轮次是第 2 轮（H 优先）", nc.SHOP_ICON_ATTEMPT == 2)
    check("失败文案不再只提按 H",
          "按 H / 点顶栏商城图标后仍未进入商城" in _source(nc))
    calls = []
    with mock.patch.object(nc, "_ocr_full", return_value=[]), \
            mock.patch.object(nc, "_is_shop_page", return_value=False), \
            mock.patch.object(nc, "_wait_for", return_value=None), \
            mock.patch.object(nc, "_dismiss_continue_screens"), \
            mock.patch.object(nc, "_window_rect", return_value=rect), \
            mock.patch.object(nc, "_focus"), \
            mock.patch.object(nc, "_focus_and_press", side_effect=lambda *a: calls.append("H")), \
            mock.patch.object(nc, "_shop_icon_point", return_value=None), \
            mock.patch.object(nc, "_click_with_alt", side_effect=lambda *a, **k: calls.append("icon")), \
            mock.patch.object(nc, "_click", side_effect=lambda *a: calls.append("icon")), \
            mock.patch.object(nc, "_click_ratio", side_effect=lambda *a, **k: calls.append("icon-ratio")), \
            mock.patch.object(nc, "_record_key_no_response") as noresp:
        result = nc._open_shop(logging.getLogger("test"), 1)
    check("三轮全失败返回 False", result is False)
    check("三次都是 H", calls == ["H", "H", "H"], calls)
    check("全程不碰鼠标（主世界光标被锁）", not any("icon" in c for c in calls), calls)
    check("三次无反应会落全屏取证", noresp.called)


def test_alt_unlock_click():
    print("[8] 主世界顶栏点击必须先按 Alt 呼出鼠标")
    seq = []
    with mock.patch("win32api.keybd_event",
                    side_effect=lambda *a: seq.append("down" if a[2] == 0 else "up")), \
            mock.patch("win32api.MapVirtualKey", return_value=1), \
            mock.patch.object(nc, "_click", side_effect=lambda p: seq.append("click")), \
            mock.patch.object(nc.time, "sleep", lambda *_: None):
        nc._click_with_alt((1503, 65))
    check("顺序：Alt 按下 → 点击 → Alt 抬起", seq == ["down", "click", "up"], seq)

    seq2 = []
    with mock.patch("win32api.keybd_event",
                    side_effect=lambda *a: seq2.append("down" if a[2] == 0 else "up")), \
            mock.patch("win32api.MapVirtualKey", return_value=1), \
            mock.patch.object(nc, "_click", side_effect=RuntimeError("boom")), \
            mock.patch.object(nc.time, "sleep", lambda *_: None):
        try:
            nc._click_with_alt((1, 2))
        except RuntimeError:
            pass
    check("点击异常也 finally 松开 Alt", seq2 == ["down", "up"], seq2)

    routed = []
    with mock.patch.object(nc, "_window_rect", return_value=(0, 0, 1920, 1080)), \
            mock.patch.object(nc, "_click_with_alt", side_effect=lambda p: routed.append(("alt", p))), \
            mock.patch.object(nc, "_click", side_effect=lambda p: routed.append(("plain", p))):
        nc._click_ratio(logging.getLogger("test"), 1, (0.5, 0.5), "测试", unlock_mouse=True)
        nc._click_ratio(logging.getLogger("test"), 1, (0.5, 0.5), "测试")
    check("unlock_mouse=True 才走 Alt 路径",
          routed == [("alt", (960, 540)), ("plain", (960, 540))], routed)
    src = _source(nc)
    tree = ast.parse(src)
    defs = {n.name: (ast.get_source_segment(src, n) or "")
            for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    check("日历流程不点图标（纯 L）",
          "_click" not in defs["_open_calendar"] and "_click_with_alt" not in defs["_open_calendar"])
    check("商城流程不点图标（纯 H）",
          "_click" not in defs["_open_shop"] and "_click_with_alt" not in defs["_open_shop"])


def test_foreground_before_key():
    print("[9] 按 H/L 前必须把游戏抢回前台")
    with mock.patch("win32gui.GetForegroundWindow", return_value=555), \
            mock.patch("win32gui.GetWindowText", return_value="无限暖暖"), \
            mock.patch("win32gui.IsIconic", return_value=False), \
            mock.patch("win32gui.BringWindowToTop"), \
            mock.patch("win32gui.SetForegroundWindow") as setfg, \
            mock.patch("win32gui.ShowWindow"), \
            mock.patch("win32process.GetWindowThreadProcessId", return_value=(7, 100)), \
            mock.patch("win32process.AttachThreadInput") as attach, \
            mock.patch("win32api.GetCurrentThreadId", return_value=7), \
            mock.patch.object(nc.time, "sleep", lambda *_: None):
        ok = nc._ensure_game_foreground(logging.getLogger("test"), 555)
    check("目标进程已是前台 → True 且不抢焦点",
          ok is True and not setfg.called and not attach.called)

    with mock.patch("win32gui.GetForegroundWindow", return_value=999), \
            mock.patch("win32gui.GetWindowText", return_value="别的窗口"), \
            mock.patch("win32gui.IsIconic", return_value=False), \
            mock.patch("win32gui.BringWindowToTop"), \
            mock.patch("win32gui.SetForegroundWindow"), \
            mock.patch("win32gui.ShowWindow"), \
            mock.patch("win32process.GetWindowThreadProcessId",
                       side_effect=lambda h: (3, 200 if h == 555 else 999)), \
            mock.patch("win32process.AttachThreadInput") as attach2, \
            mock.patch("win32api.GetCurrentThreadId", return_value=7), \
            mock.patch.object(nc.time, "sleep", lambda *_: None):
        ok2 = nc._ensure_game_foreground(logging.getLogger("test"), 555)
    check("抢不回前台 → False 且 attach/detach 成对",
          ok2 is False and attach2.call_count == 2, attach2.call_args_list)

    with mock.patch.object(nc, "_ensure_game_foreground", return_value=True) as ensure, \
            mock.patch.object(ff, "_release_modifier_keys"), \
            mock.patch.object(nc, "_press_key") as press:
        nc._focus_and_press(555, "h")
    check("每轮按键前都先抢前台", ensure.called and press.call_args[0][0] == "h")


def test_foreground_evidence():
    print("[10] 前台窗口取证：快照含 hwnd/pid/exe/title，三次无反应落全屏图")
    with mock.patch("win32gui.GetForegroundWindow", return_value=888), \
            mock.patch("win32gui.GetWindowText", return_value="某个窗口"), \
            mock.patch("win32process.GetWindowThreadProcessId", return_value=(4, 321)):
        snap = nc._foreground_snapshot()
    check("前台快照字段齐全",
          snap.get("hwnd") == 888 and snap.get("pid") == 321
          and snap.get("title") == "某个窗口" and "exe" in snap, snap)

    shots = []
    with mock.patch("game_auto.runtime.flow_diagnostics.capture_failure_image",
                    side_effect=lambda *a, **k: shots.append(k) or ("full.jpg", "")), \
            mock.patch.object(nc, "_foreground_snapshot",
                              return_value={"hwnd": 888, "pid": 321, "exe": "x.exe",
                                            "title": "某个窗口"}):
        nc._record_key_no_response(logging.getLogger("test"), "h", 555)
    check("取证用 hwnd=None（整屏，不裁剪到游戏窗口）",
          len(shots) == 1 and shots[0].get("hwnd") is None, shots)


def main():
    test_scancode_helper()
    test_nikki_uses_helper()
    test_calendar_failure_is_visible()
    test_monthly_meta_evidence()
    test_calendar_icon_point()
    test_calendar_attempt_order()
    test_shop_entry_fallback()
    test_alt_unlock_click()
    test_foreground_before_key()
    test_foreground_evidence()


if __name__ == "__main__":
    main()
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILED: {FAILURES}")
        sys.exit(1)
    print("\nALL PASS")
