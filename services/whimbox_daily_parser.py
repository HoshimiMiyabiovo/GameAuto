"""whimbox（无限暖暖一条龙）日常任务执行情况日志解析器。

whimbox 通过 ``log_to_gui`` 把每一步进度写到日志，标准行格式::

    2026-07-16 22:09:11.267 | INFO | whimbox.task.task_template:log_to_gui:336 - ✅ 每周幻境完成情况: 2/2
    2026-07-16 22:09:32.826 | INFO | whimbox.task.task_template:log_to_gui:336 - ✅ 朝夕心愿完成度：100/500
    2026-07-16 22:17:31.111 | INFO | whimbox.task.task_template:log_to_gui:336 - ✅ 奇迹之冠巅峰赛进度为0/24
    2026-07-16 22:20:58.830 | INFO | whimbox.task.task_template:log_to_gui:336 - ✅ 一条龙结束

脚本结束后 ``run_one_dragon`` 还会打一段无时间戳的汇总::

    2026-07-16 22:21:00.184 | INFO | __main__:run_one_dragon:48 - 一条龙任务完成: 任务结果如下：
    ✅美鸭梨挖掘已完成
    ⏭️家园日常已跳过
    ✅每周幻境已完成
    ✅朝夕心愿已完成
    ✅星光结晶收集已完成
    ✅星海拾光已完成
    ✅奇迹之冠巅峰赛已完成
    ✅奇迹之旅已完成

本模块做两件事：
1. 按当前版本的真实任务名做"白名单"扫描，对每类任务只保留最后一次状态
2. 扫描汇总块（无时间戳行）作为"已完成 / 已跳过"的权威补全

任何"任务 X 失败"行（注意是任务失败，不是 OCR/不支持）记为 failed。
"一条龙结束"或汇总中出现"已完成"代表整体成功。
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

LOGGER = logging.getLogger("GameAutomation")
RESET_HOUR = 4

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_APP_CONFIG_PATH = _PROJECT_ROOT / "app_config.json"

# 日志行首：日期 时间 | 级别 | logger - 内容
LOG_LINE_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+\s*\|\s*\w+\s*\|\s*whimbox\.task\.task_template:log_to_gui:\d+\s*-\s*(.+)$"
)

# 汇总行（无时间戳）：仅出现在 `__main__:run_one_dragon` 的多行消息里
# 形如：✅美鸭梨挖掘已完成 / ⏭️家园日常已跳过 / ✅星海拾光已完成
SUMMARY_LINE_PATTERN = re.compile(r"^([✅⏭❌✔])\s*(.+?)(已完成|已跳过|未完成|失败)\s*$")

# 任务进度数字 X/Y 提取（覆盖 X/Y、进度为X/Y、完成情况: X/Y、完成度：X/Y）
PROGRESS_PATTERN = re.compile(r"(\d+)\s*/\s*(\d+)")

# 失败行：❌ 任务"X"失败，继续其他任务
FAILED_PATTERN = re.compile(r"[❌✖]\s*任务[\"“]?(.+?)[\"”]?\s*失败")
# 暂不支持行：❌ 暂不支持任务"X"，继续其他任务（不算失败，归为 skipped）
UNSUPPORTED_PATTERN = re.compile(r"[❌✖]\s*暂不支持任务[\"“]?(.+?)[\"”]?")
# 整体结束信号
END_SIGNAL_KEYWORDS = ("一条龙结束", "奇想之旅结束", "任务结束", "全部完成")

# ────────────────────────────────────────────────────────────────────
# 关键任务类别（白名单）。每个类别只识别"语义关键字"，
# 顺序决定 DailyView 显示顺序，与 whimbox 当前 run_one_dragon 任务列表一致。
# 真实任务名（参考 2026-07-13 ~ 2026-07-16 的 whimbox 日志）：
#   美鸭梨挖掘 / 每周幻境 / 朝夕心愿 / 星海拾光
#   奇迹之冠巅峰赛 / 奇迹之旅 / 星光结晶收集 / 家园日常
# ────────────────────────────────────────────────────────────────────
WHIMBOX_KEY_TASK_GROUPS: List[Dict[str, Any]] = [
    {
        # 美鸭梨挖掘：日志出现 ✅ 美鸭梨挖掘 / ✅ 成功一键收获并再次挖掘
        "name": "美鸭梨挖掘",
        "keywords": ["美鸭梨挖掘", "成功一键收获并再次挖掘", "一键收获并再次挖掘"],
        "max": 1,
    },
    {
        # 每周幻境：✅ 每周幻境完成情况: 0/2 / ✅ 每周幻境已完成
        "name": "每周幻境",
        "keywords": ["每周幻境", "完成每周幻境", "每周幻境完成情况"],
        "max": 2,
    },
    {
        # 朝夕心愿：✅ 朝夕心愿完成度：100/500 / ✅ 朝夕心愿已完成
        "name": "朝夕心愿",
        "keywords": ["朝夕心愿", "朝夕心愿完成度", "朝夕心愿完成情况"],
        "max": 500,
    },
    {
        # 星海拾光：✅ 星海拾光完成度：200/500 / ✅ 星海拾光已完成
        "name": "星海拾光",
        "keywords": ["星海拾光", "星海拾光完成度", "星海拾光完成情况"],
        "max": 500,
    },
    {
        # 奇迹之冠巅峰赛：✅ 奇迹之冠巅峰赛进度为0/24 / ✅ 奇迹之冠巅峰赛已完成 / ✅ 奇迹之冠巅峰赛已做过
        "name": "奇迹之冠巅峰赛",
        "keywords": [
            "奇迹之冠巅峰赛",
            "奇迹之冠巅峰赛进度",
            "奇迹之冠巅峰赛已完成",
            "奇迹之冠巅峰赛已做过",
        ],
        "max": 24,
    },
    {
        # 奇迹之旅：✅ 成功领取奇迹之旅奖励 / ✅ 领取奇迹之旅奖励 / ✅ 打开奇迹之旅
        "name": "奇迹之旅",
        "keywords": [
            "奇迹之旅",
            "成功领取奇迹之旅奖励",
            "领取奇迹之旅奖励",
            "打开奇迹之旅",
        ],
        "max": 1,
    },
    {
        # 星光结晶收集：✅ 收集星光结晶 / ✅ 收集星光结晶成功 / ✅ 星光结晶收集
        "name": "星光结晶收集",
        "keywords": [
            "星光结晶收集",
            "收集星光结晶",
            "收集星光结晶成功",
            "星光结晶收集已完成",
        ],
        "max": 1,
    },
    {
        # 家园日常：通常仅在汇总块出现：⏭️家园日常已跳过
        "name": "家园日常",
        "keywords": [
            "家园日常",
            "家园日常已完成",
            "家园日常已跳过",
            "家园日常跳过",
        ],
        "max": 1,
    },
]


def _today_key() -> str:
    """今天的日期键（按 RESET_HOUR=4 分界，凌晨 4 点前算前一天）。"""
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
    """今天的 whimbox 日志文件（whimbox-YYYY-MM-DD.log），没有则 None。"""
    log_dir = _whimbox_log_dir()
    if log_dir is None or not log_dir.exists():
        return None
    today = _today_key()
    target = log_dir / f"whimbox-{today}.log"
    return target if target.is_file() else None


def _match_key_task(text: str) -> Optional[Dict[str, Any]]:
    """根据白名单，判断文本是否属于某个关键任务类别。"""
    for group in WHIMBOX_KEY_TASK_GROUPS:
        for kw in group["keywords"]:
            if kw in text:
                return group
    return None


def _is_summary_line(line: str) -> bool:
    """汇总块行：✅/⏭/❌/✔ 开头 + 任务名 + 已完成/已跳过/未完成/失败。"""
    return bool(SUMMARY_LINE_PATTERN.match(line.strip()))


def _parse_summary_line(line: str) -> Optional[Tuple[str, str]]:
    """解析汇总行，返回 (icon_char, task_name) 或 None。"""
    m = SUMMARY_LINE_PATTERN.match(line.strip())
    if not m:
        return None
    icon = m.group(1)
    name = m.group(2).strip()
    suffix = m.group(3)
    if suffix == "已跳过":
        return icon, name
    return icon, name


def parse_whimbox_log(file_path: str | Path) -> Optional[Dict[str, Any]]:
    """扫描整个 whimbox 日志，按关键任务白名单提取每类任务的最新状态。

    返回的 dict 始终包含 8 项关键任务（即使没有任何 ✅ 行），
    顺序与 ``WHIMBOX_KEY_TASK_GROUPS`` 一致。
    """
    try:
        content = Path(file_path).read_text(encoding="utf-8", errors="ignore")
    except Exception as exc:
        LOGGER.warning("[whimbox] 读取日志失败: %s", exc)
        return None

    # task_name → {name, status, message, current, total, source_line}
    state: Dict[str, Dict[str, Any]] = {}
    end_signal = False
    failed_names: List[str] = []
    unsupported_names: List[str] = []
    any_log_line = False
    any_summary_line = False
    summary_seen = False  # 是否进入了"一条龙任务完成"汇总块

    for raw in content.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue

        # ── 1) 汇总块（无时间戳）──
        if not raw[:1].isdigit():
            # 进入汇总块：__main__:run_one_dragon:48 之后的几行
            # 用上下文判断：是否见到过"任务结果如下"
            if summary_seen or "任务结果如下" in stripped or _is_summary_line(stripped):
                summary_seen = True
                sm = _parse_summary_line(stripped)
                if sm is not None:
                    icon, task_name = sm
                    any_summary_line = True
                    group = _match_key_task(task_name)
                    if group is not None:
                        if icon in ("⏭", "⏭️"):
                            new_status = "skipped"
                            new_msg = "已跳过"
                        elif icon in ("❌", "✖"):
                            new_status = "failed"
                            new_msg = "失败"
                        else:
                            new_status = "success"
                            new_msg = "已完成"
                        state[group["name"]] = {
                            "name": group["name"],
                            "status": new_status,
                            "message": new_msg,
                            "current": group["max"] if new_status == "success" else 0,
                            "total": group["max"],
                        }
            # 不在 log_to_gui 行的处理范围
            continue

        # ── 2) 标准 log_to_gui 行 ──
        m = LOG_LINE_PATTERN.match(stripped)
        if m is None:
            continue
        any_log_line = True
        text = m.group(1).strip()

        # 整体结束信号
        if text.startswith("✅") and any(kw in text for kw in END_SIGNAL_KEYWORDS):
            end_signal = True
            continue

        # 任务失败行：❌ 任务"X"失败
        m_fail = FAILED_PATTERN.search(text)
        if m_fail:
            task_name = m_fail.group(1).strip()
            failed_names.append(task_name)
            for group in WHIMBOX_KEY_TASK_GROUPS:
                if any(kw in task_name for kw in group["keywords"]):
                    state[group["name"]] = {
                        "name": group["name"],
                        "status": "failed",
                        "message": "任务失败",
                        "current": 0,
                        "total": group["max"],
                    }
                    break
            continue

        # 暂不支持任务：❌ 暂不支持任务"X"
        m_unsup = UNSUPPORTED_PATTERN.search(text)
        if m_unsup:
            task_name = m_unsup.group(1).strip()
            unsupported_names.append(task_name)
            for group in WHIMBOX_KEY_TASK_GROUPS:
                if any(kw in task_name for kw in group["keywords"]):
                    if state.get(group["name"], {}).get("status") != "success":
                        state[group["name"]] = {
                            "name": group["name"],
                            "status": "skipped",
                            "message": "暂不支持",
                            "current": 0,
                            "total": group["max"],
                        }
                    break
            continue

        # ✅ 任务进度行：✅ XX完成度: X/Y / ✅ XX进度为X/Y / ✅ XX完成情况: X/Y
        if text.startswith("✅"):
            payload = text[1:].strip()
            group = _match_key_task(payload)
            if group is None:
                continue

            # 优先解析进度数字
            cur: Optional[int] = None
            total: Optional[int] = None
            pm = PROGRESS_PATTERN.search(payload)
            if pm:
                cur = int(pm.group(1))
                total = int(pm.group(2))

            # 已完成信号优先（不要被中间进度覆盖）
            if "已完成" in payload or "已做过" in payload:
                state[group["name"]] = {
                    "name": group["name"],
                    "status": "success",
                    "message": f"{cur}/{total}" if (cur is not None and total is not None)
                              else "已完成",
                    "current": cur if cur is not None else group["max"],
                    "total": total if (total is not None and total > 0) else group["max"],
                }
                continue

            # "已跳过"信号
            if "已跳过" in payload or "跳过" in payload:
                if state.get(group["name"], {}).get("status") != "success":
                    state[group["name"]] = {
                        "name": group["name"],
                        "status": "skipped",
                        "message": "已跳过",
                        "current": 0,
                        "total": group["max"],
                    }
                continue

            if cur is not None:
                # 进度行：当前进度
                state[group["name"]] = {
                    "name": group["name"],
                    "status": "success" if cur >= group["max"] else "pending",
                    "message": f"{cur}/{total if total else group['max']}",
                    "current": cur,
                    "total": total if (total and total > 0) else group["max"],
                }
            else:
                # 纯"开始"信号：✅ 开始完成X，不算成功，仅当首次出现时记为 pending
                if group["name"] not in state:
                    state[group["name"]] = {
                        "name": group["name"],
                        "status": "pending",
                        "message": "进行中",
                        "current": 0,
                        "total": group["max"],
                    }

    # 没有任何 log_to_gui 行也没有汇总块 → 没有可识别的输出
    if not any_log_line and not any_summary_line:
        LOGGER.debug("[whimbox] 日志无 log_to_gui/汇总 行: %s", file_path)
        return None

    # 按白名单顺序输出（始终保证 8 项）
    # 注意：current/total 必须同时放进 extra —— bridge 注入与 tracker.record_task
    # 都从 extra 取计数，缺 extra 会让"朝夕心愿 100/500"这类真实进度被当成 0
    tasks_data: List[Dict[str, Any]] = []
    completed_count = 0
    for group in WHIMBOX_KEY_TASK_GROUPS:
        entry = state.get(group["name"])
        if entry is None:
            tasks_data.append({
                "task_key": group["name"],
                "task_name": group["name"],
                "status": "pending",
                "message": "未追踪",
                "current": 0,
                "total": group["max"],
                "extra": {"current": 0, "total": group["max"]},
            })
        else:
            tasks_data.append({
                "task_key": entry["name"],
                "task_name": entry["name"],
                "status": entry["status"],
                "message": entry["message"],
                "current": entry["current"],
                "total": entry["total"],
                "extra": {"current": entry["current"], "total": entry["total"]},
            })
            if entry["status"] in ("success", "skipped"):
                completed_count += 1

    # 整体成功 = (没有 failed) AND (end_signal 存在 OR 至少有关键任务完成)
    is_all_success = (not any(t["status"] == "failed" for t in tasks_data)) and (
        end_signal or completed_count > 0
    )
    has_failed = any(t["status"] == "failed" for t in tasks_data)

    errors: List[str] = []
    for entry in state.values():
        if entry["status"] == "failed":
            errors.append(f"{entry['name']} 失败")

    return {
        "game_key": "nikki",
        "game_name": "无限暖暖",
        "uid": "",
        "nickname": "whimbox",
        "is_all_success": is_all_success,
        "has_failed": has_failed,
        "progress_percent": int(
            sum(1 for t in tasks_data if t["status"] in ("success", "skipped"))
            / max(1, len(tasks_data)) * 100
        ),
        "tasks": tasks_data,
        "errors": errors,
        "report_time": "",
    }


def parse_latest_whimbox_log() -> Optional[Dict[str, Any]]:
    """解析今天的 whimbox 日志文件，今天没跑则返回 None。"""
    log_file = find_today_whimbox_log()
    if log_file is None:
        return None
    return parse_whimbox_log(log_file)
