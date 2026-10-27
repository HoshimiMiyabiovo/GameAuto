"""自动化失败诊断记录（2026-10-18 新增）。

解决的问题：以前"步骤没匹配上 / 任务失败"只落 `logs/auto_runner.log` 的 warning
自由文本，既没有截图，也没法跨天按流程/步骤聚合统计，事后想复盘只能全文 grep 日志。

这里提供一站式失败取证：
    record_failure(logger, flow_key, step, error, hwnd=..., game_key=..., ...)
        ├─ 抓失败当刻的窗口截图 → artifacts/flow_failures/{时间}_{流程}_{步骤}.jpg
        ├─ 抓当帧 OCR 文本（可传已有结果，省一次识别开销）
        ├─ 写 workflow.sqlite3 的 flow_failures 表（结构化，可查询/聚合）
        └─ 顺手按 mtime 清理过期截图

三条铁律：
1. **永不抛异常**：诊断失败绝不能反过来搞崩正在跑的任务（调用点在失败分支里）。
2. **不重复识别**：调用方若已经有当帧 OCR 结果，务必传 `ocr_results` 复用，
   一次失败重试动辄几秒，别为了记录再花 1~2s 跑一遍 RapidOCR。
3. **写库走 WAL 的 WorkflowDB**，与 GUI 多视图并发读不互斥（见 storage/workflow_db.py）。
"""

import json
import logging
import re
import time
from typing import Any, Dict, List, Optional

from ..settings.paths import FLOW_FAILURE_DIR, FLOW_FAILURE_KEEP_DAYS, WORKFLOW_DB_PATH

# 截图压缩参数（与白荆回廊验证图一致：宽 960 + JPEG q72，单张约 40~80KB）
EVIDENCE_MAX_WIDTH = 960
EVIDENCE_JPEG_QUALITY = 72
# 默认抓取的 OCR 文本条数（够看出"当时屏幕上有什么"，又不至于把 JSON 撑爆）
DEFAULT_OCR_SAMPLE = 15
# 文件名里的非法字符
_UNSAFE_CHARS = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff._-]+")

_logger = logging.getLogger("GameAutomation.Diagnostics")
_prune_lock = None
_last_prune_ts = 0.0


def _safe_tag(text: str, limit: int = 24) -> str:
    tag = _UNSAFE_CHARS.sub("_", str(text or "").strip())
    return (tag[:limit] or "unknown").strip("_")


def capture_failure_image(logger: logging.Logger, hwnd=None, tag: str = "") -> str:
    """抓窗口截图压缩落盘，返回相对 artifacts 的文件名；失败返回空串。"""
    try:
        from PIL import Image
        from .ocr_click_tester import grab_screen

        if hwnd:
            import win32gui

            try:
                rect = win32gui.GetWindowRect(int(hwnd))
                region = tuple(rect) if rect and len(rect) == 4 else None
            except Exception:
                region = None
            if not region:
                # 目标窗口已关/最小化时**不要**退化成全屏截图：会把用户桌面存进证据目录
                _logger.debug("[诊断] 窗口 %s 取不到区域，跳过截图", hwnd)
                return ""
        else:
            region = None
        img = grab_screen(region=region) if region else grab_screen()
        w, h = img.size
        if w > EVIDENCE_MAX_WIDTH:
            img = img.resize(
                (EVIDENCE_MAX_WIDTH, max(1, int(h * EVIDENCE_MAX_WIDTH / w))), Image.LANCZOS
            )
        if img.mode != "RGB":
            img = img.convert("RGB")
        FLOW_FAILURE_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        name = f"{stamp}_{_safe_tag(tag)}.jpg"
        path = FLOW_FAILURE_DIR / name
        img.save(str(path), "JPEG", quality=EVIDENCE_JPEG_QUALITY, optimize=True)
        return name
    except Exception as exc:
        _logger.debug("[诊断] 失败截图抓取异常: %s", exc)
        return ""


def collect_ocr_sample(
    logger: logging.Logger, hwnd=None, limit: int = DEFAULT_OCR_SAMPLE
) -> List[str]:
    """取当帧 OCR 文本样本（给不出 hwnd 时返回空列表，不做全屏识别）。"""
    if not hwnd:
        return []
    try:
        from .ocr_click_tester import ocr_window_content

        results = ocr_window_content(logger, hwnd=hwnd) or []
    except Exception as exc:
        _logger.debug("[诊断] 失败取证 OCR 异常: %s", exc)
        return []
    out: List[str] = []
    for r in results[:limit]:
        try:
            text = str(r.get("text") or "").strip()
            conf = float(r.get("confidence") or 0.0)
            center = r.get("center") or (0, 0)
            out.append(f"{text}({conf:.2f})@{int(center[0])},{int(center[1])}")
        except Exception:
            continue
    return out


def _window_title(hwnd) -> str:
    if not hwnd:
        return ""
    try:
        import win32gui

        return str(win32gui.GetWindowText(int(hwnd)) or "")
    except Exception:
        return ""


def prune_old_failure_images(logger: logging.Logger, keep_days: int = FLOW_FAILURE_KEEP_DAYS) -> int:
    """按 mtime 删除过期失败截图，返回删除数量。"""
    global _last_prune_ts
    if not keep_days or keep_days <= 0:
        return 0
    # 同一进程内 10 分钟内最多清一次，避免每次失败都扫目录
    now = time.time()
    if now - _last_prune_ts < 600:
        return 0
    _last_prune_ts = now
    removed = 0
    try:
        if not FLOW_FAILURE_DIR.exists():
            return 0
        cutoff = now - keep_days * 86400
        for fp in FLOW_FAILURE_DIR.glob("*.jpg"):
            try:
                if fp.stat().st_mtime < cutoff:
                    fp.unlink()
                    removed += 1
            except Exception:
                continue
    except Exception as exc:
        _logger.debug("[诊断] 清理失败截图异常: %s", exc)
    return removed


def record_failure(
    logger: logging.Logger,
    flow_key: str,
    step: str,
    error: str,
    hwnd=None,
    game_key: str = "",
    retry_count: int = 0,
    ocr_results: Optional[List[Dict[str, Any]]] = None,
    ocr_sample: Optional[List[str]] = None,
    save_image: bool = True,
    extra: Optional[Dict[str, Any]] = None,
) -> Optional[int]:
    """记录一次失败，返回 flow_failures.id（写库失败返回 None）。绝不抛异常。

    ocr_results: 调用方已有的当帧 OCR 结果（复用，别重复识别）。
    ocr_sample : 已整理好的文本样本（优先于 ocr_results）。
    """
    try:
        sample = list(ocr_sample) if ocr_sample else None
        if sample is None:
            if ocr_results:
                sample = []
                for r in list(ocr_results)[:DEFAULT_OCR_SAMPLE]:
                    try:
                        text = str(r.get("text") or "").strip()
                        conf = float(r.get("confidence") or 0.0)
                        center = r.get("center") or (0, 0)
                        sample.append(f"{text}({conf:.2f})@{int(center[0])},{int(center[1])}")
                    except Exception:
                        continue
            else:
                sample = collect_ocr_sample(logger, hwnd=hwnd)

        image_name = ""
        if save_image and hwnd:
            image_name = capture_failure_image(logger, hwnd=hwnd, tag=f"{flow_key}_{step}")
        prune_old_failure_images(logger)

        now = time.time()
        record = {
            "ts": now,
            "date_key": time.strftime("%Y-%m-%d", time.localtime(now)),
            "flow_key": str(flow_key or ""),
            "game_key": str(game_key or ""),
            "step": str(step or ""),
            "error": str(error or "")[:1000],
            "retry_count": int(retry_count or 0),
            "hwnd": int(hwnd or 0),
            "window_title": _window_title(hwnd)[:200],
            "image_path": image_name,
            "ocr_texts": sample,
            "extra": extra or {},
        }
        from ..storage.workflow_db import WorkflowDB

        row_id = WorkflowDB(str(WORKFLOW_DB_PATH)).record_flow_failure(record)
        # 日志上报是锦上添花：即便 logger 本身炸了也不能把已写库的 id 吞掉
        try:
            if logger is not None:
                logger.warning(
                    f"[失败记录] #{row_id} {flow_key}/{step}: {str(error)[:120]} "
                    f"截图={image_name or '无'}（artifacts/flow_failures/）"
                )
        except Exception:
            pass
        return row_id
    except Exception as exc:
        try:
            if logger is not None:
                logger.debug(f"[失败记录] 记录失败（已忽略）: {exc}")
        except Exception:
            pass
        return None


def purge_old_records(logger: logging.Logger, keep_days: int = 30) -> int:
    """清理过期失败记录（截图由 prune_old_failure_images 负责）。"""
    try:
        from ..storage.workflow_db import WorkflowDB

        removed = WorkflowDB(str(WORKFLOW_DB_PATH)).purge_old_flow_failures(keep_days)
        if removed and logger is not None:
            logger.info(f"[诊断] 清理过期失败记录 {removed} 条")
        return removed
    except Exception as exc:
        _logger.debug("[诊断] 清理失败记录异常: %s", exc)
        return 0


def format_failure_report(
    summary: List[Dict[str, Any]], recent: Optional[List[Dict[str, Any]]] = None, days: int = 7
) -> str:
    """把聚合结果渲染成给人看的 markdown（tools/flow_failure_report.py 复用）。"""
    lines = [f"# 自动化失败分析（最近 {days} 天）", ""]
    if not summary:
        lines.append("最近无失败记录 ✅")
        return "\n".join(lines)
    lines.append("## 按流程/步骤聚合（次数越多越该优先优化）")
    lines.append("")
    lines.append("| 次数 | 流程 | 步骤 | 最近一次 | 证据截图 | 样例错误 |")
    lines.append("|---|---|---|---|---|---|")
    for row in summary:
        ts = row.get("last_ts") or 0
        when = time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else "-"
        err = str(row.get("sample_error") or "").replace("\n", " ")[:60]
        lines.append(
            f"| {row.get('fail_count')} | {row.get('flow_key') or '-'} | {row.get('step') or '-'} "
            f"| {when} | {'有' if row.get('has_image') else '无'} | {err} |"
        )
    if recent:
        lines += ["", "## 最近明细", ""]
        for r in recent[:15]:
            when = time.strftime("%m-%d %H:%M:%S", time.localtime(r.get("ts") or 0))
            img = r.get("image_path") or ""
            img_md = f"artifacts/flow_failures/{img}" if img else "-"
            texts = (r.get("ocr_texts") or [])[:6]
            lines.append(
                f"- `{when}` **{r.get('flow_key')}** / {r.get('step')}: "
                f"{str(r.get('error') or '')[:100]}\n  - 截图: {img_md}"
            )
            if texts:
                lines.append("  - 当帧文字: " + " ｜ ".join(texts))
    return "\n".join(lines)


def _json_default(obj: Any) -> Any:  # pragma: no cover - 报告脚本用
    if isinstance(obj, (bytes, bytearray)):
        return obj.decode("utf-8", "replace")
    return str(obj)


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=_json_default)