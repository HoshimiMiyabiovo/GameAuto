"""ok-ap 一键日常汇总解析器。

ok-ap 在 ``%TEMP%/ok-ap/一键日常/`` 写执行汇总，区分完成、部分失败、
异常结束和未开始；只有状态为「完成」且至少一项成功、没有失败项才判成功。
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

LOGGER = logging.getLogger("GameAutomation")
RESET_HOUR = 4

_REPORT_NAME_RE = re.compile(r"^一键日常_(\d{8}_\d{6})(?:_\d+)?\.txt$")
_FAILURE_DETAIL_RE = re.compile(r"^-\s*(.+?)\s*:\s*(.+)$")
_STATUS_PRIORITY = {"skipped": 1, "success": 2, "failed": 3}


def _today_key() -> str:
    tm = time.localtime()
    if tm.tm_hour < RESET_HOUR:
        tm = time.localtime(time.time() - 86400)
    return time.strftime("%Y-%m-%d", tm)


def _report_dir(base_dir: str | Path | None = None) -> Path:
    if base_dir is not None:
        root = Path(base_dir)
    else:
        candidates = [
            os.environ.get("TEMP"),
            os.environ.get("TMP"),
            tempfile.gettempdir(),
        ]
        if os.name == "nt":
            candidates.append(str(Path.home() / "AppData" / "Local" / "Temp"))
        root = next(
            (Path(item) for item in candidates if item and Path(item).is_dir()),
            Path(tempfile.gettempdir()),
        )
    return root / "ok-ap" / "一键日常"


def _report_stamp(path: Path) -> str:
    match = _REPORT_NAME_RE.match(path.name)
    return match.group(1) if match else ""


def _report_sort_key(path: Path) -> tuple[str, float, str]:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    return _report_stamp(path), mtime, path.name


def find_today_ok_ap_report(base_dir: str | Path | None = None) -> Optional[Path]:
    """返回今天最新的一份 ok-ap 汇总；同秒冲突文件按 mtime 取最新。"""
    today = _today_key().replace("-", "")
    report_dir = _report_dir(base_dir)
    if not report_dir.exists():
        return None
    candidates = [
        path for path in report_dir.glob("一键日常_*.txt")
        if path.is_file() and _report_stamp(path).startswith(today)
    ]
    return max(candidates, key=_report_sort_key) if candidates else None


def _parse_task_list(text: str) -> List[str]:
    return [
        item.strip() for item in text.split(",")
        if item.strip() and item.strip() != "无"
    ]


def _record_tasks(task_states: Dict[str, str], status: str, names: List[str]) -> None:
    for name in names:
        previous = task_states.get(name)
        if previous is None or _STATUS_PRIORITY[status] > _STATUS_PRIORITY[previous]:
            task_states[name] = status


def parse_ok_ap_report(file_path: str | Path) -> Optional[Dict[str, Any]]:
    """解析单份 ok-ap 汇总，返回与每日进度卡片一致的数据结构。"""
    path = Path(file_path)
    try:
        content = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        LOGGER.warning("[ok-ap] 读取汇总失败: %s", exc)
        return None

    lines = content.splitlines()
    report_time = ""
    if lines:
        match = re.match(r".+执行情况汇总\s*-\s*(.+)", lines[0].strip())
        if match:
            report_time = match.group(1).strip()

    raw_status = ""
    for line in lines:
        match = re.match(r"执行状态:\s*(.+)", line.strip())
        if match:
            raw_status = match.group(1).strip()
            break

    task_states: Dict[str, str] = {}
    failure_details: Dict[str, List[str]] = {}
    exception_lines: List[str] = []
    current_section: Optional[str] = None
    in_exception = False

    for raw_line in lines:
        stripped = raw_line.strip()
        if stripped == "异常信息:":
            in_exception = True
            current_section = None
            continue
        if in_exception:
            if stripped:
                exception_lines.append(stripped)
            else:
                in_exception = False
            continue

        detail_match = _FAILURE_DETAIL_RE.match(stripped)
        if detail_match:
            task_name = detail_match.group(1).strip()
            message = detail_match.group(2).strip()
            failure_details.setdefault(task_name, []).append(message)
            continue

        if stripped == "成功任务:":
            current_section = "success"
            continue
        if stripped == "失败任务:":
            current_section = "failed"
            continue
        if stripped == "跳过任务:":
            current_section = "skipped"
            continue
        if not stripped:
            current_section = None
            continue
        if current_section:
            _record_tasks(task_states, current_section, _parse_task_list(stripped))

    failed_tasks = [name for name, status in task_states.items() if status == "failed"]
    success_tasks = [name for name, status in task_states.items() if status == "success"]
    skipped_tasks = [name for name, status in task_states.items() if status == "skipped"]
    exception_text = "\n".join(exception_lines).strip()

    tasks_data: List[Dict[str, str]] = []
    errors: List[str] = []
    if exception_text:
        errors.append(exception_text)
    for task_name, status in task_states.items():
        message = ""
        if status == "failed":
            message = "；".join(dict.fromkeys(failure_details.get(task_name, []))) or "未知原因"
            errors.append(f"{task_name}: {message}")
        tasks_data.append({
            "task_key": task_name,
            "task_name": task_name,
            "status": status,
            "message": message,
        })

    if raw_status not in ("完成", "部分失败", "异常结束") and not errors:
        errors.append(f"执行状态: {raw_status or '未知'}")

    total = len(task_states)
    stamp = _report_stamp(path)
    log_date = report_time[:10] or (
        f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}" if len(stamp) >= 8 else ""
    )
    return {
        "game_key": "azur_promilia",
        "game_name": "蓝色星原：旅谣",
        "uid": "",
        "nickname": "ok-ap",
        "is_all_success": raw_status == "完成" and not failed_tasks and bool(success_tasks),
        "has_failed": bool(failed_tasks) or raw_status in ("部分失败", "异常结束"),
        "progress_percent": int(len(success_tasks) * 100 / total) if total else 0,
        "tasks": tasks_data,
        "errors": errors,
        "report_time": report_time,
        "raw_status": raw_status,
        "log_date": log_date,
        "success_count": len(success_tasks),
        "failed_count": len(failed_tasks),
        "skipped_count": len(skipped_tasks),
    }


def parse_latest_ok_ap_report() -> Optional[Dict[str, Any]]:
    """解析今天最新的 ok-ap 汇总；今天没跑则返回 None。"""
    report = find_today_ok_ap_report()
    return parse_ok_ap_report(report) if report else None
