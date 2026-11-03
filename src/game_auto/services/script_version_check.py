"""第三方脚本版本检测与更新提示。

职责：
- 从 games 注册表收集 script_tools 元数据，结合 app_config 解析出的安装路径，
  检测本地版本（目录名版本正则 → exe 文件版本信息）。
- 对照 GitHub Releases 最新 tag（releases/latest 端点），带每仓库 24h TTL 缓存
  与失败回退（沿用旧值并标 stale），绝不抛异常打断调用方。
- 组装报告行给配置向导"脚本版本"卡片；启动静默检查时返回新发现的更新清单
  （供 toast 提醒，同版本只提醒一次，notified 记录落盘）。

数据流：games/registry（静态元数据）+ settings.APPS（安装路径）→ 本模块
→ data/cache/script_versions.json（远端 tag 缓存 / 已提醒记录）
→ ui/bridge_views/setup.py（槽 + 后台线程）。

不变量与坑：
- games 模块不能反向 import settings（注册表扫描期约束），所以本模块对
  settings 包与 games.registry 的 import 全部放函数内，避免连带触发
  settings 初始化；paths 是叶子模块，可以顶层 import。
- GitHub 未认证限速 60 次/时/IP：只查 releases/latest 一个端点，TTL 24h，
  9 个脚本一天一查远低于限速。
- 本地版本源按 spec 策略（见 ScriptToolSpec 注释与 08-game-config.md 真值表）：
  ok 系读 pyappify app.json，March7th 读 version.txt，BetterGI 读 exe 元数据，
  ScriptChainer 用目录名，其余老实报未知。目录名默认不可信——原地自更新后
  目录名停留在旧版（2026-11-03 实盘查证：4 个"有更新"里 3 个是目录名误报）。
- 本地版本未知时只展示远端版本，不判"有更新"（无法比较就别误报）。
"""

import json
import logging
import os
import re
import tempfile
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..settings.paths import CACHE_DIR

logger = logging.getLogger(__name__)

CACHE_FILE = CACHE_DIR / "script_versions.json"
CACHE_TTL_SEC = 24 * 3600
REQUEST_TIMEOUT_SEC = 10
# 公开给 script_tool_installer 复用（同一套 GitHub 请求约定）
GITHUB_HEADERS = {
    "User-Agent": "game-auto-script-version-check",
    "Accept": "application/vnd.github+json",
}
_GITHUB_HEADERS = GITHUB_HEADERS
# 目录名/exe 名里的版本号：ok-ww-v1.4.81 → 1.4.81；March7thAssistant_v2.2.0.3_full → 2.2.0.3
# 前后排除数字与要求至少一个点，避免命中 "March7th" 的 7、纯目录名 "2026" 等
_VERSION_IN_NAME_RE = re.compile(r"(?<![0-9])[vV]?(\d+(?:\.\d+)+)(?![0-9])")


# ── 本地版本检测 ──────────────────────────────────────────────────


def _extract_version(text: str) -> str:
    """从任意文本提取首个点分数字版本（"v2026.10.31\\n" → "2026.10.31"）。

    所有来源（版本文件 / app.json / exe 元数据 / 目录名）统一走这里，
    保证带 v 前缀的 tag 和裸数字在展示与比较时口径一致。
    """
    m = _VERSION_IN_NAME_RE.search(text or "")
    return m.group(1) if m else ""


def _version_from_dirs(p: Path) -> str:
    """文件名/父目录名里的版本号，最深优先（ok-ww-v1.4.81\\bin\\ok-ww.exe 命中二级父目录）。"""
    for candidate in (p.name, p.parent.name, p.parent.parent.name,
                      p.parent.parent.parent.name):
        m = _VERSION_IN_NAME_RE.search(candidate or "")
        if m:
            return m.group(1)
    return ""


def _read_text_head(path: Path, limit: int = 200) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return ""


def _pyappify_version(path: str) -> str:
    """pyappify 安装器的应用清单版本：<root>/data/apps/<exe名>/app.json 的 current_version。

    ok 系（ok-ww/ok-ef/ok-nte）原地自更新后这是唯一真值；清单缺失（老版安装器
    布局变更）返回 ""，调用方按本地未知处理，不回落到会过期的目录名。
    """
    p = Path(path)
    manifest = p.parent / "data" / "apps" / p.stem / "app.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return str(data.get("current_version") or "")


def _exe_file_version(path: str) -> str:
    """读 exe 的 ProductVersion（BetterGI：目录无版本，更新器替换 exe，元数据跟真版本）。

    .bat/.cmd/无版本资源的文件返回 ""；win32api 缺失（非 Windows 环境）也返回 ""。
    注意：pyappify 包装器 exe（ok 系）的 ProductVersion 是框架版本，必须由
    spec.trust_exe_version=False 挡掉，不能靠这里区分。
    """
    if not path.lower().endswith((".exe", ".dll")):
        return ""
    try:
        import win32api
    except ImportError:
        logger.debug("[脚本版本] pywin32 不可用，跳过 exe 版本读取")
        return ""
    try:
        trans = win32api.GetFileVersionInfo(path, "\\VarFileInfo\\Translation")
        if trans:
            lang, codepage = trans[0]
            key = f"\\StringFileInfo\\{lang:04x}{codepage:04x}\\ProductVersion"
            val = win32api.GetFileVersionInfo(path, key)
            if val:
                return str(val).strip()
        # ProductVersion 缺失时回落到二进制 FileVersion
        info = win32api.GetFileVersionInfo(path, "\\")
        ms, ls = info.get("FileVersionMS", 0), info.get("FileVersionLS", 0)
        if ms or ls:
            return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
    except Exception as exc:  # 无版本资源的 exe 会抛 win32api.error，属预期
        logger.debug("[脚本版本] 读取 exe 版本失败 %s: %s", path, exc)
    return ""


def detect_local_version(path: str, spec: Any = None) -> str:
    """按 spec 的版本源策略提取本地版本号，取不到返回 ""（诚实报未知）。

    优先级（spec 为 ScriptToolSpec 时）：
    pyappify 清单 → version_file → exe 元数据（trust_exe_version）→
    目录名正则（trust_dir_version）→ ""。
    spec 为 None 时维持旧行为（目录正则 → exe），供纯函数测试与兜底。

    为什么目录名默认不可信：March7th/ok 系都支持原地自更新，更新后目录名
    停留在初次安装的旧版（实测 March7thAssistant_v2.2.0.3_full 目录里
    version.txt 已是 v2026.10.31），拿目录名判断会永久误报"有更新"。
    """
    if not path:
        return ""
    p = Path(path)
    if spec is not None:
        if getattr(spec, "pyappify", False):
            return _extract_version(_pyappify_version(str(p)))
        version_file = str(getattr(spec, "version_file", "") or "")
        if version_file:
            return _extract_version(_read_text_head(p.parent / version_file))
        if getattr(spec, "trust_exe_version", False):
            v = _extract_version(_exe_file_version(str(p)))
            if v:
                return v
        if getattr(spec, "trust_dir_version", False):
            return _version_from_dirs(p)
        return ""
    return _version_from_dirs(p) or _exe_file_version(str(p))


def resolve_install_path(app_cfg: Dict[str, Any], option_name: str) -> str:
    """从 app 配置解析脚本安装路径（正式字段优先，_discovery 缓存回落）。"""
    if not app_cfg:
        return ""
    if option_name:
        for opt in app_cfg.get("launcher_options") or []:
            if opt.get("name") == option_name and opt.get("launcher"):
                return str(opt["launcher"])
        disc_opt = ((app_cfg.get("_discovery") or {}).get("launcher_options")
                    or {}).get(option_name) or {}
        return str(disc_opt.get("launcher") or "")
    discovery = app_cfg.get("_discovery") or {}
    return str(app_cfg.get("launcher") or discovery.get("launcher")
               or discovery.get("script_exe_path") or "")


def collect_specs() -> List[Dict[str, Any]]:
    """汇总全部 script_tools 元数据并解析当前安装路径（settings 只在此 import）。"""
    from ..games.registry import iter_profiles
    from ..settings import APPS

    specs: List[Dict[str, Any]] = []
    for profile in iter_profiles():
        app_cfg = APPS.get(profile.app_key) or {}
        for tool in profile.script_tools:
            specs.append({
                "app_key": profile.app_key,
                "display_name": profile.display_name,
                "label": tool.label,
                "repo": tool.repo,
                "path": resolve_install_path(app_cfg, tool.option_name),
                "spec": tool,
            })
    return specs


# ── GitHub 最新版本 ───────────────────────────────────────────────


def fetch_release_with_assets(repo: str) -> Optional[Dict[str, Any]]:
    """查询 repo 最新 release 的 tag 与资产列表（script_tool_installer 也用）。

    返回 {"tag": str, "assets": [{"name","size","url"}]}；"" tag = 无 release
    （含 404）；None = 查询失败（网络/限速），调用方自行兜底。
    """
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    req = urllib.request.Request(url, headers=_GITHUB_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SEC) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            logger.info("[脚本版本] %s 无 release（404），按无更新源处理", repo)
            return {"tag": "", "assets": []}
        logger.info("[脚本版本] GitHub 查询失败 %s: HTTP %d", repo, exc.code)
        return None
    except Exception as exc:  # URLError/超时/解析失败：尽力而为，不抛
        logger.info("[脚本版本] GitHub 查询失败 %s: %s", repo, exc)
        return None
    assets = [
        {
            "name": str(item.get("name") or ""),
            "size": int(item.get("size") or 0),
            "url": str(item.get("browser_download_url") or ""),
        }
        for item in (data.get("assets") or [])
        if item.get("browser_download_url")
    ]
    return {"tag": str(data.get("tag_name") or ""), "assets": assets}


def fetch_latest_release(repo: str) -> Optional[str]:
    """查询 repo 的最新 Release tag（fetch_release_with_assets 的薄封装）。

    返回 tag（如 "v1.4.82"）；"" = 无 release（含 404，当作没有更新源）；
    None = 查询失败（网络/限速），调用方应沿用旧缓存。
    """
    info = fetch_release_with_assets(repo)
    if info is None:
        return None
    return info["tag"]


def parse_tag_version(tag: str) -> Optional[Tuple[int, ...]]:
    """从 tag 提取数字版本元组；v2.2.0.3 → (2,2,0,3)，无数字版本返回 None。"""
    m = re.search(r"(\d+(?:\.\d+)+)", tag or "")
    if not m:
        return None
    return tuple(int(x) for x in m.group(1).split("."))


def is_newer(latest: str, local: str) -> bool:
    """远端是否比本地新（两边都能解析成数字版本才比较，位数不足补零）。"""
    a, b = parse_tag_version(latest), parse_tag_version(local)
    if a is None or b is None:
        return False
    n = max(len(a), len(b))
    a += (0,) * (n - len(a))
    b += (0,) * (n - len(b))
    return a > b


# ── 缓存 ─────────────────────────────────────────────────────────


def _load_cache() -> Dict[str, Any]:
    try:
        if CACHE_FILE.exists():
            data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("repos", {})
                data.setdefault("notified", {})
                return data
    except Exception as exc:  # 损坏的缓存按空处理，不影响主流程
        logger.debug("[脚本版本] 缓存读取失败，按空缓存处理: %s", exc)
    return {"repos": {}, "notified": {}}


def _save_cache(cache: Dict[str, Any]) -> None:
    """同目录临时文件 + os.replace 原子落盘（约定同 settings.manager）。"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(CACHE_DIR), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CACHE_FILE)
    except Exception as exc:
        logger.debug("[脚本版本] 缓存写入失败: %s", exc)
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _cache_fresh(entry: Dict[str, Any]) -> bool:
    checked = str(entry.get("checked_at") or "")
    if not checked:
        return False
    try:
        checked_dt = datetime.fromisoformat(checked)
    except ValueError:
        return False
    return (datetime.now() - checked_dt).total_seconds() < CACHE_TTL_SEC


# ── 汇总入口 ─────────────────────────────────────────────────────


def check_all(force: bool = False,
              allow_network: bool = True) -> Tuple[List[Dict[str, Any]], List[str]]:
    """检查全部第三方脚本版本。

    返回 (报告行列表, 待提醒文案列表)：
    - 报告行见下方 row 结构，QML 按 status 决定状态 Tag 颜色与文案；
    - toasts 只在本次真正联网取到数据、发现"有更新"且该版本未提醒过时非空
      （allow_network=False 永远为空，避免页面加载就消费掉启动提醒额度）。
    force=True 忽略 TTL 强制联网；网络失败沿用旧值并标 stale。
    """
    specs = collect_specs()
    cache = _load_cache()
    repos: Dict[str, Any] = cache.setdefault("repos", {})
    notified: Dict[str, Any] = cache.setdefault("notified", {})
    now_iso = datetime.now().isoformat(timespec="seconds")
    dirty = False
    toasts: List[str] = []

    if allow_network:
        needed = sorted({s["repo"] for s in specs if s["repo"]})
        logger.info("[脚本版本] 开始检查 %d 个脚本更新源（force=%s）", len(needed), force)
        for repo in needed:
            entry = dict(repos.get(repo) or {})
            if not force and _cache_fresh(entry):
                continue
            tag = fetch_latest_release(repo)
            if tag is None:
                # 查询失败：沿用旧值并标 stale，UI 显示"网络失败"
                entry["stale"] = True
                entry["checked_at"] = now_iso
                repos[repo] = entry
                continue
            if tag != entry.get("tag"):
                logger.info("[脚本版本] %s 最新 %s（上次 %s）",
                            repo, tag or "无 release", entry.get("tag") or "未查过")
            repos[repo] = {"tag": tag, "checked_at": now_iso, "stale": False}
            dirty = True
    else:
        logger.info("[脚本版本] 读取脚本版本缓存（不联网）")

    rows: List[Dict[str, Any]] = []
    for s in specs:
        repo = s["repo"]
        entry = repos.get(repo) or {}
        latest = str(entry.get("tag") or "")
        local = detect_local_version(s["path"], s.get("spec"))
        stale = bool(entry.get("stale"))
        update = is_newer(latest, local) if (latest and local) else False
        if not s["path"]:
            status = "no_path"
        elif not repo:
            status = "no_repo"
        elif not latest:
            status = "no_release"
        elif not local:
            status = "local_unknown"
        elif update:
            status = "update"
        else:
            status = "latest"
        spec = s.get("spec")
        rows.append({
            "app_key": s["app_key"],
            "display_name": s["display_name"],
            "label": s["label"],
            "path": s["path"],
            "local": local,
            "latest": latest,
            "has_update": update,
            "repo": repo,
            "repo_url": f"https://github.com/{repo}/releases" if repo else "",
            "status": status,
            "stale": stale,
            "checked_at": str(entry.get("checked_at") or ""),
            # 脚本管理页/向导一键安装用（spec.tool_key 空 = 不进下载体系）
            "tool_key": str(getattr(spec, "tool_key", "") or ""),
            "downloadable": bool(getattr(spec, "asset_patterns", ()) and getattr(spec, "launcher_name", "")),
        })
        # 仅联网路径做提醒记账：页面缓存加载不该消费"同版本只提醒一次"的额度
        if allow_network and update and repo and notified.get(repo) != latest:
            notified[repo] = latest
            dirty = True
            local_txt = f"本地 {local}" if local else "本地版本未知"
            toasts.append(
                f"{s['label']} 有新版本 {latest}（{local_txt}），建议更新后再跑日常")

    if dirty:
        _save_cache(cache)
    return rows, toasts
