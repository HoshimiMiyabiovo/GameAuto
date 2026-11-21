"""无限暖暖启动器「有新版本」弹窗回归（2026-11-21）。

实机背景（用户截图）：新版启动器 v1.3.3.1 开局弹「有新版本 / 立即更新 / 退出」
模态框，底下的「启动游戏」按钮还在但点不动。旧逻辑只认右下主按钮，点完直接当
成功 → 300s 等不到游戏窗口 → 整条暖暖任务中止（prelaunch 失败即 return）。

本套件全部用假 OCR 帧，不启动启动器、不点击桌面：
  1. 只认「有新版本/版本更新说明」；按几何范围精确挑「立即更新」，绝不点「退出」
  2. 弹窗在但 OCR 没认出按钮 → 返回 present，不去点底下无效的启动游戏
  3. 启动器 → 弹窗 → 立即更新 → 等更新完成 → 再点启动游戏 全链路
  4. 启动器自更新重建窗口（旧 HWND 失效）→ 按进程名重新接管后继续
  5. 更新资源还在下载（主按钮提前恢复成「启动游戏」）→ 不提前点
  6. 启动器消失且没重建 → 按 launcher_restart_timeout_sec 诚实失败
"""

import inspect
import logging
from unittest.mock import patch

from game_auto.runtime import nikki_launcher as nl

LOGGER = logging.getLogger("nikki_update_prompt_test")

PROMPT_FRAME = [
    {"text": "有新版本", "confidence": 0.99, "center": (640, 290)},
    {"text": "当前版本 v1.3.1.402", "confidence": 0.98, "center": (610, 330)},
    {"text": "版本更新说明：", "confidence": 0.97, "center": (600, 380)},
    {"text": "立即更新", "confidence": 0.96, "center": (757, 497)},
    {"text": "退出", "confidence": 0.93, "center": (671, 497)},
    {"text": "启动游戏", "confidence": 0.95, "center": (1047, 642)},
]


def test_update_prompt_state():
    marker, button = nl._update_prompt_state(PROMPT_FRAME)
    assert marker is not None and marker["text"] == "有新版本"
    assert button is not None and button["center"] == (757, 497), button

    # 没有「有新版本/版本更新说明」→ 不是弹窗（普通启动器页面不能被当成更新弹窗）
    plain = [
        {"text": "启动游戏", "confidence": 0.95, "center": (1047, 642)},
        {"text": "立即更新", "confidence": 0.95, "center": (300, 200)},
    ]
    assert nl._update_prompt_state(plain) == (None, None)

    # OCR 把「立即更新」读成「立即更 新」也能命中
    spaced = [dict(item) for item in PROMPT_FRAME]
    spaced[3]["text"] = "立即更 新"
    assert nl._update_prompt_state(spaced)[1]["center"] == (757, 497)

    # 页面别处（标题上方/很远）的同名按钮不参与
    far = list(PROMPT_FRAME) + [
        {"text": "立即更新", "confidence": 0.99, "center": (1200, 120)},
        {"text": "立即更新", "confidence": 0.99, "center": (300, 690)},
    ]
    assert nl._update_prompt_state(far)[1]["center"] == (757, 497)

    # 按钮置信度太低 → 只报 present，不盲点
    low = [dict(item) for item in PROMPT_FRAME]
    low[3]["confidence"] = 0.30
    marker, button = nl._update_prompt_state(low)
    assert marker is not None and button is None

    # 「版本更新说明」若只是公告/新闻面板标题（旁边没有「立即更新」），不当弹窗
    news = [
        {"text": "版本更新说明", "confidence": 0.95, "center": (500, 600)},
        {"text": "启动游戏", "confidence": 0.95, "center": (1047, 642)},
    ]
    assert nl._update_prompt_state(news) == (None, None)

    # 正文标题 + 弹窗按钮一起出现时仍按弹窗处理（防标题文案变化）
    only_body = [
        {"text": "版本更新说明", "confidence": 0.95, "center": (600, 380)},
        {"text": "立即更新", "confidence": 0.95, "center": (757, 497)},
    ]
    marker, button = nl._update_prompt_state(only_body)
    assert marker is not None and button["center"] == (757, 497)


def test_handle_prompt_clicks_immediately():
    clicks = []
    frames = [PROMPT_FRAME, []]  # 点击后弹窗消失

    def fake_ocr(logger, hwnd, region=None):
        return frames.pop(0) if frames else []

    with patch.multiple(
        nl,
        _ocr=fake_ocr,
        _click=lambda center: clicks.append(center),
    ), patch.object(nl.time, "sleep", lambda *_: None):
        state = nl._handle_update_prompt(LOGGER, 123, {"update_prompt_wait_sec": 5})

    assert state == "clicked", state
    assert clicks == [(757, 497)], clicks


def test_handle_prompt_without_button():
    clicks = []
    frame = [
        {"text": "有新版本", "confidence": 0.99, "center": (640, 290)},
        {"text": "退出", "confidence": 0.93, "center": (671, 497)},
        {"text": "启动游戏", "confidence": 0.95, "center": (1047, 642)},
    ]
    with patch.multiple(
        nl,
        _ocr=lambda *a, **k: frame,
        _click=lambda center: clicks.append(center),
    ), patch.object(nl.time, "sleep", lambda *_: None):
        state = nl._handle_update_prompt(LOGGER, 123, {})

    assert state == "present", state
    assert clicks == [], clicks


def test_launcher_prompt_then_start():
    clicks = []
    frames = iter([PROMPT_FRAME, [], []])

    def fake_ocr(logger, hwnd, region=None):
        return next(frames, [])

    with patch.multiple(
        nl,
        _window_rect=lambda hwnd: (320, 156, 1280, 720),
        _focus=lambda hwnd: None,
        _window_alive=lambda hwnd: True,
        _find_game_process=lambda: None,
        _click=lambda center: clicks.append(center),
        _ocr=fake_ocr,
        _launcher_button=lambda logger, hwnd: ("启动游戏", {"center": (1047, 642)}),
    ), patch.object(nl.time, "sleep", lambda *_: None):
        ok, msg = nl._launch_game_from_launcher(
            LOGGER, 123, {"launcher_timeout_sec": 10}, None
        )

    assert ok, msg
    assert msg == "更新完成并启动", msg
    assert clicks == [(757, 497), (1047, 642)], clicks


def test_launcher_restart_reacquire():
    clicks = []
    alive = {123: False, 999: True}

    with patch.multiple(
        nl,
        _window_alive=lambda hwnd: alive.get(hwnd, True),
        _find_game_process=lambda: None,
        _find_launcher_window=lambda logger: 999,
        _focus=lambda hwnd: None,
        _ocr=lambda *a, **k: [],
        _click=lambda center: clicks.append(center),
        _launcher_button=lambda logger, hwnd: ("启动游戏", {"center": (1047, 642)}),
    ), patch.object(nl.time, "sleep", lambda *_: None):
        ok, msg = nl._wait_update_done(
            LOGGER, 123, {"update_timeout_sec": 10}, None
        )

    assert ok, msg
    assert clicks == [(1047, 642)], clicks


def test_busy_blocks_early_start():
    clicks = []
    busy = iter([True, True, False])

    with patch.multiple(
        nl,
        _window_alive=lambda hwnd: True,
        _find_game_process=lambda: None,
        _focus=lambda hwnd: None,
        _ocr=lambda *a, **k: [],
        _click=lambda center: clicks.append(center),
        _launcher_busy=lambda logger, hwnd: next(busy, False),
        _launcher_button=lambda logger, hwnd: ("启动游戏", {"center": (1047, 642)}),
    ), patch.object(nl.time, "sleep", lambda *_: None):
        ok, msg = nl._wait_update_done(
            LOGGER, 123, {"update_timeout_sec": 10}, None, after_prompt=True
        )

    assert ok, msg
    assert clicks == [(1047, 642)], clicks


def test_launcher_gone_fails():
    with patch.multiple(
        nl,
        _window_alive=lambda hwnd: False,
        _find_game_process=lambda: None,
        _find_launcher_window=lambda logger: None,
        _ocr=lambda *a, **k: [],
    ), patch.object(nl.time, "sleep", lambda *_: None):
        ok, msg = nl._wait_update_done(
            LOGGER,
            123,
            {"update_timeout_sec": 10, "launcher_restart_timeout_sec": 0},
            None,
        )

    assert not ok
    assert "启动器窗口已关闭" in msg, msg


def test_wiring():
    src = inspect.getsource(nl._launch_game_from_launcher)
    src += inspect.getsource(nl._wait_update_done)
    assert "_handle_update_prompt" in src
    assert nl.UPDATE_PROMPT_BUTTONS == ("立即更新",)
    assert "退出" not in nl.UPDATE_PROMPT_BUTTONS
