from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from . import manager as cm
from .paths import BASE_DIR
from ..runtime.process_supervisor import ProcessSnapshot, ProcessSupervisor


DISCOVERY_KEY = "_discovery"
MONITOR_CONFIG_KEYS = (
    "bettergi_log_monitor",
    "ok_script_log_monitor",
    "ok_ww_log_monitor",
    "whimbox_log_monitor",
)
WRAPPER_PROCESS_NAMES = {
    "cmd.exe",
    "conhost.exe",
    "explorer.exe",
    "powershell.exe",
    "pwsh.exe",
    "python.exe",
    "pythonw.exe",
}
LOG_DIR_NAMES = ("log", "logs")
FILE_SEARCH_DEPTH = 4
DIR_SEARCH_DEPTH = 3


def _as_str(value: object) -> str:
    return str(value or "").strip()


def _existing_file(path_value: object) -> str:
    path_str = _as_str(path_value)
    if not path_str:
        return ""
    path = Path(path_str)
    return str(path) if path.is_file() else ""


def _existing_dir(path_value: object) -> str:
    path_str = _as_str(path_value)
    if not path_str:
        return ""
    path = Path(path_str)
    return str(path) if path.is_dir() else ""


def _is_wrapper_process_name(name_value: object) -> bool:
    return _as_str(name_value).lower() in WRAPPER_PROCESS_NAMES


def _is_wrapper_process_path(path_value: object) -> bool:
    path_str = _as_str(path_value)
    if not path_str:
        return False
    return Path(path_str).name.lower() in WRAPPER_PROCESS_NAMES


def _sanitize_process_cache(cache: Dict, *, include_game: bool = True):
    if _is_wrapper_process_name(cache.get("script_process_name")):
        cache.pop("script_process_name", None)
    if _is_wrapper_process_path(cache.get("script_exe_path")):
        cache.pop("script_exe_path", None)

    if not include_game:
        return

    if _is_wrapper_process_name(cache.get("game_process_name")):
        cache.pop("game_process_name", None)
    if _is_wrapper_process_path(cache.get("game_exe_path")):
        cache.pop("game_exe_path", None)


def _sanitize_runtime_process_overrides(app_cfg: Dict):
    if _is_wrapper_process_name(app_cfg.get("script_process_name")):
        app_cfg.pop("script_process_name", None)

    game_cfg = app_cfg.get("game")
    if isinstance(game_cfg, dict):
        if _is_wrapper_process_name(game_cfg.get("process_name")):
            game_cfg.pop("process_name", None)
        if _is_wrapper_process_path(game_cfg.get("exe_path")):
            game_cfg.pop("exe_path", None)

    discovery = _target_discovery_cache(app_cfg, None, create=False)
    if discovery:
        _sanitize_process_cache(discovery)

        option_map = discovery.get("launcher_options")
        if isinstance(option_map, dict):
            for option_cache in option_map.values():
                if isinstance(option_cache, dict):
                    _sanitize_process_cache(option_cache, include_game=False)


def _set_if_changed(parent: Dict, key: str, value: object) -> bool:
    if parent.get(key) == value:
        return False
    parent[key] = value
    return True


def _log(logger, level: str, message: str):
    if logger is None:
        return
    log_func = getattr(logger, level, None)
    if callable(log_func):
        log_func(message)


def _search_roots(hints: Iterable[object]) -> List[Path]:
    roots: List[Path] = []
    seen: set[str] = set()

    def add(path_value: object):
        path_str = _as_str(path_value)
        if not path_str:
            return
        path = Path(path_str)
        candidates = [path]
        if path.suffix:
            candidates.append(path.parent)
        if path.anchor:
            candidates.append(Path(path.anchor))
        for candidate in candidates:
            key = str(candidate).lower()
            if key in seen or not candidate.exists():
                continue
            seen.add(key)
            roots.append(candidate)

    add(BASE_DIR)
    add(BASE_DIR.parent)
    for env_name in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA", "USERPROFILE"):
        add(os.environ.get(env_name, ""))
    home = Path.home()
    add(home)
    add(home / "Desktop")
    add(home / "Downloads")
    add(home / "Documents")
    for drive in ("C:\\", "D:\\", "E:\\", "F:\\"):
        add(drive)
    for hint in hints:
        add(hint)
    return roots


def _search_file_by_names(file_names: Iterable[str], hints: Iterable[object]) -> str:
    targets = {name.lower() for name in file_names if _as_str(name)}
    if not targets:
        return ""

    ignored_dirs = {".git", ".venv", "node_modules"}
    for root in _search_roots(hints):
        if root.is_file() and root.name.lower() in targets:
            return str(root)

        try:
            for current_root, dirnames, filenames in os.walk(root, topdown=True):
                current_path = Path(current_root)
                depth = max(0, len(current_path.parts) - len(root.parts))
                if depth >= FILE_SEARCH_DEPTH:
                    dirnames[:] = []
                else:
                    dirnames[:] = [
                        name for name in dirnames if not name.startswith(".") and name.lower() not in ignored_dirs
                    ]
                for filename in filenames:
                    if filename.lower() in targets:
                        return str(current_path / filename)
        except OSError:
            continue
    return ""


def _resolve_launcher_option(app_cfg: Dict, script_choice: Optional[str]) -> Optional[Dict]:
    options = app_cfg.get("launcher_options") or []
    if not isinstance(options, list) or not options:
        return None

    chosen_name = _as_str(script_choice)
    if chosen_name:
        for option in options:
            if isinstance(option, dict) and _as_str(option.get("name")) == chosen_name:
                return option

    for option in options:
        if isinstance(option, dict):
            return option
    return None


def _target_discovery_cache(app_cfg: Dict, option_name: Optional[str], *, create: bool = False) -> Dict:
    discovery = app_cfg.get(DISCOVERY_KEY)
    if not isinstance(discovery, dict):
        if not create:
            return {}
        discovery = {}
        app_cfg[DISCOVERY_KEY] = discovery

    if option_name:
        option_map = discovery.get("launcher_options")
        if not isinstance(option_map, dict):
            if not create:
                return {}
            option_map = {}
            discovery["launcher_options"] = option_map
        cache = option_map.get(option_name)
        if isinstance(cache, dict):
            return cache
        if not create:
            return {}
        cache = {}
        option_map[option_name] = cache
        return cache

    return discovery


def _monitor_cache(app_cfg: Dict, monitor_key: str, *, create: bool = False) -> Dict:
    discovery = _target_discovery_cache(app_cfg, None, create=create)
    if not discovery and not create:
        return {}
    monitors = discovery.get("monitors")
    if not isinstance(monitors, dict):
        if not create:
            return {}
        monitors = {}
        discovery["monitors"] = monitors
    cache = monitors.get(monitor_key)
    if isinstance(cache, dict):
        return cache
    if not create:
        return {}
    cache = {}
    monitors[monitor_key] = cache
    return cache


def _candidate_launcher_names(app_cfg: Dict, target_cfg: Dict, target_cache: Dict) -> List[str]:
    names: List[str] = []

    def add(name_value: object):
        name = _as_str(name_value)
        if name and name not in names:
            names.append(name)

    for value in (
        target_cfg.get("launcher"),
        target_cache.get("launcher"),
        target_cfg.get("script_process_name"),
        target_cache.get("script_process_name"),
        target_cache.get("script_exe_path"),
    ):
        string_value = _as_str(value)
        if string_value:
            add(Path(string_value).name)

    for base in (_as_str(app_cfg.get("script_name")), _as_str(target_cfg.get("display_name"))):
        if not base:
            continue
        if Path(base).suffix:
            add(Path(base).name)
            continue
        for ext in (".exe", ".bat", ".cmd"):
            add(f"{base}{ext}")
    return names


def _search_running_launcher(target_cfg: Dict, target_cache: Dict) -> Optional[ProcessSnapshot]:
    candidate_names: List[str] = []
    for value in (
        target_cfg.get("script_process_name"),
        target_cache.get("script_process_name"),
        target_cfg.get("launcher"),
        target_cache.get("script_exe_path"),
    ):
        name = Path(_as_str(value)).name
        if name and name.lower() not in WRAPPER_PROCESS_NAMES and name not in candidate_names:
            candidate_names.append(name)

    for name in candidate_names:
        snapshot = ProcessSupervisor.find_process_snapshot(name, exact_name=True)
        if snapshot is not None and _existing_file(snapshot.exe_path):
            return snapshot
    return None


def _remember_launcher(target_cfg: Dict, target_cache: Dict, launcher_path: str) -> bool:
    changed = False
    if launcher_path:
        target_cfg["launcher"] = launcher_path
        changed |= _set_if_changed(target_cache, "launcher", launcher_path)
    return changed


def _discover_launcher_path(app_key: str, app_cfg: Dict, target_cfg: Dict, option_name: Optional[str], logger) -> Tuple[str, bool]:
    target_cache = _target_discovery_cache(app_cfg, option_name, create=True)
    changed = False

    current_launcher = _existing_file(target_cfg.get("launcher"))
    if current_launcher:
        changed |= _remember_launcher(target_cfg, target_cache, current_launcher)
        script_process_name = _as_str(target_cfg.get("script_process_name"))
        if script_process_name:
            changed |= _set_if_changed(target_cache, "script_process_name", script_process_name)
        return current_launcher, changed

    cached_launcher = _existing_file(target_cache.get("launcher"))
    if cached_launcher:
        target_cfg["launcher"] = cached_launcher
        _log(logger, "info", f"[路径发现] {app_key} 启动器命中缓存: {cached_launcher}")
        return cached_launcher, changed

    cached_script_exe = _existing_file(target_cache.get("script_exe_path"))
    if cached_script_exe and Path(cached_script_exe).name.lower() not in WRAPPER_PROCESS_NAMES:
        target_cfg["launcher"] = cached_script_exe
        changed |= _set_if_changed(target_cache, "launcher", cached_script_exe)
        _log(logger, "info", f"[路径发现] {app_key} 启动器改用缓存脚本路径: {cached_script_exe}")
        return cached_script_exe, changed

    running_snapshot = _search_running_launcher(target_cfg, target_cache)
    if running_snapshot is not None:
        changed |= _set_if_changed(target_cache, "script_process_name", running_snapshot.name)
        changed |= _set_if_changed(target_cache, "script_exe_path", running_snapshot.exe_path)
        if Path(running_snapshot.exe_path).name.lower() not in WRAPPER_PROCESS_NAMES:
            changed |= _set_if_changed(target_cache, "launcher", running_snapshot.exe_path)
            target_cfg["launcher"] = running_snapshot.exe_path
            if running_snapshot.name:
                target_cfg["script_process_name"] = running_snapshot.name
            _log(logger, "info", f"[路径发现] {app_key} 启动器命中运行中进程: {running_snapshot.exe_path}")
            return running_snapshot.exe_path, changed

    candidate_names = _candidate_launcher_names(app_cfg, target_cfg, target_cache)
    hint_paths = [
        target_cfg.get("launcher"),
        target_cache.get("launcher"),
        target_cache.get("script_exe_path"),
        app_cfg.get("launcher"),
    ]
    discovered = _search_file_by_names(candidate_names, hint_paths)
    if discovered:
        changed |= _remember_launcher(target_cfg, target_cache, discovered)
        _log(logger, "info", f"[路径发现] {app_key} 启动器自动识别成功: {discovered}")
        return discovered, changed

    _log(logger, "warning", f"[路径发现] {app_key} 启动器未识别到可用路径，候选名: {', '.join(candidate_names) or '无'}")
    return "", changed


def _monitor_base_directories(path_values: Iterable[object]) -> List[Path]:
    bases: List[Path] = []
    seen: set[str] = set()

    def add(path_value: object):
        path_str = _as_str(path_value)
        if not path_str:
            return
        path = Path(path_str)
        directory = path if path.is_dir() else path.parent
        if not directory.exists():
            return
        candidates = [directory]
        if directory.parent.exists():
            candidates.append(directory.parent)
        if directory.parent.exists() and directory.parent.parent.exists():
            candidates.append(directory.parent.parent)

        for candidate in candidates:
            key = str(candidate).lower()
            if key in seen:
                continue
            seen.add(key)
            bases.append(candidate)

    for value in path_values:
        add(value)
    return bases


def _discover_log_dir_near_paths(file_pattern: str, path_values: Iterable[object]) -> str:
    fallback = ""
    ignored_dirs = {".git", ".venv", "node_modules"}

    for base_dir in _monitor_base_directories(path_values):
        for name in LOG_DIR_NAMES:
            candidate = base_dir / name
            if candidate.is_dir():
                try:
                    if any(candidate.glob(file_pattern)):
                        return str(candidate)
                except OSError:
                    pass
                if not fallback:
                    fallback = str(candidate)

        try:
            for current_root, dirnames, _ in os.walk(base_dir, topdown=True):
                current_path = Path(current_root)
                depth = max(0, len(current_path.parts) - len(base_dir.parts))
                if depth >= DIR_SEARCH_DEPTH:
                    dirnames[:] = []
                else:
                    dirnames[:] = [
                        name for name in dirnames if not name.startswith(".") and name.lower() not in ignored_dirs
                    ]
                if current_path.name.lower() not in LOG_DIR_NAMES:
                    continue
                try:
                    if any(current_path.glob(file_pattern)):
                        return str(current_path)
                except OSError:
                    pass
                if not fallback:
                    fallback = str(current_path)
        except OSError:
            continue

    return fallback


def _ensure_monitor_log_dirs(app_key: str, app_cfg: Dict, launcher_paths: Iterable[object], logger) -> bool:
    changed = False
    discovery = _target_discovery_cache(app_cfg, None, create=True)
    extra_paths = [
        discovery.get("launcher"),
        discovery.get("script_exe_path"),
        discovery.get("game_exe_path"),
        *launcher_paths,
    ]

    for monitor_key in MONITOR_CONFIG_KEYS:
        monitor_cfg = app_cfg.get(monitor_key)
        if not isinstance(monitor_cfg, dict):
            continue

        cache = _monitor_cache(app_cfg, monitor_key, create=True)
        current_log_dir = _existing_dir(monitor_cfg.get("log_dir"))
        if current_log_dir:
            changed |= _set_if_changed(cache, "log_dir", current_log_dir)
            continue

        cached_log_dir = _existing_dir(cache.get("log_dir"))
        if cached_log_dir:
            monitor_cfg["log_dir"] = cached_log_dir
            _log(logger, "info", f"[路径发现] {app_key} {monitor_key} 日志目录命中缓存: {cached_log_dir}")
            continue

        file_pattern = _as_str(monitor_cfg.get("file_pattern")) or "*"
        discovered_log_dir = _discover_log_dir_near_paths(file_pattern, extra_paths)
        if discovered_log_dir:
            monitor_cfg["log_dir"] = discovered_log_dir
            changed |= _set_if_changed(cache, "log_dir", discovered_log_dir)
            _log(logger, "info", f"[路径发现] {app_key} {monitor_key} 日志目录自动识别成功: {discovered_log_dir}")
            continue

        _log(logger, "warning", f"[路径发现] {app_key} {monitor_key} 日志目录未识别到可用路径")

    return changed


def _apply_cached_discovery(app_cfg: Dict):
    discovery = _target_discovery_cache(app_cfg, None, create=False)
    if discovery:
        cached_launcher = _existing_file(discovery.get("launcher"))
        if cached_launcher and not _existing_file(app_cfg.get("launcher")):
            app_cfg["launcher"] = cached_launcher

        cached_script_name = _as_str(discovery.get("script_process_name"))
        if cached_script_name and not _is_wrapper_process_name(cached_script_name) and not app_cfg.get("launcher_options"):
            app_cfg["script_process_name"] = cached_script_name

        cached_game_name = _as_str(discovery.get("game_process_name"))
        if cached_game_name and not _is_wrapper_process_name(cached_game_name):
            game_cfg = app_cfg.get("game")
            if not isinstance(game_cfg, dict):
                game_cfg = {}
                app_cfg["game"] = game_cfg
            game_cfg["process_name"] = cached_game_name

        cached_game_exe = _existing_file(discovery.get("game_exe_path"))
        if cached_game_exe and not _is_wrapper_process_path(cached_game_exe):
            game_cfg = app_cfg.get("game")
            if not isinstance(game_cfg, dict):
                game_cfg = {}
                app_cfg["game"] = game_cfg
            game_cfg["exe_path"] = cached_game_exe

    for option in app_cfg.get("launcher_options") or []:
        if not isinstance(option, dict):
            continue
        option_name = _as_str(option.get("name"))
        option_cache = _target_discovery_cache(app_cfg, option_name, create=False)
        if not option_cache:
            continue

        cached_launcher = _existing_file(option_cache.get("launcher"))
        if cached_launcher and not _existing_file(option.get("launcher")):
            option["launcher"] = cached_launcher

        cached_script_name = _as_str(option_cache.get("script_process_name"))
        if cached_script_name and not _is_wrapper_process_name(cached_script_name):
            option["script_process_name"] = cached_script_name

    for monitor_key in MONITOR_CONFIG_KEYS:
        monitor_cfg = app_cfg.get(monitor_key)
        if not isinstance(monitor_cfg, dict):
            continue
        cache = _monitor_cache(app_cfg, monitor_key, create=False)
        cached_log_dir = _existing_dir(cache.get("log_dir"))
        if cached_log_dir and not _existing_dir(monitor_cfg.get("log_dir")):
            monitor_cfg["log_dir"] = cached_log_dir


def apply_runtime_discovery(runtime_config: Dict) -> Dict:
    apps = runtime_config.get("apps")
    if not isinstance(apps, dict):
        return runtime_config
    for app_cfg in apps.values():
        if isinstance(app_cfg, dict):
            _sanitize_runtime_process_overrides(app_cfg)
            _apply_cached_discovery(app_cfg)
    return runtime_config


def prepare_app_for_runtime(app_key: str, script_choice: Optional[str] = None, logger=None) -> bool:
    config = cm.load(force=True)
    apps = config.get("apps")
    if not isinstance(apps, dict):
        return False

    app_cfg = apps.get(app_key)
    if not isinstance(app_cfg, dict):
        return False

    _apply_cached_discovery(app_cfg)
    option_cfg = _resolve_launcher_option(app_cfg, script_choice)
    option_name = _as_str(option_cfg.get("name")) if isinstance(option_cfg, dict) else None
    target_cfg = option_cfg if isinstance(option_cfg, dict) else app_cfg

    changed = False
    launcher_path, launcher_changed = _discover_launcher_path(app_key, app_cfg, target_cfg, option_name, logger)
    changed |= launcher_changed
    changed |= _ensure_monitor_log_dirs(app_key, app_cfg, [launcher_path], logger)

    if changed:
        cm.save(config)
    return changed


def remember_process_snapshot(
    app_key: str,
    role: str,
    snapshot: Optional[ProcessSnapshot],
    *,
    script_choice: Optional[str] = None,
    logger=None,
) -> bool:
    if snapshot is None:
        return False

    config = cm.load(force=True)
    apps = config.get("apps")
    if not isinstance(apps, dict):
        return False

    app_cfg = apps.get(app_key)
    if not isinstance(app_cfg, dict):
        return False

    changed = False
    if role == "game":
        if _is_wrapper_process_name(snapshot.name) or _is_wrapper_process_path(snapshot.exe_path):
            _log(logger, "warning", f"[路径发现] {app_key} 忽略包装器game进程缓存: {ProcessSupervisor.describe_process(snapshot)}")
            return False
        cache = _target_discovery_cache(app_cfg, None, create=True)
        changed |= _set_if_changed(cache, "game_process_name", snapshot.name)
        if snapshot.exe_path:
            changed |= _set_if_changed(cache, "game_exe_path", snapshot.exe_path)
        game_cfg = app_cfg.get("game")
        if isinstance(game_cfg, dict):
            game_cfg["process_name"] = snapshot.name
    else:
        if _is_wrapper_process_name(snapshot.name) or _is_wrapper_process_path(snapshot.exe_path):
            _log(logger, "warning", f"[路径发现] {app_key} 忽略包装器script进程缓存: {ProcessSupervisor.describe_process(snapshot)}")
            return False
        option_cfg = _resolve_launcher_option(app_cfg, script_choice)
        option_name = _as_str(option_cfg.get("name")) if isinstance(option_cfg, dict) else None
        cache = _target_discovery_cache(app_cfg, option_name, create=True)
        changed |= _set_if_changed(cache, "script_process_name", snapshot.name)
        if snapshot.exe_path:
            changed |= _set_if_changed(cache, "script_exe_path", snapshot.exe_path)
            if Path(snapshot.exe_path).name.lower() not in WRAPPER_PROCESS_NAMES:
                changed |= _set_if_changed(cache, "launcher", snapshot.exe_path)

        target_cfg = option_cfg if isinstance(option_cfg, dict) else app_cfg
        target_cfg["script_process_name"] = snapshot.name

    if not changed:
        return False

    cm.save(config)
    _log(logger, "info", f"[路径发现] {app_key} 已回写{role}进程缓存: {ProcessSupervisor.describe_process(snapshot)}")
    return True