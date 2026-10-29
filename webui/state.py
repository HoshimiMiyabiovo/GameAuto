"""WebUI 全局状态：复用桌面同一 AutomationController 单例与日志广播。"""
from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Deque, Dict, List

from game_auto.settings import manager as cfg_manager

logger = logging.getLogger("GameAutomation.WebUI")

# ── 单例控制器（与桌面共用逻辑，Web 侧独立实例避免与 QML 抢锁） ──
_controller = None
_controller_lock = threading.Lock()

def get_controller():
    global _controller
    with _controller_lock:
        if _controller is None:
            from game_auto.runtime.automation_controller import AutomationController
            _controller = AutomationController()
            logger.info("[WebUI] AutomationController 已初始化")
        return _controller

# ── 内存日志环（供轮询与 WS 推送，带单调 seq 支持增量拉取） ──
LOG_MAX = 500
_log_buffer: Deque[str] = deque(maxlen=LOG_MAX)
_log_seq_buffer: Deque[int] = deque(maxlen=LOG_MAX)
_log_seq: int = 0
_log_lock = threading.Lock()
_log_wait = threading.Condition(_log_lock)

class WebLogHandler(logging.Handler):
    def emit(self, record):
        global _log_seq
        try:
            msg = self.format(record)
            with _log_wait:
                _log_seq += 1
                _log_buffer.append(msg)
                _log_seq_buffer.append(_log_seq)
                _log_wait.notify_all()
        except Exception:
            # 日志 handler 自身失败不能再写日志，避免递归
            pass

def get_log_cursor() -> int:
    with _log_lock:
        return int(_log_seq)

def get_logs_since(since: int, limit: int = 100) -> tuple[List[str], int]:
    """增量拉取：返回 seq>since 的日志，基于单调 seq 计算偏移，O(1)"""
    if limit <= 0:
        return [], get_log_cursor()
    if limit > 500:
        limit = 500
    with _log_lock:
        cur_seq = int(_log_seq)
        n = len(_log_buffer)
        if since <= 0 or since < cur_seq - n - 200:
            buf = list(_log_buffer)[-limit:]
            return buf, cur_seq
        if since >= cur_seq:
            return [], cur_seq
        # 增量条数 = cur - since，已去重缓冲长度裁剪
        count = int(cur_seq - since)
        if count > n:
            count = n
        if count > limit:
            count = limit
        buf = list(_log_buffer)[-count:]
        # 如 count 被 limit 截断，仅返回最新 limit 条
        if len(buf) > limit:
            buf = buf[-limit:]
        return buf, cur_seq

_web_handler: WebLogHandler | None = None

def ensure_log_handler():
    global _web_handler
    root = logging.getLogger("GameAutomation")
    # 已创建且仍挂载则直接返回；若被 AutomationController 清掉则重新挂载
    if _web_handler is not None:
        if _web_handler in root.handlers:
            return
        root.addHandler(_web_handler)
        logger.info("[WebUI] 日志转发已重新挂载")
        return
    _web_handler = WebLogHandler()
    _web_handler.setLevel(logging.INFO)
    fmt = logging.Formatter('[%(asctime)s.%(msecs)03d] [%(levelname)-8s] %(name)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    _web_handler.setFormatter(fmt)
    root.addHandler(_web_handler)
    logger.info("[WebUI] 日志转发已挂载")

_log_file_cache: Dict = {"mtime": 0, "size": 0, "path": None, "lines": []}
_LOG_FILE_TTL = 1.5  # 秒，文件 mtime 缓存
_login_cache: Dict = {"ts": 0.0, "limit": 0, "data": []}
_LOGIN_TTL = 2.0

def _get_log_file_path():
    """解析当前配置的日志文件路径（兼容自定义 log.dir/file），带 1.5s 缓存"""
    import time as _t
    now = _t.time()
    cached = _log_file_cache.get("_path_cache")
    if cached and now - cached.get("ts", 0) < _LOG_FILE_TTL and cached.get("path") is not None:
        return cached["path"]
    try:
        from game_auto.settings.manager import get_log_config
        cfg = get_log_config()
        d = str(cfg.get("dir", "data/logs"))
        f = str(cfg.get("file", "auto_runner.log"))
        from pathlib import Path
        p = Path(d) / f if not Path(d).is_absolute() and not Path(f).is_absolute() else Path(f)
        if not p.is_absolute():
            from game_auto.settings.paths import BASE_DIR
            p = BASE_DIR / p
    except Exception:
        try:
            from game_auto.settings.paths import LOG_FILE
            p = LOG_FILE
        except Exception:
            p = None
    _log_file_cache["_path_cache"] = {"ts": now, "path": p}
    return p

def _tail_log_file(limit: int) -> List[str]:
    """高效读文件尾部，带 mtime/size 缓存，流式 deque 避免全量 load"""
    if limit <= 0:
        return []
    # clamp 防止前端误传大 limit 打爆内存
    if limit > 2000:
        limit = 2000
    p = _get_log_file_path()
    if p is None:
        return []
    try:
        if not p.exists():
            return []
        stat = p.stat()
        mtime = stat.st_mtime
        size = stat.st_size
        cache = _log_file_cache
        # 命中缓存：文件未变更则直接切片
        if cache.get("path") == str(p) and cache.get("mtime") == mtime and cache.get("size") == size and cache.get("lines"):
            lines = cache["lines"]
            return lines[-limit:] if len(lines) > limit else list(lines)
        # 未命中：流式读取尾部
        from collections import deque as _dq
        q: Deque[str] = _dq(maxlen=limit)
        with open(p, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                q.append(line.rstrip("\r\n"))
        lines = list(q)
        cache["path"] = str(p)
        cache["mtime"] = mtime
        cache["size"] = size
        cache["lines"] = lines
        return lines
    except Exception:
        return []

def get_recent_logs(limit: int = 200) -> List[str]:
    if limit <= 0:
        return []
    if limit > 1000:
        limit = 1000
    with _log_lock:
        buf = list(_log_buffer)[-limit:]
    # 仅在内存几乎为空时回补文件，避免每次请求都做文件 IO
    # 阈值 50：正常运行后 buffer 很快填满，后续请求零文件 IO
    if len(buf) >= 50 or len(buf) >= limit:
        return buf
    need = limit - len(buf)
    # 限制单次文件读取量
    file_tail = _tail_log_file(min(need * 2, 400))
    if not file_tail:
        return buf
    buf_suffixes = set()
    for ln in buf:
        try:
            buf_suffixes.add(ln.split(" - ", 1)[-1])
        except Exception:
            buf_suffixes.add(ln)
    filtered: List[str] = []
    for ln in file_tail:
        try:
            suffix = ln.split(" - ", 1)[-1] if " - " in ln else ln
        except Exception:
            suffix = ln
        if suffix not in buf_suffixes and ln not in buf:
            filtered.append(ln)
    if filtered:
        return filtered[-need:] + buf
    return buf

def get_login_logs(limit: int = 200) -> List[str]:
    """仅返回 WebUI 登录/登出 相关日志，带 2s 缓存避免高频扫文件"""
    import time as _t
    if limit <= 0:
        return []
    if limit > 500:
        limit = 500
    now = _t.time()
    if _login_cache.get("limit") == limit and now - _login_cache.get("ts", 0) < _LOGIN_TTL and _login_cache.get("data"):
        return list(_login_cache["data"])
    # 优先从内存缓冲过滤，命中足够则不读文件
    with _log_lock:
        buf = list(_log_buffer)
    filtered = [ln for ln in buf if ("[WebUI][登录]" in ln or "[WebUI][登出]" in ln)]
    if len(filtered) >= limit:
        res = filtered[-limit:]
        _login_cache.update({"ts": now, "limit": limit, "data": list(res)})
        return res
    # 内存不足再读文件尾部（带缓存的 _tail），按消息体去重，按时间正序合并
    file_all = _tail_log_file(2000)
    # 先收集文件侧去重后的登录行（保持文件原序）
    seen_suffix = set()
    for ln in filtered:
        try:
            seen_suffix.add(ln.split(" - ", 1)[-1])
        except Exception:
            seen_suffix.add(ln)
    file_filtered: List[str] = []
    for ln in file_all:
        if "[WebUI][登录]" not in ln and "[WebUI][登出]" not in ln:
            continue
        try:
            suffix = ln.split(" - ", 1)[-1] if " - " in ln else ln
        except Exception:
            suffix = ln
        if suffix not in seen_suffix:
            file_filtered.append(ln)
            seen_suffix.add(suffix)
    # 时间正序：文件（旧）+ 内存（新）
    combined = file_filtered + filtered
    res = combined[-limit:]
    _login_cache.update({"ts": now, "limit": limit, "data": list(res)})
    return res

def wait_for_logs(timeout: float = 25.0) -> List[str]:
    with _log_wait:
        _log_wait.wait(timeout=timeout)
        return list(_log_buffer)

# ── 运行状态（统一供 WebUI 与 GUI 共享，实时可见） ──
_run_lock = threading.Lock()
_run_state: Dict = {
    "running": False,
    "queue": [],  # [{app_key, display_name, script_choice, state}]
    "current": None,  # {app_key, display_name, script_choice}
    "started_at": None,  # float
    "last_report": [],
    "last_warnings": [],
    "source": "",  # webui | gui | wool | ""
}

def set_running(v: bool, queue: List[Dict] | None = None, source: str = ""):
    import time as _time
    with _run_lock:
        _run_state["running"] = bool(v)
        if v:
            _run_state["started_at"] = _time.time()
            if queue is not None:
                _run_state["queue"] = [dict(q) for q in queue]
            if source:
                _run_state["source"] = source
            _run_state["current"] = None
        else:
            _run_state["started_at"] = None
            _run_state["current"] = None
            if queue is not None:
                _run_state["queue"] = [dict(q) for q in queue]
            # running=False 时保留 queue 供完成后查看
            _run_state["source"] = source or _run_state.get("source", "")

def is_running() -> bool:
    with _run_lock:
        return bool(_run_state["running"])

def set_queue(queue: List[Dict]):
    with _run_lock:
        _run_state["queue"] = [dict(q) for q in queue]

def update_queue_state(app_key: str, state: str, message: str = ""):
    with _run_lock:
        for item in _run_state.get("queue", []):
            if item.get("app_key") == app_key:
                item["state"] = state
                if message:
                    item["message"] = message
                break
        # 同步 current
        if state == "running":
            for item in _run_state.get("queue", []):
                if item.get("app_key") == app_key:
                    _run_state["current"] = dict(item)
                    break
        elif _run_state.get("current", {}).get("app_key") == app_key and state != "running":
            # 若当前任务结束，current 清除或保留为最后完成的任务（前端可显示）
            pass

def set_current(current: Dict | None):
    with _run_lock:
        _run_state["current"] = dict(current) if current else None

def set_last_result(report, warnings):
    with _run_lock:
        _run_state["last_report"] = list(report or [])
        _run_state["last_warnings"] = list(warnings or [])

def get_last_result():
    with _run_lock:
        return dict(_run_state)

def get_run_status() -> Dict:
    import time as _time
    with _run_lock:
        data = dict(_run_state)
        data["queue"] = [dict(q) for q in data.get("queue", [])]
        if data.get("current"):
            data["current"] = dict(data["current"])
        started = data.get("started_at")
        if data.get("running") and isinstance(started, (int, float)):
            data["elapsed_sec"] = int(_time.time() - float(started))
        else:
            data["elapsed_sec"] = 0
        return data
