"""2026-11-26 MiniMax「更新已来」公告弹窗关不掉（失败记录 #513）回归。

背景（auto_runner.log 2026-11-26 21:01-21:02 实录）：
  1. 新版「MiniMax Code 更新已来」公告弹窗在场，旧 dismiss 按**窗宽比例**外推 X 坐标，
     窗口非最大化时差 ~90px 点到弹窗空白处（日志：算出 (1234,341)，OCR 的 × 字形在
     ≈(1325,343)），"点 X 无效"弹窗没关；
  2. 模态遮罩吞掉「签到得400」点击 → api 日志只有 signin/status、零个 signin/claim；
  3. 自愈重试时 qoder 窗口压住签到按钮区域，点击防护两次拒绝（前台校验过、点击点
     归属别的窗口），窗口内坐标还出现过 (-130,814) 的出窗点 → 点击根本没发出去。

修法：
  - dismiss 三段式：ESC → X 候选位（OCR ×/X 字形 / 固定像素偏移 / 窗宽比例外推）
    逐个点击、**每次点击后复核弹窗真的消失** → 「立即体验」CTA 兑底；
  - 自愈重试点击改**强制置顶闭环**（聚焦→校验可点性→保留 TOPMOST 再校验→点击→
    撤 TOPMOST），出窗坐标直接丢弃。

跑法：.venv\\Scripts\\python.exe tests\\suite_minimax_dismiss_cta_20261126.py
"""

import io
import logging
import sys
import unittest.mock as mock
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sandbox_temp import install as _install_sandbox_temp

_install_sandbox_temp()
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def _logger():
    logger = logging.getLogger("test_minimax_dismiss_cta")
    logger.setLevel(logging.CRITICAL)
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


FFR = "game_auto.runtime.foreground_flow_runner"
OCT = "game_auto.runtime.ocr_click_tester"

# 与 11-26 失败现场同构的场景件（窗口 (100,200) 1600x900，坐标全为屏幕绝对值）
TITLE = {"text": "MiniMax Code 更新已来", "confidence": 0.99, "center": (690, 399)}
GLYPH = {"text": "X", "confidence": 0.55, "center": (1325, 343)}
CTA = {"text": "立即体验", "confidence": 0.99, "center": (900, 760)}
RECT = (100, 200, 1600, 900)  # left, top, width, height


def _ocr_scenes(scenes):
    """把场景队列接到 ocr_window_content 桩上，耗尽后停在最后一幕。"""
    queue = list(scenes)
    state = {"i": 0}

    def _fake(logger, hwnd=None, **_):
        i = min(state["i"], len(queue) - 1)
        state["i"] += 1
        return queue[i]

    return _fake, state


def test_static_anchors():
    print("[1] 常量与静态锚点")
    from game_auto.runtime import wool_claim_logs as wl

    check("X 固定像素偏移 = (634,-62)（2026-11-02 实测：X(1333,349)-标题中心(699,411)）",
          wl._UPDATE_POPUP_CLOSE_PX == (634, -62), wl._UPDATE_POPUP_CLOSE_PX)
    check("CTA 文案 = 立即体验", wl._UPDATE_POPUP_CTA_TEXT == "立即体验")
    check("旧窗宽比例常量保留（兼容兑底）",
          abs(wl._UPDATE_POPUP_CLOSE_DX - 634 / 1936) < 0.01
          and abs(wl._UPDATE_POPUP_CLOSE_DY - (-62 / 1048)) < 0.01)
    src = Path(wl.__file__).read_text(encoding="utf-8")
    for anchor in ("OCR字形", "固定像素偏移", "窗宽比例外推", "立即体验",
                   "_foreground_click_block_reason(", "HWND_TOPMOST", "HWND_NOTOPMOST",
                   "_focus_window(", "_click_window_local("):
        check(f"源码锚点存在：{anchor}", anchor in src)
    check("无 hwnd → dismiss False", wl.dismiss_minimax_update_popup(_logger(), None) is False)
    check("无 hwnd → 重试点 False", wl._click_minimax_sign_button(_logger(), None) is False)


def test_dismiss_cta_fallback():
    print("[2] dismiss 行为：X 三个候选位都关不掉 → 「立即体验」兑底成功")
    from game_auto.runtime import wool_claim_logs as wl
    from game_auto.runtime import foreground_flow_runner as ffr

    fake_ocr, _ = _ocr_scenes([
        [TITLE],                    # 初始检测
        [TITLE],                    # ESC 后复核：还在
        [TITLE, GLYPH, CTA],        # × 字形扫描
        [TITLE],                    # X 候选1 后复核：还在
        [TITLE],                    # X 候选2 后复核：还在
        [TITLE],                    # X 候选3 后复核：还在
        [TITLE, CTA],               # CTA 扫描
        [],                         # CTA 点击后复核：已关闭
    ])
    clicks = []
    esc = []
    with mock.patch(f"{OCT}.ocr_window_content", fake_ocr), \
         mock.patch("time.sleep"), \
         mock.patch("pyautogui.press", side_effect=lambda k: esc.append(k)), \
         mock.patch(f"{FFR}._focus_window"), \
         mock.patch(f"{FFR}._window_rect", return_value=RECT), \
         mock.patch(f"{FFR}._click_window_local",
                    side_effect=lambda h, lx, ly, **k: clicks.append((lx, ly)) or True):
        ok = wl.dismiss_minimax_update_popup(_logger(), 1234)
    check("返回 True（弹窗已关）", ok is True)
    check("先按了 ESC", esc == ["escape"], esc)
    check("X 三个候选位都试过（OCR字形/固定像素/比例外推）", len(clicks) == 4, clicks)
    check("第一个 X 候选位 = OCR 的 × 字形位置", clicks and clicks[0] == (1325 - 100, 343 - 200), clicks[:1])
    check("最后一个点击是「立即体验」", clicks and clicks[-1] == (900 - 100, 760 - 200), clicks[-1:])
    check("所有点击都在窗口内", all(0 <= lx < RECT[2] and 0 <= ly < RECT[3] for lx, ly in clicks))


def test_dismiss_glyph_first_hit():
    print("[3] dismiss 行为：OCR × 字形一次点掉（只发一次点击）")
    from game_auto.runtime import wool_claim_logs as wl
    from game_auto.runtime import foreground_flow_runner as ffr

    fake_ocr, _ = _ocr_scenes([
        [TITLE],
        [TITLE],
        [TITLE, GLYPH, CTA],
        [],  # X 候选1 点击后已关闭
    ])
    clicks = []
    with mock.patch(f"{OCT}.ocr_window_content", fake_ocr), \
         mock.patch("time.sleep"), \
         mock.patch("pyautogui.press"), \
         mock.patch(f"{FFR}._focus_window"), \
         mock.patch(f"{FFR}._window_rect", return_value=RECT), \
         mock.patch(f"{FFR}._click_window_local",
                    side_effect=lambda h, lx, ly, **k: clicks.append((lx, ly)) or True):
        ok = wl.dismiss_minimax_update_popup(_logger(), 1234)
    check("返回 True", ok is True)
    check("只点了 × 字形一处", clicks == [(1325 - 100, 343 - 200)], clicks)


def test_dismiss_no_popup_no_click():
    print("[4] dismiss 行为：没有弹窗 → False 且零点击")
    from game_auto.runtime import wool_claim_logs as wl
    from game_auto.runtime import foreground_flow_runner as ffr

    clicks = []
    with mock.patch(f"{OCT}.ocr_window_content", return_value=[CTA]), \
         mock.patch("time.sleep"), \
         mock.patch("pyautogui.press"), \
         mock.patch(f"{FFR}._focus_window"), \
         mock.patch(f"{FFR}._window_rect", return_value=RECT), \
         mock.patch(f"{FFR}._click_window_local",
                    side_effect=lambda h, lx, ly, **k: clicks.append((lx, ly)) or True):
        ok = wl.dismiss_minimax_update_popup(_logger(), 1234)
    check("返回 False（没弹窗不打扰）", ok is False)
    check("零点击", clicks == [], clicks)


def test_sign_retry_blocked_never_clicks():
    print("[5] 自愈重试：一直被挡 → 保留 TOPMOST 重试 3 次后放弃，一个输入都不发")
    from game_auto.runtime import wool_claim_logs as wl
    from game_auto.runtime import foreground_flow_runner as ffr

    setpos = []
    guarded = []
    ocr_item = {"text": "签到得400", "confidence": 1.0,
                "center": (300, 500), "box": (260, 490, 340, 510)}
    with mock.patch(f"{OCT}.ocr_window_content", return_value=[ocr_item]), \
         mock.patch("time.sleep"), \
         mock.patch(f"{FFR}._focus_window"), \
         mock.patch(f"{FFR}._window_rect", return_value=(100, 200, 800, 600)), \
         mock.patch(f"{FFR}._foreground_click_block_reason",
                    return_value="点击坐标 (122, 880) 属于其他窗口"), \
         mock.patch(f"{FFR}._pyautogui_click_guarded",
                    side_effect=lambda x, y, **k: guarded.append((x, y))), \
         mock.patch(f"{FFR}._release_modifier_keys"), \
         mock.patch("win32gui.SetWindowPos",
                    side_effect=lambda h, after, *a, **k: setpos.append(after) or 0):
        ok = wl._click_minimax_sign_button(_logger(), 1234, "签到得")
    check("返回 False（不发点击）", ok is False)
    check("零真实点击", guarded == [], guarded)
    check("被挡后每次都尝试保留 TOPMOST（3 次）",
          setpos.count(-1) == 3, setpos)  # HWND_TOPMOST == -1
    check("收尾撤掉 TOPMOST（HWND_NOTOPMOST == -2）",
          setpos.count(-2) == 1, setpos)


def test_sign_retry_topmost_then_click():
    print("[6] 自愈重试：置顶压过遮挡窗口后可点 → 点两处并撤 TOPMOST")
    from game_auto.runtime import wool_claim_logs as wl
    from game_auto.runtime import foreground_flow_runner as ffr

    setpos = []
    guarded = []
    ocr_item = {"text": "签到得400", "confidence": 1.0,
                "center": (300, 500), "box": (260, 490, 340, 510)}
    with mock.patch(f"{OCT}.ocr_window_content", return_value=[ocr_item]), \
         mock.patch("time.sleep"), \
         mock.patch(f"{FFR}._focus_window"), \
         mock.patch(f"{FFR}._window_rect", return_value=(100, 200, 800, 600)), \
         mock.patch(f"{FFR}._foreground_click_block_reason",
                    side_effect=["被遮挡", "", ""]), \
         mock.patch(f"{FFR}._pyautogui_click_guarded",
                    side_effect=lambda x, y, **k: guarded.append((x, y))), \
         mock.patch(f"{FFR}._release_modifier_keys"), \
         mock.patch("win32gui.SetWindowPos",
                    side_effect=lambda h, after, *a, **k: setpos.append(after) or 0):
        ok = wl._click_minimax_sign_button(_logger(), 1234, "签到得")
    check("返回 True", ok is True)
    check("点击两处（文字中心 + 硬币图标位）",
          guarded == [(300, 500), (242, 500)], guarded)
    check("点完撤 TOPMOST", setpos == [-1, -2], setpos)


def test_sign_retry_out_of_window_dropped():
    print("[7] 自愈重试：坐标换算后出窗 → 丢弃整组点，不发点击也不做置顶")
    from game_auto.runtime import wool_claim_logs as wl
    from game_auto.runtime import foreground_flow_runner as ffr

    setpos = []
    guarded = []
    block_calls = []
    # center (50,250) → 窗口内 (-50,50)；box0 (20,240) → (-98,50)：两点都出窗
    ocr_item = {"text": "签到得400", "confidence": 1.0,
                "center": (50, 250), "box": (20, 240, 80, 260)}
    with mock.patch(f"{OCT}.ocr_window_content", return_value=[ocr_item]), \
         mock.patch("time.sleep"), \
         mock.patch(f"{FFR}._focus_window"), \
         mock.patch(f"{FFR}._window_rect", return_value=(100, 200, 800, 600)), \
         mock.patch(f"{FFR}._foreground_click_block_reason",
                    side_effect=lambda *a, **k: block_calls.append(1) or ""), \
         mock.patch(f"{FFR}._pyautogui_click_guarded",
                    side_effect=lambda x, y, **k: guarded.append((x, y))), \
         mock.patch("win32gui.SetWindowPos",
                    side_effect=lambda h, after, *a, **k: setpos.append(after) or 0):
        ok = wl._click_minimax_sign_button(_logger(), 1234, "签到得")
    check("返回 False", ok is False)
    check("零点击", guarded == [], guarded)
    check("没走到置顶/防护环节", not setpos and not block_calls, (setpos, block_calls))


def main():
    test_static_anchors()
    test_dismiss_cta_fallback()
    test_dismiss_glyph_first_hit()
    test_dismiss_no_popup_no_click()
    test_sign_retry_blocked_never_clicks()
    test_sign_retry_topmost_then_click()
    test_sign_retry_out_of_window_dropped()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
