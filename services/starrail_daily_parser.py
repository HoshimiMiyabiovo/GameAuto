"""崩坏：星穹铁道 March7thAssistant 日常任务日志解析器。

日志位于 March7thAssistant 安装目录的 `logs/` 下，按日期命名 `YYYY-MM-DD.log`。
日志行格式::

    YYYY-MM-DD HH:MM:SS,mmm | LEVEL | 消息

关键任务完成标志：
- 副本任务完成
- 每日实训已完成 / 实训分数已达标
- 邮件奖励已领取
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

LOGGER = logging.getLogger("GameAutomation")
RESET_HOUR = 4

_APP_CONFIG_PATH = Path(__file__).resolve().parent.parent / "app_config.json"

LOG_LINE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}),\d+\s+\|\s+(\w+)\s+\|\s+(.+)$"
)

TASK_MARKERS: List[Dict[str, Any]] = [
    {"key": "副本战斗", "name": "副本战斗（开拓力）",
     "success": ["副本任务完成"], "failed": []},
    {"key": "每日实训", "name": "每日实训",
     "success": ["每日实训已完成", "实训分数已达标"], "failed": []},
    {"key": "邮件", "name": "邮件",
     "success": ["邮件奖励已领取"], "failed": []},
]


def _today_key() -> str:
    from datetime import datetime, timedelta
    now = datetime.now()
    if now.hour < RESET_HOUR:
        now = now - timedelta(days=1)
    return now.strftime("%Y-%m-%d")


def _starrail_log_dir() -> Optional[Path]:
    try:
        if not _APP_CONFIG_PATH.is_file():
            return None
        config = json.loads(_APP_CONFIG_PATH.read_text(encoding="utf-8"))
        apps = config.get("apps") or {}
        hsr_cfg = apps.get("HonkaiStarRail") or {}
        discovery = hsr_cfg.get("_discovery") or {}
        monitors = discovery.get("monitors") or {}
        monitor_cfg = monitors.get("starrail_daily_log_monitor") or {}
        log_dir = str(monitor_cfg.get("log_dir", "") or "").strip()
        return Path(log_dir) if log_dir else None
    except Exception as exc:
        LOGGER.warning("[starrail] 读取日志目录配置失败: %s", exc)
        return None


def find_today_starrail_log() -> Optional[Path]:
    """找到今天的星铁日志文件，按 4:00 分界。"""
    log_dir = _starrail_log_dir()
    if log_dir is None or not log_dir.exists():
        return None
    today = _today_key()
    log_file = log_dir / f"{today}.log"
    if log_file.is_file():
        return log_file
    return None


def parse_starrail_log(file_path: str | Path) -> Optional[Dict[str, Any]]:
    """解析单个星铁日志文件，返回每日进度数据结构。"""
    try:
        content = Path(file_path).read_text(encoding="utf-8", errors="ignore")
    except Exception as exc:
        LOGGER.warning("[starrail] 读取日志失败: %s", exc)
        return None

    lines = content.splitlines()

    subtasks: List[Dict[str, str]] = []
    report_time = ""

    for task_def in TASK_MARKERS:
        task_key = task_def["key"]
        task_name = task_def["name"]
        success_markers = task_def["success"]

        status = "pending"
        message = ""
        last_time = ""

        for line in lines:
            m = LOG_LINE_RE.match(line)
            if not m:
                continue
            date_str = m.group(1)
            time_str = m.group(2)
            body = m.group(4)

            for marker in success_markers:
                if marker in body:
                    status = "success"
                    message = marker
                    last_time = f"{date_str} {time_str}"
                    break

        if last_time and last_time > report_time:
            report_time = last_time

        if status == "success":
            subtasks.append({
                "task_key": task_key,
                "task_name": task_name,
                "status": "success",
                "message": message,
            })

    has_failed = any(t["status"] == "failed" for t in subtasks)
    success_count = sum(1 for t in subtasks if t["status"] in ("success", "already"))
    progress_percent = int(success_count / max(len(TASK_MARKERS), 1) * 100)

    errors: List[str] = []
    for task_def in TASK_MARKERS:
        found = any(t["task_key"] == task_def["key"] for t in subtasks)
        if not found:
            subtasks.append({
                "task_key": task_def["key"],
                "task_name": task_def["name"],
                "status": "failed",
                "message": "未执行",
            })
            errors.append(f"{task_def['name']} 未执行")

    overall_success = len(errors) == 0

    if not report_time:
        for line in reversed(lines):
            m = LOG_LINE_RE.match(line)
            if m:
                report_time = f"{m.group(1)} {m.group(2)}"
                break

    return {
        "game_key": "honkai_sr",
        "game_name": "崩坏：星穹铁道",
        "uid": "",
        "nickname": "March7th",
        "is_all_success": overall_success,
        "has_failed": has_failed or not overall_success,
        "progress_percent": progress_percent,
        "tasks": subtasks,
        "errors": errors,
        "report_time": report_time,
    }


def parse_latest_starrail_log() -> Optional[Dict[str, Any]]:
    """解析今天的星铁日志文件，今天没跑则返回 None。"""
    log_file = find_today_starrail_log()
    if log_file is None:
        return None
    return parse_starrail_log(log_file)
