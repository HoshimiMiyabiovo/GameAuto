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
) -> Tuple[bool, str]:
    logger = logger or LOGGER
    end = time.time() + max(1, int(timeout_sec))
    while time.time() < end:
        path = minimax_log_path()
        try:
            if path.is_file():
                hit = _minimax_claim(_tail(path))
                if hit:
                    logger.info("[MiniMax] 签到日志命中: %s", hit)
                    return True, "MiniMax 签到接口成功（日志）"
        except OSError as exc:
            logger.debug("[MiniMax] 读取日志失败: %s", exc)
        time.sleep(1.0)

    # 已经签到过时可能不会再次发 claim 请求，用 UI「今日已签到」文案兜底。
    hit_text = _minimax_ui_signed(logger, hwnd, ui_text)
    if hit_text:
        logger.info("[MiniMax] UI 命中签到文案: %s", hit_text)
        return True, "MiniMax UI 显示今日已签到"
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
        )
    if source == "TRAE":
        return verify_trae(logger, timeout_sec)
    if source == "WORKBUDDY":
        return False, "WorkBuddy 没有可用的本地领取日志"
    return False, f"不支持的日志核验来源: {source or '空'}"
