"""BetterGI（原神自动化工具）每日委托与探索派遣日志解析器。

BetterGI 执行完毕后会在日志目录下生成形如 `better-genshin-impactYYYYMMDD.log` 的文件，
日志中包含每日委托奖励和探索派遣的完成状态行：

    ▶ "领取『每日委托』奖励！"                       ← 已完成
    ▶ "领取『每日委托』奖励" 未完成或者已领取         ← 未完成
    探索派遣："完成"                                  ← 已完成
    ▶ "探索派遣" 未探索完成或已领取                   ← 未完成

本模块解析今天日志中的关键状态行：
只要"每日委托奖励"和"探索派遣"都确认完成就算整体成功。
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

KEY_TASKS = ("每日委托奖励", "探索派遣")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_APP_CONFIG_PATH = _PROJECT_ROOT / "app_config.json"


def _today_key() -> str:
    """今天的日期键（按 RESET_HOUR=4 分界，凌晨 4 点前算前一天）。"""
    tm = time.localtime()
    if tm.tm_hour < RESET_HOUR:
        tm_yesterday = time.localtime(time.time() - 86400)
        return time.strftime("%Y-%m-%d", tm_yesterday)
    return time.strftime("%Y-%m-%d", tm)


def _today_file_token() -> str:
    """BetterGI 日志文件名中的日期标记：YYYYMMDD（按 4:00 分界）。"""
    tm = time.localtime()
    if tm.tm_hour < RESET_HOUR:
        tm = time.localtime(time.time() - 86400)
    return time.strftime("%Y%m%d", tm)


def _bettergi_log_dir() -> Optional[Path]:
    """从 app_config.json 读取 BetterGI 日志目录。"""
    try:
        if not _APP_CONFIG_PATH.is_file():
            return None
        config = json.loads(_APP_CONFIG_PATH.read_text(encoding="utf-8"))
        apps = config.get("apps") or {}
        gi_cfg = apps.get("GenshinImpact") or {}
        discovery = gi_cfg.get("_discovery") or {}
        monitors = discovery.get("monitors") or {}
        monitor_cfg = monitors.get("bettergi_log_monitor") or {}
        log_dir = str(monitor_cfg.get("log_dir", "") or "").strip()
        return Path(log_dir) if log_dir else None
    except Exception as exc:
        LOGGER.warning("[bettergi] 读取日志目录配置失败: %s", exc)
        return None


def find_today_bettergi_log() -> Optional[Path]:
    """今天的 BetterGI 日志文件，没有则 None。"""
    log_dir = _bettergi_log_dir()
    if log_dir is None or not log_dir.exists():
        return None
    token = _today_file_token()
    target = log_dir / f"better-genshin-impact{token}.log"
    return target if target.is_file() else None


def _parse_bettergi_log(file_path: str | Path) -> Optional[Dict[str, Any]]:
    """解析单个 BetterGI 日志文件，返回符合每日进度数据结构的 game 项。"""
    try:
        content = Path(file_path).read_text(encoding="utf-8", errors="ignore")
    except Exception as exc:
        LOGGER.warning("[bettergi] 读取日志失败: %s", exc)
        return None

    lines = content.splitlines()

    commission_completed = False
    expedition_completed = False
    commission_incomplete_seen = False
    expedition_incomplete_seen = False
    commission_completed_line = ""
    commission_incomplete_line = ""
    expedition_completed_line = ""
    expedition_incomplete_line = ""
    log_time = ""

    for raw in lines:
        line = raw.strip()
        if not line:
            continue

        if not log_time:
            m = re.match(r"\[(\d{2}:\d{2}:\d{2}\.\d+)\]", line)
            if m:
                log_time = m.group(1)

        if "每日委托" in line and "▶" in line:
            if "！" in line or "!" in line:
                commission_completed = True
                commission_completed_line = line
            elif "未完成" in line or "已领取" in line:
                commission_incomplete_seen = True
                commission_incomplete_line = line

        if "探索派遣" in line:
            if "▶" in line and ("未探索完成" in line or "已领取" in line):
                expedition_incomplete_seen = True
                expedition_incomplete_line = line
            elif "完成" in line and "未探索完成" not in line and "再次派遣" not in line and "▶" not in line:
                expedition_completed = True
                expedition_completed_line = line

    last_commission_line = commission_completed_line or commission_incomplete_line
    last_expedition_line = expedition_completed_line or expedition_incomplete_line

    if not commission_completed and not commission_incomplete_seen and not expedition_completed and not expedition_incomplete_seen:
        LOGGER.debug("[bettergi] 日志中未找到每日委托/探索派遣状态: %s", file_path)
        return None

    tasks_data: List[Dict[str, str]] = []

    commission_status = "success" if commission_completed else ("failed" if commission_incomplete_seen else "unknown")
    tasks_data.append({
        "task_key": "每日委托奖励",
        "task_name": "每日委托奖励",
        "status": commission_status,
        "message": last_commission_line,
    })

    expedition_status = "success" if expedition_completed else ("failed" if expedition_incomplete_seen else "unknown")
    tasks_data.append({
        "task_key": "探索派遣",
        "task_name": "探索派遣",
        "status": expedition_status,
        "message": last_expedition_line,
    })

    is_all_success = commission_completed and expedition_completed
    has_failed = (commission_incomplete_seen and not commission_completed) or (expedition_incomplete_seen and not expedition_completed)

    errors: List[str] = []
    if not commission_completed:
        errors.append("每日委托奖励未确认完成")
    if not expedition_completed:
        errors.append("探索派遣未确认完成")

    completed_count = int(commission_completed) + int(expedition_completed)
    progress_percent = int(completed_count / len(KEY_TASKS) * 100)

    return {
        "game_key": "genshin",
        "game_name": "原神",
        "uid": "",
        "nickname": "BetterGI",
        "is_all_success": is_all_success,
        "has_failed": has_failed,
        "progress_percent": progress_percent,
        "tasks": tasks_data,
        "errors": errors,
        "report_time": log_time,
    }


def parse_latest_bettergi_log() -> Optional[Dict[str, Any]]:
    """解析今天的 BetterGI 日志文件，今天没跑则返回 None。"""
    log_file = find_today_bettergi_log()
    if log_file is None:
        return None
    return _parse_bettergi_log(log_file)
