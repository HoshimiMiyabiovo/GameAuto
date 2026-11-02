"""ok-ef 日常任务执行情况日志解析器。

ok-ef（终末地自动化工具）执行完毕后会在 Temp 目录下生成日志：
  %TEMP%/日常执行情况/ok-ef/日常执行情况_YYYYMMDD_HHMMSS.txt

本模块解析该日志，按"关键任务"判定整体成功状态：
只要 ⭐活动奖励、⭐日常奖励 成功，就算整体成功；其他失败任务列出但不影响判定。
"""

from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

LOGGER = logging.getLogger("GameAutomation")

KEY_TASKS = ("⭐活动奖励", "⭐日常奖励")
RESET_HOUR = 4


def _today_key() -> str:
    """今天的日期键（按 RESET_HOUR=4 分界，凌晨 4 点前算前一天）。"""
    tm = time.localtime()
    if tm.tm_hour < RESET_HOUR:
        tm_yesterday = time.localtime(time.time() - 86400)
        return time.strftime("%Y-%m-%d", tm_yesterday)
    return time.strftime("%Y-%m-%d", tm)


def _ok_ef_report_dirs() -> list[Path]:
    """返回所有可能的报告目录（新旧两种路径）。"""
    temp = Path(os.environ.get("TEMP", ""))
    return [
        temp / "ok-ef" / "日常任务",       # 新路径（7/9 之后）
        temp / "日常执行情况" / "ok-ef",   # 旧路径（7/8 之前）
    ]


def _ok_ef_report_date(file_path: Path) -> str:
    """从文件名提取日期 YYYYMMDD，用于排序。"""
    m = re.match(r"(?:日常执行情况|日常任务)_(\d{8})_\d{6}\.txt", file_path.name)
    return m.group(1) if m else "0"


def _ok_ef_report_stamp(file_path: Path) -> str:
    """从文件名提取 YYYYMMDD_HHMMSS，用于在当天多份报告间取最新。"""
    m = re.match(r"(?:日常执行情况|日常任务)_(\d{8}_\d{6})\.txt", file_path.name)
    return m.group(1) if m else ""


def find_today_ok_ef_report() -> Optional[Path]:
    """找今天的 ok-ef 报告文件，同一天有多份时取最新一份。

    ok-ef 一天可能产出多份报告：异常结束重跑、执行中的周期性快照。
    必须取时间戳最大的那份，否则复核会读到最早的过期快照
    （2026-10-05 事故：读到 15:00 异常快照，把 16:36 的成功运行改判失败并触发重试）。
    """
    today = _today_key().replace("-", "")
    candidates: List[Path] = []
    for report_dir in _ok_ef_report_dirs():
        if not report_dir.exists():
            continue
        for f in report_dir.glob("*.txt"):
            if not f.is_file():
                continue
            if _ok_ef_report_date(f) == today:
                candidates.append(f)
    if not candidates:
        return None
    return max(candidates, key=_ok_ef_report_stamp)


def _dedup(lst: List[str]) -> List[str]:
    """保序去重（ok-ef 多轮执行会重复写同名任务）。"""
    seen: set[str] = set()
    result: List[str] = []
    for x in lst:
        if x not in seen:
            seen.add(x)
            result.append(x)
    return result


def _parse_task_list(text: str) -> List[str]:
    """逗号分隔的任务行拆成列表；占位符「无」（空列表）必须滤掉——曾产出"无: 未知原因"。"""
    # 「无」是 ok-ef 对空列表的占位符，不是任务名；空轮次报告（异常结束）
    # 三个区块都会写「无」，不滤掉会被当成任务并产出"无: 未知原因"
    return [t.strip() for t in text.split(",") if t.strip() and t.strip() != "无"]


def parse_ok_ef_report(file_path: str | Path) -> Optional[Dict[str, Any]]:
    """解析单个 ok-ef 日志文件，返回符合每日进度数据结构的 game 项。"""
    try:
        content = Path(file_path).read_text(encoding="utf-8")
    except Exception as exc:
        LOGGER.warning("[ok-ef] 读取日志失败: %s", exc)
        return None

    lines = content.splitlines()

    report_time = ""
    if lines:
        m = re.match(r"日常任务执行情况汇总\s*-\s*(.+)", lines[0].strip())
        if m:
            report_time = m.group(1).strip()

    raw_status = ""
    for line in lines:
        m = re.match(r"执行状态:\s*(.+)", line.strip())
        if m:
            raw_status = m.group(1).strip()
            break

    all_success: List[str] = []
    all_failed: List[str] = []
    all_skipped: List[str] = []
    failure_details: Dict[str, str] = {}

    current_section: Optional[str] = None
    in_failure_details = False

    for line in lines:
        stripped = line.strip()

        if stripped.startswith("=== 账号:"):
            in_failure_details = True
            current_section = "failure_msg"
            continue

        if stripped.startswith("--- 第") and "轮" in stripped:
            in_failure_details = False
            current_section = None
            continue

        if stripped == "成功任务:":
            current_section = "success"
            continue
        if stripped == "失败任务:" and not in_failure_details:
            current_section = "failed"
            continue
        if stripped == "跳过任务:":
            current_section = "skipped"
            continue

        if in_failure_details and stripped.startswith("- "):
            m = re.match(r"-\s*(.+?)\s*:\s*(.+)", stripped)
            if m:
                failure_details[m.group(1).strip()] = m.group(2).strip()
            continue

        if current_section in ("success", "failed", "skipped") and stripped:
            tasks = _parse_task_list(stripped)
            if current_section == "success":
                all_success.extend(tasks)
            elif current_section == "failed":
                all_failed.extend(tasks)
            else:
                all_skipped.extend(tasks)

    all_success = _dedup(all_success)
    all_failed = _dedup(all_failed)
    all_skipped = _dedup(all_skipped)

    key_failed = [t for t in all_failed if t in KEY_TASKS]
    is_all_success = len(key_failed) == 0 and all(t in all_success for t in KEY_TASKS)
    has_failed = len(all_failed) > 0

    tasks_data: List[Dict[str, str]] = []
    for task in all_success:
        tasks_data.append({
            "task_key": task,
            "task_name": task,
            "status": "success",
            "message": "关键任务" if task in KEY_TASKS else "",
        })
    for task in all_failed:
        tasks_data.append({
            "task_key": task,
            "task_name": task,
            "status": "failed",
            "message": failure_details.get(task, ""),
        })
    for task in all_skipped:
        tasks_data.append({
            "task_key": task,
            "task_name": task,
            "status": "skipped",
            "message": "",
        })

    errors: List[str] = []
    for task in key_failed:
        errors.append(f"关键任务失败: {task}: {failure_details.get(task, '未知原因')}")
    for task in all_failed:
        if task not in KEY_TASKS:
            errors.append(f"{task}: {failure_details.get(task, '未知原因')}")

    total = len(all_success) + len(all_failed) + len(all_skipped)
    progress_percent = int(len(all_success) / total * 100) if total > 0 else 0

    return {
        "game_key": "endfield",
        "game_name": "终末地",
        "uid": "",
        "nickname": "ok-ef",
        "is_all_success": is_all_success,
        "has_failed": has_failed,
        "progress_percent": progress_percent,
        "tasks": tasks_data,
        "errors": errors,
        "report_time": report_time,
        "raw_status": raw_status,
    }


def parse_latest_ok_ef_report() -> Optional[Dict[str, Any]]:
    """解析今天的 ok-ef 报告文件，今天没跑则返回 None。"""
    report = find_today_ok_ef_report()
    if report is None:
        return None
    return parse_ok_ef_report(report)
