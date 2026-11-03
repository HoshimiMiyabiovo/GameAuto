"""脚本管理视图的槽：工具清单、安装/更新编排、路径自定义。

安装任务全局同时只跑一个（带宽与磁盘都友好）；进度经 tool_install 事件
推回主线程（bridge._handle_ui_event 分支），QML 订阅 toolInstallChanged
渲染进度条。取消用 threading.Event，安装器在下载分片级响应。
"""

import json
import logging
import os
import threading
import time
from pathlib import Path

from PySide6.QtCore import Slot

from .common import logger

# 进度事件节流：QML 不需要每 256KB 刷一次
_PROGRESS_EMIT_INTERVAL = 0.3


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
