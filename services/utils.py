import os
import sys
from typing import Dict, List, Optional, Tuple

from ..settings import APP_ORDER, APPS, WORKFLOW_DB_PATH, get_app_display_name
from ..storage.workflow_db import WorkflowDB

def clear_keyboard_buffer():
    # 键盘监听功能已移除，保留空实现以兼容旧调用
    return

def format_duration(seconds: int) -> str:
    minutes = seconds // 60
    seconds = seconds % 60
    return f"{minutes}分{seconds}秒"


def _load_launch_preferences() -> Dict[str, Dict]:
    try:
        return WorkflowDB(WORKFLOW_DB_PATH).get_all_app_launch_preferences()
    except Exception:
        return {}


def get_saved_script_choice(app_name: str) -> Optional[str]:
    pref = _load_launch_preferences().get(app_name, {})
    script_choice = str(pref.get("script_choice", "") or "").strip()
    if not script_choice:
        return None
    options = APPS.get(app_name, {}).get("launcher_options", [])
    if any(opt.get("name") == script_choice for opt in options):
        return script_choice
    return None


def get_saved_helper_enabled(app_name: str) -> bool:
    pref = _load_launch_preferences().get(app_name, {})
    return bool(pref.get("helper_enabled", 0))


def get_saved_target_apps() -> List[Tuple[str, Optional[str]]]:
    prefs = _load_launch_preferences()
    targets: List[Tuple[str, Optional[str]]] = []
    for app_name in APP_ORDER:
        pref = prefs.get(app_name, {})
        if bool(pref.get("enabled", 0)):
            targets.append((app_name, get_saved_script_choice(app_name)))
    return targets

def get_user_selection() -> List[Tuple[str, str]]:
    clear_keyboard_buffer()
    code_len = len(APP_ORDER)
    print(f"\n请选择要执行的任务（输入{code_len}位数字代码，1表示执行，0表示不执行）：")
    print("顺序：" + ", ".join(get_app_display_name(app) for app in APP_ORDER))
    print("示例：" + "1" * code_len + " - 全部执行")
   
    while True:
        code = input(f"请输入{code_len}位数字代码：").strip()
        if len(code) == code_len and code.isdigit() and all(c in ('0', '1') for c in code):
            selected_apps = []
            for i in range(len(code)):
                if code[i] == '1':
                    app_name = APP_ORDER[i]
                    selected_apps.append((app_name, get_saved_script_choice(app_name)))
            return selected_apps
        print(f"输入无效，请输入{code_len}位由0和1组成的数字代码")
# qBittorrent 可以移到 controller 或这里