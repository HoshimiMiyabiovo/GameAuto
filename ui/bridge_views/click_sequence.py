"""固定点击序列（录制 / 回放）视图的 QML 槽与后台实现。"""

import json
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Slot

from ...settings.paths import CACHE_DIR, PROTECTED_CLICK_SEQUENCES
from .common import (
    TONE_DANGER,
    TONE_INFO,
    TONE_SUCCESS,
    TONE_WARNING,
    _make_ui_click_recorder_class,
    logger,
)


class ClickSequenceView:
    """点击录制 / 回放 / 窗口选择。"""
    # ── 固定点击序列（录制 / 回放）slots ───────────────────────────

    @Slot(str, str, int)
    def startClickRecording(self, name: str, window_keyword: str, window_hwnd: int):
        """开始录制鼠标点击序列。

        ClickRecorder.start() 内部用 pynput 监听线程，非阻塞，可直接返回；
        再起一个 watcher 线程等录制结束（热键小键盘0 / UI 停止）后收尾保存。

        window_hwnd: 下拉里选中的窗口句柄（0 = 不限窗口，记录全屏点击）。
            传句柄而不是仅传标题，是为了多开同名窗口时锁定到具体那一个。
        """
        if self._click_seq_recording:
            self.emit_event("toast", message="已经在录制中", color="#b66f1f")
            return
        name = (name or "").strip()
        if not name:
            self.emit_event("toast", message="请先填写序列名称", color="#b04b44")
            return
        if name in PROTECTED_CLICK_SEQUENCES:
            # 已集成一条龙的序列：覆盖会直接影响自动化，提醒用户
            self.emit_event(
                "toast",
                message=f"「{name}」为一条龙内置序列，重新录制将覆盖原文件，请确认后再操作",
                color="#b66f1f",
            )
            logger.warning("[Bridge] 录制受保护序列: %s（允许覆盖但已提示）", name)
        win_kw = (window_keyword or "").strip() or None
        try:
            hwnd = int(window_hwnd or 0)
        except Exception:
            hwnd = 0
        try:
            recorder_cls = _make_ui_click_recorder_class()
        except Exception as exc:
            logger.exception("[Bridge] 加载 click_sequence 模块失败")
            self.emit_event("toast", message=f"录制模块加载失败: {exc}", color="#b04b44")
            return

        # 选了窗口但句柄已失效（游戏重启过）→ 退化为标题匹配，仍可录制
        if hwnd and not self._is_window_alive(hwnd):
            logger.warning("[Bridge] 所选窗口句柄 %s 已失效，回退标题匹配: %s", hwnd, win_kw)
            if win_kw:
                self.emit_event(
                    "toast", message="所选窗口已关闭，改按标题匹配录制", color="#b66f1f",
                )
            hwnd = 0

        recorder = recorder_cls(
            self, logger=logger, window_title_keyword=win_kw, window_hwnd=hwnd or None,
        )
        try:
            recorder.start()
        except Exception as exc:
            logger.warning("[Bridge] 启动录制失败: %s", exc)
            self.emit_event("toast", message=f"启动录制失败: {exc}", color="#b04b44")
            return

        self._click_recorder = recorder
        self._click_recorder_name = name
        self._click_recorder_hwnd = hwnd
        self._click_record_finalized = False
        self._click_seq_count = 0
        self._click_seq_last_json = "{}"
        self._click_seq_recording = True
        self.clickSeqRecordingChanged.emit(True)
        self.clickSeqCountChanged.emit(0)
        self.clickSeqLastChanged.emit("{}")
        logger.info(
            "[Bridge] 开始录制点击序列: name=%s win=%s hwnd=%s", name, win_kw, hwnd or "-",
        )
        self.emit_event(
            "toast",
            message=f"开始录制「{name}」，小键盘0 停止，小键盘+ 打标记",
            color="#2f7d4a",
        )
        threading.Thread(target=self._click_record_watch, daemon=True).start()
        # 选了窗口就把游戏顶到前面，省得用户自己切窗口（延迟一点，等 UI 收尾）
        if hwnd:
            threading.Thread(
                target=self._focus_target_window_delayed, args=(hwnd,), daemon=True,
            ).start()

    @Slot()
    def stopClickRecording(self):
        """手动停止录制（UI 按钮触发）。热键停止由 watcher 线程收尾。"""
        if not self._click_seq_recording:
            return
        logger.info("[Bridge] 手动停止录制")
        self._finalize_click_recording()

    @Slot(str, int, bool)
    def playClickSequence(self, name: str, loop: int, dry_run: bool):
        """回放序列（后台线程执行，用 stopClickPlayback 中止）。"""
        if self._click_seq_playing:
            self.emit_event("toast", message="正在回放中，请先停止", color="#b66f1f")
            return
        name = (name or "").strip()
        if not name:
            return
        self._click_play_stop = threading.Event()
        self._click_seq_playing = True
        self.clickSeqPlayingChanged.emit(True)
        threading.Thread(
            target=self._click_play_backend,
            args=(name, max(1, int(loop)), bool(dry_run), self._click_play_stop),
            daemon=True,
        ).start()

    @Slot()
    def stopClickPlayback(self):
        """中止正在进行的回放。"""
        if self._click_play_stop is not None:
            self._click_play_stop.set()
            logger.info("[Bridge] 已请求停止回放")

    @Slot(str)
    def deleteClickSequence(self, name: str):
        """删除已保存的序列 JSON 文件。受保护序列禁止删除。"""
        name = (name or "").strip()
        if not name:
            return
        if name in PROTECTED_CLICK_SEQUENCES:
            logger.warning("[Bridge] 拒绝删除受保护序列: %s", name)
            self.emit_event(
                "toast",
                message=f"「{name}」为一条龙内置序列，已做防误删保护，禁止删除",
                color="#b04b44",
            )
            return
        try:
            from ...runtime.click_sequence import sequence_path
            path = sequence_path(name)
            if path.exists():
                path.unlink()
                logger.info("[Bridge] 已删除点击序列: %s", path)
                self.emit_event("toast", message=f"已删除序列「{name}」", color="#2f7d4a")
            else:
                self.emit_event("toast", message=f"序列不存在: {name}", color="#b66f1f")
        except ValueError as exc:
            logger.warning("[Bridge] 拒绝非法点击序列名: %s", name)
            self.emit_event("toast", message=str(exc), color="#b04b44")
        except Exception as exc:
            logger.exception("[Bridge] 删除点击序列失败")
            self.emit_event("toast", message=f"删除失败: {exc}", color="#b04b44")
        # 若删除的正是当前展开明细的序列，清空明细面板
        try:
            cur = json.loads(self._click_seq_detail_json or "{}").get("name")
            if cur == name:
                self._click_seq_detail_json = "{}"
                self.clickSeqDetailChanged.emit("{}")
        except Exception as exc:
            logger.debug("[Bridge] 刷新序列详情失败: %s", exc)
        self.refreshClickSequences()

    def _ensure_protected_backup(self):
        """确保受保护序列有备份；缺失时从备份恢复不影响主流程，仅打日志。"""
        try:
            import shutil
            from ...settings.paths import CLICK_SEQUENCES_DIR, PROTECTED_BACKUP_DIR
            from ...runtime.click_sequence import sequence_path
            PROTECTED_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            # 1) 有原文件 → 同步到备份（仅当备份不存在时创建，保留首次可恢复版本，不随覆盖更新）
            for name in PROTECTED_CLICK_SEQUENCES:
                src = sequence_path(name, str(CLICK_SEQUENCES_DIR))
                dst = sequence_path(name, str(PROTECTED_BACKUP_DIR))
                if src.exists() and not dst.exists():
                    try:
                        shutil.copy2(str(src), str(dst))
                    except Exception:
                        logger.debug("[Bridge] 备份受保护序列失败: %s", name, exc_info=True)
            # 2) 检测缺失（仅日志，不自动恢复，避免与用户有意清理冲突；恢复由 restoreProtectedSequences 手动触发）
            missing = []
            for name in PROTECTED_CLICK_SEQUENCES:
                if not sequence_path(name).exists():
                    missing.append(name)
            if missing:
                logger.warning("[Bridge] 检测到受保护序列缺失: %s（可点击“恢复内置序列”）", "、".join(missing))
        except Exception:
            logger.debug("[Bridge] 受保护备份检查失败", exc_info=True)

    @Slot()
    def refreshClickSequences(self):
        """列出所有已保存序列（名称 / 步数 / 创建时间 / 备注 / 目标窗口）。受保护序列带 protected 标记。"""
        try:
            self._ensure_protected_backup()
            from ...runtime.click_sequence import ClickSequencePlayer
            player = ClickSequencePlayer(logger=logger)
            items = []
            for n in player.list_sequences():
                try:
                    seq = player.load(n)
                    items.append({
                        "name": n,
                        "steps": len(seq.events),
                        "created_at": seq.created_at or "",
                        "note": seq.note or "",
                        "window": seq.window_title_keyword or "",
                        "protected": n in PROTECTED_CLICK_SEQUENCES,
                    })
                except Exception:
                    logger.warning("[Bridge] 序列 %s 读取失败，按空信息处理", n)
                    items.append({
                        "name": n, "steps": 0,
                        "created_at": "", "note": "", "window": "",
                        "protected": n in PROTECTED_CLICK_SEQUENCES,
                    })
            # 补充：已受保护但当前缺失的序列也补一条占位，方便 UI 显示“缺失可恢复”
            try:
                existing_names = {it["name"] for it in items}
                for name in PROTECTED_CLICK_SEQUENCES:
                    if name not in existing_names:
                        items.append({
                            "name": name,
                            "steps": 0,
                            "created_at": "",
                            "note": "内置序列缺失，可恢复",
                            "window": "",
                            "protected": True,
                            "missing": True,
                        })
                # 受保护序列置顶
                items.sort(key=lambda it: (not it.get("protected", False), it.get("name", "")))
            except Exception as exc:
                logger.debug("[Bridge] 序列排序失败: %s", exc)
            self._click_seq_list_json = json.dumps(items, ensure_ascii=False)
            self.clickSeqListChanged.emit(self._click_seq_list_json)
        except Exception as exc:
            logger.exception("[Bridge] 读取点击序列列表失败")
            self.emit_event("toast", message=f"读取序列列表失败: {exc}", color="#b04b44")

    @Slot()
    def restoreProtectedSequences(self):
        """从备份恢复误删的受保护序列。"""
        try:
            import shutil
            from ...settings.paths import CLICK_SEQUENCES_DIR, PROTECTED_BACKUP_DIR
            from ...runtime.click_sequence import sequence_path
            restored = []
            missing = []
            for name in PROTECTED_CLICK_SEQUENCES:
                src = sequence_path(name, str(PROTECTED_BACKUP_DIR))
                dst = sequence_path(name, str(CLICK_SEQUENCES_DIR))
                if dst.exists():
                    continue
                if src.exists():
                    try:
                        shutil.copy2(str(src), str(dst))
                        restored.append(name)
                    except Exception as exc:
                        logger.warning("[Bridge] 恢复 %s 失败: %s", name, exc)
                        missing.append(name)
                else:
                    missing.append(name)
            if restored:
                logger.info("[Bridge] 已恢复受保护序列: %s", "、".join(restored))
                self.emit_event("toast", message=f"已恢复: {'、'.join(restored)}", color="#2f7d4a")
            if missing and not restored:
                self.emit_event("toast", message=f"无备份可恢复: {'、'.join(missing)}", color="#b66f1f")
            elif missing:
                self.emit_event("toast", message=f"部分未恢复（无备份）: {'、'.join(missing)}", color="#b66f1f")
            self.refreshClickSequences()
        except Exception as exc:
            logger.exception("[Bridge] 恢复受保护序列失败")
            self.emit_event("toast", message=f"恢复失败: {exc}", color="#b04b44")

    @Slot(str, result=bool)
    def isProtectedSequence(self, name: str) -> bool:
        return (name or "").strip() in PROTECTED_CLICK_SEQUENCES

    @Slot(str)
    def loadClickSequenceDetail(self, name: str):
        """加载单个序列的步骤明细，供 QML 画步骤时间轴。"""
        name = (name or "").strip()
        if not name:
            return
        try:
            from ...runtime.click_sequence import ClickSequencePlayer
            seq = ClickSequencePlayer(logger=logger).load(name)
            events = []
            prev_gap = 0.0
            for i, e in enumerate(seq.events, start=1):
                # gap_before = 本步执行前的等待（首步为 0）；gap_after 是本步
                # 点击后的等待（回放节奏用）。二者语义不同，列表展示用前者。
                gap_before = prev_gap if i > 1 else 0.0
                events.append({
                    "index": i,
                    "x": e.x,
                    "y": e.y,
                    "rel_x": e.rel_x,
                    "rel_y": e.rel_y,
                    "button": e.button,
                    "gap_after": e.gap_after if e.gap_after is not None else 0.0,
                    "gap_before": gap_before,
                    "ts": time.strftime("%m-%d %H:%M:%S", time.localtime(e.ts)) if e.ts else "",
                    "window_title": e.window_title or "",
                })
                prev_gap = e.gap_after if e.gap_after is not None else 0.0
            payload = {
                "name": seq.name,
                "created_at": seq.created_at or "",
                "note": seq.note or "",
                "window": seq.window_title_keyword or "",
                "events": events,
            }
            self._click_seq_detail_json = json.dumps(payload, ensure_ascii=False)
            self.clickSeqDetailChanged.emit(self._click_seq_detail_json)
        except Exception as exc:
            logger.exception("[Bridge] 加载点击序列明细失败")
            self.emit_event("toast", message=f"加载明细失败: {exc}", color="#b04b44")

    # ── 固定点击序列：后台线程实现 ─────────────────────────────────

    # ── 目标窗口选择（录制页下拉用）─────────────────────────────

    @Slot()
    def refreshWindowList(self):
        """枚举当前可见窗口，刷新录制页的窗口下拉。"""
        try:
            from ...runtime.click_sequence import list_windows
            items = list_windows()
            self._window_list_json = json.dumps(items, ensure_ascii=False)
            self.windowListChanged.emit(self._window_list_json)
            logger.info("[Bridge] 已刷新窗口列表: %d 个", len(items))
        except Exception as exc:
            logger.exception("[Bridge] 枚举窗口失败")
            self.emit_event("toast", message=f"枚举窗口失败: {exc}", color="#b04b44")

    @Slot(int)
    def focusWindow(self, hwnd: int):
        """把指定窗口置前（录制前先切到游戏窗口）。"""
        try:
            hwnd = int(hwnd or 0)
        except Exception:
            hwnd = 0
        if not hwnd:
            return
        try:
            import win32con
            import win32gui
            if not win32gui.IsWindow(hwnd):
                self.emit_event("toast", message="该窗口已关闭，请刷新列表", color="#b66f1f")
                return
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            win32gui.SetForegroundWindow(hwnd)
        except Exception as exc:
            logger.warning("[Bridge] 置前窗口失败: %s", exc)
            self.emit_event("toast", message=f"置前窗口失败: {exc}", color="#b04b44")

    @staticmethod
    def _is_window_alive(hwnd: int) -> bool:
        try:
            import win32gui
            return bool(hwnd) and bool(win32gui.IsWindow(int(hwnd)))
        except Exception:
            return False

    # 录制窗口行为偏好（app_config.json 的 click_seq_record 段）
    _RECORD_PREF_DEFAULTS: Dict[str, bool] = {
        # 录制时把本程序窗口最小化，让目标窗口完全露出来，用户可自由切换
        "minimize_self": True,
        # 录制时把目标窗口置顶：默认关。置顶会挡住其他窗口（也会挡住切回本程序）
        "pin_target": False,
    }

    @staticmethod
    def _record_window_prefs() -> Dict[str, bool]:
        """读取录制窗口偏好，缺省值兜底（配置缺失/损坏都不影响录制）。"""
        prefs = dict(ClickSequenceView._RECORD_PREF_DEFAULTS)
        try:
            from ...settings import manager as cfg_manager
            saved = cfg_manager.load().get("click_seq_record") or {}
        except Exception:
            saved = {}
        for key in prefs:
            if key in saved:
                prefs[key] = bool(saved[key])
        return prefs

    @Slot(result=str)
    def getRecordWindowPrefs(self) -> str:
        """返回录制窗口偏好（JSON），供录制页初始化开关。"""
        return json.dumps(self._record_window_prefs(), ensure_ascii=False)

    @Slot(str, bool)
    def saveRecordWindowPref(self, key: str, value: bool):
        """保存录制窗口偏好到 app_config.json 的 click_seq_record 段。"""
        if key not in self._RECORD_PREF_DEFAULTS:
            return
        try:
            from ...settings import manager as cfg_manager
            cfg = cfg_manager.load(force=True)
            section = cfg.setdefault("click_seq_record", {})
            section[key] = bool(value)
            cfg_manager.save(cfg)
            logger.info("[Bridge] 录制窗口偏好已保存: %s=%s", key, bool(value))
        except Exception:
            logger.exception("[Bridge] 保存录制窗口偏好失败")
            self.emit_event("toast", message="录制窗口偏好保存失败", color="#b04b44")

    def _minimize_own_windows(self):
        """把本程序主窗口最小化（录制时给目标窗口让路）。"""
        try:
            import win32con
            import win32gui
        except Exception:
            return
        for own_hwnd in self._find_own_windows():
            try:
                win32gui.ShowWindow(own_hwnd, win32con.SW_MINIMIZE)
            except Exception:
                logger.debug("[Bridge] 最小化本程序窗口失败", exc_info=True)

    def _unpin_all_on_exit(self):
        """进程退出兜底：取消所有录制期间置顶的窗口，防止留下永久置顶窗口。"""
        targets = set(self._pinned_hwnds)
        try:
            targets.update(int(h) for h in self._find_own_windows())
        except Exception as exc:
            logger.debug("[Bridge] 取消置顶时枚举自身窗口失败: %s", exc)
        for hwnd in targets:
            self._unmake_window_topmost(hwnd)

    def _focus_target_window_delayed(self, hwnd: int, delay: float = 0.6):
        """稍等再把目标窗口叫到前台，并给本程序让路（录制用）。

        历史坑：旧版本把「本程序窗口」也设成 HWND_TOPMOST（怕游戏盖住停止
        按钮），结果是 GUI 永远压在最上层、用户切不到后台操作游戏 —— 已改为
        默认最小化本程序窗口；停止录制有小键盘0 热键，不依赖 GUI 是否可见。

        - minimize_self=True（默认）：最小化本程序，目标窗口完全露出来
        - pin_target=True：把目标窗口置顶（默认关，置顶会挡住其他窗口）
        延迟期间录制若已结束则不再动窗口；置顶过的句柄记入 _pinned_hwnds，
        由 _finalize_click_recording 与 _unpin_all_on_exit 统一取消。
        """
        time.sleep(delay)
        if not self._click_seq_recording:
            return
        prefs = self._record_window_prefs()
        try:
            import win32con
            import win32gui
            if not win32gui.IsWindow(hwnd):
                return
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            win32gui.SetForegroundWindow(hwnd)
            if prefs.get("pin_target"):
                win32gui.SetWindowPos(
                    hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                    win32con.SWP_NOMOVE | win32con.SWP_NOSIZE,
                )
                self._pinned_hwnds.add(int(hwnd))
        except Exception:
            logger.debug("[Bridge] 置前/置顶目标窗口失败", exc_info=True)

        if prefs.get("minimize_self"):
            self._minimize_own_windows()

    @staticmethod
    def _find_own_windows() -> List[int]:
        """枚举本进程所有可见主窗口句柄（标题非空）。"""
        try:
            import win32gui
            from win32process import GetWindowThreadProcessId
        except Exception:
            return []
        own_pid = os.getpid()
        found: List[int] = []

        def cb(h, _):
            try:
                if not win32gui.IsWindowVisible(h):
                    return
                _, pid = GetWindowThreadProcessId(h)
                if pid == own_pid and win32gui.GetWindowText(h).strip():
                    found.append(int(h))
            except Exception as exc:
                logger.debug("[Bridge] 枚举自身窗口失败: %s", exc)

        try:
            win32gui.EnumWindows(cb, None)
        except Exception as exc:
            logger.debug("[Bridge] EnumWindows 失败: %s", exc)
        return found

    @staticmethod
    def _unmake_window_topmost(hwnd: int):
        """取消窗口置顶（录制结束时恢复常规层级）。"""
        try:
            import win32con
            import win32gui
            if not win32gui.IsWindow(int(hwnd or 0)):
                return
            win32gui.SetWindowPos(
                int(hwnd), win32con.HWND_NOTOPMOST, 0, 0, 0, 0,
                win32con.SWP_NOMOVE | win32con.SWP_NOSIZE,
            )
        except Exception:
            logger.debug("[Bridge] 取消置顶失败", exc_info=True)

    @staticmethod
    def _focus_own_window() -> bool:
        """把本程序主窗口叫回前台（按进程号找，标题无关）。"""
        try:
            import win32con
            import win32gui
            from win32process import GetWindowThreadProcessId
        except Exception:
            return False
        own_pid = os.getpid()
        found = []

        def cb(h, _):
            try:
                if not win32gui.IsWindowVisible(h):
                    return
                _, pid = GetWindowThreadProcessId(h)
                if pid == own_pid and win32gui.GetWindowText(h).strip():
                    found.append(h)
            except Exception as exc:
                logger.debug("[Bridge] 定位自身窗口失败: %s", exc)

        try:
            win32gui.EnumWindows(cb, None)
        except Exception:
            return False
        if not found:
            return False
        try:
            if win32gui.IsIconic(found[0]):
                win32gui.ShowWindow(found[0], win32con.SW_RESTORE)
            win32gui.SetForegroundWindow(found[0])
            return True
        except Exception:
            return False

    def _on_click_recorded(self, recorder):
        """录制回调（pynput 线程）：把最新一次点击推给 UI。"""
        if not recorder.events:
            return
        e = recorder.events[-1]
        self._click_seq_count = len(recorder.events)
        self.clickSeqCountChanged.emit(self._click_seq_count)
        payload = {
            "index": self._click_seq_count,
            "x": e.x,
            "y": e.y,
            "rel_x": e.rel_x,
            "rel_y": e.rel_y,
            "button": e.button,
            "gap": e.gap_after if e.gap_after is not None else 0.0,
            "kind": getattr(e, "kind", "click"),
            "key": getattr(e, "key", ""),
        }
        self._click_seq_last_json = json.dumps(payload, ensure_ascii=False)
        self.clickSeqLastChanged.emit(self._click_seq_last_json)

    def _click_record_watch(self):
        """等录制结束（热键 ESC / q 或 UI 停止）后收尾保存。

        必须轮询录制器自己的 _recording 标志：热键回调只置它，
        不改 bridge 的 _click_seq_recording（否则热键永远停不下来）。
        UI 停止路径先置 _click_seq_recording=False，这里循环退出后
        再调一次 finalize，由 finalized 标志防重复。
        """
        recorder = self._click_recorder
        while (
            self._click_seq_recording
            and recorder is not None
            and getattr(recorder, "_recording", False)
        ):
            time.sleep(0.2)
        self._finalize_click_recording()

    def _finalize_click_recording(self):
        """停止录制器并保存序列（用 finalized 标志防 watcher 与 UI 重复收尾）。"""
        recorder = self._click_recorder
        if recorder is None or self._click_record_finalized:
            return
        self._click_record_finalized = True
        try:
            recorder.stop()
        except Exception:
            logger.debug("[Bridge] 停止录制器异常", exc_info=True)

        count = len(recorder.events)
        name = self._click_recorder_name
        hwnd = self._click_recorder_hwnd
        self._click_recorder = None
        self._click_recorder_name = ""
        self._click_recorder_hwnd = 0
        self._click_seq_recording = False
        self.clickSeqRecordingChanged.emit(False)
        # 录制期间可能置顶过目标窗口（pin_target 开启时），旧版本还置顶过
        # 本程序窗口 —— 这里连同本程序窗口一起全部取消置顶，避免任何窗口
        # 卡在 topmost 层（用户切不走）。
        unpins = set(self._pinned_hwnds)
        self._pinned_hwnds.clear()
        if hwnd:
            unpins.add(int(hwnd))
        try:
            unpins.update(int(h) for h in self._find_own_windows())
        except Exception as exc:
            logger.debug("[Bridge] 结束录制时枚举自身窗口失败: %s", exc)
        for pinned in unpins:
            try:
                self._unmake_window_topmost(pinned)
            except Exception:
                logger.debug("[Bridge] 取消窗口置顶失败", exc_info=True)
        try:
            self._focus_own_window()
        except Exception:
            logger.debug("[Bridge] 恢复本程序窗口焦点失败", exc_info=True)

        if count == 0:
            logger.info("[Bridge] 录制结束，未记录到点击，不保存")
            self.emit_event("toast", message="未记录到点击，已取消保存", color="#b66f1f")
            self._click_seq_count = 0
            self.clickSeqCountChanged.emit(0)
            return

        try:
            path = recorder.save(name)
            logger.info("[Bridge] 录制结束: name=%s steps=%d path=%s", name, count, path)
            self.emit_event("toast", message=f"已保存「{name}」共 {count} 步", color="#2f7d4a")
        except Exception as exc:
            logger.exception("[Bridge] 保存点击序列失败")
            self.emit_event("toast", message=f"保存失败: {exc}", color="#b04b44")
        self.refreshClickSequences()

    def _click_play_backend(self, name: str, loop: int, dry_run: bool, stop_event):
        """回放线程体。"""
        try:
            from ...runtime.click_sequence import ClickSequencePlayer
            player = ClickSequencePlayer(logger=logger)
            seq = player.load(name)
            logger.info("[Bridge] 回放开始: name=%s loop=%d dry=%s", name, loop, dry_run)
            ok = player.play(seq, loop=loop, dry_run=dry_run, stop_event=stop_event)
            logger.info("[Bridge] 回放结束: name=%s ok=%s", name, ok)
            if dry_run:
                self.emit_event("toast", message=f"试跑完成（未真实点击）", color="#295d86")
            else:
                self.emit_event(
                    "toast",
                    message="回放完成" if ok else "回放已中止",
                    color="#2f7d4a" if ok else "#b66f1f",
                )
        except Exception as exc:
            logger.exception("[Bridge] 回放点击序列失败")
            self.emit_event("toast", message=f"回放失败: {exc}", color="#b04b44")
        finally:
            self._click_seq_playing = False
            self.clickSeqPlayingChanged.emit(False)
