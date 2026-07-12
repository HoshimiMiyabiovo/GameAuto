"""whimbox（无限暖暖一条龙）日常任务执行情况日志解析器。

whimbox 执行完毕后会在日志目录下生成形如 `whimbox-YYYY-MM-DD.log` 的文件，
日志末尾会写入 "一条龙任务完成: 任务结果如下：" 标记，随后逐行列出每个子任务状态：

    ✅XXX已完成
    ⏭️XXX已跳过
    ❌XXX未完成

本模块解析该汇总块，只要没有 ❌ 未完成任务就算整体成功（⏭️ 已跳过不算失败）。
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

LOGGER = logging.getLogger("GameAutomation")
RESET_HOUR = 4

RESULT_HEADER_MARKER = "一条龙任务完成: 任务结果如下："
LOG_LINE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d+)?\s+\|")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_APP_CONFIG_PATH = _PROJECT_ROOT / "app_config.json"


def _today_key() -> str:
    tm = time.localtime()
    if tm.tm_hour < RESET_HOUR:
        tm_yesterday = time.localtime(time.time() - 86400)
        return time.strftime("%Y-%m-%d", tm_yesterday)
    return time.strftime("%Y-%m-%d", tm)


def _whimbox_log_dir() -> Optional[Path]:
    """从 app_config.json 读取 whimbox 日志目录。"""
    try:
        if not _APP_CONFIG_PATH.is_file():
            return None
        config = json.loads(_APP_CONFIG_PATH.read_text(encoding="utf-8"))
        apps = config.get("apps") or {}
        nikki_cfg = apps.get("InfinityNikki") or {}
        discovery = nikki_cfg.get("_discovery") or {}
        monitors = discovery.get("monitors") or {}
        monitor_cfg = monitors.get("whimbox_log_monitor") or {}
        log_dir = str(monitor_cfg.get("log_dir", "") or "").strip()
        return Path(log_dir) if log_dir else None
    except Exception as exc:
        LOGGER.warning("[whimbox] 读取日志目录配置失败: %s", exc)
        return None


def find_today_whimbox_log() -> Optional[Path]:
    log_dir = _whimbox_log_dir()
    if log_dir is None or not log_dir.exists():
        return None
    today = _today_key()
    target = log_dir / f"whimbox-{today}.log"
    return target if target.is_file() else None


def _parse_task_status_line(line: str) -> Optional[tuple[str, str]]:
    """解析单行任务状态，返回 (bucket, task_name) 或 None。

    bucket ∈ {"completed", "skipped", "failed"}
    """
    candidates = (
        ("✅", "completed", ("已完成",)),
        ("⏭️", "skipped", ("已跳过",)),
        ("⏭", "skipped", ("已跳过",)),
        ("❌", "failed", ("未完成", "失败", "已失败")),
    )
    for prefix, bucket, suffixes in candidates:
        if not line.startswith(prefix):
            continue
        task_name = line[len(prefix):].strip()
        for suffix in suffixes:
            if task_name.endswith(suffix):
                task_name = task_name[: -len(suffix)].strip()
                break
        task_name = task_name.strip("-:： ")
        if not task_name:
            return None
        return bucket, task_name
    return None


def parse_whimbox_log(file_path: str | Path) -> Optional[Dict[str, Any]]:
    """解析单个 whimbox 日志文件，返回符合每日进度数据结构的 game 项。"""
    try:
        content = Path(file_path).read_text(encoding="utf-8", errors="ignore")
    except Exception as exc:
        LOGGER.warning("[whimbox] 读取日志失败: %s", exc)
        return None

    lines = content.splitlines()

    completed: List[str] = []
    skipped: List[str] = []
    failed: List[str] = []

    capture = False
    header_line = ""
    for line in lines:
        stripped = line.strip()

        if RESULT_HEADER_MARKER in stripped:
            capture = True
            header_line = stripped
            continue

        if not capture:
            continue

        if LOG_LINE_PATTERN.match(stripped):
            break

        parsed = _parse_task_status_line(stripped)
        if parsed is None:
            continue
        bucket, task_name = parsed
        if bucket == "completed" and task_name not in completed:
            completed.append(task_name)
        elif bucket == "skipped" and task_name not in skipped:
            skipped.append(task_name)
        elif bucket == "failed" and task_name not in failed:
            failed.append(task_name)

    if not completed and not skipped and not failed:
        LOGGER.debug("[whimbox] 日志中未找到任务汇总块: %s", file_path)
        return None

    report_time = ""
    m = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", header_line)
    if m:
        report_time = m.group(1)

    is_all_success = len(failed) == 0
    has_failed = len(failed) > 0

    tasks_data: List[Dict[str, str]] = []
    for task in completed:
        tasks_data.append({
            "task_key": task,
            "task_name": task,
            "status": "success",
            "message": "",
        })
    for task in skipped:
        tasks_data.append({
            "task_key": task,
            "task_name": task,
            "status": "skipped",
            "message": "",
        })
    for task in failed:
        tasks_data.append({
            "task_key": task,
            "task_name": task,
            "status": "failed",
            "message": "",
        })

    errors: List[str] = [f"未完成: {task}" for task in failed]

    total = len(completed) + len(skipped) + len(failed)
    progress_percent = int(len(completed) / total * 100) if total > 0 else 0

    return {
        "game_key": "nikki",
        "game_name": "无限暖暖",
        "uid": "",
        "nickname": "whimbox",
        "is_all_success": is_all_success,
        "has_failed": has_failed,
        "progress_percent": progress_percent,
        "tasks": tasks_data,
        "errors": errors,
        "report_time": report_time,
    }


def parse_latest_whimbox_log() -> Optional[Dict[str, Any]]:
    """解析今天的 whimbox 日志文件，今天没跑则返回 None。"""
    log_file = find_today_whimbox_log()
    if log_file is None:
        return None
    return parse_whimbox_log(log_file)
