# -*- coding: utf-8 -*-
"""磁盘占用统计（磁盘管理页的服务层，纯逻辑、无窗口操作）。

数据流：collect_targets() 收集三组扫描目标（游戏 / 第三方脚本 / 本工具 data），
scan_all() 逐个 scan_target() 求目录字节数并经 on_item_done 回调渐进上报（bridge
后台线程消费，QML 行级刷新），结束写 data/cache/disk_usage_cache.json 供下次
打开页面秒显。UI 覆盖目录存 app_config 顶层 `disk_scan_overrides`（manager.py
update_disk_scan_overrides 写）。

不变量与坑：
- 游戏扫描根 = `_discovery.game_exe_path` 所在目录上溯 `profile.disk_root_up_levels`
  层（exe 深度因游戏而异，见 games/base.py 字段注释）；discovery 没回填的游戏
  exists=False，不猜路径。
- 脚本目录与游戏根/data 目录重叠时跳过（is_relative_to），避免同一段磁盘算两遍。
- 扫描不 follow symlink（Windows 下防环 + 用户不关心挂载点内部）；目录不可读
  只 debug 跳过，绝不让单个坏目录炸掉整轮统计。
- 取消走 stop_check：scan_target 内部每 256 个条目查一次（几十万文件的目录
  里不能等到下一个游戏才停）。
"""
import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..settings.paths import CACHE_DIR, DATA_DIR

LOGGER = logging.getLogger("GameAutomation")

CACHE_FILE = CACHE_DIR / "disk_usage_cache.json"

# scan_target 的取消检查粒度（条目数）：太密白耗 CPU，太疏取消延迟可感知
_STOP_CHECK_INTERVAL = 256

StopCheck = Optional[Callable[[], bool]]
ItemDone = Callable[[Dict[str, Any], int, int], None]


class ScanCancelled(Exception):
    """用户请求停止（扫描器在遍历中响应 stop_check）。"""


def _up_levels(path: Path, levels: int) -> Path:
    """从 path 向上取 levels 层父目录；到盘符根就停（不越出盘符）。"""
    cur = path
    for _ in range(max(0, int(levels))):
        parent = cur.parent
        if parent == cur:
            break
        cur = parent
    return cur


def _norm_root(path: str) -> str:
    """目录路径归一化（绝对化 + 小写），用于去重与包含关系判断。"""
    try:
        return str(Path(path).resolve()).rstrip("\\/").lower()
    except (OSError, ValueError):
        return str(path or "").rstrip("\\/").lower()


def _is_inside(child: str, parent: str) -> bool:
    """child 是否在 parent 目录内（含相等）。任意一方解析失败按 False。"""
    c, p = _norm_root(child), _norm_root(parent)
    if not c or not p:
        return False
    try:
        return Path(c).is_relative_to(Path(p))
    except (OSError, ValueError):
        return False


def _game_targets() -> List[Dict[str, Any]]:
    """8 个注册游戏的扫描根：app_config 覆盖 > exe 上溯推导 > 缺失（不猜路径）。

    exe 来源两级：`_discovery.game_exe_path`（进程快照维护，最准）；缺失时回落
    `apps.<key>.launcher`——仅当 profile.script_tools 为空（base.py 口径：
    无第三方脚本的游戏，launcher 就是游戏本体，如白荆内置流程）。有第三方脚本
    的游戏 launcher 可能是脚本 exe（原神的 BetterGI），回落会扫错根。
    """
    from ..games import registry
    from ..settings import manager

    overrides = manager.get("disk_scan_overrides") or {}
    out: List[Dict[str, Any]] = []
    for profile in registry.iter_profiles():
        app = manager.get_app(profile.app_key) or {}
        discovery = app.get("_discovery") or {}
        exe = str(discovery.get("game_exe_path") or "").strip()
        if not exe and not profile.script_tools:
            exe = str(app.get("launcher") or "").strip()
        root = str(_up_levels(Path(exe).parent, profile.disk_root_up_levels)) if exe else ""
        override = str(overrides.get(profile.game_key) or "").strip()
        if override:
            root = override
        out.append({
            "key": profile.game_key,
            "group": "games",
            "name": profile.display_name,
            "path": root,
            "exists": bool(root) and os.path.isdir(root),
        })
    return out


def _script_targets(game_roots: List[str]) -> List[Dict[str, Any]]:
    """第三方脚本目录，两个来源（都是现成解析器，不自己猜）：
    - `script_version_check.collect_specs()`：script_tools 各工具安装路径
      （内部已处理 launcher_options 方案名 + _discovery 回落——崩铁/绝区零的
      脚本 exe 只在 launcher_options 里，单 launcher 字段不可靠）；
    - app_config 顶层 `flow_exe_paths`：GUI 配置的独立程序（XM/CJ/MiniMax 等），
      支持通配符（exe 名带版本号），glob 取最新后取其目录。
    目录去重；与游戏根或 data/ 重叠的跳过（同一段磁盘不算两遍）。"""
    import glob as _glob

    from ..settings import manager
    from .script_version_check import collect_specs

    roots: Dict[str, str] = {}

    def _add_exe(exe: Any) -> None:
        exe = str(exe or "").strip()
        if not exe:
            return
        if any(ch in exe for ch in "*?["):
            matches = sorted(_glob.glob(exe), key=os.path.getmtime, reverse=True)
            exe = matches[0] if matches else ""
            if not exe:
                return
        root = Path(exe.replace("/", "\\")).parent
        key = _norm_root(str(root))
        if key and key not in roots:
            roots[key] = str(root)

    for spec in collect_specs():
        _add_exe(spec.get("path"))
    try:
        overrides = manager.load().get("flow_exe_paths") or {}
    except Exception as exc:  # noqa: BLE001 - 配置读不到只少脚本行
        LOGGER.debug("[磁盘统计] flow_exe_paths 读取失败: %s", exc)
        overrides = {}
    for exe in overrides.values():
        _add_exe(exe)

    out: List[Dict[str, Any]] = []
    for key, root in sorted(roots.items()):
        if _is_inside(root, str(DATA_DIR)):
            continue
        if any(_is_inside(root, gr) for gr in game_roots if gr):
            continue
        out.append({
            # key 用归一化目录（顺序稳定、游戏根变了也不漂移），缓存回填靠它对行
            "key": f"script:{key}",
            "group": "scripts",
            "name": Path(root).name or root,
            "path": root,
            "exists": os.path.isdir(root),
        })
    return out


def _data_targets() -> List[Dict[str, Any]]:
    """本工具 data/ 占用：顶层子目录逐个一行，顶层散文件并成一行。"""
    out: List[Dict[str, Any]] = []
    if not DATA_DIR.is_dir():
        return out
    loose_files = 0
    for child in sorted(DATA_DIR.iterdir(), key=lambda p: p.name):
        if child.is_dir():
            out.append({
                "key": f"data:{child.name}",
                "group": "data",
                "name": child.name,
                "path": str(child),
                "exists": True,
            })
        else:
            try:
                loose_files += child.stat().st_size
            except OSError:
                pass
    if loose_files:
        out.append({
            "key": "data:<root>",
            "group": "data",
            "name": "（data 根散文件）",
            "path": "",
            "exists": True,
            "size": loose_files,  # 散文件在收集期就已求和，扫描期直接沿用
        })
    return out


def collect_targets() -> List[Dict[str, Any]]:
    """三组扫描目标（games/scripts/data）合并清单，顺序稳定供进度计数。"""
    games = _game_targets()
    scripts = _script_targets([g["path"] for g in games if g["path"]])
    data = _data_targets()
    return games + scripts + data


def scan_target(path: str, stop_check: StopCheck = None) -> int:
    """求目录总字节数（含全部子层级）。路径为空/不存在/是文件返回 0。

    **path 为空必须拦**：`Path("")` 在 Python 里等于当前目录 `.`，2026-11-08
    首跑实锤——「data 根散文件」行 path 为空，结果把整个项目目录（含 .venv）
    扫成 27.69 GB。
    迭代栈实现（几十万文件的现代游戏目录，递归会爆栈也慢）；symlink 不 follow；
    单个目录 PermissionError 只 debug 跳过。stop_check 两档粒度：每个目录出栈时
    查一次（小目录也能即时取消）+ 每 256 个条目查一次（大目录里的高频响应），
    命中抛 ScanCancelled。
    """
    if not str(path or "").strip():
        return 0
    root = Path(path)
    if not root.is_dir():
        return 0
    total = 0
    checked = 0
    stack = [str(root)]
    while stack:
        if stop_check is not None and stop_check():
            raise ScanCancelled()
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    checked += 1
                    if stop_check is not None and checked % _STOP_CHECK_INTERVAL == 0 \
                            and stop_check():
                        raise ScanCancelled()
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        else:
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError as exc:  # noqa: PERF203 - 单条目坏掉不炸整轮
                        LOGGER.debug("[磁盘统计] 条目不可读 %s: %s", entry.path, exc)
        except PermissionError as exc:
            LOGGER.debug("[磁盘统计] 目录不可读 %s: %s", current, exc)
    return total


def scan_all(
    targets: List[Dict[str, Any]],
    stop_check: StopCheck = None,
    on_item_done: Optional[ItemDone] = None,
) -> List[Dict[str, Any]]:
    """顺序扫描全部目标，每完成一个回调 on_item_done(item, done, total)，结束写缓存。

    返回带 size 的完整清单（顺序同 targets）；取消抛 ScanCancelled、不写缓存
    （保留上次完整结果）。
    """
    items: List[Dict[str, Any]] = []
    total = len(targets)
    for idx, target in enumerate(targets):
        if stop_check is not None and stop_check():
            raise ScanCancelled()
        item = dict(target)
        if str(item.get("path") or "").strip():
            try:
                item["size"] = scan_target(str(item["path"]), stop_check)
            except ScanCancelled:
                raise
            except OSError as exc:
                LOGGER.debug("[磁盘统计] 目标扫描失败 %s: %s", item["path"], exc)
                item["size"] = 0
        # path 为空的行（data 根散文件）不扫目录：size 用收集期已求和的值
        items.append(item)
        if on_item_done is not None:
            on_item_done(item, idx + 1, total)
    _save_cache(items)
    return items


def load_cache() -> Optional[Dict[str, Any]]:
    """读上次完整扫描缓存 {ts, items}；无缓存/损坏返回 None（页面显示未扫描态）。"""
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception as exc:  # noqa: BLE001 - 缓存损坏按未扫描处理，不阻断页面
        LOGGER.debug("[磁盘统计] 缓存读取失败: %s", exc)
        return None
    return data if isinstance(data, dict) and isinstance(data.get("items"), list) else None


def _save_cache(items: List[Dict[str, Any]]) -> None:
    try:
        from ..settings import manager

        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        manager.atomic_write_json(CACHE_FILE, {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "items": items,
        })
    except Exception as exc:  # noqa: BLE001 - 缓存写失败只影响下次秒显
        LOGGER.debug("[磁盘统计] 缓存写入失败: %s", exc)


def disk_overview(paths: List[str]) -> List[Dict[str, Any]]:
    """对 paths 涉及的盘符做剩余空间总览（去重排序）；单盘失败静默跳过。"""
    drives = set()
    for p in paths:
        drive = os.path.splitdrive(str(p or ""))[0]
        if drive:
            drives.add(drive + ("\\" if len(drive) == 2 else ""))
    out: List[Dict[str, Any]] = []
    for drive in sorted(drives):
        try:
            usage = shutil.disk_usage(drive)
        except OSError as exc:
            LOGGER.debug("[磁盘统计] 盘符空间读取失败 %s: %s", drive, exc)
            continue
        out.append({
            "drive": drive.rstrip("\\"),
            "total": usage.total,
            "free": usage.free,
            "used": usage.used,
        })
    return out
