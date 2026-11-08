"""磁盘管理视图的槽：游戏/第三方脚本/本工具 data 的占用统计。

扫描全局同时只跑一个（全盘目录遍历是重 IO）；逐目标完成经 disk_scan 事件
推回主线程（bridge._handle_ui_event 分支），QML 订阅 diskScanChanged 行级
刷新 + 进度 x/y。取消用 threading.Event，扫描器在目录遍历间响应（见
services/disk_usage.py 的 _STOP_CHECK_INTERVAL）。完整结果由服务层写缓存
data/cache/disk_usage_cache.json，下次打开页面 getDiskManagerData 秒显。
"""

import json
import threading
import time

from PySide6.QtCore import Slot

from .common import logger


class DiskManagerView:
    """磁盘管理页：三组目标占用扫描 + 盘符总览 + 按游戏覆盖扫描目录。"""

    # ── 数据 ────────────────────────────────────────────────────────

    @Slot(result=str)
    def getDiskManagerData(self) -> str:
        """初始渲染数据（JSON）：目标清单（缺 size）+ 上次扫描缓存回填 + 盘符总览。"""
        try:
            from ...services import disk_usage
            targets = disk_usage.collect_targets()
            cache = disk_usage.load_cache()
            overview = disk_usage.disk_overview(
                [str(t.get("path") or "") for t in targets if t.get("path")])
        except Exception as exc:
            logger.warning("[Bridge] 读取磁盘统计目标失败: %s", exc)
            targets, cache, overview = [], None, []
        cached = {str(i.get("key") or ""): i for i in (cache or {}).get("items") or []}
        items = [
            {**t, "size": int(cached.get(str(t.get("key") or ""), {}).get("size") or 0)}
            for t in targets
        ]
        scanned_at = str((cache or {}).get("ts") or "")
        return json.dumps({
            "items": items,
            "scanned_at": scanned_at,
            "overview": overview,
        }, ensure_ascii=False)

    # ── 扫描 ────────────────────────────────────────────────────────

    @Slot()
    def startDiskScan(self):
        """启动全量扫描（后台线程）；同时只允许一个扫描任务。"""
        if self._disk_scan_running:
            self._request_toast("已有扫描任务进行中", "#b66f1f")
            return
        self._disk_scan_cancel = threading.Event()
        self._disk_scan_running = True
        self._emit_disk_scan(phase="starting")
        threading.Thread(target=self._disk_scan_worker, daemon=True).start()

    @Slot()
    def cancelDiskScan(self):
        """请求取消当前扫描（扫描器在目录遍历间响应，最多延迟数百毫秒）。"""
        cancel = self._disk_scan_cancel
        if cancel is not None and self._disk_scan_running:
            cancel.set()
            logger.info("[Bridge] 已请求取消磁盘扫描")

    def _emit_disk_scan(self, phase: str, item=None, items=None, overview=None,
                        done: int = 0, total: int = 0, scanned_at: str = "",
                        message: str = ""):
        self.emit_event("disk_scan", phase=phase, item=item, items=items,
                        overview=overview, done=done, total=total,
                        scanned_at=scanned_at, message=message)

    def _disk_scan_worker(self):
        cancel = self._disk_scan_cancel
        try:
            from ...services import disk_usage
            targets = disk_usage.collect_targets()
            total = len(targets)

            def _on_item(item, done, _total):
                self._emit_disk_scan("item", item=item, done=done, total=total)

            items = disk_usage.scan_all(
                targets, stop_check=lambda: cancel.is_set(), on_item_done=_on_item)
            scanned_at = time.strftime("%Y-%m-%d %H:%M:%S")
            overview = disk_usage.disk_overview(
                [str(i.get("path") or "") for i in items if i.get("path")])
            self._emit_disk_scan("done", items=items, overview=overview,
                                 scanned_at=scanned_at)
        except Exception as exc:  # noqa: BLE001 - 工作线程兜底，错误要回 UI
            from ...services.disk_usage import ScanCancelled
            if isinstance(exc, ScanCancelled):
                logger.info("[Bridge] 磁盘扫描已取消")
                self._emit_disk_scan("idle")
            else:
                logger.warning("[Bridge] 磁盘扫描失败: %s", exc)
                self._emit_disk_scan("error", message=str(exc))
        finally:
            self._disk_scan_running = False

    # ── 目录覆盖 ────────────────────────────────────────────────────

    @Slot(str, str, result=bool)
    def setDiskScanOverride(self, game_key: str, path: str) -> bool:
        """按游戏覆盖扫描目录（空串 = 清除覆盖，回自动推导）；存 app_config。"""
        try:
            from ...settings import manager
            overrides = dict(manager.get("disk_scan_overrides") or {})
            p = path.strip()
            if p:
                overrides[game_key] = p
            else:
                overrides.pop(game_key, None)
            manager.update_disk_scan_overrides(overrides)
            logger.info("[Bridge] %s 扫描目录已保存: %s", game_key,
                        overrides.get(game_key) or "(自动推导)")
            self._request_toast("扫描目录已保存", "#3b7d3b")
            return True
        except Exception as exc:
            logger.warning("[Bridge] 保存扫描目录失败: %s", exc)
            self._request_toast(f"保存失败: {exc}", "#b04b44")
            return False
