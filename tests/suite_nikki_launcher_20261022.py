"""无限暖暖进入主界面回归测试（2026-10-22）。

纯 mock，不启动游戏、不点击桌面。断言：
  1. 主界面模板可在游戏背景上稳定匹配
  2. 启动器按钮 OCR 只在右下角命中「启动游戏/更新/运行中」
  3. 已在游戏（含已在主界面）时不点启动器
  4. 启动器 → 启动游戏 → 等游戏窗口 → 主界面 全链路
  5. 启动器更新态 → 等更新完成 → 再点启动
  6. 登录页「点击进入游戏」/「确认」/「退出游戏」防误点
  7. 加载到主界面连续两次命中才算成功
  8. NikkiLauncher 流程定义在位，但 InfinityNikki 不挂 prelaunch_flow
     （一条龙直走 whimbox 自启动；前置流程留作手动/其他用途，默认不接线）

跑法：.venv\\Scripts\\python.exe artifacts\\test_nikki_launcher_20261022.py
"""

import inspect
import json
import logging
import sys
from unittest.mock import patch

import cv2
import numpy as np

sys.path.insert(0, r"E:\GithubProject")

from game_auto.runtime import foreground_flow_runner, nikki_launcher as nl  # noqa: E402
from game_auto.settings import APPS  # noqa: E402

FAILURES = []
LOGGER = logging.getLogger("nikki_test")


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def test_template():
    print("[1] 主界面聊天气泡模板匹配")
    from PIL import Image
    try:
        tpl = np.array(Image.open(nl.MAIN_TEMPLATE).convert("L"))
    except Exception:
        tpl = None
    check("模板存在且尺寸正确", tpl is not None and tpl.shape[:2] == (30, 36), None if tpl is None else tpl.shape)
    if tpl is None:
        return
    rng = np.random.default_rng(0)
    bg = np.clip(60 + rng.normal(0, 20, (1080, 1920)), 0, 255).astype(np.uint8)
    h, w = tpl.shape[:2]
    x, y = 48, 989
    roi = bg[y:y + h, x:x + w].copy()
    roi[tpl > 0] = 255
    bg[y:y + h, x:x + w] = roi
    score = float(cv2.matchTemplate(bg, tpl, cv2.TM_CCOEFF_NORMED).max())
    check("合成背景命中 >0.80", score > 0.80, score)


def test_launcher_button():
    print("[2] 启动器按钮 OCR 解析")
    with patch.multiple(
        nl,
        _window_rect=lambda hwnd: (320, 156, 1280, 720),
        _ocr=lambda logger, hwnd, region=None: [
            {"text": "公告", "confidence": 0.99, "center": (400, 700)},
            {"text": "启动游戏", "confidence": 0.80, "center": (1379, 798)},
        ],
    ):
        found = nl._launcher_button(LOGGER, 123)
    check("命中启动游戏", bool(found) and found[0] == "启动游戏", found)
    check("取右下角按钮中心", bool(found) and found[1]["center"] == (1379, 798), found)

    # 2026-10-23 实测文案：主按钮叫「进入游戏」，另有「使用DX11启动」备用按钮。
    # 旧匹配表只认"启动"子串 → 点到 DX11 备用按钮 → 游戏 300s 不起 → 流程失败。
    with patch.multiple(
        nl,
        _window_rect=lambda hwnd: (320, 156, 1280, 720),
        _ocr=lambda logger, hwnd, region=None: [
            {"text": "极致", "confidence": 0.60, "center": (1525, 775)},
            {"text": "使用DX11启动", "confidence": 0.76, "center": (1390, 863)},
            {"text": "进入游戏", "confidence": 0.77, "center": (1435, 819)},
        ],
    ):
        found = nl._launcher_button(LOGGER, 123)
    check("进入游戏优先于 DX11 备用按钮", bool(found) and found[0] == "进入游戏", found)
    check("不点 DX11 备用按钮", bool(found) and found[1]["center"] == (1435, 819), found)

    with patch.multiple(
        nl,
        _window_rect=lambda hwnd: (320, 156, 1280, 720),
        _ocr=lambda logger, hwnd, region=None: [
            {"text": "使用DX11启动", "confidence": 0.76, "center": (1390, 863)},
            {"text": "（游戏异常时选择）", "confidence": 0.87, "center": (1497, 863)},
        ],
    ):
        found = nl._launcher_button(LOGGER, 123)
    check("只有 DX11 备用按钮时不误点", found is None, found)


def test_already_in_main():
    print("[3] 游戏已在主界面 → 不点启动器")
    calls = []
    with patch.multiple(
        nl,
        _hwnd_pid=lambda hwnd: 100,
        _process_name=lambda pid: nl.GAME_PROCESS.lower(),
        _focus=lambda hwnd: None,
        _has_main=lambda hwnd, s, c: True,
        _launcher_button=lambda logger, hwnd: calls.append("launcher") or None,
        _window_alive=lambda hwnd: True,
    ):
        ok, msg, game_hwnd, game_pid = nl.enter_main_interface(
            LOGGER, 123, {}, [1.0], [0.8]
        )
    check("成功", ok, msg)
    check("返回游戏窗口", (game_hwnd, game_pid) == (123, 100), (game_hwnd, game_pid))
    check("未触碰启动器", calls == [], calls)


def test_launcher_to_main():
    print("[4] 启动器 → 启动游戏 → 等窗口 → 主界面")
    clicks = []
    with patch.multiple(
        nl,
        _hwnd_pid=lambda hwnd: 200,
        _process_name=lambda pid: "xstarter.exe",
        _window_rect=lambda hwnd: (320, 156, 1280, 720),
        _focus=lambda hwnd: None,
        _click=lambda center: clicks.append(center),
        _launcher_button=lambda logger, hwnd: ("启动游戏", {"center": (1379, 798)}),
        _wait_game_window=lambda step, stop: (456, 300),
        _window_alive=lambda hwnd: True,
        _has_main=lambda hwnd, s, c: hwnd == 456,
    ):
        ok, msg, game_hwnd, game_pid = nl.enter_main_interface(
            LOGGER, 123, {"launcher_timeout_sec": 5}, [1.0], [0.8]
        )
    check("成功", ok, msg)
    check("点了一次启动游戏", clicks == [(1379, 798)], clicks)
    check("返回游戏窗口", (game_hwnd, game_pid) == (456, 300), (game_hwnd, game_pid))


def test_update_then_launch():
    print("[5] 启动器更新态 → 等更新 → 点启动")
    clicks = []
    buttons = iter([
        ("更新", {"center": (1370, 798)}),
        ("启动游戏", {"center": (1380, 798)}),
    ])
    with patch.multiple(
        nl,
        _window_rect=lambda hwnd: (320, 156, 1280, 720),
        _focus=lambda hwnd: None,
        _window_alive=lambda hwnd: True,
        _find_game_process=lambda: None,
        _click=lambda center: clicks.append(center),
        _launcher_button=lambda logger, hwnd: next(buttons, None),
    ), patch.object(nl.time, "sleep", lambda *_: None):
        ok, msg = nl._wait_update_done(
            LOGGER, 123, {"update_timeout_sec": 5}, None
        )
    check("更新完成成功", ok, msg)
    check("只点了更新后的启动游戏", clicks == [(1380, 798)], clicks)


def test_login_hint():
    print("[6] 登录页点击进入游戏 / 防误点退出确认")
    clicks = []
    frames = iter([
        [
            {"text": "退出游戏", "confidence": 0.90, "center": (400, 400)},
            {"text": "确认", "confidence": 0.90, "center": (960, 600)},
        ],
        [
            {"text": "点击进入游戏", "confidence": 0.90, "center": (960, 900)},
        ],
    ])
    with patch.multiple(
        nl,
        _window_rect=lambda hwnd: (0, 0, 1920, 1080),
        _focus=lambda hwnd: None,
        _window_alive=lambda hwnd: True,
        _has_main=lambda hwnd, s, c: False,
        _ocr=lambda *a, **k: next(frames),
        _click=lambda center: clicks.append(center),
        _press_esc=lambda: None,
    ), patch.object(nl.time, "sleep", lambda *_: None):
        phase, msg = nl._enter_login(
            LOGGER, 1, [1.0], [0.8], {"login_timeout_sec": 5}, None
        )
    check("进入加载阶段", phase == "loading", (phase, msg))
    check("只点点击进入游戏，不点退出确认", clicks == [(960, 900)], clicks)


def test_wait_main():
    print("[7] 主界面连续两次命中才成功")
    checks = iter([False, True, True])
    clicks = []
    with patch.multiple(
        nl,
        _window_rect=lambda hwnd: (0, 0, 1920, 1080),
        _focus=lambda hwnd: None,
        _window_alive=lambda hwnd: True,
        _has_main=lambda hwnd, s, c: next(checks, True),
        _click=lambda center: clicks.append(center),
        _ocr=lambda *a, **k: [],
    ), patch.object(nl.time, "sleep", lambda *_: None):
        phase, msg = nl._wait_main(
            LOGGER, 1, [1.0], [0.8], {"loading_timeout_sec": 10}, None
        )
    check("成功进入主界面", phase == "ok", (phase, msg))
    check("未命中时有点击推进", len(clicks) >= 2, clicks)


def test_wiring():
    print("[8] 流程与 app 配置接线")
    cfg = json.load(open("flow_configs.json", encoding="utf-8"))
    flow = cfg.get("NikkiLauncher") or {}
    step = (flow.get("steps") or [{}])[0]
    check("流程存在且启用", bool(flow) and flow.get("enabled") is True)
    check("动作是 nikki_enter_game", step.get("action") == "nikki_enter_game", step)
    check(
        "InfinityNikki 未挂 prelaunch_flow（直走 whimbox 自启动）",
        not (APPS.get("InfinityNikki") or {}).get("prelaunch_flow"),
        (APPS.get("InfinityNikki") or {}).get("prelaunch_flow"),
    )
    src = inspect.getsource(foreground_flow_runner.run_flow)
    src += inspect.getsource(foreground_flow_runner._dispatch_step_action)
    check("执行器分发 nikki_enter_game", "nikki_enter_game" in src)
    check("关闭进程开关为 false", not flow.get("close_process_on_finish") and not flow.get("close_process_on_fail"))


def main():
    test_template()
    test_launcher_button()
    test_already_in_main()
    test_launcher_to_main()
    test_update_then_launch()
    test_login_hint()
    test_wait_main()
    test_wiring()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
