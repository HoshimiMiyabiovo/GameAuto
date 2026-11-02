"""Qoder 每日 Credits 领取状态核验（2026-10-24）。

只读 Qoder 自身的 ``main.log``，解析 ``[Campaign]`` 活动状态：
  claimable=true / claimStatus=CLAIMABLE  → 今日仍未领取
  claimable=false / claimStatus=CLAIMED   → 已领取

UI 模板/OCR 会在「活动弹窗没有自动出现」时全部空跑，不能用来报成功；
这里以 Qoder 日志为唯一真相，拿不到明确状态就 fail-closed。
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

from ..settings.paths import TEMPLATES_DIR

LOGGER = logging.getLogger("GameAutomation")

CLAIM_TEMPLATE = str(TEMPLATES_DIR / "qoder-领取.png")
CLAIMED_TEMPLATE = str(TEMPLATES_DIR / "qoder-已领取.png")
CENTER_TEMPLATE = str(TEMPLATES_DIR / "qoder-权益中心.png")
MENU_SCALES = [1.0, 1.25, 0.8, 1.5, 0.67]
MENU_CONFIDENCES = [0.90, 0.86, 0.82]


def logs_root() -> Path:
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(appdata) / "com.qoder.app.stable" / "logs"


def latest_main_log() -> Optional[Path]:
    root = logs_root()
    if not root.is_dir():
        return None
    candidates = []
    for session in root.iterdir():
        if not session.is_dir():
            continue
        log = session / "main.log"
        try:
            if log.is_file():
                candidates.append((log.stat().st_mtime, log))
        except OSError:
            continue
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def _tail(path: Path, max_bytes: int = 2_000_000) -> str:
    with path.open("rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - max_bytes))
        return f.read().decode("utf-8", "ignore")


def parse_state(text: str) -> Tuple[Optional[bool], str]:
    """返回 (claimed, message)；claimed=None 表示日志里没有可判断的状态。"""
    relevant = (
        "活动状态请求返回",
        "活动状态响应解析完成",
        "通知状态已更新",
    )
    last = None
    for line in reversed(text.splitlines()):
        if any(marker in line for marker in relevant):
            last = line
            break
    if not last:
        return None, "Qoder 日志中没有活动状态记录"

    # 活动状态请求返回里同时有多个 campaign，只认 CLAIM_BENEFIT
    idx = last.find('"actionType":"CLAIM_BENEFIT"')
    if idx >= 0:
        match = re.search(r'"claimStatus":"(CLAIMABLE|CLAIMED)"', last[idx:])
        if match:
            status = match.group(1)
            return status == "CLAIMED", f"Qoder 活动状态：{status}"

    match = re.search(r'"claimable":(true|false)', last)
    if match:
        claimable = match.group(1) == "true"
        return not claimable, f"Qoder 活动状态：claimable={match.group(1)}"

    match = re.search(r'"hasClaimableCampaign":(true|false)', last)
    if match:
        claimable = match.group(1) == "true"
        return not claimable, f"Qoder 活动状态：hasClaimableCampaign={match.group(1)}"

    return None, "Qoder 日志活动状态无法解析"


def _window_rect(hwnd: int):
    from .foreground_flow_runner import _window_rect as impl
    return impl(hwnd)


def _click(center) -> None:
    from .foreground_flow_runner import _click_screen
    _click_screen(int(center[0]), int(center[1]))


def _claim_button_visible(hwnd: int, scales, confidences) -> bool:
    from .foreground_flow_runner import _locate_template_in_window
    return bool(
        _locate_template_in_window(hwnd, CLAIM_TEMPLATE, scales, confidences)
        or _locate_template_in_window(hwnd, CLAIMED_TEMPLATE, scales, confidences)
    )


def _menu_center(hwnd: int, scales):
    from .foreground_flow_runner import _locate_template_on_screen
    rect = _window_rect(hwnd)
    if not rect:
        return None
    left, top, width, height = rect
    region = (
        left,
        top + int(height * 0.55),
        max(1, int(width * 0.40)),
        max(1, int(height * 0.45)),
    )
    return _locate_template_on_screen(CENTER_TEMPLATE, region, scales, MENU_CONFIDENCES)


def _ensure_visible(hwnd: int) -> Tuple[bool, str]:
    from .foreground_flow_runner import _focus_window
    from .ocr_click_tester import _window_obscured_ratio

    _focus_window(hwnd)
    time.sleep(0.8)
    try:
        ratio = _window_obscured_ratio(hwnd)
    except Exception:
        ratio = None
    if ratio is not None and ratio >= 0.3:
        _focus_window(hwnd)
        time.sleep(0.8)
        try:
            ratio = _window_obscured_ratio(hwnd)
        except Exception:
            ratio = None
    if ratio is not None and ratio >= 0.3:
        return False, f"Qoder 窗口被其他窗口遮挡约 {ratio:.0%}"
    return True, ""


def open_campaign(logger: Optional[logging.Logger] = None, hwnd: int = 0,
                  scales=None, confidences=None,
                  timeout_sec: int = 20,
                  stop_check=None) -> Tuple[bool, str]:
    """打开 Qoder「权益中心」活动页：左下角头像 → 菜单「权益中心」。

    只认模板（领取/已领取按钮、权益中心菜单行），不再用泛化的「领取」
    OCR——聊天正文里出现「领取」会被误判成弹窗已打开。
    """
    logger = logger or LOGGER
    scales = scales or [1.0, 1.25, 0.8, 1.5, 0.67]
    confidences = confidences or [0.97, 0.96, 0.95]
    rect = _window_rect(hwnd)
    if not rect:
        return False, "未找到 Qoder 窗口"
    if not os.path.isfile(CENTER_TEMPLATE):
        return False, f"权益中心菜单模板不存在: {CENTER_TEMPLATE}"

    def stopped() -> bool:
        return bool(stop_check and stop_check())

    if _claim_button_visible(hwnd, scales, confidences):
        return True, "活动页已打开"

    visible, reason = _ensure_visible(hwnd)
    if not visible:
        return False, reason

    left, top, width, height = rect
    avatar_center = (left + 20, top + height - 25)

    for attempt in range(2):
        if stopped():
            return False, "用户请求停止"
        _click(avatar_center)
        time.sleep(0.8)

        menu_center = None
        end = time.time() + max(1, timeout_sec)
        while time.time() < end:
            if stopped():
                return False, "用户请求停止"
            menu_center = _menu_center(hwnd, scales)
            if menu_center:
                break
            time.sleep(0.5)
        if not menu_center:
            if attempt == 0:
                continue
            return False, "头像菜单中未找到「权益中心」"

        _click(menu_center)
        logger.info("[Qoder] 已点击头像菜单「权益中心」")

        end = time.time() + max(1, timeout_sec)
        while time.time() < end:
            if stopped():
                return False, "用户请求停止"
            if _claim_button_visible(hwnd, scales, confidences):
                return True, "已打开权益中心"
            time.sleep(1.0)
        return False, "点击权益中心后未检测到活动页"

    return False, "打开权益中心失败"


def verify_claim(logger: Optional[logging.Logger] = None, timeout_sec: int = 60) -> Tuple[bool, str]:
    """轮询 Qoder 日志直到确认已领取；超时仍未领取则返回失败。"""
    logger = logger or LOGGER
    end = time.time() + max(1, int(timeout_sec))
    last_message = ""
    while time.time() < end:
        path = latest_main_log()
        if path is not None:
            try:
                if datetime.fromtimestamp(path.stat().st_mtime).date() != datetime.now().date():
                    last_message = "Qoder 今日没有新的活动日志"
                else:
                    claimed, message = parse_state(_tail(path))
                    last_message = message
                    if claimed is True:
                        logger.info("[Qoder] 领取状态核验通过: %s", message)
                        return True, message
            except OSError as exc:
                last_message = f"读取 Qoder 日志失败: {exc}"
        time.sleep(2.0)
    if last_message:
        return False, f"未确认领取成功（{last_message}）"
    return False, "未找到 Qoder 活动日志，无法确认领取状态"
