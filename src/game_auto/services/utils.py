import json
import os
import sys
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..settings import APP_ORDER, APPS, WORKFLOW_DB_PATH, get_app_display_name
from ..storage.workflow_db import WorkflowDB

# read_file_with_timeout 的在途读取登记表：key=(path, offset)。
# 挂起的 worker 留在集合里挡住同位置后续轮询，结算（含异常）时移除。
_INFLIGHT_LOCK = threading.Lock()
_INFLIGHT_READS: set = set()

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

def read_file_with_timeout(path, offset: int, encoding: str = "utf-8",
                           timeout: float = 10.0):
    """带超时的文件增量读取。返回 (chunk, error)；超时返回 (None, None)。

    用途：日志 watcher 轮询读取。USB/机械盘 IO 卡住时同步 read 会把
    整个 watcher/任务线程钉死（2026-10-15 复盘），读线程为 daemon，
    超时后即使仍挂起也不阻塞进程退出。

    在途去重（2026-11-09 审计 P2）：同 (path, offset) 的上一轮 worker 还
    挂着时本轮直接放弃——否则 IO 挂起期间每次轮询都泄漏一个线程+句柄，
    无上限累积。超时上限由此收敛为"每个读取位一个挂起线程"。
    """
    key = (str(path), int(offset))
    with _INFLIGHT_LOCK:
        if key in _INFLIGHT_READS:
            return None, None  # 上一轮同名读取仍挂起：本轮跳过，等它结算
        _INFLIGHT_READS.add(key)

    result: Dict[str, object] = {}

    def _worker():
        try:
            with open(path, "r", encoding=encoding, errors="ignore") as fh:
                fh.seek(offset)
                result["chunk"] = fh.read()
        except OSError as exc:
            result["error"] = exc
        except Exception as exc:  # 解码等异常不当 IO 卡死处理
            result["error"] = exc
        finally:
            with _INFLIGHT_LOCK:
                _INFLIGHT_READS.discard(key)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        # worker 仍在途（挂起读），key 留在 _INFLIGHT_READS 里挡住后续轮询
        return None, None
    return result.get("chunk"), result.get("error")


def safe_mtime_key(path):
    """sorted 的容错 key：(mtime, name)；文件在 glob 与 stat 之间被轮转/删除时返回 0.0。

    三个日志监控（ok_ww/bettergi/whimbox）的 _matching_files 此前裸 stat()
    ——BetterGI 自己会清旧日志，竞态窗口内 FileNotFoundError 会把整个
    监控线程/任务线程炸掉（2026-11-09 审计 P2-1）。所有按 mtime 排序
    日志文件的地方统一用本函数。
    """
    try:
        return (path.stat().st_mtime, path.name)
    except OSError:
        return (0.0, path.name)

# ── 解析器共享 IO 助手（2026-11-09 审计 P2-4） ───────────────────────
_LOG_TAIL_MAX_BYTES = 32 * 1024 * 1024  # 32MB：一条龙当天日志尾部语义已足够
_RAW_CONFIG_LOCK = threading.Lock()
_RAW_CONFIG_CACHE: Dict = {"key": None, "data": None}


def read_text_tail(path, max_bytes: int = _LOG_TAIL_MAX_BYTES,
                   encoding: str = "utf-8") -> str:
    """读文本文件尾部（最多 max_bytes 字节），超限从下一个行首开始。

    各日志解析器只认"最后一次 run"，当天日志随运行持续增长（BetterGI
    一条龙 45 分钟+43 条 ERR 可达数十 MB），全量 read_text 是几十 MB 级
    瞬时分配 + 多遍正则（审计 P2-4）。超限截掉头部不影响解析语义。
    文件缺失/不可读返回空串（与旧 read_text 的调用方异常处理兼容）。
    """
    p = Path(path)
    try:
        with open(p, "rb") as fh:
            size = fh.seek(0, os.SEEK_END)
            if size > max_bytes:
                fh.seek(size - max_bytes)
                fh.readline()  # 丢半行
            else:
                fh.seek(0)
            data = fh.read()
    except OSError:
        return ""
    return data.decode(encoding, errors="ignore")


def load_app_config_json_cached(path) -> Dict:
    """按 (mtime, size) 缓存的 json.load(read_text())。

    6 个日志解析器的 _xxx_log_dir() 每次调用都重读整个 app_config.json
    （审计 P2-4），同一天多次复核反复解析。返回深拷贝：调用方原地改
    不会污染缓存。文件缺失返回 {}。
    """
    from pathlib import Path as _P
    import copy as _copy
    p = _P(path)
    try:
        st = p.stat()
        key = (st.st_mtime, st.st_size)
    except OSError:
        return {}
    with _RAW_CONFIG_LOCK:
        if _RAW_CONFIG_CACHE["key"] == key and _RAW_CONFIG_CACHE["data"] is not None:
            return _copy.deepcopy(_RAW_CONFIG_CACHE["data"])
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    with _RAW_CONFIG_LOCK:
        _RAW_CONFIG_CACHE["key"] = key
        _RAW_CONFIG_CACHE["data"] = data
    return _copy.deepcopy(data)
