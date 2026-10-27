"""路径自动发现与缓存（"配置自动填空"器）。

解决的核心问题：项目要启动多款游戏与第三方自动化脚本（BetterGI/ok-ww/ok-ef/
whimbox/March7th…），每个都有启动器路径、游戏进程名、日志目录要配——本模块在
任务启动前（prepare_app_for_runtime，由 automation_controller 调用）自动
找出、验证、缓存这些路径，游戏搬家/更新导致路径失效时自愈。

**四层解析瀑布**（_discover_launcher_path，按可靠性递减）：
  1. 配置里已有且文件存在的路径 → 直接用；
  2. `_discovery` 缓存（app_config.json 每个应用下的隐藏键）命中 → 用缓存；
  3. 目标进程正在运行 → 从运行进程抓真实 exe 路径（最准，比扫盘优先）；
  4. 全盘文件搜索（_search_file_by_names）：候选名 × 搜索根（_search_roots），
     限深 FILE_SEARCH_DEPTH 层、跳过 .git/.venv/node_modules。

**进程快照回写**：任务实际跑起来后 remember_process_snapshot 把真实进程名与
exe 路径写回缓存，越用越准。**包装器识别**：第三方脚本常用 bat/python 外壳
拉起真身，WRAPPER_PROCESS_NAMES 列出必须忽略的外壳名——否则 python.exe
会被缓存成"脚本进程"，后续进程匹配/清理全部错位（日志里的
"脚本入口进程为包装器"警告即来源于此）。

**缓存位置与回读**：全部结果存 app_config.json 各应用配置的 `_discovery` 键
（对 UI 不可见的下划线开头键），apply_runtime_discovery 在配置加载时把缓存
回填到正式字段（launcher/game.process_name/log_dir 等）。

**安全边界**：全盘搜索只读（os.walk，不打开文件内容）；写回的路径会被
当作可执行文件启动——因此不要导入来路不明的 app_config.json 覆盖这些字段。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from . import manager as cm
from .paths import BASE_DIR
from ..runtime.process_supervisor import ProcessSnapshot, ProcessSupervisor


DISCOVERY_KEY = "_discovery"
# 日志监控器配置键：_ensure_monitor_log_dirs 只为这些键自动找 log_dir
MONITOR_CONFIG_KEYS = (
    "bettergi_log_monitor",
    "ok_script_log_monitor",
    "ok_ww_log_monitor",
    "whimbox_log_monitor",
)
# 已知"外壳"进程：第三方脚本用它们拉起真身（bat→cmd、py→python），
# 缓存进程名/路径时必须跳过，否则真进程定位与清理全部错位
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
FILE_SEARCH_DEPTH = 4   # 全盘找 exe 的最大深度（相对搜索根）
DIR_SEARCH_DEPTH = 3    # 找日志目录的最大深度（相对基准目录）


def _as_str(value: object) -> str:
    return str(value or "").strip()


def _existing_file(path_value: object) -> str:
    """路径存在且是文件则原样返回，否则空串（空/失效路径统一归零）。"""
    path_str = _as_str(path_value)
    if not path_str:
        return ""
    path = Path(path_str)
    return str(path) if path.is_file() else ""


def _existing_dir(path_value: object) -> str:
    """路径存在且是目录则原样返回，否则空串。"""
    path_str = _as_str(path_value)
    if not path_str:
        return ""
    path = Path(path_str)
    return str(path) if path.is_dir() else ""


def _is_wrapper_process_name(name_value: object) -> bool:
    return _as_str(name_value).lower() in WRAPPER_PROCESS_NAMES


def _is_wrapper_process_path(path_value: object) -> bool:
    """路径的文件名部分是否属于已知外壳进程。"""
    path_str = _as_str(path_value)
    if not path_str:
        return False
    return Path(path_str).name.lower() in WRAPPER_PROCESS_NAMES


def _sanitize_process_cache(cache: Dict, *, include_game: bool = True):
    """从一份 _discovery 缓存里剔除外壳进程的 script/game 名与路径（缓存自净）。"""
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
    """应用正式字段（script_process_name/game.process_name/exe_path）中的外壳覆盖同样剔除。

    用户手填了 python.exe 之类的值时，这里会在配置加载阶段静默清掉并依赖缓存重建。
    """
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
    """赋值并返回是否真的发生变化（无变化不触发配置落盘）。"""
    if parent.get(key) == value:
        return False
    parent[key] = value
    return True


def _log(logger, level: str, message: str):
    """logger 可能为 None（调用方未传），按级别字符串安全转发。"""
    if logger is None:
        return
    log_func = getattr(logger, level, None)
    if callable(log_func):
        log_func(message)


def _search_roots(hints: Iterable[object]) -> List[Path]:
    """构建全盘搜索根（去重、必须真实存在）。

    覆盖范围是刻意选的：项目自身及上级 → 系统程序目录 → 用户目录
    （桌面/下载/文档是游戏压缩包最常落的地方）→ 四个盘根 → 最后是调用方
    提示路径（已配置/已缓存的路径及其盘符，优先级最高放末尾但命中最快）。
    """
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
    """按候选文件名全盘搜索，返回第一个命中的完整路径（无则空串）。

    只匹配文件名（大小写不敏感），不读内容；os.walk 限深 FILE_SEARCH_DEPTH，
    跳过隐藏目录与 .git/.venv/node_modules。搜索根顺序即命中优先级。
    """
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
    """按名字选 launcher_options 中的一项；未指定名字时退回第一项，无选项返回 None。"""
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
    """定位/创建 _discovery 缓存字典；option_name 非空时按启动器选项分桶（多脚本互不污染）。"""
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
    """定位/创建 _discovery.monitors 下某个监控器的缓存子字典。"""
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
    """汇总启动器的候选文件名：配置路径文件名、缓存、脚本名加 .exe/.bat/.cmd 扩展。"""
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
    """瀑布第 3 层：目标进程正在运行时直接抓其 exe 路径（比扫盘准）。"""
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
    """把发现的启动器路径回写正式配置与缓存，返回是否有变更。"""
    changed = False
    if launcher_path:
        target_cfg["launcher"] = launcher_path
        changed |= _set_if_changed(target_cache, "launcher", launcher_path)
    return changed


def _discover_launcher_path(app_key: str, app_cfg: Dict, target_cfg: Dict, option_name: Optional[str], logger) -> Tuple[str, bool]:
    """解析启动器路径——四层瀑布（见模块 docstring），返回 (路径, 是否有变更)。

    target_cfg 是"本次要启动的目标"：选择了 launcher_option（如崩铁的
    March7th/StarRailOneDragon 双脚本）时是选项自身的配置，否则是应用配置。
    缓存键随 option_name 分桶，两个脚本的发现结果互不污染。
    """
    target_cache = _target_discovery_cache(app_cfg, option_name, create=True)
    changed = False

    # 第 1 层：配置路径已是有效文件（权威来源，用户手填或上次回写）
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
    """从已知路径推导日志搜索基准：路径自身（或父目录）+ 父 + 祖父，去重。"""
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
    """在启动器/脚本路径附近找日志目录（log/ 或 logs/，限深 DIR_SEARCH_DEPTH）。

    判定标准：目录里能 glob 到 file_pattern 的才算命中；有兜底 fallback
    （目录存在但还没产出日志时先用着，等日志出现监控器自然开始读）。
    """
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
    """为 MONITOR_CONFIG_KEYS 里的监控器补齐 log_dir：现有配置 > 缓存 > 按启动器路径自动发现。"""
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
    """配置加载时把 _discovery 缓存回填到正式字段（launcher/进程名/log_dir），不落盘。"""
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
    """对 runtime_config 里全部应用批量执行缓存回填（settings/manager 加载后调用）。"""
    apps = runtime_config.get("apps")
    if not isinstance(apps, dict):
        return runtime_config
    for app_cfg in apps.values():
        if isinstance(app_cfg, dict):
            _sanitize_runtime_process_overrides(app_cfg)
            _apply_cached_discovery(app_cfg)
    return runtime_config


def prepare_app_for_runtime(app_key: str, script_choice: Optional[str] = None, logger=None) -> bool:
    """任务启动前的总入口：解析启动器路径 + 补齐日志目录（automation_controller 调用）。

    返回是否有任何配置变更（有则已顺手 cm.save 落盘）。
    抛异常或 app 不存在返回 False——调用方按"路径未变"继续，不应因此中断任务。
    """
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
    """把任务实际运行时的进程快照写回 _discovery 缓存（automation_controller 调用）。

    role="game" 记游戏进程（Endfield.exe/YuanShen.exe…），否则记脚本进程。
    返回是否有变更（有变更才会触发配置落盘）。包装器进程（python/cmd 外壳）
    直接忽略——剥洋葱找到真身后这里拿到的就是真身。
    """
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