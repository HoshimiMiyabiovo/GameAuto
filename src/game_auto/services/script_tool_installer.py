"""第三方脚本自动下载安装（脚本管理页的后端，更新永远手动触发）。

职责：
- 按 spec（ScriptToolSpec.asset_patterns）从 GitHub 最新 release 挑资产，
  流式下载（可取消、进度回调、ok 系 sha256 对账）→ 解压（zip stdlib +
  zip-slip 防护 / 7z 用 py7zr）→ 定位启动器 → 换血式落位 → 注册启动器
  路径到 app_config 并刷新 runtime。

数据流：games/registry（spec）+ settings.SCRIPT_TOOLS（根目录/每工具覆盖）
→ 本模块 → <install_root>/<tool_key>/ → settings.manager
（apps.<key>.launcher / launcher_options[*].launcher + _discovery 缓存）。

不变量与坑：
- **运行中的脚本/游戏拒绝安装**（换血要删运行中的文件），按
  script_process_name / game.process_name / cleanup_cmd_patterns 三路查进程
  （pyappify 引擎按名杀不到，必须查命令行，同 cleanup 的坑）。
- 自动安装只管理托管目录（install_root/<tool_key> 或 overrides 指定的目录）；
  当前配置路径指向别处的已有安装一律拒绝改动（2026-11-03 用户确认：
  已检测到安装的工具不受影响）。
- 更新 = 旧目录改名 .old → staging 就位 → 删 .old（best-effort）；中途崩溃
  可从 .old 手工恢复，日志会写明。
- zip 解压必须过 zip-slip 校验（成员路径逃逸出目标目录的一律拒）；7z 解压前
  同样检查成员名（py7zr 不默认防逃逸）。
- 安装器只在用户点按钮时跑：不缓存 release 信息（每次现查）；进度/收尾全放
  finally；06-robustness：daemon 线程 + 每请求 timeout + 分片级 stop_check。
"""

import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..settings.paths import SCRIPT_TOOLS_DEFAULT_DIR
from .script_version_check import GITHUB_HEADERS, fetch_release_with_assets

logger = logging.getLogger(__name__)

CHUNK_SIZE = 256 * 1024
REQUEST_TIMEOUT_SEC = 30
# 安装所需磁盘余量倍数（压缩包 + 解压副本 + 旧目录共存）
DISK_HEADROOM_FACTOR = 2.5


class InstallCancelled(Exception):
    """用户取消安装（下载分片级检查触发）。"""


# ── 路径解析 ─────────────────────────────────────────────────────────


def resolve_install_root() -> Path:
    """全局安装根目录；app_config 未配置时用项目内默认值。"""
    from ..settings import SCRIPT_TOOLS

    root = str((SCRIPT_TOOLS.get("install_root") or "")).strip()
    return Path(root) if root else SCRIPT_TOOLS_DEFAULT_DIR


def resolve_tool_install_dir(tool_key: str) -> Path:
    """工具的托管安装目录：overrides[tool_key] 优先，否则 <install_root>/<tool_key>。"""
    from ..settings import SCRIPT_TOOLS

    override = str((SCRIPT_TOOLS.get("overrides") or {}).get(tool_key) or "").strip()
    if override:
        return Path(override)
    return resolve_install_root() / tool_key


def find_tool(tool_key: str) -> Optional[Dict[str, Any]]:
    """按 tool_key 找 collect_specs 里的条目（含 spec / app_key / option_name）。"""
    from .script_version_check import collect_specs

    for s in collect_specs():
        if str(getattr(s.get("spec"), "tool_key", "") or "") == tool_key:
            return s
    return None


# ── 资产挑选 / 下载 ───────────────────────────────────────────────────


def pick_asset(assets: List[Dict[str, Any]], patterns: List[str]) -> Optional[Dict[str, Any]]:
    """按 patterns 顺序返回首个命中（re.search）的资产；都不中返回 None。"""
    for pattern in patterns or ():
        try:
            rx = re.compile(pattern)
        except re.error:
            logger.warning("[安装器] 资产正则非法，跳过: %s", pattern)
            continue
        for asset in assets:
            if rx.search(asset.get("name") or ""):
                return asset
    return None


def _download_to(url: str, dest: Path, expected_size: int = 0) -> None:
    """一次性下载（sha 清单这类小文件用，无进度/取消）。"""
    req = urllib.request.Request(url, headers=GITHUB_HEADERS)
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SEC) as resp:
        data = resp.read()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)


def _fetch_sha256_map(assets: List[Dict[str, Any]]) -> Dict[str, str]:
    """解析 release 里的 *_sha256.txt（"hash  文件名" 每行一条）；没有返回 {}。

    ok 系 release 都带这个清单，顺手校验；解析失败按无清单处理不阻断。
    """
    sha_asset = next((a for a in assets
                      if re.search(r"sha256\.txt$", a.get("name") or "", re.I)), None)
    if not sha_asset:
        return {}
    try:
        req = urllib.request.Request(sha_asset["url"], headers=GITHUB_HEADERS)
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SEC) as resp:
            text = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
        logger.info("[安装器] sha256 清单下载失败，跳过校验: %s", exc)
        return {}
    mapping: Dict[str, str] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            mapping[parts[1].lstrip("*").strip()] = parts[0].lower()
    return mapping


def download_file(url: str, dest: Path, expected_size: int = 0,
                  progress: Optional[Callable[[int, int], None]] = None,
                  stop_check: Optional[Callable[[], bool]] = None) -> None:
    """流式下载到 dest。

    256KB 分片，每片查一次 stop_check（06-robustness：0.25s 级可停）；
    取消抛 InstallCancelled；先写 .part 再原子改名；完成时对账字节数。
    网络异常包装成带指引的 RuntimeError（GitHub 下载 CDN 在国内网络
    经常握手超时，用户要知道去发布页手动下载这条路）。
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers=GITHUB_HEADERS)
    received = 0
    try:
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SEC) as resp:
                total = int(resp.headers.get("Content-Length") or expected_size or 0)
                with open(part, "wb") as f:
                    while True:
                        if stop_check is not None and stop_check():
                            raise InstallCancelled()
                        chunk = resp.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        f.write(chunk)
                        received += len(chunk)
                        if progress is not None:
                            progress(received, total)
        except InstallCancelled:
            raise
        except Exception as exc:
            raise RuntimeError(
                f"下载失败（GitHub 直连：{exc}）。可稍后重试，或从发布页手动下载后"
                "自行解压安装") from exc
        if total and received != total:
            raise RuntimeError(f"下载不完整：{received}/{total} 字节")
        os.replace(part, dest)
    finally:
        if part.exists():
            try:
                part.unlink()
            except OSError:
                pass


# ── 解压 ─────────────────────────────────────────────────────────────


def _assert_member_safe(member: str) -> None:
    """成员路径逃逸检查（zip-slip）：绝对路径 / 盘符 / .. 一律拒。"""
    name = str(member).replace("\\", "/")
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name) or ".." in name.split("/"):
        raise RuntimeError(f"压缩包成员路径可疑，拒绝解压: {member}")


def extract_archive(archive: Path, dest: Path) -> None:
    """解压到 dest（已存在的目录不清理，调用方负责 staging 隔离）。"""
    dest.mkdir(parents=True, exist_ok=True)
    suffix = archive.suffix.lower()
    if suffix == ".zip":
        with zipfile.ZipFile(archive) as zf:
            for info in zf.infolist():
                _assert_member_safe(info.filename)
            zf.extractall(dest)
    elif suffix == ".7z":
        import py7zr

        with py7zr.SevenZipFile(archive, mode="r") as z:
            for info in z.list():
                _assert_member_safe(info.filename)
            z.extractall(dest)
    else:
        raise RuntimeError(f"不支持的压缩格式: {archive.name}")


def _strip_single_root(staging: Path) -> None:
    """压缩包只有一个顶层目录时把它剥掉（ok 系 zip 根就是 ok-ww/ 这层）。

    这样托管目录 <root>/<tool_key>/ 直接就是工具根，与用户手动解压的习惯一致。
    顶层有多个条目（如 March7th exe 平铺在根）则不动。
    """
    entries = list(staging.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        inner = entries[0]
        for child in inner.iterdir():
            os.replace(child, staging / child.name)
        inner.rmdir()


def locate_launcher(staging: Path, launcher_name: str, max_depth: int = 3) -> Path:
    """在解压树里按文件名定位启动器（浅层优先，确定性排序）。"""
    matches = [p for p in staging.rglob(launcher_name)
               if p.is_file() and len(p.parts) - len(staging.parts) <= max_depth]
    if not matches:
        raise RuntimeError(f"解压后找不到启动器 {launcher_name}")
    matches.sort(key=lambda p: (len(p.parts), str(p).lower()))
    return matches[0]


# ── 进程守卫 ─────────────────────────────────────────────────────────


def describe_running_processes(app_cfg: Dict[str, Any]) -> str:
    """该游戏的脚本/游戏进程若在运行，返回描述；没有返回 ""。

    三路都查：script_process_name（含 launcher_options 里的）、游戏主进程、
    cleanup_cmd_patterns（pyappify 的 pythonw 引擎按名杀不到，必须查命令行）。
    """
    from ..runtime.process_supervisor import ProcessSupervisor

    hits: List[str] = []
    name_candidates: List[tuple] = []
    direct = str(app_cfg.get("script_process_name") or "")
    if direct:
        name_candidates.append((direct, None))
    for opt in app_cfg.get("launcher_options") or []:
        opt_name = str(opt.get("script_process_name") or "")
        if opt_name:
            name_candidates.append((opt_name, None))
    game_name = str((app_cfg.get("game") or {}).get("process_name") or "")
    if game_name:
        name_candidates.append((game_name, None))
    for pat in app_cfg.get("cleanup_cmd_patterns") or []:
        pname = str(pat.get("name") or "")
        cmdline = str(pat.get("cmdline") or "")
        if pname and cmdline:
            name_candidates.append((pname, re.escape(cmdline)))

    seen = set()
    for name_pattern, cmd_pattern in name_candidates:
        key = (name_pattern, cmd_pattern)
        if key in seen:
            continue
        seen.add(key)
        try:
            procs = ProcessSupervisor.find_processes(name_pattern, cmd_pattern=cmd_pattern)
        except Exception as exc:  # 进程枚举失败不阻断安装，但记录
            logger.debug("[安装器] 进程查询失败 %s: %s", name_pattern, exc)
            continue
        for proc in procs:
            try:
                hits.append(f"{proc.name()}(PID {proc.pid})")
            except Exception:
                hits.append(name_pattern)
    return ", ".join(hits)


# ── 路径注册 ─────────────────────────────────────────────────────────


def register_launcher_path(app_key: str, option_name: str, launcher: str) -> None:
    """安装完成后把启动器路径写进 app_config（apps + _discovery 双写）并刷新。

    写法与 ui/bridge_views/setup.py 的 saveSetupPaths 一致：顶层 launcher /
    launcher_options[i].launcher 正式字段 + _discovery 缓存，保证 defaults
    合并与 discovery 都认这份数据。
    """
    from ..settings import manager as cm

    config = cm.load()
    apps = config.setdefault("apps", {})
    app_cfg = apps.setdefault(app_key, {})
    discovery = app_cfg.setdefault("_discovery", {})
    if option_name:
        opts = app_cfg.setdefault("launcher_options", [])
        idx = next((i for i, o in enumerate(opts) if o.get("name") == option_name), None)
        if idx is None:
            opts.append({"name": option_name})
            idx = len(opts) - 1
        opts[idx]["launcher"] = launcher
        discovery.setdefault("launcher_options", {})
        discovery["launcher_options"][option_name] = {"launcher": launcher}
    else:
        app_cfg["launcher"] = launcher
        discovery["launcher"] = launcher
    cm.save(config)

    from ..settings import refresh_runtime_config

    refresh_runtime_config(force=True)
    logger.info("[安装器] %s 启动器路径已注册: %s", app_key, launcher)


# ── 编排 ─────────────────────────────────────────────────────────────


def install_tool(tool_key: str,
                 progress: Optional[Callable[[str, int, int], None]] = None,
                 phase: Optional[Callable[[str], None]] = None,
                 stop_check: Optional[Callable[[], bool]] = None) -> Dict[str, Any]:
    """下载安装/更新一个工具（阻塞，须在后台线程调用）。

    progress(received, total) 只在下载阶段回调；phase("downloading"/
    "extracting"/"installing"/"registering") 标记阶段。目标目录已存在则走
    换血式更新。返回 {"tool_key","tag","launcher","install_dir"}。
    """
    tool = find_tool(tool_key)
    if not tool:
        raise RuntimeError(f"未知工具: {tool_key}")
    spec = tool["spec"]
    if not (getattr(spec, "asset_patterns", ()) and getattr(spec, "launcher_name", "")):
        raise RuntimeError(f"{spec.label} 未配置自动下载，请按发布页指引手动安装")

    from ..settings import APPS

    app_cfg = APPS.get(tool["app_key"]) or {}

    # 守卫一：进程在跑不动手
    running = describe_running_processes(app_cfg)
    if running:
        raise RuntimeError(f"脚本或游戏正在运行（{running}），请先退出再安装")

    install_dir = resolve_tool_install_dir(tool_key)
    current_path = str(tool.get("path") or "")
    if current_path and install_dir not in Path(current_path).resolve().parents \
            and Path(current_path).resolve() != install_dir:
        raise RuntimeError(
            f"当前 {spec.label} 安装在托管目录之外（{current_path}），"
            "自动安装只管理 data/script_tools（或自定义）目录；请先用工具自带更新，"
            "或在脚本管理页把路径改回托管目录")

    # 守卫二：磁盘余量
    info = fetch_release_with_assets(spec.repo)
    if info is None:
        raise RuntimeError("GitHub 查询失败，请检查网络后重试")
    asset = pick_asset(info["assets"], list(spec.asset_patterns))
    if not asset:
        raise RuntimeError(f"最新 release（{info['tag'] or '无'}）没有匹配的安装包")
    root_parent = install_dir.parent
    root_parent.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(str(root_parent)).free
    need = int(asset["size"] * DISK_HEADROOM_FACTOR)
    if free < need:
        raise RuntimeError(f"磁盘空间不足：需要约 {need // 1048576}MB，剩余 {free // 1048576}MB")

    if phase is not None:
        phase("downloading")
    logger.info("[安装器] 开始安装 %s <- %s（%dMB）",
                tool_key, asset["name"], asset["size"] // 1048576)
    tmp_root = Path(tempfile.mkdtemp(prefix=f".{tool_key}_install_", dir=str(root_parent)))
    try:
        archive_path = tmp_root / asset["name"]

        def _progress(received: int, total: int) -> None:
            if stop_check is not None and stop_check():
                raise InstallCancelled()
            if progress is not None:
                progress(received, total)

        download_file(asset["url"], archive_path, asset["size"], _progress, stop_check)

        sha_map = _fetch_sha256_map(info["assets"])
        if asset["name"] in sha_map:
            digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
            if digest != sha_map[asset["name"]]:
                raise RuntimeError("sha256 校验失败，安装包可能不完整或被篡改")
            logger.info("[安装器] sha256 校验通过: %s", asset["name"])

        if phase is not None:
            phase("extracting")
        staging = tmp_root / "staging"
        extract_archive(archive_path, staging)
        _strip_single_root(staging)
        launcher = locate_launcher(staging, spec.launcher_name)

        if phase is not None:
            phase("installing")
        if install_dir.exists():
            old_dir = install_dir.with_name(install_dir.name + ".old")
            if old_dir.exists():
                shutil.rmtree(old_dir, ignore_errors=True)
            os.replace(install_dir, old_dir)
        try:
            os.replace(staging, install_dir)
        except OSError:
            # 就位失败：把旧目录还回去，不留半残状态
            backup = install_dir.with_name(install_dir.name + ".old")
            if backup.exists() and not install_dir.exists():
                os.replace(backup, install_dir)
            raise

        new_launcher = install_dir / launcher.relative_to(staging)
        if phase is not None:
            phase("registering")
        register_launcher_path(tool["app_key"], str(getattr(spec, "option_name", "") or ""),
                               str(new_launcher))

        # 成功后清旧目录（失败仅记日志，.old 留着手工处理）
        old_dir = install_dir.with_name(install_dir.name + ".old")
        if old_dir.exists():
            shutil.rmtree(old_dir, ignore_errors=True)
            if old_dir.exists():
                logger.warning("[安装器] 旧目录清理失败（可能被占用），保留在 %s", old_dir)

        logger.info("[安装器] %s 安装完成 tag=%s launcher=%s",
                    tool_key, info["tag"], new_launcher)
        return {
            "tool_key": tool_key,
            "tag": info["tag"],
            "launcher": str(new_launcher),
            "install_dir": str(install_dir),
        }
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)
