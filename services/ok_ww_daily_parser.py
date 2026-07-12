"""ok-ww（鸣潮自动化工具）日常任务执行情况日志解析器。

ok-ww 执行时会在日志目录下生成形如 `ok-script.log`（当前）或
`ok-script.YYYY-MM-DD.log`（历史归档）的文件。日志中包含任务状态行：

    YYYY-MM-DD HH:MM:SS,mmm INFO TaskExecutor DailyTask:info_set current task XXX
    YYYY-MM-DD HH:MM:SS,mmm INFO TaskExecutor DailyTask:Task completed
    YYYY-MM-DD HH:MM:SS,mmm INFO TaskExecutor TaskExecutor:Successfully Executed Task, Exiting Game and App!

只要日志中出现 `Successfully Executed Task, Exiting Game and App!` 就算整体成功。
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

SUCCESS_MARKER = "Successfully Executed Task, Exiting Game and App!"
TASK_COMPLETED_MARKER = "DailyTask:Task completed"
RUN_START_MARKER = "DailyTask:open_daily"

# 子任务名 → 中文显示名（键名同时也是日志中的关键词）
SUBTASK_NAMES: Dict[str, str] = {
    "open_daily": "打开每日任务",
    "claim daily": "领取每日奖励",
    "claim mail": "领取邮件",
    "battle pass": "战斗通行证",
    "check weekly garden": "周常花园",
}

LOG_LINE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})[,.]\d+\s+\w+\s+\S+\s+(.+)$"
)
ALREADY_DONE_RE = re.compile(r"DailyTask:(.+) already completed")
CANNOT_RE = re.compile(r"DailyTask:can not (.+), maybe ended")


def _today_key() -> str:
    from datetime import datetime
    now = datetime.now()
    if now.hour < RESET_HOUR:
        from datetime import timedelta
        now = now - timedelta(days=1)
    return now.strftime("%Y-%m-%d")


def _ok_ww_log_dir() -> Optional[Path]:
    """从 app_config.json 读取 ok-ww 日常日志目录。"""
    try:
        if not _APP_CONFIG_PATH.is_file():
            return None
        config = json.loads(_APP_CONFIG_PATH.read_text(encoding="utf-8"))
        apps = config.get("apps") or {}
        ww_cfg = apps.get("WutheringWaves") or {}
        discovery = ww_cfg.get("_discovery") or {}
        monitors = discovery.get("monitors") or {}
        monitor_cfg = monitors.get("ok_ww_daily_log_monitor") or {}
        log_dir = str(monitor_cfg.get("log_dir", "") or "").strip()
        return Path(log_dir) if log_dir else None
    except Exception as exc:
        LOGGER.warning("[ok-ww] 读取日志目录配置失败: %s", exc)
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


def find_today_ok_ww_log() -> Optional[Path]:
    """找到今天的 ok-ww 日常日志文件，按 4:00 分界。"""
    log_dir = _ok_ww_log_dir()
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


def parse_ok_ww_log(file_path: str | Path) -> Optional[Dict[str, Any]]:
    """解析单个 ok-ww 日志文件，返回每日进度数据结构。"""
    try:
        content = Path(file_path).read_text(encoding="utf-8", errors="ignore")
    except Exception as exc:
        LOGGER.warning("[ok-ww] 读取日志失败: %s", exc)
        return None

    lines = content.splitlines()

    # 找到最后一次 run 的起始位置（open_daily）
    last_run_start = -1
    for i, line in enumerate(lines):
        if RUN_START_MARKER in line:
            last_run_start = i

    if last_run_start < 0:
        # 没有找到 open_daily，尝试用整个文件
        last_run_start = 0

    run_lines = lines[last_run_start:]

    # 检查整体成功标志
    overall_success = any(SUCCESS_MARKER in line for line in run_lines)
    task_completed = any(TASK_COMPLETED_MARKER in line for line in run_lines)

    # 提取子任务和它们的状态
    subtasks: List[Dict[str, str]] = []
    seen_tasks: set[str] = set()
    current_task: Optional[str] = None
    report_time = ""

    for line in run_lines:
        m = LOG_LINE_RE.match(line)
        if not m:
            continue
        date_str = m.group(1)
        time_str = m.group(2)
        body = m.group(3)

        # 更新报告时间（取最后一行的时间戳）
        report_time = f"{date_str} {time_str}"

        # 检测 "already completed" → 标记为 already
        done_m = ALREADY_DONE_RE.search(body)
        if done_m:
            task_keyword = done_m.group(1).strip()
            _mark_subtask(subtasks, task_keyword, "already", "已完成，跳过")
            continue

        # 检测 "can not XXX, maybe ended" → 标记为 already
        cannot_m = CANNOT_RE.search(body)
        if cannot_m:
            task_keyword = cannot_m.group(1).strip()
            _mark_subtask(subtasks, task_keyword, "already", "已完成，跳过")
            continue

        # 检测子任务开始：同时匹配 info_set current task XXX 和 standalone DailyTask:XXX
        if "DailyTask:" in body and "already completed" not in body and "can not" not in body:
            for keyword in SUBTASK_NAMES:
                if keyword in body:
                    current_task = keyword
                    if keyword not in seen_tasks:
                        seen_tasks.add(keyword)
                        subtasks.append({
                            "task_key": keyword,
                            "task_name": SUBTASK_NAMES[keyword],
                            "status": "success",
                            "message": "",
                        })
                    break

        # 检测 ERROR 行 → 标记当前子任务为 failed
        if " ERROR " in line and "DailyTask:" in body:
            if current_task:
                _mark_subtask(subtasks, current_task, "failed", body.split("DailyTask:")[-1].strip())

    # 如果没有提取到任何子任务，但有成功标志，添加一个汇总任务
    if not subtasks and overall_success:
        subtasks.append({
            "task_key": "daily",
            "task_name": "每日任务",
            "status": "success",
            "message": "",
        })

    has_failed = any(t["status"] == "failed" for t in subtasks)
    progress_percent = int(
        sum(1 for t in subtasks if t["status"] in ("success", "already"))
        / max(len(subtasks), 1) * 100
    )

    errors: List[str] = []
    for t in subtasks:
        if t["status"] == "failed":
            errors.append(f"{t['task_name']} 失败")
    if not overall_success and not task_completed:
        errors.append("未检测到任务完成标志")

    return {
        "game_key": "wuwa",
        "game_name": "鸣潮",
        "uid": "",
        "nickname": "ok-ww",
        "is_all_success": overall_success,
        "has_failed": has_failed,
        "progress_percent": progress_percent,
        "tasks": subtasks,
        "errors": errors,
        "report_time": report_time,
    }


def _mark_subtask(subtasks: List[Dict[str, str]], task_keyword: str,
                   status: str, message: str) -> None:
    """根据关键词匹配子任务并更新状态。"""
    for st in subtasks:
        if task_keyword in st["task_key"]:
            st["status"] = status
            st["message"] = message
            return


def parse_latest_ok_ww_log() -> Optional[Dict[str, Any]]:
    """解析今天的 ok-ww 日志文件，今天没跑则返回 None。"""
    log_file = find_today_ok_ww_log()
    if log_file is None:
        return None
    return parse_ok_ww_log(log_file)
