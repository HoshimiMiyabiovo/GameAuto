"""MiniMax / TRAE 薅羊毛领取日志核验（2026-10-24）。

日志证据（本机实测）：
- MiniMax: ``%APPDATA%/MiniMax/logs/api-MM-DD.log``
  ``POST .../minimax-cloud/api/v1/signin/claim`` → status 200 + baseRespStatusCode 0
- TRAE: ``%APPDATA%/Trae CN/logs/<YYYYMMDD...>/main.log``
  ``ttnet fetch response .../checkin_credits/claim 200 OK``

WorkBuddy 没有可用的本地领取日志（logs/main.log 停在 2026-09-03，
debug.log 只有 crashpad 错误），仍用 UI「今日已领取」非 optional 确认。
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

from ..settings.paths import BASE_DIR

LOGGER = logging.getLogger("GameAutomation")


def _appdata() -> Path:
    root = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(root)


def _tail(path: Path, max_bytes: int = 2_000_000) -> str:
    with path.open("rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - max_bytes))
        return f.read().decode("utf-8", "ignore")


def _abs_template(path: str) -> str:
    if not path:
        return ""
    if os.path.isabs(path):
        return path
    return str(BASE_DIR / path)


def minimax_log_path() -> Path:
    return _appdata() / "MiniMax" / "logs" / f"api-{datetime.now():%m-%d}.log"


def _minimax_claim(text: str) -> Optional[str]:
    for line in reversed(text.splitlines()):
        if (
            "signin/claim" in line
            and '"status":"200' in line
            and '"baseRespStatusCode":0' in line
        ):
            return line[:240]
    return None


def _minimax_opened_card(text: str) -> bool:
    """日志里有没有签到卡片的 config/status 请求（= 卡片确实被打开过）。"""
    return "signin/status" in text or "signin/config" in text


# 「MiniMax Code 更新已来」公告弹窗（服务端灰度下发，2026-11-02 实测）：
#   - 标题文本框 (590,398)-(808,424) @1936x1048 全屏抓图，conf 0.85
#   - 关闭 X 按钮在 (1333,349) → 相对标题中心固定像素偏移 (+634,-62)
#   - 2026-11-26 #513 复盘：弹窗是固定像素尺寸的模态卡；旧逻辑把偏移按**窗宽比例**
#     外推，窗口非最大化时 X 估算差 ~90px 点到弹窗空白处（实测：算出 (1234,341)，
#     OCR 的 × 字形在 ≈(1325,343)，点了个寂寞）。X 必须以标题中心 + 固定像素偏移为主。
#   - 弹窗底部通栏按钮「立即体验」：OCR 可定位，X 关不掉时的兑底出口。
#   - 版本全天 3.1.6 没变 → 是**公告**不是本地更新；本地也没有"已读"标记
#     （%APPDATA%/MiniMax/minimax-agent-cn-config.json 里没有相关键）
#     → **每次启动都可能再弹**，自动化必须自己关，不能指望用户手动关。
_UPDATE_POPUP_TEXTS = ("更新已来", "本次更新", "立即体验")
_UPDATE_POPUP_TITLE = "更新已来"
_UPDATE_POPUP_CLOSE_DX = 0.327   # X 按钮相对标题中心的水平偏移（窗宽比例，旧兑底）
_UPDATE_POPUP_CLOSE_DY = -0.059  # 垂直偏移（窗高比例，旧兑底）
_UPDATE_POPUP_CLOSE_PX = (634, -62)  # X 相对标题中心的固定像素偏移（2026-11-02 实测，主判据）
_UPDATE_POPUP_CTA_TEXT = "立即体验"
# 弹窗右上角 ×/X 的 OCR 字形（置信度低 ~0.55，只能当候选位，不能当唯一判据）
_UPDATE_POPUP_GLYPHS = ("×", "x", "X", "✕", "ⅹ", "Ⅹ")


def dismiss_minimax_update_popup(
    logger: Optional[logging.Logger],
    hwnd: Optional[int],
) -> bool:
    """检测到「更新已来」公告弹窗就关掉它（ESC → 点 X → 点「立即体验」）。

    2026-11-02 实锤：那一次自动化点「签到得」坐标完全正确（OCR 框正中），
    但应用**没有发出任何 signin/claim**；同一时刻屏幕上就摆着这个公告弹窗，
    而 17:08 用户手动点同一个按钮 → 成功发出 claim。
    公告是服务端灰度下发的（版本没变），本地无"已读"标记 → 每次启动都可能再弹。

    2026-11-26 #513 加固：
      - 新版弹窗点 X 没反应 → 旧偏移按窗宽比例外推，窗口非最大化时差 ~90px
        点到弹窗空白处；改为**标题中心+固定像素偏移**为主，OCR 找弹窗右上角
        ×/X 字形与旧比例外推做备选，逐个候选位点击、每次点击后复核弹窗真的消失；
      - 新增兑底出口「立即体验」（弹窗底部通栏 CTA，OCR 定位）；
      - 动手前先把焦点给 MiniMax：wool 批量并行时 qoder 等窗口可能压在上面，
        ESC 会发进别的程序、点击会落在遮挡窗口上（当晚实测两次都如此）。
    """
    if not hwnd:
        return False
    try:
        import pyautogui

        from .foreground_flow_runner import _click_window_local, _focus_window, _window_rect
        from .ocr_click_tester import _normalize_text, ocr_window_content

        def _find_title():
            results = ocr_window_content(logger, hwnd=hwnd) or []
            for item in results:
                text = _normalize_text(str(item.get("text") or ""))
                if _UPDATE_POPUP_TITLE in text and item.get("center"):
                    return item
            return None

        title = _find_title()
        if not title:
            return False  # 没有弹窗 → 不打扰

        logger.warning("[MiniMax] 检测到「%s」公告弹窗，先关掉再签到", _UPDATE_POPUP_TITLE)
        # 焦点先给 MiniMax：ESC/后续点击都要落进目标窗口
        _focus_window(hwnd)
        time.sleep(0.4)

        # ① 先试 ESC（Electron 模态卡多数支持）
        try:
            pyautogui.press("escape")
            time.sleep(1.0)
        except Exception as exc:
            logger.debug("[MiniMax] ESC 关弹窗异常: %s", exc)
        if not _find_title():
            logger.info("[MiniMax] ESC 已关闭公告弹窗")
            return True

        # ② 还在 → 逐个候选位点右上角 X，每点一次都复核弹窗是否真的消失
        rect = _window_rect(hwnd)
        if not rect:
            return False
        left, top, win_w, win_h = rect
        tx, ty = int(title["center"][0]), int(title["center"][1])

        def _in_window(lx: int, ly: int) -> bool:
            return 0 <= lx < win_w and 0 <= ly < win_h

        candidates = []  # (来源标签, 屏幕坐标)
        for item in (ocr_window_content(logger, hwnd=hwnd) or []):
            glyph = str(item.get("text") or "").strip()
            center = item.get("center")
            if not center or glyph not in _UPDATE_POPUP_GLYPHS:
                continue
            # 只认弹窗右上角区域：标题右侧 250px 外、竖直 ±90px 内；
            # 窗口标题栏的关闭 ×（竖直差 ~300px）必须排除
            dx_glyph, dy_glyph = center[0] - tx, center[1] - ty
            if dx_glyph >= 250 and abs(dy_glyph) <= 90 and float(item.get("confidence") or 0) >= 0.4:
                candidates.append(("OCR字形", (int(center[0]), int(center[1]))))
        candidates.append(("固定像素偏移", (tx + _UPDATE_POPUP_CLOSE_PX[0], ty + _UPDATE_POPUP_CLOSE_PX[1])))
        candidates.append(("窗宽比例外推", (int(tx + _UPDATE_POPUP_CLOSE_DX * win_w), int(ty + _UPDATE_POPUP_CLOSE_DY * win_h))))
        for label, (sx, sy) in candidates:
            lx, ly = sx - left, sy - top
            if not _in_window(lx, ly):
                continue
            if not _click_window_local(hwnd, lx, ly):
                logger.debug("[MiniMax] 点 X(%s) 被点击防护拦下，试下一候选位", label)
                continue
            logger.info("[MiniMax] 已点击公告弹窗关闭 X(%s) @ 窗口内 (%s,%s)", label, lx, ly)
            time.sleep(1.0)
            if not _find_title():
                logger.info("[MiniMax] 公告弹窗已关闭（%s）", label)
                return True
            logger.debug("[MiniMax] 点 X(%s) 后弹窗仍在，试下一候选位", label)

        # ③ X 全部无效 → 点「立即体验」（弹窗底部通栏 CTA，OCR 定位最高置信度）
        cta = None
        for item in (ocr_window_content(logger, hwnd=hwnd) or []):
            text = _normalize_text(str(item.get("text") or ""))
            center = item.get("center")
            if not center or _UPDATE_POPUP_CTA_TEXT not in text:
                continue
            if cta is None or float(item.get("confidence") or 0) > float(cta.get("confidence") or 0):
                cta = item
        if cta and cta.get("center"):
            lx, ly = int(cta["center"][0]) - left, int(cta["center"][1]) - top
            if _in_window(lx, ly) and _click_window_local(hwnd, lx, ly):
                logger.info("[MiniMax] 已点击「立即体验」@ 窗口内 (%s,%s)", lx, ly)
                time.sleep(1.5)
                if not _find_title():
                    logger.info("[MiniMax] 公告弹窗已关闭（立即体验）")
                    return True

        logger.warning("[MiniMax] 公告弹窗仍未关闭（X/立即体验均无效）")
        return False
    except Exception as exc:
        logger.debug("[MiniMax] 关公告弹窗失败: %s", exc)
        return False


def _click_minimax_sign_button(
    logger: Optional[logging.Logger],
    hwnd: Optional[int],
    target_text: str = "签到得",
) -> bool:
    """重新定位并点击「签到得」按钮（核验失败时的自愈重试）。

    2026-11-02 实锤：步骤4 的点击坐标落在按钮上（OCR 命中「签到得 ⊙ 400」，
    点击 (182,958)），但应用没发出 signin/claim —— api 日志里只有
    signin/config + signin/status 两个 GET，**没有任何 claim POST**。
    也就是说「点了但没生效」，不是「日志没落盘」。这种时候再点一次是有效的。

    点击打两个位置：文字中心 + 文字左侧 ~18px（硬币图标位置），
    避免"文字是标签、真正的按钮是左边图标"时点了没反应。

    2026-11-26 #513 加固：
      - wool 批量并行时 qoder 窗口压住签到按钮区域，原「聚焦→立刻点」被点击
        防护拒绝（前台校验过了、点击点归属却是别的窗口——遮挡窗口浮在目标之上，
        `_focus_window` 的 TOPMOST→NOTOPMOST 舞步压不过同样置顶的窗口），
        两次点击全被拦下、根本没发出去。现在改成**强制置顶闭环**：
        聚焦 → 校验可点性 → 仍被挡就保留 TOPMOST 压住遮挡窗口再校验 →
        通过才发点击，点完（或放弃）立刻撤掉 TOPMOST 还别人窗口；
      - OCR 与点击之间窗口可能移动，换算后落在窗口外的坐标直接丢弃
        （当晚实测出现窗口内坐标 (-130,814) 的无效点击）。
    """
    if not hwnd or not target_text:
        return False
    try:
        import win32con
        import win32gui

        from .foreground_flow_runner import (
            _focus_window,
            _foreground_click_block_reason,
            _pyautogui_click_guarded,
            _release_modifier_keys,
            _window_rect,
        )
        from .ocr_click_tester import _normalize_text, ocr_window_content

        results = ocr_window_content(logger, hwnd=hwnd) or []
        want = _normalize_text(target_text)
        best = None
        for item in results:
            text = _normalize_text(str(item.get("text") or ""))
            if not want or want not in text:
                continue
            if best is None or float(item.get("confidence") or 0) > float(best.get("confidence") or 0):
                best = item
        if not best or not best.get("center"):
            return False
        rect = _window_rect(hwnd)
        if not rect:
            return False
        left, top, win_w, win_h = rect
        box = best.get("box") or ()
        center = best["center"]
        # 窗口内坐标；出窗的点丢弃（窗口移动/遮挡时 OCR 坐标可能是陈旧帧的）
        points = [(int(center[0]) - left, int(center[1]) - top)]
        if len(box) == 4:
            points.append((int(box[0]) - left - 18, int(center[1]) - top))  # 硬币图标候选位
        points = [(lx, ly) for lx, ly in points if 0 <= lx < win_w and 0 <= ly < win_h]
        if not points:
            logger.warning("[MiniMax] 「%s」坐标换算后不在窗口内，放弃点击", target_text)
            return False
        # 强制置顶闭环：确认窗口在前台且点击点不被别的窗口压住后才发点击。
        first_sx, first_sy = left + points[0][0], top + points[0][1]
        reason = ""
        topmost_held = False
        try:
            for attempt in range(3):
                if attempt == 0:
                    _focus_window(hwnd)
                    time.sleep(0.5)
                reason = _foreground_click_block_reason(hwnd, first_sx, first_sy)
                if not reason:
                    break
                # 还被挡：保留 TOPMOST（不走 _focus_window 的 TOPMOST→NOTOPMOST
                # 舞步），压住同样置顶/浮在上面的遮挡窗口，再校验一次
                try:
                    win32gui.SetWindowPos(
                        hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                        win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE,
                    )
                    topmost_held = True
                except Exception as exc:
                    logger.debug("[MiniMax] 保留 TOPMOST 失败: %s", exc)
                time.sleep(0.3)
            if reason:
                logger.warning(
                    "[MiniMax] 重试点「%s」前无法让窗口可点（%s），不发点击",
                    target_text, reason,
                )
                return False
            _release_modifier_keys()
            for lx, ly in points:
                _pyautogui_click_guarded(left + lx, top + ly)
                logger.info("[MiniMax] 点击「%s」窗口内 (%s,%s)", target_text, lx, ly)
                time.sleep(0.8)
            return True
        finally:
            if topmost_held:
                try:
                    win32gui.SetWindowPos(
                        hwnd, win32con.HWND_NOTOPMOST, 0, 0, 0, 0,
                        win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE,
                    )
                except Exception:
                    pass
    except Exception as exc:
        logger.debug("[MiniMax] 重试点签到按钮失败: %s", exc)
        return False


def _minimax_ui_signed(
    logger: Optional[logging.Logger],
    hwnd: Optional[int],
    ui_text: str,
) -> Optional[str]:
    """UI 兜底：OCR 找「今日已签到」文案，返回命中文本。

    这里刻意**不用模板**：模板 minimax-今日已签到.png 是"灰底圆角按钮"，与未签到时
    黑底的「签到得 ⊙ 400」按钮在 TM_CCOEFF_NORMED 下高度相似（2026-10-26 实测
    在未签到截图上 conf=0.9477，兜底阈值仅 0.88 → 必然假阳性，把"没签"判成"已签"）。
    OCR 文案匹配无歧义，也不受按钮配色/字号变化影响。
    """
    if not hwnd or not ui_text:
        return None
    try:
        from .ocr_click_tester import ocr_window_content

        results = ocr_window_content(logger, hwnd=hwnd) or []
    except Exception:
        logger.debug("[MiniMax] UI OCR 兜底失败", exc_info=True)
        return None
    want = str(ui_text).replace(" ", "")
    for item in results:
        text = str(item.get("text") or "").replace(" ", "")
        if want and want in text:
            return text
    return None


def verify_minimax(
    logger: Optional[logging.Logger] = None,
    hwnd: Optional[int] = None,
    scales=None,
    confidences=None,
    ui_template: str = "",
    timeout_sec: int = 30,
    ui_text: str = "今日已签到",
    retry_click_text: str = "签到得",
) -> Tuple[bool, str]:
    logger = logger or LOGGER
    start = time.time()
    end = start + max(1, int(timeout_sec))
    retried = False
    tail_text = ""
    while time.time() < end:
        path = minimax_log_path()
        try:
            if path.is_file():
                tail_text = _tail(path)
                hit = _minimax_claim(tail_text)
                if hit:
                    logger.info("[MiniMax] 签到日志命中: %s", hit)
                    return True, "MiniMax 签到接口成功（日志）"
        except OSError as exc:
            logger.debug("[MiniMax] 读取日志失败: %s", exc)
        # 自愈：过了一半时间还没 claim，且按钮还在界面上 → 说明上次点击没被
        # 应用接收，再点一次（2026-11-02 实测这种"点了没生效"确实会发生：
        # 坐标完全正确、应用却一个 signin/claim 都没发）。
        if (
            not retried
            and retry_click_text
            and (time.time() - start) >= max(5, int(timeout_sec)) / 2
        ):
            retried = True
            # 先关掉可能挡住签到浮层的「更新已来」公告弹窗（服务端灰度下发，每次启动都可能再弹）
            dismiss_minimax_update_popup(logger, hwnd)
            if _click_minimax_sign_button(logger, hwnd, retry_click_text):
                # 重新点完再等一轮，别立刻返回失败
                end = max(end, time.time() + max(5, int(timeout_sec)) // 2)
        time.sleep(1.0)

    # 已经签到过时可能不会再次发 claim 请求，用 UI「今日已签到」文案兜底。
    hit_text = _minimax_ui_signed(logger, hwnd, ui_text)
    if hit_text:
        logger.info("[MiniMax] UI 命中签到文案: %s", hit_text)
        return True, "MiniMax UI 显示今日已签到"
    if tail_text and _minimax_opened_card(tail_text):
        return False, (
            "MiniMax 签到卡片已打开（api 日志有 signin/status），但始终没有 signin/claim"
            + ("；已自动重试点击一次仍未生效" if retried else "")
            + "——点击没被应用接收（可能被更新弹窗/失焦吞掉）"
        )
    return False, "MiniMax 今日日志与 UI 均未确认签到成功"


def trae_log_roots():
    root = _appdata()
    return [root / "Trae CN" / "logs", root / "TRAE SOLO CN" / "logs"]


def _trae_claim(text: str) -> Optional[str]:
    for line in text.splitlines():
        if "checkin_credits/claim" in line and "200 OK" in line:
            return line[:240]
    return None


def verify_trae(
    logger: Optional[logging.Logger] = None,
    timeout_sec: int = 30,
) -> Tuple[bool, str]:
    logger = logger or LOGGER
    today = datetime.now().strftime("%Y%m%d")
    end = time.time() + max(1, int(timeout_sec))
    while time.time() < end:
        for root in trae_log_roots():
            if not root.is_dir():
                continue
            for session in sorted(root.glob(f"{today}*")):
                log = session / "main.log"
                try:
                    if log.is_file():
                        hit = _trae_claim(_tail(log))
                        if hit:
                            logger.info("[TRAE] 签到日志命中: %s", hit)
                            return True, "TRAE 签到接口成功（日志）"
                except OSError:
                    continue
        time.sleep(1.0)
    return False, "TRAE 今日日志未找到 checkin_credits/claim 200 OK"


def verify(
    logger: Optional[logging.Logger] = None,
    source: str = "",
    hwnd: Optional[int] = None,
    scales=None,
    confidences=None,
    step: Optional[dict] = None,
    timeout_sec: int = 30,
) -> Tuple[bool, str]:
    source = str(source or "").upper()
    step = step or {}
    if source == "MINIMAX":
        return verify_minimax(
            logger,
            hwnd,
            scales,
            confidences,
            str(step.get("ui_template") or ""),
            timeout_sec,
            str(step.get("ui_text") or "今日已签到"),
            str(step.get("retry_click_text") or "签到得"),
        )
    if source == "TRAE":
        return verify_trae(logger, timeout_sec)
    if source == "WORKBUDDY":
        return False, "WorkBuddy 没有可用的本地领取日志"
    return False, f"不支持的日志核验来源: {source or '空'}"
