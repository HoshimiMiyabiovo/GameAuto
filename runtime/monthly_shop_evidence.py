"""每月商店采购的截图证据（2026-11-01 用户要求）。

为什么单独一套：月商店改成「页面上看不到商品 = 本月已买过」之后，光看日志没法复核
当时页面到底什么样——是商品真买完了，还是模板/OCR 失灵。于是采购前、采购后各抓一张
窗口截图落盘，并在 index.jsonl 追加一条索引，事后按时间/流程就能翻出来对。

沿用 flow_diagnostics 的三条约束：
1. **永不抛异常**：截图失败绝不能反过来影响采购本身（调用点在正常业务流程里）；
2. **窗口取不到区域时不退化成全屏截图**：否则会把用户桌面存进证据目录；
3. **压到宽 960 / JPEG q72**：单张 40~80KB，和失败取证、白荆验证图同一规格。

落盘位置：``data/artifacts/monthly_shop/YYYYMMDD_HHMMSS_<流程>_<阶段>.jpg``
长期保留，**不参与** flow_failures 的按天清理（每月只跑一次，占不了多少空间）。
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Dict, Optional

from ..settings.paths import BASE_DIR, MONTHLY_SHOP_EVIDENCE_DIR

LOGGER = logging.getLogger("GameAutomation.MonthlyShop")

EVIDENCE_MAX_WIDTH = 960
EVIDENCE_JPEG_QUALITY = 72
INDEX_FILENAME = "index.jsonl"

_UNSAFE_CHARS = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff._-]+")


def _safe_tag(text: str, limit: int = 20) -> str:
    """文件名安全片段（保留中文，去掉斜杠空格等）。"""
    tag = _UNSAFE_CHARS.sub("_", str(text or "").strip())
    return (tag[:limit] or "unknown").strip("_")


def _window_region(hwnd) -> Optional[tuple]:
    """取窗口区域，返回 (left, top, width, height)；拿不到时 None。

    注意 `grab_screen` 跟 pyautogui 一样吃 **(left, top, width, height)**，
    不是 win32 的 (left, top, right, bottom)——这里换算好再传，别把 right/bottom
    当宽高，那样 WGC 分支 `crop(left, top, left+w, top+h)` 会截偏。
    """
    if not hwnd:
        return None
    try:
        import win32gui

        rect = win32gui.GetWindowRect(int(hwnd))
        if rect and len(rect) == 4:
            left, top, right, bottom = rect
            if right > left and bottom > top:
                return (left, top, right - left, bottom - top)
    except Exception as exc:  # noqa: BLE001 - 窗口没了就跳过，不做全屏兜底
        LOGGER.debug("[月商店证据] 取窗口区域失败(hwnd=%s): %s", hwnd, exc)
    return None


def _append_index(entry: Dict[str, Any]) -> None:
    """追加一行索引；写失败只记 debug，不影响截图已经落盘这件事。"""
    try:
        MONTHLY_SHOP_EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
        path = MONTHLY_SHOP_EVIDENCE_DIR / INDEX_FILENAME
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001 - 索引只是便利，失败不影响主流程
        LOGGER.debug("[月商店证据] 写索引失败: %s", exc)


def capture_shop_evidence(
    logger: logging.Logger,
    hwnd,
    *,
    flow_label: str,
    stage: str,
    summary: str = "",
) -> str:
    """抓一张采购证据图，返回相对项目根目录的路径；失败返回空串。

    flow_label: 流程中文名（余烬兑换 / 信源兑换 / 后勤商店）
    stage: 阶段（采购前 / 采购后），直接拼进文件名
    summary: 采购摘要（写进 index.jsonl，便于不打开图片就知道买了几件）
    """
    try:
        from PIL import Image

        from .ocr_click_tester import grab_screen

        region = _window_region(hwnd)
        if region is None:
            # 窗口已关闭或是无 hwnd 的调用：宁可不留证据，也不截用户桌面
            logger.debug("[%s] 取不到游戏窗口，跳过%s截图", flow_label, stage)
            return ""

        img = grab_screen(region=region)
        width, height = img.size
        if width > EVIDENCE_MAX_WIDTH:
            img = img.resize(
                (EVIDENCE_MAX_WIDTH, max(1, int(height * EVIDENCE_MAX_WIDTH / width))),
                Image.LANCZOS,
            )
        if img.mode != "RGB":
            img = img.convert("RGB")

        MONTHLY_SHOP_EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        name = f"{stamp}_{_safe_tag(flow_label)}_{_safe_tag(stage)}.jpg"
        path = MONTHLY_SHOP_EVIDENCE_DIR / name
        img.save(str(path), "JPEG", quality=EVIDENCE_JPEG_QUALITY, optimize=True)

        rel_path = path.relative_to(BASE_DIR).as_posix()
        logger.info("[%s] 已留%s截图: %s", flow_label, stage, rel_path)
        _append_index({
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "flow": flow_label,
            "stage": stage,
            "file": rel_path,
            "summary": summary,
        })
        return rel_path
    except Exception as exc:  # noqa: BLE001 - 证据截图失败不能影响采购
        logger.debug("[%s] %s截图失败: %s", flow_label, stage, exc)
        return ""
