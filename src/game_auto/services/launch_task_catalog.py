from __future__ import annotations

import copy
from typing import Dict, List, Tuple


STANDALONE_TASK_APPS: Dict[str, Dict[str, object]] = {
    "XMFlow": {
        "display_name": "XM 独立流程",
        "script_name": "XM 前台流程",
        "virtual_task": True,
        "skip_min_run_warning": True,
        "friendly_note": "独立执行 XM 前台流程，不再强绑原神前置。",
    },
    "CJFlow": {
        "display_name": "重聚独立流程",
        "script_name": "重聚前台流程",
        "virtual_task": True,
        "skip_min_run_warning": True,
        "friendly_note": "独立执行重聚前台流程，可放在任意位置。",
    },
    "WoolFlow": {
        "display_name": "薅羊毛集合",
        "script_name": "薅羊毛集合流程",
        "virtual_task": True,
        "skip_min_run_warning": True,
        "friendly_note": "依次执行各软件签到薅羊毛流程（当前：MiniMax Code 每日签到、WorkBuddy 每日领取、Trae CN 每日签到、Qoder 每日积分领取）。",
    },
    "QoderFlow": {
        "display_name": "Qoder 每日积分",
        "script_name": "Qoder 积分领取",
        "virtual_task": True,
        "skip_min_run_warning": True,
        "friendly_note": "每天 10:00 积分刷新后领取 Qoder Credits；10:00 前自动跳过（QODER 流程的 not_before=10:00）。",
    },
}

# 每月商店（崩铁余烬 / 绝区零信号残响+后勤 / 原神星尘）不再是独立任务：
# 由 task_batch_runner._MONTHLY_FLOW_BY_APP 在对应游戏日常跑完后自动补跑，
# 月度门控见 background_template_runner.is_monthly_flow_done。
STANDALONE_TASK_ORDER: List[str] = ["XMFlow", "CJFlow", "WoolFlow", "QoderFlow"]

# 历史上存在的独立任务，已随「月商店并入日常」删除（2026-11-01）。
# 老配置的 app_order 里可能还留着这些 key：读配置时静默丢弃，否则它们既不是
# standalone 也没有 apps 定义，会被当成普通游戏任务丢进 execute_task 直接崩。
REMOVED_TASK_KEYS = frozenset({"HsrMonthlyFlow", "ZzzMonthlyFlow", "GenshinMonthlyFlow"})


def is_standalone_task(app_key: str) -> bool:
    return app_key in STANDALONE_TASK_APPS


def get_standalone_task_config(app_key: str) -> Dict[str, object]:
    return copy.deepcopy(STANDALONE_TASK_APPS.get(app_key, {}))


def merge_launch_apps(app_order: List[str], apps: Dict[str, Dict]) -> Tuple[List[str], Dict[str, Dict]]:
    # 老配置残留的已删任务直接过滤，别让 UI 出现点不动的幽灵条目
    merged_order = [key for key in (app_order or []) if key not in REMOVED_TASK_KEYS]
    merged_apps = copy.deepcopy(apps or {})

    for task_key, task_cfg in STANDALONE_TASK_APPS.items():
        merged_apps[task_key] = copy.deepcopy(task_cfg)
        if task_key not in merged_order:
            merged_order.append(task_key)

    return merged_order, merged_apps