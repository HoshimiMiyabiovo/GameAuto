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
}

STANDALONE_TASK_ORDER: List[str] = ["XMFlow", "CJFlow"]


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