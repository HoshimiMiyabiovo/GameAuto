"""ok-nte（异环自动化工具）日常任务执行情况日志解析器。

ok-nte 执行时会在日志目录下生成形如 `ok-script.log`（当前）或
`ok-script.YYYY-MM-DD.log`（历史归档）的文件，日志中包含任务状态行：

    YYYY-MM-DD HH:MM:SS,mmm INFO TaskExecutor DailyTask:开始任务: XXX
    YYYY-MM-DD HH:MM:SS,mmm INFO TaskExecutor DailyTask:任务完成: XXX
    YYYY-MM-DD HH:MM:SS,mmm INFO TaskExecutor DailyTask:任务失败: XXX

本模块解析今天日志中的任务状态行，按"关键任务"判定整体成功状态：
只要"领取活跃度奖励"任务完成就算整体成功；其他失败任务列出但不影响判定。
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

KEY_TASKS = ("领取活跃度奖励",)

LOG_LINE_PATTERN = re.compile(
    r"^(\d{4}-\d{2}-\d{2}) \d{2}:\d{2}:\d{2}[,.]\d+\s+\w+\s+\S+\s+DailyTask:(开始任务|任务完成|任务失败):\s*(.+)$"
)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_APP_CONFIG_PATH = _PROJECT_ROOT / "app_config.json"


def _today_key() -> str:
    tm = time.localtime()
    if tm.tm_hour < RESET_HOUR:
        tm_yesterday = time.localtime(time.time() - 86400)
        return time.strftime("%Y-%m-%d", tm_yesterday)
    return time.strftime("%Y-%m-%d", tm)


def _ok_nte_log_dir() -> Optional[Path]:
    """从 app_config.json 读取 ok-nte 日常日志目录。"""
    try:
        if not _APP_CONFIG_PATH.is_file():
            return None
        config = json.loads(_APP_CONFIG_PATH.read_text(encoding="utf-8"))
        apps = config.get("apps") or {}
        nte_cfg = apps.get("NevernessToEverness") or {}
        discovery = nte_cfg.get("_discovery") or {}
        monitors = discovery.get("monitors") or {}
        monitor_cfg = monitors.get("ok_nte_daily_log_monitor") or {}
        log_dir = str(monitor_cfg.get("log_dir", "") or "").strip()
        return Path(log_dir) if log_dir else None
    except Exception as exc:
        LOGGER.warning("[ok-nte] 读取日志目录配置失败: %s", exc)
        return None


def _detect_log_date(path: Path) -> Optional[str]:
    """读取日志文件前几行，提取日期（YYYY-MM-DD）。"""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as handle:
            for _ in range(50):
                line = handle.readline()
                if not line:
                    break
                m = re.match(r"^(\d{4}-\d{2}-\d{2}) ", line)
                if m:
                    return m.group(1)
    except OSError:
        pass
    return None


def find_today_ok_nte_log() -> Optional[Path]:
    """找到今天的 ok-nte 日常日志文件，按 4:00 分界。"""
    log_dir = _ok_nte_log_dir()
    if log_dir is None or not log_dir.exists():
        return None
    today = _today_key()

    archived = log_dir / f"ok-script.{today}.log"
    if archived.is_file():
        return archived

    current = log_dir / "ok-script.log"
    if current.is_file():
        log_date = _detect_log_date(current)
        if log_date == today:
            return current

    return None


def parse_ok_nte_log(file_path: str | Path) -> Optional[Dict[str, Any]]:
    """解析单个 ok-nte 日志文件，返回符合每日进度数据结构的 game 项。"""
    try:
        content = Path(file_path).read_text(encoding="utf-8", errors="ignore")
    except Exception as exc:
        LOGGER.warning("[ok-nte] 读取日志失败: %s", exc)
        return None

    lines = content.splitlines()

    tasks: Dict[str, Dict[str, str]] = {}
    report_time = ""
    log_date = ""

    for raw in lines:
        line = raw.rstrip()
        if not line:
            continue

        if not log_date:
            m = re.match(r"^(\d{4}-\d{2}-\d{2}) ", line)
            if m:
                log_date = m.group(1)

        m = LOG_LINE_PATTERN.match(line)
        if not m:
            continue

        line_date, action, task_name = m.group(1), m.group(2), m.group(3).strip()
        if not task_name:
            continue
        report_time = line[len(m.group(1)) + 1: ].split(",")[0] if not report_time else report_time

        if task_name not in tasks:
            tasks[task_name] = {"status": "started", "started_at": line_date}

        if action == "任务完成":
            tasks[task_name]["status"] = "completed"
            tasks[task_name]["completed_at"] = line_date
        elif action == "任务失败":
            tasks[task_name]["status"] = "failed"
            tasks[task_name]["failed_at"] = line_date

    if not tasks:
        LOGGER.debug("[ok-nte] 日志中未找到 DailyTask 任务标记: %s", file_path)
        return None

    completed: List[str] = []
    failed: List[str] = []
    started: List[str] = []
    for name, info in tasks.items():
        status = info.get("status", "started")
        if status == "completed":
            completed.append(name)
        elif status == "failed":
            failed.append(name)
        else:
            started.append(name)

    key_failed = [t for t in failed if t in KEY_TASKS]
    is_all_success = len(key_failed) == 0 and all(t in completed for t in KEY_TASKS)
    has_failed = len(failed) > 0

    tasks_data: List[Dict[str, str]] = []
    for task in completed:
        tasks_data.append({
            "task_key": task,
            "task_name": task,
            "status": "success",
            "message": "关键任务" if task in KEY_TASKS else "",
        })
    for task in failed:
        tasks_data.append({
            "task_key": task,
            "task_name": task,
            "status": "failed",
            "message": "",
        })
    for task in started:
        tasks_data.append({
            "task_key": task,
            "task_name": task,
            "status": "skipped",
            "message": "已开始但未确认完成",
        })

    errors: List[str] = []
    for task in key_failed:
        errors.append(f"关键任务失败: {task}")
    for task in failed:
        if task not in KEY_TASKS:
            errors.append(f"{task} 失败")
    for task in started:
        errors.append(f"{task} 未确认完成")

    total = len(completed) + len(failed) + len(started)
    progress_percent = int(len(completed) / total * 100) if total > 0 else 0

    return {
        "game_key": "nte",
        "game_name": "异环",
        "uid": "",
        "nickname": "ok-nte",
        "is_all_success": is_all_success,
        "has_failed": has_failed,
        "progress_percent": progress_percent,
        "tasks": tasks_data,
        "errors": errors,
        "report_time": report_time,
        "log_date": log_date,
    }


def parse_latest_ok_nte_log() -> Optional[Dict[str, Any]]:
    """解析今天的 ok-nte 日志文件，今天没跑则返回 None。"""
    log_file = find_today_ok_nte_log()
    if log_file is None:
        return None
    return parse_ok_nte_log(log_file)
