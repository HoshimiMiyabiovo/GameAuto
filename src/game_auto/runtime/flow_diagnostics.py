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
from typing import Any, Dict, List, Optional, Tuple

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


def _load_font(size: int):
    """加载支持中文的字体（找不到就退回 PIL 默认字体，绝不抛异常）。"""
    try:
        from PIL import ImageFont
    except Exception:
        return None
    for path in (
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\msyhbd.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
        r"C:\Windows\Fonts\simsun.ttc",
        "/System/Library/Fonts/PingFang.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    ):
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    try:
        from PIL import ImageFont

        return ImageFont.load_default()
    except Exception:
        return None


def _annotate_ocr_overlay(img, results: List[Dict[str, Any]], title: str, scale: float = 1.0):
    """把 OCR 框画在截图上、编号+图例放在下方扩展区，返回新图（失败返回 None）。

    2026-11-03 加：失败证据只有一张干净截图时，事后还得对着 ocr_texts 的
    坐标脑补“哪个框是哪个字”。这里直接把识别框画上去。

    为什么不把文字标签直接贴在框上（第一版就是那样，验收不过）：
    游戏界面文字密集，标签必然和原生文字/相邻标签糊在一起，还会跑出画面。
    改成“框内标号 + 图下方图例列表”，框不遮挡正文，图例可查全文和置信度。
    坐标按 scale 缩放到已压缩的截图尺寸（OCR 结果是原始窗口分辨率）。
    """
    try:
        from PIL import Image, ImageDraw

        base = img.convert("RGBA")
        draw = ImageDraw.Draw(base, "RGBA")
        small = _load_font(max(10, int(base.width * 0.0125)))
        badge_font = _load_font(max(9, int(base.width * 0.011)))

        items: List[Tuple[int, str, float]] = []
        for idx, r in enumerate(results or [], 1):
            try:
                text = str(r.get("text") or "").strip()
                conf = float(r.get("confidence") or 0.0)
                box = r.get("box")
                if box and len(box) >= 4:
                    x1, y1, x2, y2 = (int(float(v) * scale) for v in list(box)[:4])
                else:
                    center = r.get("center") or (0, 0)
                    cx, cy = int(float(center[0]) * scale), int(float(center[1]) * scale)
                    x1, y1, x2, y2 = cx - 4, cy - 4, cx + 4, cy + 4
                if x2 <= x1 or y2 <= y1:
                    continue
                draw.rectangle((x1, y1, x2, y2), outline=(0, 200, 255), width=2)
                # 框左上角小徽标写序号：不压住框内文字，也不占用画面其它区域
                num = str(idx)
                bw = 8 + len(num) * max(5, int(base.width * 0.006))
                bx = max(0, min(x1, base.width - bw))
                by = max(0, y1 - 14)
                draw.rectangle((bx, by, bx + bw, by + 13), fill=(0, 0, 0, 205))
                if badge_font:
                    draw.text((bx + 3, by + 1), num, fill=(0, 255, 180), font=badge_font)
                items.append((idx, text, conf))
            except Exception:
                continue

        if not items:
            return base.convert("RGB")
        # 图例区贴在原图下方（不遮挡游戏画面），每行两条
        cols = 2
        rows = (len(items) + cols - 1) // cols
        line_h = max(13, int(base.width * 0.017))
        title_h = max(16, int(base.width * 0.019))
        legend_h = 4 + title_h + rows * line_h + 4
        canvas = Image.new("RGBA", (base.width, base.height + legend_h), (18, 18, 22, 255))
        canvas.paste(base, (0, 0))
        ld = ImageDraw.Draw(canvas)
        head_font = _load_font(max(11, int(base.width * 0.0135)))
        if head_font:
            # 标题画在图例区顶部（base.height + 4），别压到游戏画面上
            ld.text((6, base.height + 4), title[:120], fill=(235, 240, 245), font=head_font)
        if small:
            col_w = base.width // cols
            for i, (idx, text, conf) in enumerate(items):
                c, r = divmod(i, rows)
                x = 8 + c * col_w
                y = base.height + 4 + title_h + r * line_h
                label = f"{idx}. {text} ({conf:.2f})"
                ld.text((x, y), label[:38], fill=(190, 220, 245), font=small)
        return canvas.convert("RGB")
    except Exception as exc:
        _logger.debug("[诊断] OCR 标注图生成失败: %s", exc)
        return None


def annotate_ocr_overlay(img, results: List[Dict[str, Any]], title: str, scale: float = 1.0):
    """公共入口：给截图叠加 OCR 框 + 编号 + 图例（月商店证据图等复用）。

    实现在 `_annotate_ocr_overlay`，这里只做转发，避免外部直接依赖私有函数。
    """
    return _annotate_ocr_overlay(img, results, title, scale=scale)


def capture_failure_image(
    logger: logging.Logger,
    hwnd=None,
    tag: str = "",
    ocr_results: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[str, str]:
    """抓窗口截图压缩落盘，返回 (原图名, OCR 标注图名)；失败返回 ("", "")。

    传了 ocr_results 时额外生成 `*_ocr.jpg`：把识别框 + 文字 + 置信度画在
    截图上，事后不用再对着坐标表脑补。标注失败不影响原图。
    """
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
                return "", ""
        else:
            region = None
        img = grab_screen(region=region) if region else grab_screen()
        w, h = img.size
        scale = 1.0
        if w > EVIDENCE_MAX_WIDTH:
            scale = EVIDENCE_MAX_WIDTH / w
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

        annotated_name = ""
        if ocr_results:
            title = f"{tag or 'failure'} · {len(ocr_results)} 个文本区域 · {stamp}"
            annotated = _annotate_ocr_overlay(img, ocr_results, title, scale=scale)
            if annotated is not None:
                ann_name = f"{stamp}_{_safe_tag(tag)}_ocr.jpg"
                annotated.save(
                    str(FLOW_FAILURE_DIR / ann_name), "JPEG",
                    quality=EVIDENCE_JPEG_QUALITY, optimize=True,
                )
                annotated_name = ann_name
        return name, annotated_name
    except Exception as exc:
        _logger.debug("[诊断] 失败截图抓取异常: %s", exc)
        return "", ""


def _collect_ocr_results(
    logger: logging.Logger, hwnd=None, limit: int = DEFAULT_OCR_SAMPLE
) -> List[Dict[str, Any]]:
    """取当帧 OCR 原始结果（带 box/center，供标注图画框）；无 hwnd 返回空列表。"""
    if not hwnd:
        return []
    try:
        from .ocr_click_tester import ocr_window_content

        return list(ocr_window_content(logger, hwnd=hwnd) or [])[:limit]
    except Exception as exc:
        _logger.debug("[诊断] 失败取证 OCR 异常: %s", exc)
        return []


def _results_to_sample(results: List[Dict[str, Any]]) -> List[str]:
    """把 OCR 原始结果压成 `文字(置信度)@x,y` 样本（写库用）。"""
    out: List[str] = []
    for r in results or []:
        try:
            text = str(r.get("text") or "").strip()
            conf = float(r.get("confidence") or 0.0)
            center = r.get("center") or (0, 0)
            out.append(f"{text}({conf:.2f})@{int(center[0])},{int(center[1])}")
        except Exception:
            continue
    return out


def collect_ocr_sample(
    logger: logging.Logger, hwnd=None, limit: int = DEFAULT_OCR_SAMPLE
) -> List[str]:
    """取当帧 OCR 文本样本（给不出 hwnd 时返回空列表，不做全屏识别）。"""
    return _results_to_sample(_collect_ocr_results(logger, hwnd=hwnd, limit=limit))


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
        # 一次取原始结果，同时用于文本样本和 OCR 标注图，避免重复识别
        results = (
            list(ocr_results)[:DEFAULT_OCR_SAMPLE]
            if ocr_results
            else _collect_ocr_results(logger, hwnd=hwnd)
        )
        sample = list(ocr_sample) if ocr_sample else _results_to_sample(results)

        image_name = ""
        ocr_image_name = ""
        if save_image:
            # 没有窗口句柄时也抓（capture_failure_image 内部 hwnd=None → 全屏）：
            # 2026-11-02 白荆「启动器『开始游戏』按钮未找到」就是卡在启动器阶段、
            # 还没有游戏窗口句柄，旧的 `and hwnd` 判定导致这条失败**完全没有截图**，
            # 事后无从判断启动器长什么样。启动器级失败只有全屏才看得见。
            # 注意：给了 hwnd 但窗口已关/最小化时，capture_failure_image 仍会返回空串
            # （不退化成全屏），避免把用户桌面存进证据目录。
            image_name, ocr_image_name = capture_failure_image(
                logger, hwnd=hwnd, tag=f"{flow_key}_{step}", ocr_results=results
            )
        prune_old_failure_images(logger)

        now = time.time()
        extra_payload = dict(extra or {})
        # 标注图作为对外证据（打开就能看到识别框）；原图留在 extra 里可回退
        image_field = ocr_image_name or image_name
        if ocr_image_name:
            extra_payload["raw_image"] = image_name
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
            "image_path": image_field,
            "ocr_texts": sample,
            "extra": extra_payload,
        }
        from ..storage.workflow_db import WorkflowDB

        row_id = WorkflowDB(str(WORKFLOW_DB_PATH)).record_flow_failure(record)
        # 日志上报是锦上添花：即便 logger 本身炸了也不能把已写库的 id 吞掉
        try:
            if logger is not None:
                raw_hint = f" 原图={image_name}" if ocr_image_name else ""
                logger.warning(
                    f"[失败记录] #{row_id} {flow_key}/{step}: {str(error)[:120]} "
                    f"截图={image_field or '无'}{raw_hint}（artifacts/flow_failures/）"
                )
        except Exception:
            # 失败取证自身的日志写入失败不能再写日志，避免递归
            pass
        return row_id
    except Exception as exc:
        try:
            if logger is not None:
                logger.debug(f"[失败记录] 记录失败（已忽略）: {exc}")
        except Exception:
            # logger 本身抛异常时绝不能再记录，避免递归
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