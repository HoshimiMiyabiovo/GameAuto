"""绝区零 OneDragon 日常任务执行情况日志解析器。

日志位于 OneDragon 安装目录的 `.log/` 下：
- `log.txt`：当天日志
- `log.txt.YYYY-MM-DD`：轮转归档

日志行格式（只有时间，没有日期）::

    [HH:MM:SS.mmm] [operation.py 675] [INFO]: 指令[ 任务名 ] 执行成功 返回状态 XXX
    [HH:MM:SS.mmm] [operation.py 677] [ERROR]: 指令[ 任务名 ] 执行失败 返回状态 XXX
    [HH:MM:SS.mmm] [operation.py 429] [INFO]: 指令[ 任务名 ] 节点 A -> B 返回状态 ZZZ

一条龙运行以 `指令[ 一条龙 ]` 开始，以 `指令[ 一条龙 ] 执行成功 返回状态 全部结束` 结束。
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

ONE_DRAGON_END = "全部结束"

KEY_TASKS: Dict[str, str] = {
    "兑换码": "兑换码",
    "邮件": "邮件",
    "录像店营业": "录像店营业",
    "卦象集录": "卦象集录",
    "咖啡店": "咖啡店",
    "体力刷本": "体力刷本",
    "活跃度奖励": "活跃度奖励",
    "丽都城募": "丽都城募",
    "丽都周纪 (领奖励)": "丽都周纪",
    "驱动盘拆解": "驱动盘拆解",
}

TASK_RESULT_RE = re.compile(
    r"指令\[\s*(.+?)\s*\]\s+执行(成功|失败)\s+返回状态\s+(.*)"
)
NODE_STATUS_RE = re.compile(
    r"指令\[\s*(.+?)\s*\]\s+节点\s+.+?\s+返回状态\s+(.*)"
)
TIME_RE = re.compile(r"^\[(\d{2}:\d{2}:\d{2})\.\d+\]")


def _today_key() -> str:
    """今天的日期键（按 RESET_HOUR=4 分界，凌晨 4 点前算前一天）。"""
    from datetime import datetime, timedelta
    now = datetime.now()
    if now.hour < RESET_HOUR:
        now = now - timedelta(days=1)
    return now.strftime("%Y-%m-%d")


def _zzz_log_dir() -> Optional[Path]:
    """从 app_config.json 的 zzz_daily_log_monitor 读日志目录。"""
    try:
        if not _APP_CONFIG_PATH.is_file():
            return None
        config = json.loads(_APP_CONFIG_PATH.read_text(encoding="utf-8"))
        apps = config.get("apps") or {}
        zzz_cfg = apps.get("ZenlessZoneZero") or {}
        discovery = zzz_cfg.get("_discovery") or {}
        monitors = discovery.get("monitors") or {}
        monitor_cfg = monitors.get("zzz_daily_log_monitor") or {}
        log_dir = str(monitor_cfg.get("log_dir", "") or "").strip()
        return Path(log_dir) if log_dir else None
    except Exception as exc:
        LOGGER.warning("[zzz] 读取日志目录配置失败: %s", exc)
        return None


def find_today_zzz_log() -> Optional[Path]:
    """找到今天的 ZZZ 日志文件，按 4:00 分界。"""
    log_dir = _zzz_log_dir()
    if log_dir is None or not log_dir.exists():
        return None
    today = _today_key()

    archived = log_dir / f"log.txt.{today}"
    if archived.is_file():
        return archived

    current = log_dir / "log.txt"
    if current.is_file():
        import os
        mtime = os.path.getmtime(current)
        from datetime import datetime
        mtime_dt = datetime.fromtimestamp(mtime)
        if mtime_dt.hour < RESET_HOUR:
            from datetime import timedelta
            mtime_dt = mtime_dt - timedelta(days=1)
        if mtime_dt.strftime("%Y-%m-%d") == today:
            return current

    return None


def parse_zzz_log(file_path: str | Path) -> Optional[Dict[str, Any]]:
    """解析单个 ZZZ 日志文件，返回每日进度数据结构。"""
    try:
        content = Path(file_path).read_text(encoding="utf-8", errors="ignore")
    except Exception as exc:
        LOGGER.warning("[zzz] 读取日志失败: %s", exc)
        return None

    lines = content.splitlines()

    # 只看最后一次一条龙分段：一天可能跑多次（早上自动跑挂了晚上手跑），
    # 必须按"本次启动行"切分，否则早场的失败会污染晚场的成功。
    # 启动行特征：`指令[ 一条龙 ] 节点 进入游戏序章 返回状态 ...`（首节点，无 ->）；
    # 年中的 `进入游戏序章 -> X` 是流转行，不能当切分点。
    # 找不到启动行时退化为整文件（兼容旧日志格式）。
    seg_start = 0
    for idx, line in enumerate(lines):
        if re.search(r"指令\[\s*一条龙\s*\]\s+节点\s+进入游戏序章\s+返回状态", line):
            seg_start = idx
    run_lines = lines[seg_start:]

    # 整体完成标志：以最后一次"一条龙 执行成功/失败"的结果为准，
    # 而不是整文件任意一处"全部结束"（一天多跑时早场的成功不能给晚场的失败背书）
    dragon_results = [
        line for line in run_lines
        if re.search(r"指令\[\s*一条龙\s*\]\s+执行(成功|失败)", line)
    ]
    overall_success = bool(dragon_results) and ONE_DRAGON_END in dragon_results[-1]

    # 提取子任务状态
    # 策略：对每个关键任务，记录最后一次"执行成功/失败"的结果
    # 特殊处理活跃度奖励：需要检查节点详情
    task_results: Dict[str, Dict[str, Any]] = {}
    report_time = ""

    # 先收集活跃度奖励的节点详情
    activity_nodes: List[str] = []

    for line in run_lines:
        m_time = TIME_RE.match(line)
        if m_time:
            report_time = m_time.group(1)

        # 活跃度奖励节点详情
        if "活跃度奖励" in line and "节点" in line:
            node_m = NODE_STATUS_RE.search(line)
            if node_m:
                activity_nodes.append(node_m.group(2))

        # 任务执行结果
        result_m = TASK_RESULT_RE.search(line)
        if not result_m:
            continue

        task_name = result_m.group(1).strip()
        status = result_m.group(2)  # 成功 or 失败
        return_status = result_m.group(3).strip()

        # 只关注关键任务
        matched_key = None
        for key, display in KEY_TASKS.items():
            if key in task_name:
                matched_key = display
                break

        if matched_key is None:
            continue

        if status == "成功":
            if "无需运行" in return_status:
                task_results[matched_key] = {
                    "status": "already",
                    "message": return_status,
                }
            else:
                task_results[matched_key] = {
                    "status": "success",
                    "message": return_status,
                }
        else:
            task_results[matched_key] = {
                "status": "failed",
                "message": return_status,
            }

    # 特殊处理活跃度奖励
    # 优先级：活跃度已满 > 活跃度未满 > 日常奖励领取成功 > 原始执行结果
    if "活跃度奖励" in task_results:
        result = task_results["活跃度奖励"]
        has_claim_success = any("日常奖励领取成功" in n for n in activity_nodes)
        has_activity_full = any("活跃度已满" in n for n in activity_nodes)
        has_not_full = any("活跃度未满" in n for n in activity_nodes)
        if has_activity_full:
            result["status"] = "success"
            result["message"] = "活跃度已满"
        elif has_not_full:
            result["status"] = "failed"
            result["message"] = "活跃度未满"
        elif has_claim_success:
            result["status"] = "success"
            result["message"] = "日常奖励领取成功"

    # 构建子任务列表（按 KEY_TASKS 顺序）
    subtasks: List[Dict[str, str]] = []
    for key, display in KEY_TASKS.items():
        if display in task_results:
            r = task_results[display]
            subtasks.append({
                "task_key": display,
                "task_name": display,
                "status": r["status"],
                "message": r["message"],
            })

    # 通用失败收录：KEY_TASKS 之外的执行失败（如 进入游戏/打开并登录游戏）
    # 也要上卡，否则登录挂了卡片还是一片空白
    seen_names = {t["task_name"] for t in subtasks}
    for line in run_lines:
        fail_m = TASK_RESULT_RE.search(line)
        if not fail_m or fail_m.group(2) != "失败":
            continue
        fail_name = fail_m.group(1).strip()
        if not fail_name or fail_name in seen_names or fail_name == "一条龙":
            continue
        seen_names.add(fail_name)
        subtasks.append({
            "task_key": fail_name,
            "task_name": fail_name,
            "status": "failed",
            "message": fail_m.group(3).strip(),
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
    if not overall_success and subtasks:
        errors.append("一条龙未正常结束")

    return {
        "game_key": "zzz",
        "game_name": "绝区零",
        "uid": "",
        "nickname": "OneDragon",
        "is_all_success": overall_success and not has_failed,
        "has_failed": has_failed,
        "progress_percent": progress_percent,
        "tasks": subtasks,
        "errors": errors,
        "report_time": report_time,
    }


def parse_latest_zzz_log() -> Optional[Dict[str, Any]]:
    """解析今天的 ZZZ 日志文件，今天没跑则返回 None。"""
    log_file = find_today_zzz_log()
    if log_file is None:
        return None
    return parse_zzz_log(log_file)
