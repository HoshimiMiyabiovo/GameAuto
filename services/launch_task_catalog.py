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
    "HsrMonthlyFlow": {
        "display_name": "崩铁每月任务",
        "script_name": "崩铁每月流程",
        "virtual_task": True,
        "skip_min_run_warning": True,
        "friendly_note": "启动崩铁并进入主界面，随后 ESC → 商店 → 兑换商店 → 余烬兑换（页签未真正切换/亮度不达标、或仍看到星芒兑换的五星光锥都会失败）；每月自动执行一次，兑换清单待定。",
    },
    "ZzzMonthlyFlow": {
        "display_name": "绝区零每月任务",
        "script_name": "绝区零每月流程",
        "virtual_task": True,
        "skip_min_run_warning": True,
        "friendly_note": "启动绝区零并进入主界面，随后打开后勤商店采购：哨站后勤（原装母带×2、日常业绩×10、邦布券×5、调律校音器×1）+ 协会物资（日常业绩×10、邦布券×5、调律校音器×1、仓鼠笼访问器×1，OCR 兜底定位）；并进入商城 → 信源清单 → 信号残响（清单待定）；每月自动执行一次。",
    },
    "GenshinMonthlyFlow": {
        "display_name": "原神每月任务",
        "script_name": "原神每月流程",
        "virtual_task": True,
        "skip_min_run_warning": True,
        "friendly_note": "直接启动原神本体并进入主界面，随后 ESC → 商城 → 尘辉兑换 → 星尘兑换（必须确认在星尘兑换：出现宗室武器即判定失败）；每月自动执行一次，兑换清单待定。",
    },
}

STANDALONE_TASK_ORDER: List[str] = ["XMFlow", "CJFlow", "WoolFlow", "QoderFlow", "HsrMonthlyFlow", "ZzzMonthlyFlow", "GenshinMonthlyFlow"]


def is_standalone_task(app_key: str) -> bool:
    return app_key in STANDALONE_TASK_APPS


def get_standalone_task_config(app_key: str) -> Dict[str, object]:
    return copy.deepcopy(STANDALONE_TASK_APPS.get(app_key, {}))


def merge_launch_apps(app_order: List[str], apps: Dict[str, Dict]) -> Tuple[List[str], Dict[str, Dict]]:
    merged_order = list(app_order or [])
    merged_apps = copy.deepcopy(apps or {})

    for task_key, task_cfg in STANDALONE_TASK_APPS.items():
        merged_apps[task_key] = copy.deepcopy(task_cfg)
        if task_key not in merged_order:
            merged_order.append(task_key)

    return merged_order, merged_apps