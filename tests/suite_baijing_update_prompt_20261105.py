"""白荆回廊启动器更新提示回归（2026-11-05）。

事故：11-05 08:53 / 09:36 两轮都卡在「等待游戏进程」，启动器实际停在
「检测到游戏有新版本，请确认是否更新？」弹窗上（截图
`20261105_093654_BaiJing_等待游戏进程.jpg`）。旧流程只找「开始游戏」，
从不点「确定」，游戏本体进程 300s 起不来。

修复：
    1. 点「开始游戏」前先识别更新弹窗，点「确定」，再等启动器就绪；
    2. 点完「开始游戏」后如果才弹更新提示，`_wait_process` 的轮询回调兜住；
    3. 确认过更新后等待上限放宽到 `UPDATE_WAIT_TIMEOUT_SEC`（仍可停止）；
    4. 失败诊断里明确写「启动器仍显示更新提示」，不再只说点击未生效。

跑法：.venv\\Scripts\\python.exe tests\\suite_baijing_update_prompt_20261105.py
"""

import logging
import sys
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import game_auto.runtime.baijing_flow as bf  # noqa: E402

LAUNCHER_HWND = 1234
FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def _logger():
    logger = logging.getLogger("test_baijing_update_prompt")
    logger.setLevel(logging.CRITICAL)
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


def _ocr_item(text, center, conf=0.9):
    return {"text": text, "confidence": conf, "center": center}


class _Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))


def _with_confirm_env(ocr_side_effect, click=None):
    """给 _confirm_launcher_update 套上 OCR/点击/睡眠替身。"""
    click = click or _Recorder()
    patches = [
        mock.patch.object(bf, "ocr_window_content", side_effect=ocr_side_effect),
        mock.patch.object(bf, "_pyautogui_click_guarded", click),
        mock.patch.object(bf.time, "sleep", lambda *_: None),
    ]
    return patches, click


def test_1_no_prompt():
    print("[1] 没有更新弹窗：不点任何东西")
    patches, click = _with_confirm_env(
        [[_ocr_item("开始游戏", (100, 200)), _ocr_item("确定", (300, 400))]]
    )
    with patches[0], patches[1], patches[2]:
        ok = bf._confirm_launcher_update(_logger(), LAUNCHER_HWND)
    check("返回 False", ok is False, ok)
    check("没点「确定」", not click.calls, click.calls)


def test_2_confirm_prompt():
    print("[2] 更新弹窗 + 确定：点击确定并等待弹窗消失")
    marker = _ocr_item("检测到游戏有新版本，请确认是否更新？", (500, 300))
    cancel = _ocr_item("取消", (400, 400))
    confirm = _ocr_item("确定", (600, 400))
    start = _ocr_item("开始游戏", (700, 600))
    patches, click = _with_confirm_env(
        [
            [marker, cancel, confirm, start],
            [start],
        ]
    )
    with patches[0], patches[1], patches[2]:
        ok = bf._confirm_launcher_update(_logger(), LAUNCHER_HWND)
    check("返回 True", ok is True, ok)
    check("点了一次", len(click.calls) == 1, click.calls)
    if click.calls:
        pos = click.calls[0][0][:2]
        check("点的是「确定」", pos == (600, 400), pos)


def test_3_prompt_without_confirm():
    print("[3] 有更新弹窗但 OCR 没找到「确定」：不盲点")
    marker = _ocr_item("检测到游戏有新版本，请确认是否更新？", (500, 300))
    patches, click = _with_confirm_env([[marker]])
    with patches[0], patches[1], patches[2]:
        ok = bf._confirm_launcher_update(_logger(), LAUNCHER_HWND)
    check("返回 False", ok is False, ok)
    check("没点任何东西", not click.calls, click.calls)


def test_4_diagnosis_mentions_update():
    print("[4] 失败诊断明确写出「启动器仍显示更新提示」")
    patch_list = [
        mock.patch.object(bf, "_window_region", lambda hwnd: (0, 0, 800, 600)),
        mock.patch.object(bf, "_window_obscured_ratio", lambda hwnd: 0.0),
        mock.patch.object(bf, "_find_by_template", lambda *a, **k: (700, 600)),
        mock.patch.object(
            bf,
            "ocr_window_content",
            lambda *a, **k: [
                _ocr_item("检测到游戏有新版本，请确认是否更新？", (500, 300)),
                _ocr_item("确定", (600, 400)),
            ],
        ),
        mock.patch.object(bf.win32gui, "IsWindow", lambda hwnd: True),
    ]
    with patch_list[0], patch_list[1], patch_list[2], patch_list[3], patch_list[4]:
        text = bf._diagnose_launcher_state(_logger(), LAUNCHER_HWND)
    check("含更新提示", "更新提示" in text, text)


def test_5_wiring():
    print("[5] 启动流程先处理更新提示，再点开始游戏")
    src = Path(bf.__file__).read_text(encoding="utf-8")
    check("调用了 _confirm_launcher_update", "_confirm_launcher_update(" in src)
    check(
        "确认更新后放宽开始按钮等待",
        "start_timeout = UPDATE_WAIT_TIMEOUT_SEC if update_confirmed else 120" in src,
    )
    check(
        "等待进程时有更新轮询回调",
        "on_poll=_poll_update" in src,
    )
    idx_update = src.index("update_confirmed = _confirm_launcher_update(")
    idx_click = src.index('"launcher_start",\n        "开始游戏"')
    check("更新处理在「开始游戏」之前", idx_update < idx_click, (idx_update, idx_click))


def main():
    test_1_no_prompt()
    test_2_confirm_prompt()
    test_3_prompt_without_confirm()
    test_4_diagnosis_mentions_update()
    test_5_wiring()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
