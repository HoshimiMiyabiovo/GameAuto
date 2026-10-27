"""ok-nte（异环自动化工具）日常任务执行情况日志解析器。

ok-nte 执行时会在日志目录下生成形如 `ok-script.log`（当前）或
`ok-script.YYYY-MM-DD.log`（历史归档）的文件。日志里日常任务的真实状态
**以收尾汇总行为准**，例如：

    2026-08-29 21:30:20,534 ... DailyRoutineTask:info_set success ['异象界域', '一咖舍', '日常领取', '影院约会', '喷泉签到', '羁遇赠礼']
    2026-08-29 21:30:20,534 ... DailyRoutineTask:info_set failed ['异象家具']
    2026-08-29 21:30:20,534 ... DailyRoutineTask:info_set skipped ['异象追猎']
    2026-08-29 21:30:20,534 ... DailyRoutineTask:success: ['异象界域', '一咖舍', '日常领取', '影院约会', '喷泉签到', '羁遇赠礼']
    failed: ['异象家具']          # 续行，无时间戳前缀
    skipped: ['异象追猎']         # 续行，无时间戳前缀
    2026-08-29 21:30:20,535 ... DailyRoutineTask:结束执行日常任务

注意：
- 任务行标签是 `DailyRoutineTask:`（含 Routine），不是 `DailyTask:`；
- `failed:` / `skipped:` 有时作为上一行的续行打印，**没有**时间戳前缀；
- 另有逐任务事件行可兜底：
  `DailyRoutineTask:(开始任务|任务完成|任务失败): 任务名`。

本模块解析今天日志，按收尾汇总行（缺失时回退逐任务事件）判定整体状态：
无 failed 任务即视为整体成功；skipped 视为「未执行」而非失败。
"""

from __future__ import annotations

import ast
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

LOGGER = logging.getLogger("GameAutomation")
RESET_HOUR = 4

# 逐任务事件行：DailyRoutineTask:(开始任务|任务完成|任务失败): 任务名
TASK_LINE_PATTERN = re.compile(
    r"DailyRoutineTask:(开始任务|任务完成|任务失败):\s*(.+?)\s*$"
)
# 收尾汇总行（带或不带 INFO 前缀 / 冒号）：success/failed/skipped [列表]
SUMMARY_PATTERN = re.compile(
    r"\b(success|failed|skipped)\s*:?\s*(\[.*\])\s*$"
)
# 时间戳：YYYY-MM-DD HH:MM:SS
TS_PATTERN = re.compile(r"^(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_APP_CONFIG_PATH = _PROJECT_ROOT / "app_config.json"


def _today_key() -> str:
    """今天的日期键（按 RESET_HOUR=4 分界，凌晨 4 点前算前一天）。"""
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
    """读取日志文件，返回最近一次运行日期（YYYY-MM-DD）。

    扫描整文件并保留最后一个日期匹配，以兼容跨日滚动的当前日志文件。
    """
    try:
        log_date = ""
        with open(path, "r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                m = re.match(r"^(\d{4}-\d{2}-\d{2}) ", line)
                if m:
                    log_date = m.group(1)
        return log_date or None
    except OSError:
        return None


_UPDATE_LINE_RE = re.compile(r"Game update is in progress")


def is_update_in_progress(max_age_sec: int = 180) -> bool:
    """ok-nte 当前是否正在更新游戏客户端。

    读当前 ok-script.log 尾部，找近期（默认 180s 内）的
    `LauncherTask: Game update is in progress` 行。有则说明官方启动器
    正在下载/校验更新，此时等 HTGame 出现没有意义，调用方应直接跳过。
    """
    log_dir = _ok_nte_log_dir()
    if log_dir is None:
        return False
    path = log_dir / "ok-script.log"
    try:
        if not path.is_file():
            return False
        if time.time() - path.stat().st_mtime > max_age_sec:
            return False
        with open(path, "rb") as handle:
            try:
                handle.seek(-65536, 2)
            except OSError:
                handle.seek(0)
            tail = handle.read().decode("utf-8", errors="ignore").splitlines()[-200:]
    except OSError:
        return False
    now = time.time()
    for line in reversed(tail):
        if "Game update is in progress" not in line:
            continue
        m = TS_PATTERN.match(line)
        if not m:
            continue
        try:
            ts = time.mktime(time.strptime(
                f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            continue
        return (now - ts) <= max_age_sec
    return False


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

    summary: Dict[str, Optional[List[str]]] = {
        "success": None,
        "failed": None,
        "skipped": None,
    }
    per_task: Dict[str, str] = {}  # 任务名 -> completed / failed / started（兜底）
    log_date = ""
    last_ts = ""

    for raw in content.splitlines():
        line = raw.rstrip()
        if not line:
            continue

        dm = TS_PATTERN.match(line)
        if dm:
            log_date = dm.group(1)
            last_ts = f"{dm.group(1)} {dm.group(2)}"

        # 收尾汇总行（权威）：success/failed/skipped [列表]
        sm = SUMMARY_PATTERN.search(line)
        if sm:
            cat = sm.group(1)
            try:
                lst = ast.literal_eval(sm.group(2))
            except Exception:
                lst = None
            if isinstance(lst, list):
                summary[cat] = [str(x) for x in lst]
                if dm:
                    last_ts = f"{dm.group(1)} {dm.group(2)}"

        # 逐任务事件行（兜底）：DailyRoutineTask:(开始任务|任务完成|任务失败): 任务名
        tm = TASK_LINE_PATTERN.search(line)
        if tm:
            action, name = tm.group(1), tm.group(2).strip()
            if not name:
                continue
            if action == "任务完成":
                per_task[name] = "completed"
            elif action == "任务失败":
                per_task[name] = "failed"
            else:
                per_task.setdefault(name, "started")

    # 优先用收尾汇总；三类都为空才回退逐任务事件
    use_summary = (
        summary["success"] is not None
        or summary["failed"] is not None
        or summary["skipped"] is not None
    )
    if use_summary:
        completed = list(summary["success"] or [])
        failed = list(summary["failed"] or [])
        skipped = list(summary["skipped"] or [])
    else:
        completed, failed, skipped = [], [], []
        for name, st in per_task.items():
            if st == "completed":
                completed.append(name)
            elif st == "failed":
                failed.append(name)
            else:
                skipped.append(name)

    if not (completed or failed or skipped):
        LOGGER.debug("[ok-nte] 日志中未找到日常任务状态: %s", file_path)
        return None

    # 合并去重：failed > completed(success) > skipped
    status_map: Dict[str, str] = {}
    for t in skipped:
        status_map.setdefault(t, "skipped")
    for t in completed:
        status_map[t] = "success"
    for t in failed:
        status_map[t] = "failed"

    tasks_data: List[Dict[str, str]] = []
    for name, st in status_map.items():
        tasks_data.append({
            "task_key": name,
            "task_name": name,
            "status": st,
            "message": "",
        })

    is_all_success = len(failed) == 0
    has_failed = len(failed) > 0
    errors: List[str] = []
    for t in failed:
        errors.append(f"{t} 失败")
    for t in skipped:
        errors.append(f"{t} 跳过（未执行）")

    total = len(status_map)
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
        "report_time": last_ts,
        "log_date": log_date,
    }


def parse_latest_ok_nte_log() -> Optional[Dict[str, Any]]:
    """解析今天的 ok-nte 日志文件，今天没跑则返回 None。"""
    log_file = find_today_ok_nte_log()
    if log_file is None:
        return None
    return parse_ok_nte_log(log_file)
