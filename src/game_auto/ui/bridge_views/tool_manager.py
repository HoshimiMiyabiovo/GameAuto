"""脚本管理视图的槽：工具清单、安装/更新编排、路径自定义。

安装任务全局同时只跑一个（带宽与磁盘都友好）；进度经 tool_install 事件
推回主线程（bridge._handle_ui_event 分支），QML 订阅 toolInstallChanged
渲染进度条。取消用 threading.Event，安装器在下载分片级响应。
"""

import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from PySide6.QtCore import Slot

from .common import logger

# 进度事件节流：QML 不需要每 256KB 刷一次
_PROGRESS_EMIT_INTERVAL = 0.3

# ── 内置 ok-ww GUI 启停 ─────────────────────────────────────────
# 内置副本 = <项目>/data/okww/ok-ww-v*/（pyappify 布局）。开关直启引擎
# pythonw main.py，而不是 ok-ww.exe 壳：壳会自我提权弹 UAC，且界面里的
# 「自动启动」是开机自启、不是开壳自动拉起引擎（2026-11-15 实锤）。
_OKWW_APP_REL = Path("data") / "apps" / "ok-ww"
_OKWW_PYTHONW_REL = _OKWW_APP_REL / "python" / "pythonw.exe"
_OKWW_MAIN_REL = _OKWW_APP_REL / "working" / "main.py"


def _okww_layout(root: Path) -> Optional[Dict[str, Path]]:
    """校验一个 ok-ww 安装根（pythonw + working/main.py 都在才算）。"""
    try:
        root = Path(root).resolve()
    except OSError:
        return None
    pythonw = root / _OKWW_PYTHONW_REL
    working = root / _OKWW_APP_REL / "working"
    main_py = working / "main.py"
    if pythonw.is_file() and main_py.is_file():
        return {"root": root, "pythonw": pythonw, "working": working, "main_py": main_py}
    return None


def _builtin_okww_candidates(app_cfg: Dict, data_dir: Path) -> List[Path]:
    """内置 ok-ww 根候选：配置指向的副本（仅限项目 data/ 下）+ data/okww 扫描。"""
    roots: List[Path] = []
    try:
        data_root = Path(data_dir).resolve()
    except OSError:
        return roots

    def add(value: object):
        text = str(value or "").strip()
        if not text:
            return
        path = Path(text)
        try:
            # 目录名可能自带点号（ok-ww-v3.8.3），必须先判目录再按后缀判文件
            if path.is_file() or (path.suffix and not path.is_dir()):
                path = path.parent
            resolved = path.resolve()
            if not resolved.is_relative_to(data_root):
                return
            if resolved not in roots:
                roots.append(resolved)
        except (OSError, ValueError):
            return

    discovery = app_cfg.get("_discovery") if isinstance(app_cfg, dict) else {}
    discovery = discovery if isinstance(discovery, dict) else {}
    add(discovery.get("script_exe_path"))
    add(app_cfg.get("launcher") if isinstance(app_cfg, dict) else "")

    module_cfg = app_cfg.get("module_mode") if isinstance(app_cfg, dict) else {}
    module_cfg = module_cfg if isinstance(module_cfg, dict) else {}
    runtime_root = Path(str(module_cfg.get("runtime_root") or "").strip() or ".")
    if runtime_root.name.lower() == "working" and runtime_root.parent.name.lower() == "ok-ww":
        # <安装根>/data/apps/ok-ww/working → parents[3] 是安装根
        if len(runtime_root.parents) >= 4:
            add(runtime_root.parents[3])

    okww_dir = data_root / "okww"
    if okww_dir.is_dir():
        children = [p for p in okww_dir.iterdir() if p.is_dir()]
        # 转正安装目录名以 ok-ww 开头，优先于试验源码树；同组内版本名降序
        children.sort(key=lambda p: p.name.lower(), reverse=True)
        children.sort(key=lambda p: 0 if p.name.lower().startswith("ok-ww") else 1)
        for child in children:
            add(child)
    return roots


def resolve_builtin_okww_paths(
    app_cfg: Optional[Dict] = None,
    data_dir: Optional[Path] = None,
) -> Optional[Dict[str, Path]]:
    """解析项目内置 ok-ww 安装（不碰外置/个人安装）。"""
    if app_cfg is None or data_dir is None:
        from ...settings import APPS
        from ...settings.paths import DATA_DIR
        if app_cfg is None:
            app_cfg = APPS.get("WutheringWaves") if isinstance(APPS, dict) else {}
        if data_dir is None:
            data_dir = DATA_DIR
    if not isinstance(app_cfg, dict):
        app_cfg = {}
    for root in _builtin_okww_candidates(app_cfg, Path(data_dir)):
        layout = _okww_layout(root)
        if layout is not None:
            return layout
    return None


def _is_builtin_okww_process(name: object, exe: object, cmdline: object, pythonw: Path) -> bool:
    """只认该安装自带的 pythonw + main.py，避开一条龙的 python.exe runner。"""
    if str(name or "").strip().lower() != "pythonw.exe":
        return False
    expected = os.path.normcase(os.path.abspath(str(pythonw)))
    actual = os.path.normcase(os.path.abspath(str(exe or "")))
    if not actual or actual != expected:
        return False
    return "main.py" in str(cmdline or "").lower()


def _find_builtin_okww_pids(paths: Dict[str, Path]) -> List[int]:
    import psutil

    pids: List[int] = []
    for proc in psutil.process_iter(["name", "exe", "cmdline"]):
        try:
            info = proc.info
            if _is_builtin_okww_process(
                info.get("name"), info.get("exe"),
                " ".join(info.get("cmdline") or []), paths["pythonw"],
            ):
                pids.append(proc.pid)
        except Exception:
            continue
    return pids


def _strip_project_src(pythonpath: str, project_src: str) -> str:
    """从 PYTHONPATH 剥掉项目 src（ok-ww 内嵌 python 命中本机 onnxocr 的坑）。"""
    target = project_src.replace("/", "\\").strip().rstrip("\\").lower()
    kept: List[str] = []
    for entry in str(pythonpath or "").split(os.pathsep):
        norm = entry.replace("/", "\\").strip().rstrip("\\").lower()
        if norm and norm != target:
            kept.append(entry)
    return os.pathsep.join(kept)


def _okww_child_env() -> Dict[str, str]:
    from ...settings.paths import BASE_DIR

    env = os.environ.copy()
    env["PYTHONPATH"] = _strip_project_src(
        env.get("PYTHONPATH", ""), str(Path(BASE_DIR) / "src"))
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    return env


class ToolManagerView:
    """脚本管理页：版本状态 + 托管目录安装/更新 + 根目录与每工具路径自定义。"""

    # ── 数据 ────────────────────────────────────────────────────────

    @Slot(result=str)
    def getToolManagerData(self) -> str:
        """工具清单（JSON）：版本检测行 + 下载能力 + 托管目录信息。"""
        try:
            from ...services.script_version_check import check_all
            rows, _toasts = check_all(allow_network=False)
        except Exception as exc:
            logger.warning("[Bridge] 读取工具清单失败: %s", exc)
            rows = []

        try:
            from ...services.script_tool_installer import (
                resolve_install_root, resolve_tool_install_dir)
            install_root = str(resolve_install_root())
        except Exception as exc:
            logger.warning("[Bridge] 解析安装根目录失败: %s", exc)
            install_root = ""
            rows = []

        out = []
        for r in rows:
            tool_key = str(r.get("tool_key") or "")
            path = str(r.get("path") or "")
            managed_dir = ""
            managed = False
            if tool_key:
                try:
                    from ...services.script_tool_installer import resolve_tool_install_dir
                    managed_dir = str(resolve_tool_install_dir(tool_key))
                except Exception as exc:
                    logger.debug("[Bridge] 解析托管目录失败 %s: %s", tool_key, exc)
                if managed_dir:
                    if not path:
                        managed = True  # 未安装 → 可装到托管目录
                    else:
                        try:
                            managed = Path(path).resolve().is_relative_to(
                                Path(managed_dir).resolve())
                        except (OSError, ValueError):
                            managed = False
            out.append({**r, "install_root": install_root,
                        "managed_dir": managed_dir, "managed": managed})
        return json.dumps(out, ensure_ascii=False)

    # ── 安装 / 更新 ────────────────────────────────────────────────

    @Slot(str)
    def installTool(self, tool_key: str):
        """启动安装/更新（后台线程）；同时只允许一个安装任务。"""
        if self._tool_install_running:
            self._request_toast("已有安装任务进行中", "#b66f1f")
            return
        from ...services.script_tool_installer import find_tool
        if find_tool(tool_key) is None:
            self._request_toast(f"未知工具: {tool_key}", "#b04b44")
            return
        self._tool_install_cancel = threading.Event()
        self._tool_install_running = True
        self._emit_tool_install(tool_key, "starting", 0, 0, "")
        threading.Thread(target=self._tool_install_worker, args=(tool_key,),
                         daemon=True).start()

    @Slot(str)
    def cancelInstall(self, tool_key: str):
        """请求取消当前安装（下载分片级生效，解压/换血阶段不可中断）。"""
        cancel = self._tool_install_cancel
        if cancel is not None and self._tool_install_running:
            cancel.set()
            logger.info("[Bridge] 已请求取消安装: %s", tool_key)

    def _emit_tool_install(self, tool_key: str, phase: str,
                           received: int, total: int, message: str):
        self.emit_event("tool_install", tool_key=tool_key, phase=phase,
                        received=received, total=total, message=message)

    def _tool_install_worker(self, tool_key: str):
        cancel = self._tool_install_cancel
        last_emit = 0.0

        def _progress(received: int, total: int):
            nonlocal last_emit
            now = time.time()
            if now - last_emit >= _PROGRESS_EMIT_INTERVAL or (total and received >= total):
                last_emit = now
                self._emit_tool_install(tool_key, "downloading", received, total, "")

        def _phase(p: str):
            self._emit_tool_install(tool_key, p, 0, 0, "")

        try:
            from ...services.script_tool_installer import install_tool
            result = install_tool(tool_key, progress=_progress, phase=_phase,
                                  stop_check=lambda: cancel.is_set())
            self._emit_tool_install(tool_key, "done", 0, 0,
                                    f"{result.get('tag') or ''} 安装完成")
        except Exception as exc:
            from ...services.script_tool_installer import InstallCancelled
            if isinstance(exc, InstallCancelled):
                self._emit_tool_install(tool_key, "idle", 0, 0, "已取消")
            else:
                logger.warning("[Bridge] 安装 %s 失败: %s", tool_key, exc)
                self._emit_tool_install(tool_key, "error", 0, 0, str(exc))
        finally:
            self._tool_install_running = False

    # ── 路径自定义 ────────────────────────────────────────────────

    @Slot(str, result=bool)
    def checkDirExists(self, path: str) -> bool:
        return bool(path) and os.path.isdir(path)

    @Slot(str, result=bool)
    def setToolInstallRoot(self, path: str) -> bool:
        """设置全局安装根目录（空串 = 恢复项目默认 data/script_tools）。"""
        try:
            from ...settings import manager as cm
            from ...settings import refresh_runtime_config
            cfg = cm.get_script_tools_config()
            cfg["install_root"] = path.strip()
            cm.update_script_tools_config(cfg)
            refresh_runtime_config(force=True)
            logger.info("[Bridge] 安装根目录已保存: %s", cfg["install_root"] or "(默认)")
            self._request_toast("安装根目录已保存", "#3b7d3b")
            return True
        except Exception as exc:
            logger.warning("[Bridge] 保存安装根目录失败: %s", exc)
            self._request_toast(f"保存失败: {exc}", "#b04b44")
            return False

    @Slot(str, str, result=bool)
    def setToolOverridePath(self, tool_key: str, path: str) -> bool:
        """为单个工具指定托管安装目录（空串 = 清除覆盖，回到全局根）。"""
        try:
            from ...settings import manager as cm
            from ...settings import refresh_runtime_config
            cfg = cm.get_script_tools_config()
            overrides = dict(cfg.get("overrides") or {})
            if path.strip():
                overrides[tool_key] = path.strip()
            else:
                overrides.pop(tool_key, None)
            cfg["overrides"] = overrides
            cm.update_script_tools_config(cfg)
            refresh_runtime_config(force=True)
            logger.info("[Bridge] %s 安装目录已保存: %s", tool_key,
                        overrides.get(tool_key) or "(全局根)")
            self._request_toast("安装目录已保存", "#3b7d3b")
            return True
        except Exception as exc:
            logger.warning("[Bridge] 保存 %s 安装目录失败: %s", tool_key, exc)
            self._request_toast(f"保存失败: {exc}", "#b04b44")
            return False

    # ── 内置 ok-ww GUI 启停 ───────────────────────────────────────

    @Slot(result=str)
    def getBuiltinOkwwState(self) -> str:
        """内置 ok-ww 运行状态（JSON）：installed/running/pid/root/busy。"""
        try:
            paths = resolve_builtin_okww_paths()
            pids = _find_builtin_okww_pids(paths) if paths else []
            payload = {
                "installed": bool(paths),
                "running": bool(pids),
                "pid": pids[0] if pids else 0,
                "root": str(paths["root"]) if paths else "",
                "busy": bool(getattr(self, "_okww_gui_busy", False)),
            }
        except Exception as exc:
            logger.warning("[Bridge] 读取内置 ok-ww 状态失败: %s", exc)
            payload = {"installed": False, "running": False, "pid": 0,
                       "root": "", "busy": False, "error": str(exc)}
        return json.dumps(payload, ensure_ascii=False)

    @Slot(bool, result=bool)
    def setBuiltinOkwwEnabled(self, enabled: bool) -> bool:
        """开关内置 ok-ww GUI：True = 直启引擎，False = 结束该引擎进程。"""
        if getattr(self, "_okww_gui_busy", False):
            self._request_toast("内置 ok-ww 正在启动/关闭中", "#b66f1f")
            return False
        if getattr(self, "_is_running", False):
            self._request_toast("一条龙任务运行中，暂不能启停内置 ok-ww", "#b66f1f")
            return False
        self._okww_gui_busy = True
        threading.Thread(target=self._builtin_okww_worker, args=(bool(enabled),),
                         name="builtin-okww-gui", daemon=True).start()
        return True

    def _builtin_okww_worker(self, enabled: bool):
        try:
            paths = resolve_builtin_okww_paths()
            if not paths:
                self._request_toast("未找到内置 ok-ww（项目 data/ 下无可用安装）", "#b04b44")
                return
            pids = _find_builtin_okww_pids(paths)
            if enabled:
                self._start_builtin_okww(paths, pids)
            else:
                self._stop_builtin_okww(paths, pids)
        except Exception as exc:
            logger.warning("[Bridge] 内置 ok-ww 启停失败: %s", exc)
            self._request_toast(f"内置 ok-ww 操作失败: {exc}", "#b04b44")
        finally:
            self._okww_gui_busy = False

    def _start_builtin_okww(self, paths: Dict[str, Path], pids: List[int]):
        if pids:
            self._request_toast("内置 ok-ww 已在运行", "#b66f1f")
            return
        from ...settings.paths import LOG_DIR

        log_path = Path(LOG_DIR) / "okww_gui_launch.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "ab", buffering=0) as handle:
            handle.write(
                f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] launch "
                f"{paths['pythonw']} main.py (cwd={paths['working']})\n".encode("utf-8"))
            proc = subprocess.Popen(
                [str(paths["pythonw"]), "main.py"],
                cwd=str(paths["working"]),
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=subprocess.STDOUT,
                env=_okww_child_env(),
            )

        deadline = time.time() + 10.0
        while time.time() < deadline:
            pids = _find_builtin_okww_pids(paths)
            if pids:
                logger.info("[Bridge] 内置 ok-ww 已启动 (PID=%s, root=%s)", pids[0], paths["root"])
                self._request_toast("内置 ok-ww 已启动", "#3b7d3b")
                return
            if proc.poll() is not None:
                break
            time.sleep(0.3)

        try:
            tail = log_path.read_text(encoding="utf-8", errors="replace")[-500:]
            logger.warning("[Bridge] 内置 ok-ww 启动失败，日志尾部: %s", tail)
        except OSError:
            pass
        self._request_toast(f"内置 ok-ww 启动失败（见 {log_path.name}）", "#b04b44")

    def _stop_builtin_okww(self, paths: Dict[str, Path], pids: List[int]):
        if not pids:
            self._request_toast("内置 ok-ww 未在运行", "#b66f1f")
            return
        from ...runtime.process_supervisor import ProcessSupervisor

        for pid in pids:
            ProcessSupervisor.safe_kill_process(pid, timeout=5)
        deadline = time.time() + 8.0
        while time.time() < deadline and _find_builtin_okww_pids(paths):
            time.sleep(0.3)
        if _find_builtin_okww_pids(paths):
            self._request_toast("内置 ok-ww 进程仍在，可能已提权（可手动结束）", "#b04b44")
            return
        logger.info("[Bridge] 内置 ok-ww 已关闭 (pids=%s, root=%s)", pids, paths["root"])
        self._request_toast("内置 ok-ww 已关闭", "#3b7d3b")
