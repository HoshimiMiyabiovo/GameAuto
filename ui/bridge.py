"""Central bridge object exposed to QML.

A single QObject that carries all UI-facing state as Qt properties.
QML binds to these properties declaratively; Python updates them
and change-notify signals trigger automatic re-renders.

Thread safety: Qt's cross-thread signal delivery (QueuedConnection)
ensures that background threads can emit uiEvent and the handler
slot executes on the main thread — no polling needed.
"""

import atexit
import json
import logging
import os
import subprocess
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from PySide6.QtCore import QObject, Signal, Property, Slot, QTimer

from ..settings.paths import CACHE_DIR, ENDFIELD_SKLAND_CACHE, PROTECTED_CLICK_SEQUENCES, WUWA_DAILY_CACHE
from ..services.game_update_calendar import (
    CN_TZ,
    OPEN_BUFFER_MIN,
    UPDATE_RETRY_FILE,
    get_maintenance_state,
    get_update_info,
)

logger = logging.getLogger("GameAutomation.Bridge")

# 全局 Bridge 单例，供 WebUI 同步运行状态（WebUI 与 GUI 同进程时可见）
_active_bridge: "UiBridge | None" = None

def get_active_bridge() -> "UiBridge | None":
    return _active_bridge

# ── Status tone constants ────────────────────────────────────────
TONE_INFO = "info"
TONE_RUNNING = "running"
TONE_SUCCESS = "success"
TONE_WARNING = "warning"
TONE_DANGER = "danger"

# 没有官方公告接口/公告尚未发布时，按常规维护窗口兜底（仅用于失败/跳过后排补跑）
_FALLBACK_UPDATE_WINDOWS = {
    "WutheringWaves": ("04:00", "11:00"),
    "InfinityNikki": ("10:00", "11:00"),
    "NevernessToEverness": ("06:00", "11:00"),
}

TONE_MAP: Dict[str, tuple] = {
    TONE_INFO:    ("待命中", "#dff1e4", "#2f7d4a"),
    TONE_RUNNING: ("进行中", "#e9f3fb", "#295d86"),
    TONE_SUCCESS: ("已完成", "#dff1e4", "#2f7d4a"),
    TONE_WARNING: ("请留意", "#faebd7", "#b66f1f"),
    TONE_DANGER:  ("已中断", "#f7dfdd", "#b04b44"),
}

# 终末地森空岛缓存新鲜度窗口（秒）：超过后才在进页面时后台自动刷新
ENDFIELD_SKLAND_TTL = 300
WUWA_DAILY_TTL = 300


def _mask_address(addr: str) -> str:
    """日志中脱敏邮箱地址：a*c@example.com。"""
    if not addr or "@" not in addr:
        return "***"
    name, domain = addr.split("@", 1)
    if len(name) <= 2:
        masked = "*" * len(name)
    else:
        masked = name[0] + "*" * (len(name) - 2) + name[-1]
    return f"{masked}@{domain}"


def _make_ui_click_recorder_class():
    """懒加载 ClickRecorder，返回带 UI 回调的子类。

    子类覆写 _on_click：先走基类记录逻辑，再把最新一次点击推给 bridge，
    由 bridge 发信号实时刷新 QML 的计数与"最近点击"面板。
    注意 _on_click 运行在 pynput 监听线程，Qt 信号跨线程会自动排队投递。
    """
    from ..runtime.click_sequence import ClickRecorder

    class _UiClickRecorder(ClickRecorder):
        def __init__(self, bridge, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._bridge = bridge

        def _on_click(self, x, y, button, pressed):
            super()._on_click(x, y, button, pressed)
            try:
                self._bridge._on_click_recorded(self)
            except Exception:
                logger.debug("[Bridge] 推送录制点击到 UI 失败", exc_info=True)

        def _on_key(self, key):
            super()._on_key(key)
            # 键盘事件/标记也要实时推 UI（停止热键时 events 无新增，推送无害）
            try:
                if self._recording:
                    self._bridge._on_click_recorded(self)
            except Exception:
                logger.debug("[Bridge] 推送录制按键到 UI 失败", exc_info=True)

    return _UiClickRecorder


class UiBridge(QObject):
    """Python ↔ QML data hub.

    Properties (auto-bound by QML):
        currentView, isRunning, statusTitle, statusBody, statusTone,
        statusBadge, statusBadgeBg, statusBadgeFg, progressVisible,
        selectionSummary, logText, logLines,
        appListJson, dailyProgressJson, queueListJson

    Signals:
        uiEvent(kind: str, payload: dict)
        toastRequested(message: str, color: str)
    """

    # ── Signals ───────────────────────────────────────────────────
    currentViewChanged = Signal(str)
    isRunningChanged = Signal(bool)
    statusTitleChanged = Signal(str)
    statusBodyChanged = Signal(str)
    statusToneChanged = Signal(str)
    statusBadgeChanged = Signal(str)
    statusBadgeBgChanged = Signal(str)
    statusBadgeFgChanged = Signal(str)
    progressVisibleChanged = Signal(bool)
    selectionSummaryChanged = Signal(str)
    logTextChanged = Signal(str)
    logLinesChanged = Signal()

    uiEvent = Signal(str, dict)
    toastRequested = Signal(str, str)
    queueStateChanged = Signal()
    appListChanged = Signal(str)
    dailyProgressChanged = Signal(str)
    scheduledTasksChanged = Signal(str)
    dailyHistoryChanged = Signal(str)
    dailyStatsChanged = Signal(str)
    dailyDatesChanged = Signal(str)
    queueListChanged = Signal(str)
    gachaAccountsChanged = Signal(str)
    gachaAnalysisChanged = Signal(str)
    nikkiGachaChanged = Signal(str)
    wuwaGachaChanged = Signal(str)
    wuwaDailyChanged = Signal(str)
    endfieldGachaChanged = Signal(str)
    credentialsChanged = Signal(str)
    endfieldQueryResultChanged = Signal(str)
    tajiduoDailyChanged = Signal(str)
    webUIStatusChanged = Signal(str)

    # ── 固定点击序列（录制 / 回放）──
    clickSeqRecordingChanged = Signal(bool)
    clickSeqCountChanged = Signal(int)
    clickSeqLastChanged = Signal(str)
    clickSeqListChanged = Signal(str)
    clickSeqDetailChanged = Signal(str)
    clickSeqPlayingChanged = Signal(bool)
    windowListChanged = Signal(str)

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        global _active_bridge
        _active_bridge = self

        # ── Private state ──
        self._current_view = "automation"
        self._is_running = False
        self._status_title = "准备就绪"
        self._status_body = "先勾选任务，再点击开始运行。"
        self._status_tone = TONE_INFO
        self._progress_visible = False
        self._selection_summary = "还没有选择任务"
        self._log_lines: List[str] = []
        self._log_dirty: bool = False
        self._last_log_emit: float = 0.0
        self._shutdown_scheduled = False
        # 关机/邮件推迟：因定时任务让路而没执行时置 True，全部跑完后统一收尾；
        # 用户关掉"完成后关机"偏好后，新一轮启动时清掉（以最新意图为准）
        self._shutdown_deferred = False
        self._shutdown_pending = False
        self._email_pending = False
        self._email_sent_date = ""
        self._email_report: List[str] = []
        self._email_warnings: List[str] = []
        # startAutomation 同步占位锁：running 事件要等主线程处理，
        # 仅靠 _is_running 布尔值拦不住同一 30s tick 内的两次启动
        self._run_lock = threading.Lock()
        # 批次代数：异步发信线程用它判断期间是否已启动新批次，避免旧批次把
        # 新任务所在的机器关掉
        self._run_generation = 0

        # ── Tone-derived badge state ──
        self._refresh_tone_badge()

        # ── 定时启动调度器：每 30s 检查一次到点任务 ──
        # 任务持久化在 app_config.json 的 scheduled_tasks；GUI 必须开着才会触发
        # （挂机场景 GUI 本来常开）。触发时复用 startAutomation，邮件/关机跟随
        # "完成后操作"里的全局偏好。
        self._sched_timer = QTimer(self)
        self._sched_timer.setInterval(30_000)
        self._sched_timer.timeout.connect(self._check_scheduled_tasks)
        self._sched_timer.start()
        # 到点时撞上在跑的任务 → 排队等本轮结束再执行，不直接跳过
        self._pending_scheduled: List[Dict] = []
        # 更新补跑队列：任务因官方维护跳过后，按公告开服时间重排
        self._update_retry_lock = threading.RLock()
        threading.Thread(target=self._prefetch_update_calendar, daemon=True).start()

        # ── Queue state (app_key → "待执行" | "运行中" | "已完成" | "失败" | "已停止") ──
        self._queue_state: Dict[str, str] = {}
        self._queue_order: List[str] = []

        # ── Data properties (JSON strings for QML) ──
        self._app_list_json = "[]"
        self._daily_progress_json = "{}"
        self._daily_history_json = "{}"
        self._daily_stats_json = "{}"
        self._daily_dates_json = "[]"
        self._queue_list_json = "[]"
        self._gacha_accounts_json = "[]"
        self._gacha_analysis_json = "{}"
        self._nikki_gacha_json = "{}"
        self._wuwa_gacha_json = "{}"
        self._wuwa_daily_json = "{}"
        self._endfield_gacha_json = "{}"
        self._credentials_json = "{}"
        self._endfield_query_result_json = "{}"
        self._tajiduo_daily_json = "{}"
        self._webui_status_json = "{}"

        # ── 固定点击序列（录制 / 回放）──
        self._click_seq_recording = False
        self._click_seq_count = 0
        self._click_seq_last_json = "{}"
        self._click_seq_list_json = "[]"
        self._click_seq_detail_json = "{}"
        self._click_seq_playing = False
        self._click_recorder = None
        self._click_recorder_name = ""
        self._click_recorder_hwnd = 0
        self._window_list_json = "[]"
        self._click_play_stop: Optional[threading.Event] = None
        self._click_record_finalized = True
        # 录制期间被置顶的窗口句柄，收尾/退出时统一取消，防止留下永久置顶窗口
        self._pinned_hwnds: set = set()
        atexit.register(self._unpin_all_on_exit)

        # ── 终末地森空岛查询结果缓存（内存 + 磁盘）──
        # 进页面即时展示上次结果，过期才后台刷新，避免每次进入都打网络。
        self._endfield_skland_cache = None
        self._endfield_skland_refreshing = False
        self._load_endfield_skland_cache()

        # 鸣潮库街区每日数据缓存（与终末地同套机制）
        self._wuwa_daily_cache = None
        self._wuwa_daily_refreshing = False
        self._load_wuwa_daily_cache()

        # ── Backend controller (lazy init) ──
        self._controller = None
        self._preference_db = None

        # ── Wire internal events (queued delivery from bg threads) ──
        self.uiEvent.connect(self._handle_ui_event)

        logger.info("[Bridge] Initialized, default view=%s", self._current_view)

    # ═══════════════════════════════════════════════════════════════
    #  Properties
    # ═══════════════════════════════════════════════════════════════

    @Property(str, notify=currentViewChanged)
    def currentView(self) -> str:
        return self._current_view

    @currentView.setter
    def currentView(self, value: str):
        if value != self._current_view:
            logger.info("[UI] 切换视图: %s -> %s", self._current_view, value)
            self._current_view = value
            self.currentViewChanged.emit(value)

    @Property(bool, notify=isRunningChanged)
    def isRunning(self) -> bool:
        return self._is_running

    @isRunning.setter
    def isRunning(self, value: bool):
        if value != self._is_running:
            self._is_running = value
            self.isRunningChanged.emit(value)

    # ── Shutdown scheduling ──
    shutdownScheduledChanged = Signal()

    @Property(bool, notify=shutdownScheduledChanged)
    def shutdownScheduled(self) -> bool:
        return self._shutdown_scheduled

    # ── Email configuration indicator ──
    emailConfiguredChanged = Signal()

    def _check_email_configured(self) -> bool:
        try:
            from ..services.email_reporter import is_email_configured
            return bool(is_email_configured())
        except Exception:
            return False

    @Property(bool, notify=emailConfiguredChanged)
    def emailConfigured(self) -> bool:
        return self._check_email_configured()

    dpapiAvailableChanged = Signal()

    @Property(bool, constant=True)
    def dpapiAvailable(self) -> bool:
        """DPAPI 是否可用（Windows + pywin32）。constant 因为进程内不变。"""
        try:
            from ..services.email_reporter import is_dpapi_available
            return bool(is_dpapi_available())
        except Exception:
            return False

    @Slot(result=str)
    def getEmailConfig(self) -> str:
        """返回脱敏后的 email 配置 JSON，供 UI 弹窗回填。password 永远为空。"""
        from ..services import email_reporter
        from ..services.email_reporter import load_email_config, mask_email_config
        try:
            cfg = load_email_config()
        except Exception as exc:
            logger.warning("[Bridge] 读取 email 配置失败: %s", exc)
            cfg = None
        masked = mask_email_config(cfg)
        return json.dumps(masked, ensure_ascii=False)

    @Slot(str)
    def saveEmailConfig(self, cfg_json: str):
        """保存 UI 表单提交的 email 配置。password 经 DPAPI 加密后落盘。

        UI 端如果 password 字段为空，则视为"不改密码"，沿用已存在的密文。
        """
        from ..services import email_reporter
        from ..services.email_reporter import (
            is_dpapi_available,
            load_raw_email_section,
            protect_password,
            save_email_section,
        )
        try:
            incoming = json.loads(cfg_json)
        except json.JSONDecodeError as exc:
            self._request_toast(f"配置 JSON 解析失败: {exc}", "#b04b44")
            return

        if not is_dpapi_available():
            self._request_toast(
                "当前环境无 win32crypt，无法加密密码；请安装 pywin32 后再试",
                "#b04b44",
            )
            return

        # 收集新值
        new_password_plain = str(incoming.get("password") or "")
        # 沿用旧 password（如果 UI 没填）
        old_section = load_raw_email_section()
        if new_password_plain:
            try:
                stored_password = protect_password(new_password_plain)
            except Exception as exc:
                logger.exception("[Bridge] 加密密码失败")
                self._request_toast(f"加密密码失败: {exc}", "#b04b44")
                return
            password_protect = "dpapi"
        else:
            stored_password = str(old_section.get("password") or "")
            password_protect = str(old_section.get("password_protect") or "")

        recipients = incoming.get("recipients") or []
        if isinstance(recipients, str):
            recipients = [r.strip() for r in recipients.replace("，", ",").split(",") if r.strip()]
        else:
            recipients = [str(r).strip() for r in recipients if str(r).strip()]

        try:
            smtp_port = int(incoming.get("smtp_port") or 465)
        except (TypeError, ValueError):
            smtp_port = 465

        new_section = {
            "enabled": bool(incoming.get("enabled", True)),
            "smtp_host": str(incoming.get("smtp_host") or "").strip(),
            "smtp_port": smtp_port,
            "use_ssl": bool(incoming.get("use_ssl", True)),
            "sender": str(incoming.get("sender") or "").strip(),
            "password": stored_password,
            "password_protect": password_protect,
            "recipients": recipients,
            "subject_prefix": str(incoming.get("subject_prefix") or "[GameAuto]").strip() or "[GameAuto]",
        }

        ok, err = save_email_section(new_section)
        if not ok:
            self._request_toast(f"保存配置失败: {err}", "#b04b44")
            return
        self.emailConfiguredChanged.emit()
        masked_sender = _mask_address(new_section["sender"])
        logger.info(
            "[Bridge] 邮件配置已保存: smtp=%s:%s ssl=%s sender=%s recipients=%d has_password=%s",
            new_section["smtp_host"], new_section["smtp_port"], new_section["use_ssl"],
            masked_sender, len(new_section["recipients"]), bool(new_section["password"]),
        )
        self._request_toast("邮件配置已保存（密码已加密）", "#2f7d4a")

    @Slot(result=bool)
    def isDpapiAvailable(self) -> bool:
        """UI 检查 DPAPI 是否可用（Windows 才有）。"""
        try:
            from ..services.email_reporter import is_dpapi_available
            return bool(is_dpapi_available())
        except Exception:
            return False

    @Property(str, notify=statusTitleChanged)
    def statusTitle(self) -> str:
        return self._status_title

    @statusTitle.setter
    def statusTitle(self, value: str):
        if value != self._status_title:
            self._status_title = value
            self.statusTitleChanged.emit(value)

    @Property(str, notify=statusBodyChanged)
    def statusBody(self) -> str:
        return self._status_body

    @statusBody.setter
    def statusBody(self, value: str):
        if value != self._status_body:
            self._status_body = value
            self.statusBodyChanged.emit(value)

    @Property(str, notify=statusToneChanged)
    def statusTone(self) -> str:
        return self._status_tone

    @statusTone.setter
    def statusTone(self, value: str):
        if value != self._status_tone:
            self._status_tone = value
            self._refresh_tone_badge()
            self.statusToneChanged.emit(value)

    @Property(str, notify=statusBadgeChanged)
    def statusBadge(self) -> str:
        return self._status_badge

    @Property(str, notify=statusBadgeBgChanged)
    def statusBadgeBg(self) -> str:
        return self._status_badge_bg

    @Property(str, notify=statusBadgeFgChanged)
    def statusBadgeFg(self) -> str:
        return self._status_badge_fg

    @Property(bool, notify=progressVisibleChanged)
    def progressVisible(self) -> bool:
        return self._progress_visible

    @progressVisible.setter
    def progressVisible(self, value: bool):
        if value != self._progress_visible:
            self._progress_visible = value
            self.progressVisibleChanged.emit(value)

    @Property(str, notify=selectionSummaryChanged)
    def selectionSummary(self) -> str:
        return self._selection_summary

    @selectionSummary.setter
    def selectionSummary(self, value: str):
        if value != self._selection_summary:
            self._selection_summary = value
            self.selectionSummaryChanged.emit(value)

    @Property(str, notify=logTextChanged)
    def logText(self) -> str:
        return "\n".join(self._log_lines)

    @Property(list, notify=logLinesChanged)
    def logLines(self) -> List[str]:
        return list(self._log_lines)

    @Property(str, notify=appListChanged)
    def appListJson(self) -> str:
        return self._app_list_json

    @Property(str, notify=dailyProgressChanged)
    def dailyProgressJson(self) -> str:
        return self._daily_progress_json

    @Property(str, notify=dailyHistoryChanged)
    def dailyHistoryJson(self) -> str:
        """历史某一天的每日进度快照（回看用）。"""
        return self._daily_history_json

    @Property(str, notify=dailyStatsChanged)
    def dailyStatsJson(self) -> str:
        """最近 N 天各游戏完成率统计。"""
        return self._daily_stats_json

    @Property(str, notify=dailyDatesChanged)
    def dailyDatesJson(self) -> str:
        """有记录的日期列表（倒序），供日期下拉框使用。"""
        return self._daily_dates_json

    @Property(str, notify=queueListChanged)
    def queueListJson(self) -> str:
        return self._queue_list_json

    @Property(str, notify=gachaAccountsChanged)
    def gachaAccountsJson(self) -> str:
        return self._gacha_accounts_json

    @Property(str, notify=gachaAnalysisChanged)
    def gachaAnalysisJson(self) -> str:
        return self._gacha_analysis_json

    @Property(str, notify=nikkiGachaChanged)
    def nikkiGachaJson(self) -> str:
        return self._nikki_gacha_json

    @Property(str, notify=wuwaGachaChanged)
    def wuwaGachaJson(self) -> str:
        return self._wuwa_gacha_json

    @Property(str, notify=wuwaDailyChanged)
    def wuwaDailyJson(self) -> str:
        """鸣潮库街区每日数据（体力/活跃度/周本/深塔等）的 JSON 快照。"""
        return self._wuwa_daily_json

    @Property(str, notify=endfieldGachaChanged)
    def endfieldGachaJson(self) -> str:
        return self._endfield_gacha_json

    @Property(str, notify=credentialsChanged)
    def credentialsJson(self) -> str:
        return self._credentials_json

    @Property(str, notify=endfieldQueryResultChanged)
    def endfieldQueryResultJson(self) -> str:
        return self._endfield_query_result_json

    @Property(str, notify=tajiduoDailyChanged)
    def tajiduoDailyJson(self) -> str:
        return self._tajiduo_daily_json

    @Property(str, notify=webUIStatusChanged)
    def webUIStatusJson(self) -> str:
        return self._webui_status_json

    # ── 固定点击序列（录制 / 回放）──

    @Property(bool, notify=clickSeqRecordingChanged)
    def clickSeqRecording(self) -> bool:
        return self._click_seq_recording

    @Property(int, notify=clickSeqCountChanged)
    def clickSeqCount(self) -> int:
        return self._click_seq_count

    @Property(str, notify=clickSeqLastChanged)
    def clickSeqLastJson(self) -> str:
        return self._click_seq_last_json

    @Property(str, notify=clickSeqListChanged)
    def clickSeqListJson(self) -> str:
        return self._click_seq_list_json

    @Property(str, notify=clickSeqDetailChanged)
    def clickSeqDetailJson(self) -> str:
        return self._click_seq_detail_json

    @Property(bool, notify=clickSeqPlayingChanged)
    def clickSeqPlaying(self) -> bool:
        return self._click_seq_playing

    @Property(str, notify=windowListChanged)
    def windowListJson(self) -> str:
        """当前可见窗口列表（JSON），供录制页下拉选择目标窗口。"""
        return self._window_list_json

    # ═══════════════════════════════════════════════════════════════
    #  Slots — QML → Python
    # ═══════════════════════════════════════════════════════════════

    @Slot(str)
    def switchView(self, view_key: str):
        valid = {
            "automation", "stamina_config", "wool", "daily", "gacha", "nikki",
            "wuwa", "tajiduo", "endfield_center", "setup_wizard",
            "click_sequence", "webui"
        }
        if view_key not in valid:
            logger.warning("[UI] 未知视图: %s", view_key)
            return
        self.currentView = view_key

    @Slot()
    def clearLog(self):
        """清空日志缓冲区。"""
        self._log_lines.clear()
        self._log_dirty = False
        self._last_log_emit = 0.0
        self.logLinesChanged.emit()
        self.logTextChanged.emit("")

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
            from ..runtime.click_sequence import sequence_path
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
        except Exception:
            pass
        self.refreshClickSequences()

    def _ensure_protected_backup(self):
        """确保受保护序列有备份；缺失时从备份恢复不影响主流程，仅打日志。"""
        try:
            import shutil
            from ..settings.paths import CLICK_SEQUENCES_DIR, PROTECTED_BACKUP_DIR
            from ..runtime.click_sequence import sequence_path
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
            from ..runtime.click_sequence import ClickSequencePlayer
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
            except Exception:
                pass
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
            from ..settings.paths import CLICK_SEQUENCES_DIR, PROTECTED_BACKUP_DIR
            from ..runtime.click_sequence import sequence_path
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
            from ..runtime.click_sequence import ClickSequencePlayer
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
            from ..runtime.click_sequence import list_windows
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
        prefs = dict(UiBridge._RECORD_PREF_DEFAULTS)
        try:
            from ..settings import manager as cfg_manager
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
            from ..settings import manager as cfg_manager
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
        except Exception:
            pass
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
            except Exception:
                pass

        try:
            win32gui.EnumWindows(cb, None)
        except Exception:
            pass
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
            except Exception:
                pass

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
        except Exception:
            pass
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
            from ..runtime.click_sequence import ClickSequencePlayer
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

    # ── Setup wizard slots ─────────────────────────────────────────

    @Slot(result=bool)
    def isSetupCompleted(self) -> bool:
        from ..settings.manager import is_setup_completed
        return is_setup_completed()

    # 注意：QML 会使用这几个方法的返回值，@Slot 必须声明 result 类型，
    # 否则 Qt 按 void slot 处理，QML 拿到 undefined
    # （2026-09-29 事故：@Slot() 丢返回值 → 向导列表全空、保存后不跳转）
    @Slot(result=str)
    def getSetupItems(self) -> str:
        """返回需要配置的路径项清单（JSON 字符串）。"""
        import os
        from ..settings import APPS, QBITTORRENT
        items = []

        app_configs = [
            ("GenshinImpact", "原神", "launcher", "BetterGI 启动器", True),
            ("WutheringWaves", "鸣潮", "launcher", "ok-ww 启动器", True),
            ("ArknightsEndfield", "终末地", "launcher", "ok-ef 启动器", True),
            ("NevernessToEverness", "异环", "launcher", "ok-nte 启动器", True),
            ("InfinityNikki", "无限暖暖", "launcher", "whimbox 启动器 (run_one_dragon.bat)", True),
            ("BaiJing", "白荆回廊", "launcher", "白荆回廊游戏程序", False),
        ]
        for app_key, display_name, field, label, required in app_configs:
            cfg = APPS.get(app_key, {})
            current = ""
            if field == "launcher":
                current = cfg.get("launcher", "")
            items.append({
                "app_key": app_key,
                "display_name": display_name,
                "field": field,
                "label": label,
                "current_value": current,
                "required": required,
            })

        hsr_cfg = APPS.get("HonkaiStarRail", {})
        for i, opt in enumerate(hsr_cfg.get("launcher_options", [])):
            items.append({
                "app_key": "HonkaiStarRail",
                "display_name": "星穹铁道",
                "field": f"launcher_options[{i}].launcher",
                "label": opt.get("display_name", f"选项{i}"),
                "current_value": opt.get("launcher", ""),
                "required": True,
            })

        zzz_cfg = APPS.get("ZenlessZoneZero", {})
        for i, opt in enumerate(zzz_cfg.get("launcher_options", [])):
            items.append({
                "app_key": "ZenlessZoneZero",
                "display_name": "绝区零",
                "field": f"launcher_options[{i}].launcher",
                "label": opt.get("display_name", f"选项{i}"),
                "current_value": opt.get("launcher", ""),
                "required": True,
            })

        items.append({
            "app_key": "",
            "display_name": "下载工具",
            "field": "qbittorrent.path",
            "label": "qBittorrent（可选）",
            "current_value": QBITTORRENT.get("path", ""),
            "required": False,
        })

        # 独立/每月流程的 exe 路径：
        # - 崩铁/绝区零每月流程直接复用 apps 里该游戏的启动路径（apps[app_key].launcher），
        #   向导里编辑的是同一份数据，无默认值，未配置显示为空
        # - MiniMax 等独立程序走 flow_exe_paths（GUI 覆盖，无默认值）
        from ..settings import manager as _cm
        config = _cm.load()
        flow_overrides = config.get("flow_exe_paths") or {}
        for app_key, flow_label in (
            ("HonkaiStarRail", "崩铁游戏程序（崩铁每月任务）"),
            ("ZenlessZoneZero", "绝区零游戏程序（绝区零每月任务）"),
        ):
            items.append({
                "app_key": app_key,
                "display_name": "",
                "field": "launcher",
                "label": flow_label,
                "current_value": (APPS.get(app_key) or {}).get("launcher", ""),
                "required": False,
            })
        items.append({
            "app_key": "",
            "display_name": "",
            "field": "flow_exe.MINIMAX",
            "label": "MiniMax Code 程序（薅羊毛集合）",
            "current_value": flow_overrides.get("MINIMAX", ""),
            "required": False,
        })
        items.append({
            "app_key": "",
            "display_name": "",
            "field": "flow_exe.WORKBUDDY",
            "label": "WorkBuddy 程序（薅羊毛集合）",
            "current_value": flow_overrides.get("WORKBUDDY", ""),
            "required": False,
        })
        items.append({
            "app_key": "",
            "display_name": "",
            "field": "flow_exe.TRAE",
            "label": "Trae CN 程序（薅羊毛集合）",
            "current_value": flow_overrides.get("TRAE", ""),
            "required": False,
        })
        items.append({
            "app_key": "",
            "display_name": "",
            "field": "flow_exe.QODER",
            "label": "Qoder 启动器程序（薅羊毛集合）",
            "current_value": flow_overrides.get("QODER", ""),
            "required": False,
        })
        for flow_key, flow_label in (
            ("XM", "XM 脱机程序（XM 独立流程，可含 * 通配符）"),
            ("CJ", "重聚脱机程序（重聚独立流程，可含 * 通配符）"),
        ):
            items.append({
                "app_key": "",
                "display_name": "",
                "field": f"flow_exe.{flow_key}",
                "label": flow_label,
                "current_value": flow_overrides.get(flow_key, ""),
                "required": False,
            })

        # ── 分组 + 排序：让页面按「游戏启动器 → 独立/每月流程 → 其它工具」分区展示 ──
        # 之前 18 项混排且行 label 不带游戏名，用户分不清哪行是哪个游戏（2026-09-29 反馈）。
        def _group_of(it: Dict) -> str:
            label = it.get("label") or ""
            if "薅羊毛" in label:
                return "薅羊毛集合"
            if it.get("field", "").startswith("flow_exe.") or "每月任务" in label:
                return "独立 / 每月流程"
            if it.get("app_key") == "" or "qBittorrent" in label:
                return "其它工具"
            return "游戏启动器 / 自动化工具"

        for it in items:
            it["group"] = _group_of(it)
        group_order = {"游戏启动器 / 自动化工具": 0, "独立 / 每月流程": 1,
                       "薅羊毛集合": 2, "其它工具": 3}
        items.sort(key=lambda it: (group_order.get(it["group"], 9),
                                   0 if it.get("required") else 1))

        return json.dumps(items, ensure_ascii=False)

    @Slot(str, result=bool)
    def checkFileExists(self, path: str) -> bool:
        import os
        return bool(path) and os.path.isfile(path)

    @Slot(str, result=bool)
    def saveSetupPaths(self, json_paths: str) -> bool:
        """保存路径配置到 app_config.json，标记 setup 已完成。"""
        try:
            items = json.loads(json_paths)
        except json.JSONDecodeError:
            logger.warning("[Bridge] saveSetupPaths 参数解析失败")
            return False

        from ..settings import manager as cm
        config = cm.load()
        apps = config.setdefault("apps", {})

        for item in items:
            app_key = item.get("app_key", "")
            field = item.get("field", "")
            value = item.get("value", "").strip()

            if not field:
                continue

            if field == "qbittorrent.path":
                config.setdefault("qbittorrent", {})["path"] = value
                continue

            if field.startswith("flow_exe."):
                # 独立/每月流程的 exe 路径（GUI 覆盖 flow_configs.json 默认值）
                flow_key = field.split(".", 1)[1]
                if flow_key:
                    config.setdefault("flow_exe_paths", {})[flow_key] = value
                continue

            if not app_key:
                continue

            app_cfg = apps.setdefault(app_key, {})
            discovery = app_cfg.setdefault("_discovery", {})

            if field == "launcher":
                # 同时写 apps 顶层覆盖与 _discovery 缓存：
                # 顶层覆盖保证 defaults 里有写死路径的游戏（白荆回廊/归龙潮）
                # 在 GUI 修改后也能生效；清空时两者同空，回落 defaults。
                app_cfg["launcher"] = value
                discovery["launcher"] = value
            elif field.startswith("launcher_options["):
                import re
                m = re.match(r"launcher_options\[(\d+)\]\.launcher", field)
                if m:
                    idx = int(m.group(1))
                    opts = app_cfg.setdefault("launcher_options", [])
                    while len(opts) <= idx:
                        opts.append({})
                    opts[idx]["launcher"] = value
                    opt_name = opts[idx].get("name", f"option_{idx}")
                    discovery.setdefault("launcher_options", {})
                    discovery["launcher_options"][opt_name] = {"launcher": value}

        config["setup_completed"] = True
        cm.save(config)

        from ..settings import refresh_runtime_config
        refresh_runtime_config(force=True)

        logger.info("[Bridge] 路径配置已保存，setup 已完成")
        self._request_toast("路径配置已保存", "#3b7d3b")
        return True

    @Slot()
    def skipSetup(self):
        """跳过向导，标记 setup 已完成。"""
        from ..settings.manager import mark_setup_completed
        mark_setup_completed()
        logger.info("[Bridge] 用户跳过路径配置向导")
        self.switchView("automation")

    # ── Automation slots ──────────────────────────────────────────

    @Slot()
    def loadAppList(self):
        """加载应用列表，更新 appListJson 属性。"""
        threading.Thread(target=self._load_app_list_backend, daemon=True).start()

    @Slot(str, bool, bool, bool)
    def startAutomation(self, apps_json: str, shutdown_on_finish: bool = False, email_on_finish: bool = False, force_rerun: bool = False):
        """启动自动化任务。apps_json: [{"app_key":"genshin","script_choice":"ok"}, ...]
        shutdown_on_finish: True 时，当天所有定时任务/更新补跑结束后 60 秒关机
        email_on_finish: True 时，当天所有任务结束后统一发送一封每日进度邮件
        force_rerun: True 时，跳过"今日已完成"检查，强制重新执行所有任务
        """
        try:
            targets = json.loads(apps_json)
        except json.JSONDecodeError:
            self._request_toast("参数解析失败", "#b04b44")
            return
        if not targets:
            self._request_toast("请先勾选至少一个任务", "#b66f1f")
            return
        # 同步占位：running 事件要等主线程处理，仅靠 _is_running 拦不住同一
        # 30s tick 内"定时任务 + 更新补跑"的两次启动。
        with self._run_lock:
            if self._is_running:
                busy = True
            else:
                busy = False
                self._is_running = True
                self._run_generation += 1
        if busy:
            self._request_toast("已有任务在运行中", "#b66f1f")
            return
        if not shutdown_on_finish:
            self._shutdown_pending = False
            self._shutdown_deferred = False
        # 新任务开始时清空旧日志，避免多任务日志混杂导致时间错乱
        self.clearLog()
        self.isRunningChanged.emit(True)
        threading.Thread(
            target=self._run_automation_backend,
            args=(targets, force_rerun, bool(email_on_finish), bool(shutdown_on_finish)),
            daemon=True,
        ).start()

    @Slot()
    def stopAutomation(self):
        """请求停止当前自动化任务。"""
        if self._controller:
            self._controller.stop_current_task = True
            logger.info("[Bridge] 用户请求停止任务")
        else:
            self._request_toast("没有运行中的任务", "#6d6257")
        # 同步停止 WebUI 侧控制器，保证 WebUI 发起的任务也能被 GUI 停止
        try:
            from game_auto.webui.state import get_controller as _ws_get_ctrl
            try:
                _ws_ctrl = _ws_get_ctrl()
                if _ws_ctrl is not None:
                    _ws_ctrl.stop_current_task = True
            except Exception:
                pass
        except Exception:
            pass
        try:
            from game_auto.webui.state import update_queue_state, get_run_status
            st = get_run_status()
            cur = st.get("current") or {}
            if cur.get("app_key"):
                update_queue_state(cur["app_key"], "stopped", "用户停止")
        except Exception:
            pass

    @Slot()
    def cancelShutdown(self):
        """取消已计划的自动关机（shutdown /a）。"""
        self._shutdown_pending = False
        self._shutdown_deferred = False
        if not self._shutdown_scheduled:
            return
        try:
            completed = subprocess.run(
                ["shutdown", "/a"],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                capture_output=True,
                timeout=10,
            )
            rc = completed.returncode
        except Exception as exc:
            logger.warning("[Bridge] 调用 shutdown /a 失败: %s", exc)
            self._request_toast(f"取消关机失败: {exc}", "#b04b44")
            return
        self._shutdown_scheduled = False
        logger.info("[Bridge] 用户已取消自动关机 (rc=%s)", rc)
        self._request_toast("已取消自动关机", "#2f7d4a")

    @Slot(str)
    def saveLaunchPreferences(self, prefs_json: str):
        """保存启动偏好。prefs_json: [{"app_key":"genshin","enabled":true,"script_choice":"ok"}, ...]"""
        try:
            prefs = json.loads(prefs_json)
        except json.JSONDecodeError:
            self._request_toast("参数解析失败", "#b04b44")
            return
        threading.Thread(
            target=self._save_preferences_backend,
            args=(prefs,),
            daemon=True,
        ).start()

    @Slot()
    def reloadConfig(self):
        """重新读取配置文件并刷新应用列表。"""
        threading.Thread(target=self._load_app_list_backend, daemon=True).start()

    @Slot(str)
    def reorderApps(self, order_json: str):
        """保存任务显示/执行顺序。order_json: app_key 数组（按新顺序）。

        复用既有 app_config.json 的 app_order 持久化机制，刷新运行时配置后重载列表；
        enabled/script_choice 来自 DB 偏好，重载后会保留。
        """
        try:
            order = json.loads(order_json)
        except json.JSONDecodeError:
            self._request_toast("排序参数解析失败", "#b04b44")
            return
        if not isinstance(order, list) or not all(isinstance(k, str) for k in order):
            self._request_toast("排序参数格式错误", "#b04b44")
            return
        try:
            from ..settings import APP_ORDER, manager as cfg_manager
            # 仅保留已知 app_key，并补入缺失项，避免配置被截断导致任务消失
            known = set(APP_ORDER)
            cleaned = [k for k in order if k in known]
            for k in APP_ORDER:
                if k not in cleaned:
                    cleaned.append(k)
            cfg_manager.update_app_order(cleaned)
            self._request_toast("任务顺序已保存", "#2f7d4a")
            # 刷新并重载列表（异步，避免阻塞 UI 线程）
            threading.Thread(target=self._load_app_list_backend, daemon=True).start()
        except Exception as exc:
            logger.exception("[Bridge] 保存任务顺序失败")
            self._request_toast(f"保存顺序失败: {exc}", "#b04b44")

    @Slot(result=str)
    def loadPostRunPrefs(self) -> str:
        """加载"完成后操作"的持久化偏好。返回 JSON: {"email":bool,"shutdown":bool}"""
        try:
            from ..settings import manager as cfg_manager
            cfg = cfg_manager.load()
            prefs = cfg.get("post_run") or {}
            return json.dumps({
                "email": bool(prefs.get("email_on_finish", False)),
                "shutdown": bool(prefs.get("shutdown_on_finish", False)),
            }, ensure_ascii=False)
        except Exception:
            return '{"email":false,"shutdown":false}'

    # ═══════════ 定时启动（scheduled_tasks） ═══════════

    _SCHED_TIME_RE = None  # 惰性编译，见 _valid_sched_time

    @staticmethod
    def _valid_sched_time(time_str: str) -> bool:
        """校验 HH:MM 格式（00:00 ~ 23:59）。"""
        import re
        return bool(re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", (time_str or "").strip()))

    @staticmethod
    def _normalize_sched_time(time_str: str) -> str:
        """宽松归一化：接受 "7:30" / "07:3" / 带空格，统一成 "HH:MM"；非法返回 ""。"""
        import re
        s = (time_str or "").strip().replace("：", ":").replace("．", ":").replace(".", ":")
        m = re.fullmatch(r"(\d{1,2})\s*:\s*(\d{1,2})", s)
        if not m:
            return ""
        try:
            h, mi = int(m.group(1)), int(m.group(2))
        except ValueError:
            return ""
        if 0 <= h <= 23 and 0 <= mi <= 59:
            return f"{h:02d}:{mi:02d}"
        return ""

    def _load_scheduled_tasks(self) -> List[Dict]:
        from ..settings import manager as cfg_manager
        # force=True 绕过内存缓存：定时器每 30s 读一次（文件小，开销可忽略），
        # 否则 save 后 load 仍返回旧缓存，导致到点任务永远读不到
        cfg = cfg_manager.load(force=True)
        tasks = cfg.get("scheduled_tasks") or []
        return tasks if isinstance(tasks, list) else []

    def _save_scheduled_tasks(self, tasks: List[Dict]) -> bool:
        from ..settings import manager as cfg_manager
        try:
            root_cfg = cfg_manager.load(force=True)
            root_cfg["scheduled_tasks"] = tasks
            cfg_manager.save(root_cfg)
            return True
        except Exception as exc:
            logger.exception("[Bridge] 保存定时任务失败")
            self._request_toast(f"保存定时任务失败: {exc}", "#b04b44")
            return False

    @Slot(result=str)
    def getScheduledTasks(self) -> str:
        """返回定时任务列表 JSON。"""
        try:
            tasks = self._load_scheduled_tasks()
        except Exception:
            tasks = []
        return json.dumps(tasks, ensure_ascii=False)

    @Slot(str, result=bool)
    def addScheduledTask(self, task_json: str) -> bool:
        """新增定时任务。task_json: {"name","time","apps":[{"app_key","script_choice"}],"enabled"}"""
        try:
            task = json.loads(task_json or "{}")
        except json.JSONDecodeError:
            self._request_toast("定时任务参数解析失败", "#b04b44")
            return False

        time_str = self._normalize_sched_time(str(task.get("time") or ""))
        apps = task.get("apps") or []
        if not time_str:
            self._request_toast("时间格式不对，随便写就行（如 7:30 / 07:30）", "#b66f1f")
            return False
        apps_clean = [
            {"app_key": str(a.get("app_key") or ""), "script_choice": a.get("script_choice") or None}
            for a in apps if isinstance(a, dict) and a.get("app_key")
        ]
        if not apps_clean:
            self._request_toast("请至少选择一个任务", "#b66f1f")
            return False

        # 同一时间+同一组任务的重复项直接拒绝，防止连点产生重复条目
        app_keys = sorted(a["app_key"] for a in apps_clean)
        for t in self._load_scheduled_tasks():
            if t.get("time") == time_str and sorted(
                    a.get("app_key") for a in (t.get("apps") or [])) == app_keys:
                self._request_toast("该任务在此时段已有定时配置", "#b66f1f")
                return False

        tasks = self._load_scheduled_tasks()
        new_task = {
            "id": f"st-{int(time.time() * 1000)}",
            "name": str(task.get("name") or "").strip() or f"定时 {time_str}",
            "time": time_str,
            "apps": apps_clean,
            "enabled": bool(task.get("enabled", True)),
            # True = 仅补跑今日未完成的任务；到点时已成功的游戏由
            # run_task_batch 的“今日已完成”检查跳过（force_rerun=False）。
            "skip_if_done_today": bool(task.get("skip_if_done_today", False)),
            "last_fired_date": "",
        }
        tasks.append(new_task)
        if not self._save_scheduled_tasks(tasks):
            return False
        self.scheduledTasksChanged.emit(self.getScheduledTasks())
        self._request_toast(f"定时任务已添加：每天 {time_str}", "#2f7d4a")
        return True

    @Slot(str)
    def removeScheduledTask(self, task_id: str):
        tasks = self._load_scheduled_tasks()
        tasks = [t for t in tasks if t.get("id") != task_id]
        if self._save_scheduled_tasks(tasks):
            self.scheduledTasksChanged.emit(self.getScheduledTasks())

    @Slot(str, bool)
    def toggleScheduledTask(self, task_id: str, enabled: bool):
        tasks = self._load_scheduled_tasks()
        for t in tasks:
            if t.get("id") == task_id:
                t["enabled"] = bool(enabled)
                break
        if self._save_scheduled_tasks(tasks):
            self.scheduledTasksChanged.emit(self.getScheduledTasks())

    def _check_scheduled_tasks(self):
        """每 30s 由 QTimer 调用；到点且今天未触发过的任务启动一次。"""
        try:
            today = time.strftime("%Y-%m-%d")
            now_hm = time.strftime("%H:%M")
            tasks = self._load_scheduled_tasks()
            dirty = False
            pending_retry_keys = {
                str(e.get("app_key") or "") for e in self._pending_update_retries()
            }
            for task in tasks:
                if not task.get("enabled"):
                    continue
                if str(task.get("time")) != now_hm:
                    continue
                if task.get("last_fired_date") == today:
                    continue
                fired_task = task
                apps = task.get("apps") or []
                if pending_retry_keys:
                    remaining = [
                        a for a in apps
                        if str(a.get("app_key") or "") not in pending_retry_keys
                    ]
                    if len(remaining) != len(apps):
                        logger.info(
                            "[定时任务] %s: %d 个任务由更新补跑接管，跳过固定补跑",
                            task.get("name") or task.get("id"), len(apps) - len(remaining),
                        )
                        if not remaining:
                            task["last_fired_date"] = today
                            dirty = True
                            continue
                        fired_task = dict(task)
                        fired_task["apps"] = remaining
                # 无论是否真的启动，当天都只触发一次（避免 30s 窗口内重复）
                task["last_fired_date"] = today
                dirty = True
                self._fire_scheduled_task(fired_task)
            if dirty:
                self._save_scheduled_tasks(tasks)
                self.scheduledTasksChanged.emit(self.getScheduledTasks())
            self._check_update_retries()
        except Exception:
            logger.exception("[Bridge] 定时任务检查失败")

    def _unfired_scheduled_today(self) -> List[Dict]:
        """今天仍会执行的定时任务（含排队中的），用于邮件/关机让路计数。"""
        out: List[Dict] = [dict(p) for p in self._pending_scheduled]
        try:
            today = time.strftime("%Y-%m-%d")
            now_hm = time.strftime("%H:%M")
            seen = {p.get("id") for p in out}
            for t in self._load_scheduled_tasks():
                if not t.get("enabled") or t.get("last_fired_date") == today:
                    continue
                if t.get("id") in seen:
                    continue
                # _check_scheduled_tasks 只在整分钟匹配时触发；已过点的任务
                # 不会再运行，不能让它永久阻塞邮件/关机收尾。
                if str(t.get("time") or "") < now_hm:
                    continue
                out.append(t)
        except Exception:
            pass
        return out

    def _has_unfired_scheduled_today(self) -> bool:
        """今天是否还有启用但未执行的定时任务/更新补跑（含排队中的）。"""
        return bool(self._unfired_scheduled_today()) or bool(self._pending_update_retries())

    def _fire_scheduled_task(self, task: Dict):
        name = task.get("name") or task.get("id")
        apps = task.get("apps") or []
        skip_if_done = bool(task.get("skip_if_done_today", False))
        logger.info(
            "[定时任务] 到点触发: %s (每天 %s, %d 个任务, 仅补跑未完成=%s)",
            name, task.get("time"), len(apps), skip_if_done,
        )
        if self._is_running:
            # 改为排队：本轮结束后自动执行，不再直接跳过
            if not any(p.get("id") == task.get("id") for p in self._pending_scheduled):
                self._pending_scheduled.append(dict(task))
                logger.info("[定时任务] %s：当前有任务在运行，已排队等本轮结束", name)
            self._request_toast(f"定时任务 {name} 到点，当前有任务在运行，结束后自动执行", "#b66f1f")
            return
        apps_json = json.dumps(apps, ensure_ascii=False)
        # 邮件/关机跟随"完成后操作"全局偏好（定时跑完自动关机的常见用法）
        email_on_finish, shutdown_on_finish = False, False
        try:
            prefs = json.loads(self.loadPostRunPrefs() or "{}")
            # 任务级覆盖：Qoder 10:01 等补跑任务可只跑不重复发邮件；
            # 关机仍跟随全局意图，避免补跑后不关机。
            email_on_finish = bool(task.get("email_on_finish", prefs.get("email")))
            shutdown_on_finish = bool(task.get("shutdown_on_finish", prefs.get("shutdown")))
        except Exception:
            pass
        # 仅补跑模式：到点后交给 run_task_batch 按“今日脚本任务已完成”逐游戏跳过；
        # 普通模式保持旧行为（force_rerun=True，用户手动改过配置也强制重跑）。
        self.startAutomation(
            apps_json, shutdown_on_finish, email_on_finish, force_rerun=not skip_if_done
        )
        suffix = "，已完成的游戏会跳过" if skip_if_done else ""
        self._request_toast(
            f"定时任务 {name} 已启动（每天 {task.get('time')}）{suffix}", "#2f7d4a"
        )

    def _prefetch_update_calendar(self):
        """GUI 启动时后台预热公告日历，避免首次维护检查阻塞任务线程。"""
        try:
            from ..services.game_update_calendar import refresh_all
            refresh_all()
        except Exception:
            logger.debug("[更新日历] 后台预取失败", exc_info=True)

    def _load_update_retries(self) -> List[Dict]:
        try:
            if UPDATE_RETRY_FILE.exists():
                data = json.loads(UPDATE_RETRY_FILE.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    return [e for e in data if isinstance(e, dict)]
        except Exception:
            logger.warning("[更新补跑] 读取队列失败", exc_info=True)
        return []

    def _save_update_retries(self, items: List[Dict]) -> None:
        try:
            UPDATE_RETRY_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = UPDATE_RETRY_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(UPDATE_RETRY_FILE)
        except Exception:
            logger.warning("[更新补跑] 保存队列失败", exc_info=True)

    @staticmethod
    def _parse_retry_dt(value) -> Optional[datetime]:
        try:
            dt = datetime.fromisoformat(str(value or ""))
            return dt if dt.tzinfo else dt.replace(tzinfo=CN_TZ)
        except Exception:
            return None

    def _pending_update_retries(self, now: Optional[datetime] = None) -> List[Dict]:
        now = now or datetime.now(CN_TZ)
        today = now.strftime("%Y-%m-%d")
        with self._update_retry_lock:
            items = self._load_update_retries()
        out: List[Dict] = []
        for e in items:
            if str(e.get("date") or "") != today:
                continue
            attempt = int(e.get("attempt") or 0)
            max_attempts = int(e.get("max_attempts") or 3)
            if attempt >= max_attempts:
                continue
            due = self._parse_retry_dt(e.get("due_at"))
            if due is None:
                continue
            out.append(e)
        out.sort(key=lambda e: str(e.get("due_at") or ""))
        return out

    def _has_pending_update_retry(self, app_key: str) -> bool:
        return any(str(e.get("app_key") or "") == str(app_key) for e in self._pending_update_retries())

    def _split_maintenance_targets(self, target_apps: List[tuple]) -> tuple:
        """开批前预检：维护中的游戏从本批剔除，并按公告开服时间排队补跑。

        返回 (runnable, deferred)；deferred 元素为
        {"app_key", "script_choice", "state"}。预检异常时按正常启动处理，
        避免公告接口故障把整批游戏误判成维护中。
        """
        from ..services.launch_task_catalog import is_standalone_task

        runnable: List[tuple] = []
        deferred: List[Dict] = []
        for app_key, script_choice in target_apps:
            if is_standalone_task(app_key):
                runnable.append((app_key, script_choice))
                continue
            try:
                state = get_maintenance_state(app_key)
            except Exception:
                logger.warning(
                    "[更新预检] 检查 %s 维护状态失败，按正常启动处理",
                    app_key, exc_info=True,
                )
                state = None
            if not state:
                runnable.append((app_key, script_choice))
                continue
            try:
                self._schedule_update_retry(app_key, script_choice, state, source="preflight")
            except Exception:
                logger.warning("[更新预检] %s 排队补跑失败", app_key, exc_info=True)
            deferred.append({
                "app_key": app_key,
                "script_choice": script_choice,
                "state": state,
            })
        return runnable, deferred

    def _fallback_update_open(self, app_key: str, now: datetime) -> Optional[datetime]:
        """无公告时按常规维护窗口给出兜底开服时间。"""
        window = _FALLBACK_UPDATE_WINDOWS.get(str(app_key))
        if not window:
            return None
        start_s, end_s = window
        now_s = now.strftime("%H:%M")
        if not (start_s <= now_s <= end_s):
            return None
        h, m = map(int, end_s.split(":"))
        return now.replace(hour=h, minute=m, second=0, microsecond=0)

    def _schedule_update_retry(
        self,
        app_key: str,
        script_choice,
        info: Dict,
        source: str = "",
        now: Optional[datetime] = None,
    ):
        """把因维护跳过的游戏排到公告开服时间 + 5 分钟。"""
        app_key = str(app_key or "")
        open_at = info.get("open_at_dt") if info else None
        if not app_key or open_at is None:
            return
        now = now or datetime.now(CN_TZ)
        due = max(open_at + timedelta(minutes=OPEN_BUFFER_MIN), now + timedelta(minutes=5))
        today = now.strftime("%Y-%m-%d")
        with self._update_retry_lock:
            items = [e for e in self._load_update_retries() if str(e.get("date") or "") >= today]
            entry = next(
                (e for e in items
                 if str(e.get("app_key") or "") == app_key and str(e.get("date") or "") == today),
                None,
            )
            if entry is None:
                entry = {
                    "app_key": app_key,
                    "script_choice": script_choice,
                    "date": today,
                    "attempt": 0,
                    "max_attempts": 3,
                    "created_at": now.isoformat(timespec="seconds"),
                }
                items.append(entry)
            entry.update({
                "script_choice": script_choice,
                "due_at": due.isoformat(timespec="seconds"),
                "open_at": open_at.isoformat(timespec="seconds"),
                "version": str(info.get("version") or ""),
                "title": str(info.get("title") or ""),
                "source": source or "maintenance",
                "last_seen_at": now.isoformat(timespec="seconds"),
            })
            self._save_update_retries(items)
        logger.info(
            "[更新补跑] %s 已排队：%s 补跑（公告开服 %s，%s）",
            app_key, due.strftime("%H:%M"), open_at.strftime("%H:%M"),
            info.get("version") or "",
        )

    def _remove_update_retry(self, app_key: str, reason: str = "") -> None:
        app_key = str(app_key or "")
        if not app_key:
            return
        today = datetime.now(CN_TZ).strftime("%Y-%m-%d")
        with self._update_retry_lock:
            items = self._load_update_retries()
            new_items = [
                e for e in items
                if not (str(e.get("app_key") or "") == app_key and str(e.get("date") or "") == today)
            ]
            if len(new_items) != len(items):
                self._save_update_retries(new_items)
                if reason:
                    logger.info("[更新补跑] %s 移除队列：%s", app_key, reason)

    def _handle_update_retry_result(self, result, now: Optional[datetime] = None) -> None:
        """根任务结果更新补跑队列：成功移除，维护跳过/失败按公告重排。"""
        try:
            app_key = str(getattr(result, "app_key", "") or "")
            if not app_key:
                return
            state = str(getattr(result, "state", "") or "")
            message = str(getattr(result, "message", "") or "")
            success = bool(getattr(result, "success", False)) or state in ("success", "already")
            update_skip = state == "skipped" and any(k in message for k in ("更新", "维护"))
            now = now or datetime.now(CN_TZ)
            today = now.strftime("%Y-%m-%d")
            with self._update_retry_lock:
                items = self._load_update_retries()
            entry = next(
                (e for e in items
                 if str(e.get("app_key") or "") == app_key and str(e.get("date") or "") == today),
                None,
            )
            if success or (state == "skipped" and not update_skip):
                if entry is not None:
                    self._remove_update_retry(app_key, "已完成/无需补跑")
                return
            if state not in ("failed", "skipped"):
                return
            info = get_update_info(app_key, now=now)
            open_dt = info.get("open_at_dt") if info else None
            # 没有公告（接口失败/公告未发布）→ 当前若在常规维护窗口内，
            # 用窗口结束时间兜底；有精确公告时一律以公告时间为准。
            if open_dt is None:
                fallback_open = self._fallback_update_open(app_key, now)
                if fallback_open is not None:
                    info = {
                        "version": (info or {}).get("version") or "",
                        "title": (info or {}).get("title") or "维护窗口兜底",
                        "open_at_dt": fallback_open,
                        "open_at": fallback_open.isoformat(timespec="seconds"),
                    }
                    open_dt = fallback_open
            if not info or open_dt is None:
                if entry is not None:
                    self._remove_update_retry(app_key, "无法获取公告开服时间")
                return
            open_at = info["open_at_dt"]
            attempt = int(entry.get("attempt") or 0) if entry else 0
            max_attempts = int(entry.get("max_attempts") or 3) if entry else 3
            # 首次维护跳过 → 按开服时间排队；补跑失败 → 再给几次机会（最多 3 次）
            retryable = update_skip or (state == "failed" and entry is not None)
            if not retryable and now < open_at + timedelta(minutes=OPEN_BUFFER_MIN):
                retryable = True
            if retryable and attempt < max_attempts:
                self._schedule_update_retry(
                    app_key, getattr(result, "script_choice", None), info,
                    source=message or state, now=now,
                )
                return
            self._remove_update_retry(app_key, f"重试结束（{state}）")
        except Exception:
            logger.warning("[更新补跑] 处理任务结果失败", exc_info=True)

    def _check_update_retries(self) -> None:
        """每 30s 由定时器调用：到点的更新补跑启动一批。"""
        if self._is_running:
            return
        now = datetime.now(CN_TZ)
        today = now.strftime("%Y-%m-%d")
        with self._update_retry_lock:
            items = self._load_update_retries()
            due = []
            for e in items:
                if str(e.get("date") or "") != today:
                    continue
                attempt = int(e.get("attempt") or 0)
                max_attempts = int(e.get("max_attempts") or 3)
                if attempt >= max_attempts:
                    continue
                due_at = self._parse_retry_dt(e.get("due_at"))
                if due_at is not None and due_at <= now:
                    due.append(e)
            if not due:
                return
            apps = []
            seen = set()
            for e in due:
                e["attempt"] = int(e.get("attempt") or 0) + 1
                e["last_fired_at"] = now.isoformat(timespec="seconds")
                app_key = str(e.get("app_key") or "")
                if app_key and app_key not in seen:
                    seen.add(app_key)
                    apps.append({"app_key": app_key, "script_choice": e.get("script_choice")})
            self._save_update_retries(items)
        if not apps:
            return
        try:
            prefs = json.loads(self.loadPostRunPrefs() or "{}")
        except Exception:
            prefs = {}
        logger.info("[更新补跑] 到点执行: %s", ", ".join(a["app_key"] for a in apps))
        self.startAutomation(
            json.dumps(apps, ensure_ascii=False),
            bool(prefs.get("shutdown")),
            bool(prefs.get("email")),
            force_rerun=False,
        )

    @Slot(bool, bool)
    def savePostRunPrefs(self, email_on_finish: bool, shutdown_on_finish: bool):
        """保存"完成后操作"偏好到 app_config.json。"""
        try:
            from ..settings import manager as cfg_manager
            cfg = cfg_manager.load(force=True)
            cfg["post_run"] = {
                "email_on_finish": bool(email_on_finish),
                "shutdown_on_finish": bool(shutdown_on_finish),
            }
            cfg_manager.save(cfg)
        except Exception as exc:
            logger.warning("[Bridge] 保存完成后操作偏好失败: %s", exc)

    # ── 薅羊毛集合 slots ─────────────────────────────────────────

    _WOOL_FLOW_META = {
        "MINIMAX": "MiniMax Code 每日签到",
        "WORKBUDDY": "WorkBuddy 每日领取",
        "TRAE": "Trae CN 每日签到",
        "QODER": "Qoder 每日积分领取",
    }

    @Slot(result=str)
    def getWoolFlows(self) -> str:
        """返回薅羊毛集合的执行列表（key/名称/启用状态/路径就绪状态）。"""
        import glob as _glob
        import os as _os
        from ..runtime.foreground_flow_runner import _load_flow_config
        from ..runtime.background_template_runner import WOOL_FLOW_KEYS
        from ..settings import manager as cm

        overrides = cm.load().get("flow_exe_paths") or {}
        flows = []
        for key in WOOL_FLOW_KEYS:
            cfg = _load_flow_config(key) or {}
            exe = str(overrides.get(key) or "").strip()
            exe_ready = bool(exe) and bool(
                _os.path.exists(exe) or _glob.glob(exe)
            )
            flows.append({
                "key": key,
                "name": self._WOOL_FLOW_META.get(key, key),
                "enabled": bool(cfg.get("enabled", True)),
                "close_on_finish": bool(cfg.get("close_process_on_finish", True)),
                "exe_ready": exe_ready,
                "exe_path": exe,
            })
        return json.dumps(flows, ensure_ascii=False)

    def _save_wool_flow_field(self, flow_key: str, field: str, value: bool, log_label: str):
        """写本地 flow_configs.json 中薅羊毛子流程的布尔字段。"""
        try:
            from ..settings.paths import ensure_flow_config_path
            from ..settings.manager import atomic_write_json
            path = ensure_flow_config_path()
            with open(path, encoding="utf-8") as f:
                all_cfg = json.load(f)
            section = all_cfg.get(flow_key)
            if not isinstance(section, dict):
                self.emit_event("toast", message=f"未知流程: {flow_key}", color="#b04b44")
                return
            section[field] = bool(value)
            atomic_write_json(path, all_cfg)
            logger.info("[Bridge] 薅羊毛流程 %s %s: %s", flow_key, log_label, value)
        except Exception as exc:
            logger.exception("[Bridge] 保存薅羊毛流程%s失败", log_label)
            self.emit_event("toast", message=f"保存失败: {exc}", color="#b04b44")

    @Slot(str, bool)
    def saveWoolFlowEnabled(self, flow_key: str, enabled: bool):
        """切换薅羊毛子流程的启用状态（写 flow_configs.json 的 enabled）。"""
        self._save_wool_flow_field(flow_key, "enabled", enabled, "启用状态")

    @Slot(str, bool)
    def saveWoolFlowCloseOnFinish(self, flow_key: str, close_on_finish: bool):
        """切换「任务完成后关闭程序」（写 flow_configs.json 的 close_process_on_finish）。"""
        self._save_wool_flow_field(flow_key, "close_process_on_finish", close_on_finish, "完成后关闭")

    @Slot()
    def runWoolCollection(self):
        """运行薅羊毛集合（后台线程，仅执行启用的子流程）。"""
        if self._is_running:
            self.emit_event("toast", message="有任务正在运行，请先等待完成", color="#b66f1f")
            return
        threading.Thread(target=self._run_wool_backend, daemon=True).start()

    def _run_wool_backend(self):
        try:
            self.emit_event("status", title="正在运行", body="薅羊毛集合执行中", tone=TONE_RUNNING)
            self.isRunning = True
            try:
                from game_auto.webui.state import set_running as _ws_set_running_w, logger as _ws_logger_w
                _ws_set_running_w(True, queue=[{"app_key": "WoolFlow", "display_name": "薅羊毛集合", "script_choice": "", "state": "running"}], source="gui")
                _ws_logger_w.info("[WebUI][运行] GUI 发起薅羊毛")
            except Exception:
                pass
            from ..runtime.background_template_runner import run_wool_background
            ok, msg = run_wool_background(logger)
            for line in msg.split("；"):
                if line.strip():
                    self.emit_event("log", message=line)
            self.emit_event(
                "status",
                title="运行结束",
                body="薅羊毛集合完成" if ok else "薅羊毛集合有失败项",
                tone=TONE_SUCCESS if ok else TONE_DANGER,
            )
            self.emit_event(
                "toast",
                message="薅羊毛集合全部成功" if ok else "薅羊毛集合有失败项，详见日志",
                color="#2f7d4a" if ok else "#b04b44",
            )
            try:
                from game_auto.webui.state import set_running as _ws_set_running_w2, set_last_result as _ws_set_last_w2
                _ws_set_last_w2([msg], [])
                _ws_set_running_w2(False)
            except Exception:
                pass
        except Exception as exc:
            logger.exception("[Bridge] 薅羊毛集合执行失败")
            self.emit_event("status", title="运行失败", body=str(exc), tone=TONE_DANGER)
            self.emit_event("toast", message=f"薅羊毛集合失败: {exc}", color="#b04b44")
            try:
                from game_auto.webui.state import set_running as _ws_set_running_w3, set_last_result as _ws_set_last_w3
                _ws_set_last_w3([f"薅羊毛失败: {exc}"], [])
                _ws_set_running_w3(False)
            except Exception:
                pass
        finally:
            self.isRunning = False
            try:
                from game_auto.webui.state import set_running as _ws_set_running_w4
                # 确保 WebUI 也释放运行锁（若之前已在 try 中释放则重复调用无害）
                import game_auto.webui.state as _ws_state
                if _ws_state.is_running():
                    _ws_set_running_w4(False)
            except Exception:
                pass

    # ── Daily progress slots ──────────────────────────────────────

    @Slot(str)
    def checkDailyProgress(self, games_json: str):
        """查询每日进度。games_json: ["genshin","honkai_sr","zzz",...]"""
        try:
            game_keys = json.loads(games_json)
        except json.JSONDecodeError:
            game_keys = []
        threading.Thread(
            target=self._run_daily_check_backend,
            args=(game_keys,),
            daemon=True,
        ).start()

    @Slot()
    def loadDailyProgress(self):
        """从 SQLite 加载今日持久化进度。"""
        threading.Thread(target=self._load_daily_progress_backend, daemon=True).start()

    @Slot()
    def loadDailyDates(self):
        """加载有每日进度记录的日期列表（倒序）。"""
        threading.Thread(target=self._load_daily_dates_backend, daemon=True).start()

    @Slot(str)
    def loadDailyHistory(self, date_key: str):
        """回看指定日期的每日进度。date_key 形如 "2026-09-27"。"""
        threading.Thread(
            target=self._load_daily_history_backend, args=(str(date_key or "").strip(),),
            daemon=True,
        ).start()

    @Slot(int)
    def loadDailyStats(self, days: int):
        """统计最近 days 天各游戏完成率。"""
        try:
            n = int(days)
        except (TypeError, ValueError):
            n = 30
        n = max(1, min(n, 365))
        threading.Thread(target=self._load_daily_stats_backend, args=(n,), daemon=True).start()

    @Slot(str)
    def openBaijingVerifyImage(self, image_url: str):
        """用系统默认程序打开验证截图（file:// URL 或相对路径）。"""
        try:
            from pathlib import Path as _P
            from ..settings.paths import BASE_DIR as _BD, BAIJING_VERIFY_DIR
            p = None
            if image_url and image_url.startswith("file://"):
                try:
                    # file URI -> path
                    from urllib.parse import unquote
                    from urllib.request import url2pathname
                    p = _P(url2pathname(unquote(image_url[7:])))
                except Exception:
                    p = None
            elif image_url:
                p = _P(image_url)
                if not p.is_absolute():
                    p = _BD / image_url
            if p is None or not p.exists():
                # 回退：打开目录
                p = BAIJING_VERIFY_DIR
            if p.is_file():
                os.startfile(str(p))
            elif p.is_dir():
                os.startfile(str(p))
            else:
                # 目录不存在时创建并打开
                p.mkdir(parents=True, exist_ok=True)
                os.startfile(str(p))
        except Exception as exc:
            logger.error("[Bridge] 打开验证图失败: %s", exc)
            self._request_toast(f"打开失败: {exc}", "#b04b44")

    @Slot()
    def openBaijingVerifyDir(self):
        try:
            from ..settings.paths import BAIJING_VERIFY_DIR
            BAIJING_VERIFY_DIR.mkdir(parents=True, exist_ok=True)
            os.startfile(str(BAIJING_VERIFY_DIR))
        except Exception as exc:
            logger.error("[Bridge] 打开验证目录失败: %s", exc)
            self._request_toast(f"打开失败: {exc}", "#b04b44")

    # ── 失败诊断记录（flow_failures 表 + artifacts/flow_failures 截图） ──

    @Slot(int, result=str)
    def getRecentFlowFailures(self, days: int = 7):
        """返回最近 N 天的失败记录（JSON 字符串，含截图可打开路径）。

        同步读 SQLite（WAL 下只读很快），但仍放在后台线程避免阻塞 QML 主线程：
        首次冷启动要建索引，且别把 GUI 卡顿的锅再背到数据查询上。
        """
        days = max(1, min(int(days or 7), 365))
        out: Dict[str, Any] = {"days": days, "summary": [], "items": [], "error": ""}
        done = threading.Event()

        def _load() -> None:
            try:
                from ..settings.paths import FLOW_FAILURE_DIR, WORKFLOW_DB_PATH
                from ..storage.workflow_db import WorkflowDB

                db = WorkflowDB(str(WORKFLOW_DB_PATH))
                out["summary"] = db.summarize_flow_failures(days=days, group_limit=20)
                rows = db.query_flow_failures(days=days, limit=60)
                items = []
                for r in rows:
                    item = dict(r)
                    img = item.get("image_path") or ""
                    if img:
                        fp = FLOW_FAILURE_DIR / img
                        # QML Image.source 的相对路径会按 .qml 文件目录解析
                        # （曾拼成 ui/qml/views/artifacts/...），必须回绝对 file:/// URL。
                        item["image_url"] = fp.resolve().as_uri() if fp.exists() else ""
                    else:
                        item["image_url"] = ""
                    item["time_text"] = time.strftime(
                        "%m-%d %H:%M:%S", time.localtime(item.get("ts") or 0)
                    )
                    item["ocr_texts"] = (item.get("ocr_texts") or [])[:8]
                    items.append(item)
                out["items"] = items
            except Exception as exc:
                out["error"] = str(exc)
                logger.warning("[Bridge] 读取失败记录失败: %s", exc)
            finally:
                done.set()

        threading.Thread(target=_load, daemon=True).start()
        # 最多等 3s；超时先返回空列表，由 QML 侧提示"加载慢，可再点一次刷新"
        if not done.wait(timeout=3.0):
            out["error"] = out.get("error") or "加载超时"
        try:
            return json.dumps(out, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"error": f"序列化失败: {exc}"}, ensure_ascii=False)

    @Slot(str)
    def openFlowFailureImage(self, image_url: str):
        """打开失败当刻截图（兼容 file:/// URL、裸文件名和旧的相对路径）。"""
        try:
            from pathlib import Path as _P
            from urllib.parse import unquote, urlparse
            from ..settings.paths import FLOW_FAILURE_DIR

            base = FLOW_FAILURE_DIR.resolve()
            raw = str(image_url or "").strip()
            candidate = base
            if raw.startswith("file:"):
                parsed = urlparse(raw)
                local = unquote(parsed.path or "")
                # Windows: file:///E:/... 的 path 是 /E:/...，去掉前导斜杠
                if local.startswith("/") and len(local) >= 3 and local[2] == ":":
                    local = local[1:]
                candidate = _P(local)
            elif raw:
                # 旧调用可能传 artifacts/flow_failures/xxx.jpg；只取文件名
                candidate = base / _P(raw).name
            try:
                resolved = candidate.resolve()
                if resolved.is_file() and resolved.is_relative_to(base):
                    candidate = resolved
                else:
                    candidate = base
            except Exception:
                candidate = base
            candidate.parent.mkdir(parents=True, exist_ok=True)
            os.startfile(str(candidate))
        except Exception as exc:
            logger.error("[Bridge] 打开失败截图失败: %s", exc)
            self._request_toast(f"打开失败: {exc}", "#b04b44")

    @Slot()
    def openFlowFailureDir(self):
        try:
            from ..settings.paths import FLOW_FAILURE_DIR
            FLOW_FAILURE_DIR.mkdir(parents=True, exist_ok=True)
            os.startfile(str(FLOW_FAILURE_DIR))
        except Exception as exc:
            logger.error("[Bridge] 打开失败证据目录失败: %s", exc)
            self._request_toast(f"打开失败: {exc}", "#b04b44")

    # ── 第三方脚本体力刷本配置（第一期 JSON：原神/鸣潮/终末地/异环/暖暖） ──

    @Slot(result=str)
    def getStaminaConfigs(self) -> str:
        """读取 5 个脚本的体力刷本配置，返回 JSON 数组。"""
        try:
            from ..services.stamina_configs import list_configs

            data = list_configs()
            logger.info("[刷本配置] 读取 %d 个脚本配置", len(data))
            return json.dumps(data, ensure_ascii=False)
        except Exception as exc:
            logger.warning("[刷本配置] 读取失败: %s", exc)
            return json.dumps(
                [{"game_key": "", "display_name": "", "error": str(exc), "fields": [], "values": {}}],
                ensure_ascii=False,
            )

    @Slot(str, str, result=str)
    def saveStaminaConfig(self, game_key: str, values_json: str) -> str:
        """保存某个脚本的刷本配置；返回 {ok, message, backups} 或 {ok:false,error}。"""
        try:
            from ..services.stamina_configs import get_adapter

            values = json.loads(values_json or "{}")
            if not isinstance(values, dict):
                raise ValueError("配置参数不是对象")
            adapter = get_adapter(str(game_key))
            result = adapter.write(values)
            logger.info("[刷本配置] 保存 %s: %s", game_key, result.get("message"))
            self._request_toast(f"{adapter.display_name} 配置已保存", "#2f7d4a")
            return json.dumps(result, ensure_ascii=False)
        except Exception as exc:
            logger.warning("[刷本配置] 保存 %s 失败: %s", game_key, exc)
            self._request_toast(f"保存失败: {exc}", "#b04b44")
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    @Slot(str)
    def openStaminaConfigFile(self, game_key: str):
        """用系统默认程序打开脚本配置文件（方便看原文/高级字段）。"""
        try:
            from ..services.stamina_configs import get_adapter

            adapter = get_adapter(str(game_key))
            path = adapter.primary_path()
            if not path or not path.is_file():
                raise FileNotFoundError("配置文件不存在")
            os.startfile(str(path))
        except Exception as exc:
            logger.warning("[刷本配置] 打开配置文件失败: %s", exc)
            self._request_toast(f"打开失败: {exc}", "#b04b44")

    # ── Tool slots ────────────────────────────────────────────────

    @Slot()
    def openConfigFile(self):
        try:
            from ..settings.paths import APP_CONFIG_PATH
            os.startfile(str(APP_CONFIG_PATH))
        except Exception as exc:
            logger.error("[Bridge] 打开配置文件失败: %s", exc)
            self._request_toast(f"打开失败: {exc}", "#b04b44")

    @Slot()
    def openLogDir(self):
        try:
            from ..settings.paths import LOG_DIR
            os.startfile(str(LOG_DIR))
        except Exception as exc:
            logger.error("[Bridge] 打开日志目录失败: %s", exc)
            self._request_toast(f"打开失败: {exc}", "#b04b44")

    # ── Gacha slots ──

    _GACHA_GAME_MAP = {
        "原神": "hk4e",
        "星穹铁道": "hkrpg",
        "绝区零": "nap",
    }

    @Slot(str)
    def loadGachaAccounts(self, game_label: str):
        """加载指定游戏的已导入 UID 列表。"""
        game_code = self._GACHA_GAME_MAP.get(game_label, "hk4e")
        threading.Thread(
            target=self._load_gacha_accounts_backend,
            args=(game_code,),
            daemon=True,
        ).start()

    @Slot(str, str)
    def loadGachaAnalysis(self, game_label: str, uid: str):
        """加载指定游戏 + UID 的抽卡分析数据。"""
        game_code = self._GACHA_GAME_MAP.get(game_label, "hk4e")
        threading.Thread(
            target=self._load_gacha_analysis_backend,
            args=(game_code, uid),
            daemon=True,
        ).start()

    @Slot(str)
    def importGachaFile(self, file_path: str):
        """导入 UIGF JSON 文件。"""
        if not file_path:
            self._request_toast("请先选择文件", "#b04b44")
            return
        threading.Thread(
            target=self._import_gacha_backend,
            args=(file_path,),
            daemon=True,
        ).start()

    @Slot(str, str)
    def importHoyogachaDb(self, db_path: str, game_label: str):
        """从 HoYo.Gacha 本地数据库直接导入（v1/旧版双兼容）。
        game_label: 当前选中的游戏（"原神"/"星穹铁道"/"绝区零"），用于导入后刷新对应账号列表。
        """
        if not db_path:
            self._request_toast("请先选择 HoYo.Gacha 数据库文件", "#b04b44")
            return
        threading.Thread(
            target=self._import_hoyogacha_db_backend,
            args=(db_path, game_label or ""),
            daemon=True,
        ).start()

    @Slot()
    def loadNikkiGacha(self):
        """加载无限暖暖抽卡数据（从数据库）。"""
        threading.Thread(target=self._load_nikki_gacha_backend, daemon=True).start()

    @Slot()
    def loadWuwaGacha(self):
        """加载鸣潮抽卡数据（从数据库）。"""
        threading.Thread(target=self._load_wuwa_gacha_backend, daemon=True).start()

    @Slot()
    def refreshWuwaDaily(self):
        """拉取鸣潮库街区每日数据（体力/活跃度/周本/深塔等）。"""
        threading.Thread(target=self._load_wuwa_daily_backend, daemon=True).start()

    @Slot()
    def loadWuwaDailyCache(self):
        """视图进入时调用：有缓存先即时展示，缺失/过期才后台刷新。

        与终末地森空岛同套机制：切页面/重启 app 都能立刻看到上次结果，
        仅在缓存超过 TTL（默认 5 分钟）或根本没有缓存时才打网络。
        """
        cache = self._wuwa_daily_cache
        if cache is not None:
            try:
                emitted = dict(cache["payload"])
                emitted["cached_at"] = cache.get("fetched_at")
                self.wuwaDailyChanged.emit(json.dumps(emitted, ensure_ascii=False))
            except Exception:  # noqa: BLE001
                pass
        if self._wuwa_daily_needs_refresh():
            self._wuwa_daily_refreshing = True
            threading.Thread(target=self._load_wuwa_daily_backend, daemon=True).start()

    def _wuwa_daily_needs_refresh(self) -> bool:
        """缓存是否过期/缺失，需要后台刷新（已在进行中则返回 False）。"""
        if self._wuwa_daily_refreshing:
            return False
        cache = self._wuwa_daily_cache
        if cache is None:
            return True
        fetched_at = float(cache.get("fetched_at", 0) or 0)
        return (time.time() - fetched_at) > WUWA_DAILY_TTL

    def _load_wuwa_daily_cache(self) -> None:
        """从磁盘读入上次库街区每日数据（结构：{fetched_at, payload}）。"""
        try:
            p = WUWA_DAILY_CACHE
            if p.exists():
                obj = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(obj, dict) and "payload" in obj:
                    self._wuwa_daily_cache = obj
        except Exception:  # noqa: BLE001 - 损坏的缓存直接忽略
            self._wuwa_daily_cache = None

    def _save_wuwa_daily_cache(self, payload: Dict[str, Any]) -> None:
        """写入磁盘缓存（仅成功结果会调用，失败保留旧缓存）。"""
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            obj = {"fetched_at": time.time(), "payload": payload}
            WUWA_DAILY_CACHE.write_text(
                json.dumps(obj, ensure_ascii=False), encoding="utf-8")
            self._wuwa_daily_cache = obj
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Bridge] 鸣潮库街区缓存写入失败: %s", exc)

    @Slot(str)
    def importWuwaGachaFromJson(self, file_paths_json: str):
        """从 WutheringWavesTool 的 pool.json 导入抽卡记录（支持多文件合并去重）。

        Args:
            file_paths_json: JSON 数组字符串，例如 '["path1.json", "path2.json"]'
                            也兼容单路径字符串。
        """
        threading.Thread(
            target=self._import_wuwa_gacha_from_json_backend,
            args=(file_paths_json,),
            daemon=True,
        ).start()

    @Slot(str)
    def importEndfieldGacha(self, file_path: str):
        """从文件加载终末地抽卡数据并分析。"""
        if not file_path:
            self._request_toast("请先选择文件", "#b04b44")
            return
        threading.Thread(
            target=self._load_endfield_gacha_backend,
            args=(file_path,),
            daemon=True,
        ).start()

    @Slot()
    def loadEndfieldDefault(self):
        """从数据库加载终末地抽卡数据（无数据则提示用户导入文件）。"""
        threading.Thread(target=self._load_endfield_db_backend, daemon=True).start()

    @Slot()
    def fetchEndfieldGachaByToken(self):
        """用 user 提供的 token 直接从终末地官方接口拉取抽卡记录。

        参考 https://github.com/bhaoo/endfield-gacha 的协议：
        1. token + uid → OAuth grant → auth_token
        2. auth_token + uid → u8_token_by_uid → u8Token
        3. u8Token → ef-webview.{provider}.com/api/record/{char,weapon} 分页拉取

        凭据从 app_config.json 的 endfield_gacha 块读取（由 QML
        CredentialInput 写入），日志只输出 token 长度，绝不打印明文。
        """
        threading.Thread(
            target=self._fetch_endfield_gacha_by_token_backend,
            daemon=True,
        ).start()

    # ── Credentials slots ─────────────────────────────────────────

    @Slot()
    def loadCredentials(self):
        """加载所有凭据（cookie/token/api_key 等）从 app_config.json 到 QML。"""
        threading.Thread(target=self._load_credentials_backend, daemon=True).start()

    @Slot(str, str, str)
    def saveCredential(self, category: str, key: str, value: str):
        """保存单个凭据到 app_config.json。
        category: 顶层键 (如 "mihoyo_sign", "nikki")
        key: 子键 (如 "cookie", "momo_token")，支持点分多级
        value: 字符串值
        """
        threading.Thread(
            target=self._save_credential_backend,
            args=(category, key, value),
            daemon=True,
        ).start()

    @Slot(str, str, bool)
    def saveCredentialBool(self, category: str, key: str, value: bool):
        """保存布尔型凭据/配置到 app_config.json。"""
        threading.Thread(
            target=self._save_credential_backend,
            args=(category, key, value),
            daemon=True,
        ).start()

    @Slot(str)
    def saveMihoyoDevice(self, rawText: str):
        """识别并保存米游社设备指纹字段集合（APK 参数或抓包 fp/device_id）。"""
        threading.Thread(
            target=self._save_mihoyo_device_backend,
            args=(rawText,),
            daemon=True,
        ).start()

    @Slot()
    def clearMihoyoDevice(self):
        """清除米游社设备指纹绑定。"""
        threading.Thread(
            target=self._clear_mihoyo_device_backend,
            daemon=True,
        ).start()

    @Slot(str)
    def queryEndfieldCenter(self, query_type: str):
        """终末地数据中心查询（第三方协议终端，需 api_key/framework_token）。
        query_type: note | stamina | search_chars | spaceship | achieve | gacha_records | ...
        """
        threading.Thread(
            target=self._query_endfield_center_backend,
            args=(query_type,),
            daemon=True,
        ).start()

    @Slot()
    def queryEndfieldSkland(self):
        """终末地每日数据查询（森空岛官方 API，默认方式）。

        复用「森空岛签到」卡片里已填的 token，无需额外注册
        api_key / framework_token。
        """
        # 强制刷新（按钮触发）：忽略缓存新鲜度，直接打网络
        self._endfield_skland_refreshing = True
        threading.Thread(
            target=self._query_endfield_skland_backend,
            daemon=True,
        ).start()

    @Slot()
    def loadEndfieldSklandCache(self):
        """视图进入时调用：有缓存先即时展示，缺失/过期才后台刷新。

        这样切页面/重启 app 都能立刻看到上次结果，不必每次手动查询；
        仅在缓存超过 TTL（默认 5 分钟）或根本没有缓存时才打网络。
        """
        cache = self._endfield_skland_cache
        if cache is not None:
            try:
                # 给缓存结果打上 cached_at 标记，供 UI 显示"更新于"，不写入磁盘缓存
                emitted = dict(cache["result"])
                emitted["cached_at"] = cache.get("fetched_at")
                self.endfieldQueryResultChanged.emit(
                    json.dumps(emitted, ensure_ascii=False))
            except Exception:  # noqa: BLE001
                pass
        if self._endfield_skland_needs_refresh():
            self._endfield_skland_refreshing = True
            threading.Thread(
                target=self._query_endfield_skland_backend, daemon=True).start()

    def _endfield_skland_needs_refresh(self) -> bool:
        """缓存是否过期/缺失，需要后台刷新（已在进行中则返回 False）。"""
        if self._endfield_skland_refreshing:
            return False
        cache = self._endfield_skland_cache
        if cache is None:
            return True
        fetched_at = float(cache.get("fetched_at", 0) or 0)
        return (time.time() - fetched_at) > ENDFIELD_SKLAND_TTL

    def _load_endfield_skland_cache(self) -> None:
        """从磁盘读入上次查询结果（结构：{fetched_at, result}）。"""
        try:
            p = ENDFIELD_SKLAND_CACHE
            if p.exists():
                obj = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(obj, dict) and "result" in obj:
                    self._endfield_skland_cache = obj
        except Exception:  # noqa: BLE001 - 损坏的缓存直接忽略
            self._endfield_skland_cache = None

    def _save_endfield_skland_cache(self, result: Dict[str, Any]) -> None:
        """写入磁盘缓存（仅成功结果会调用，失败保留旧缓存）。"""
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            obj = {"fetched_at": time.time(), "result": result}
            ENDFIELD_SKLAND_CACHE.write_text(
                json.dumps(obj, ensure_ascii=False), encoding="utf-8")
            self._endfield_skland_cache = obj
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Bridge] 终末地缓存写入失败: %s", exc)

    @Slot()
    def queryTajiduoDaily(self):
        """查询塔吉多（异环）每日任务进度。

        复用签到用的 token + deviceid，不需要额外凭据。
        """
        threading.Thread(
            target=self._query_tajiduo_daily_backend,
            daemon=True,
        ).start()

    # ── Sign action slots (QML 按钮调用) ────────────────────────

    @Slot()
    def runSkendfieldSign(self):
        """手动触发森空岛签到（QML "执行签到" 按钮）。"""
        threading.Thread(target=self._run_skendfield_sign_backend, daemon=True).start()

    @Slot()
    def runWuwaKujiequSign(self):
        """手动触发库街区签到（QML "执行签到" 按钮）。"""
        threading.Thread(target=self._run_wuwa_kujiequ_sign_backend, daemon=True).start()

    @Slot()
    def runWuwaKujiequAdbGrab(self):
        """通过 ADB + WebView CDP 从手机端库街区 APP 抓取 token/did。

        参考：https://wave.999758.xyz/pages/advance/token.html
        前置条件：手机已启用 USB 调试并连接电脑、库街区 APP 已进入"养成计算器"。
        """
        threading.Thread(target=self._run_wuwa_kujiequ_adb_grab_backend, daemon=True).start()

    @Slot()
    def runTajiduoSign(self):
        """手动触发塔吉多（异环）签到（QML "执行签到" 按钮）。"""
        threading.Thread(target=self._run_tajiduo_sign_backend, daemon=True).start()

    # ── WebUI 账号与状态（GUI 内设置，WebUI 通过 /api/auth 鉴权） ──
    @Slot(result=str)
    def getWebUIStatus(self) -> str:
        """返回 {has_account, username, port, url, running} 供 QML 展示。"""
        try:
            from ..webui.auth import has_account, get_username
            from ..webui.app import WEBUI_DEFAULT_PORT
            has_acc = bool(has_account())
            user = get_username() if has_acc else ""
            # 检测是否在运行（尝试连端口）
            running = False
            try:
                import socket
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(0.3)
                running = s.connect_ex(("127.0.0.1", int(WEBUI_DEFAULT_PORT))) == 0
                s.close()
            except Exception:
                running = False
            info = {
                "has_account": has_acc,
                "username": user,
                "port": int(WEBUI_DEFAULT_PORT),
                "url": f"http://127.0.0.1:{int(WEBUI_DEFAULT_PORT)}",
                "running": running,
            }
            self._webui_status_json = json.dumps(info, ensure_ascii=False)
            self.webUIStatusChanged.emit(self._webui_status_json)
            return self._webui_status_json
        except Exception as exc:
            logger.warning("[Bridge] 获取 WebUI 状态失败: %s", exc)
            return json.dumps({"has_account": False, "username": "", "port": 60171, "url": "http://127.0.0.1:60171", "running": False}, ensure_ascii=False)

    @Slot(str, str)
    def saveWebUIAccount(self, username: str, password: str):
        """保存 WebUI 登录账号（GUI 输入，DPAPI 加密落盘）。"""
        try:
            from ..webui.auth import set_account
            set_account(username, password)
            started = False
            try:
                from ..webui.app import start_webui_background
                started = start_webui_background()
            except Exception as exc:
                logger.warning("[Bridge] WebUI 账号已保存但服务启动失败: %s", exc)
            logger.info("[Bridge] WebUI 账号已更新: %s (webui_started=%s)", (username or "").strip(), started)
            self.getWebUIStatus()
            if started:
                # uvicorn 绑定端口需要几百毫秒，稍后再刷新一次状态供 QML 展示
                QTimer.singleShot(800, self.getWebUIStatus)
            message = "WebUI 账号已保存（已加密），WebUI 已启动" if started else "WebUI 账号已保存（已加密），重启 GUI 后启动 WebUI"
            self.emit_event("toast", message=message, color="#2f7d4a")
        except Exception as exc:
            logger.warning("[Bridge] 保存 WebUI 账号失败: %s", exc)
            self.emit_event("toast", message=f"保存失败: {exc}", color="#b04b44")

    @Slot()
    def openWebUIInBrowser(self):
        """用系统默认浏览器打开 WebUI。"""
        try:
            from ..webui.app import WEBUI_DEFAULT_PORT
            import webbrowser
            url = f"http://127.0.0.1:{int(WEBUI_DEFAULT_PORT)}"
            webbrowser.open(url)
            self.emit_event("toast", message=f"已打开 {url}", color="#2f7d4a")
        except Exception as exc:
            self.emit_event("toast", message=f"打开失败: {exc}", color="#b04b44")

    # ═══════════════════════════════════════════════════════════════
    #  Event emit (for background threads — thread-safe)
    # ═══════════════════════════════════════════════════════════════

    def emit_event(self, kind: str, **payload):
        """Emit a uiEvent from any thread (Qt queues delivery to main)."""
        self.uiEvent.emit(kind, payload)

    # ═══════════════════════════════════════════════════════════════
    #  Backend workers (run in background threads)
    # ═══════════════════════════════════════════════════════════════

    def _get_controller(self):
        """懒初始化 AutomationController。"""
        if self._controller is None:
            from ..runtime.automation_controller import AutomationController
            self._controller = AutomationController()
        return self._controller

    def _get_preference_db(self):
        """懒初始化 WorkflowDB。"""
        if self._preference_db is None:
            from ..storage.workflow_db import WorkflowDB
            from ..settings.paths import WORKFLOW_DB_PATH
            self._preference_db = WorkflowDB(str(WORKFLOW_DB_PATH))
        return self._preference_db

    def _load_app_list_backend(self):
        """后台加载应用列表。"""
        try:
            from ..settings import (
                refresh_runtime_config, APP_ORDER, APPS,
                get_app_display_name, get_script_display_name,
            )
            refresh_runtime_config(force=True)

            db = self._get_preference_db()
            saved_prefs = db.get_all_app_launch_preferences()

            apps_list = []
            for idx, app_key in enumerate(APP_ORDER):
                cfg = APPS.get(app_key, {})
                display_name = get_app_display_name(app_key)
                pref = saved_prefs.get(app_key, {})
                enabled = bool(pref.get("enabled", False))
                script_choice = pref.get("script_choice", "")

                launcher_labels = []
                launcher_values = []
                for opt in cfg.get("launcher_options", []):
                    launcher_labels.append(opt.get("display_name", opt.get("name", "")))
                    launcher_values.append(opt.get("name", ""))

                apps_list.append({
                    "order": idx + 1,
                    "app_key": app_key,
                    "display_name": display_name,
                    "enabled": enabled,
                    "script_choice": script_choice,
                    "launcher_labels": launcher_labels,
                    "launcher_values": launcher_values,
                    "task_type": cfg.get("task_type", "常规任务"),
                    "description": cfg.get("description", ""),
                })

            self.emit_event("app_list", apps=apps_list)
        except Exception as exc:
            logger.exception("[Bridge] 加载应用列表失败")
            self.emit_event("toast", message=f"加载失败: {exc}", color="#b04b44")

    def _run_automation_backend(
        self,
        targets: List[Dict],
        force_rerun: bool = False,
        email_on_finish: bool = False,
        shutdown_on_finish: bool = False,
    ):
        """后台执行自动化任务。

        收尾兜底（重要）：邮件与自动关机放在 finally 里，无论任务正常结束、
        失败还是抛出未捕获异常，只要开关打开就必须执行。
        2026-09-29 中午事故：CJ 抛 FailSafeException 穿透 run_task_batch，
        邮件+关机代码在 try 主体里被整体跳过 → 挂机跑完没关机。
        """
        result = None
        run_error: Optional[Exception] = None
        scheduled_shutdown = False
        try:
            controller = self._get_controller()
            self.emit_event("status", title="正在运行", body="自动化任务已启动", tone=TONE_RUNNING)
            self.emit_event("running", value=True)

            target_apps = [(t["app_key"], t.get("script_choice") or None) for t in targets]
            from ..settings import get_app_display_name, get_script_display_name

            # 开批前预检：公告维护窗口内的游戏不启动脚本，直接从本批剔除，
            # 按公告开服时间排队补跑；其余游戏照常执行。
            runnable_apps, deferred_apps = self._split_maintenance_targets(target_apps)
            deferred_keys = {d["app_key"] for d in deferred_apps}
            for d in deferred_apps:
                display = get_app_display_name(d["app_key"])
                open_dt = d["state"].get("open_at_dt")
                open_txt = open_dt.strftime("%H:%M") if open_dt else "?"
                logger.info(
                    "[更新预检] %s 维护中，本批不启动，已排队 %s 开服后补跑",
                    display, open_txt,
                )
                self.emit_event(
                    "log",
                    message=f"[更新预检] {display} 维护中，已排队到 {open_txt} 开服后补跑",
                )
            if deferred_apps:
                self._request_toast(
                    f"{len(deferred_apps)} 个游戏更新中，已从本批剔除并排队补跑",
                    "#b66f1f",
                )
            if not runnable_apps:
                logger.info("[更新预检] 全部目标游戏均在维护中，本批不启动游戏脚本")
                self.emit_event(
                    "log",
                    message="[更新预检] 全部目标游戏均在维护中，已全部排队到开服后补跑",
                )

            # 构建队列列表
            queue_items = []
            for app_key, script_choice in target_apps:
                display = get_app_display_name(app_key)
                script_label = get_script_display_name(app_key, script_choice)
                queue_items.append({
                    "app_key": app_key,
                    "task_name": f"{display}({script_label})",
                    "script_label": script_label,
                    "state": "已排队（更新）" if app_key in deferred_keys else "待执行",
                })
            self.emit_event("queue_list", items=queue_items)
            # 同步到 WebUI 共享状态，保证 WebUI 能看到 GUI 发起的任务
            try:
                from game_auto.webui.state import set_running as _ws_set_running, update_queue_state as _ws_upd, set_last_result as _ws_set_last, logger as _ws_logger
                # 转换为 WebUI 队列格式
                ws_queue = []
                for it in queue_items:
                    ws_queue.append({
                        "app_key": it["app_key"],
                        "display_name": it["task_name"],
                        "script_choice": it.get("script_label", ""),
                        "state": "pending",
                    })
                _ws_set_running(True, queue=ws_queue, source="gui")
                for app_key in deferred_keys:
                    _ws_upd(app_key, "skipped", "更新中，已排队补跑")
                _ws_logger.info("[WebUI][运行] GUI 发起任务 %s", ", ".join([q["app_key"] for q in ws_queue]))
            except Exception:
                pass

            from ..runtime.task_batch_runner import run_task_batch, TaskBatchHooks

            def on_before_app(app_key, script_choice, display_name):
                self.emit_event("queue_state", app_key=app_key, state="运行中")
                self.emit_event("queue_list_update", app_key=app_key, state="运行中")
                self.emit_event("status", title="正在运行",
                                body=f"正在执行: {display_name}",
                                tone=TONE_RUNNING)
                try:
                    from game_auto.webui.state import update_queue_state as _ws_upd2
                    _ws_upd2(app_key, "running", display_name)
                except Exception:
                    pass

            def on_after_app(result):
                app_key = result.app_key if hasattr(result, "app_key") else ""
                result_state = getattr(result, "state", "")
                if result_state == "skipped":
                    ui_state = "已跳过"
                elif result.success:
                    ui_state = "已完成"
                else:
                    ui_state = "失败"
                self.emit_event("queue_state", app_key=app_key, state=ui_state)
                self.emit_event("queue_list_update", app_key=app_key, state=ui_state)
                try:
                    from game_auto.webui.state import update_queue_state as _ws_upd3
                    # 映射到 WebUI 状态
                    ws_state = "completed" if result.success else "failed"
                    if result_state == "skipped":
                        ws_state = "skipped"
                    elif result_state == "stopped":
                        ws_state = "stopped"
                    _ws_upd3(app_key, ws_state, getattr(result, "message", ""))
                except Exception:
                    pass
                try:
                    self._handle_update_retry_result(result)
                except Exception:
                    logger.warning("[更新补跑] 处理任务结果失败", exc_info=True)

            hooks = TaskBatchHooks(
                on_before_app=on_before_app,
                on_after_app=on_after_app,
            )

            result = run_task_batch(
                controller,
                runnable_apps,
                hooks=hooks,
                force_rerun=force_rerun,
            )
        except Exception as exc:
            run_error = exc
            logger.exception("[Bridge] 自动化执行失败")
        finally:
            # 一条龙：邮件和关机是两个独立动作；邮件异步发送不阻塞 UI。
            # 无论上面成功、失败还是异常，这里都要执行（收尾兜底）。
            report = list(result.report) if result is not None else []
            warnings = list(result.warnings) if result is not None else []
            if run_error is not None:
                report = report + [f"❌ 任务异常中断（收尾兜底已执行）: {run_error}"]

            for line in report:
                self.emit_event("log", message=line)
            for warning in warnings:
                self.emit_event("log", message=f"[警告] {warning}")

            # 邮件/关机统一延迟到"当天所有批次 + 定时任务 + 更新补跑"结束后
            # 各执行一次：按批次各自发会重复发日报，共享开关也会被后续批次覆盖。
            if email_on_finish:
                self._email_pending = True
            if self._email_pending:
                self._email_report.extend(report)
                self._email_warnings.extend(warnings)
            if shutdown_on_finish:
                self._shutdown_pending = True

            pending_total = (
                len(self._unfired_scheduled_today())
                + len(self._pending_update_retries())
            )
            if pending_total:
                if self._email_pending or self._shutdown_pending:
                    self._shutdown_deferred = self._shutdown_pending
                    logger.info(
                        "[Bridge] 还有 %d 个定时任务/更新补跑未执行，邮件/关机推迟到全部结束",
                        pending_total,
                    )
                    self.emit_event(
                        "toast",
                        message=f"还有{pending_total}个定时任务/更新补跑未执行，邮件/关机将等全部结束后统一处理",
                        color="#b66f1f",
                    )
            else:
                if self._finalize_run_actions():
                    scheduled_shutdown = True

            # 一条龙结束后主动刷新每日进度，让 DailyView 立即看到今日记录
            try:
                self.loadDailyProgress()
            except Exception:
                logger.debug("[Bridge] 结束后刷新每日进度失败", exc_info=True)

            # 同步最终报告到 WebUI 共享状态
            try:
                from game_auto.webui.state import set_running as _ws_set_running2, set_last_result as _ws_set_last2
                _ws_set_last2(report, warnings)
                _ws_set_running2(False)
            except Exception:
                pass
            self.emit_event("finished", toast=True, shutdown_scheduled=scheduled_shutdown)
            if run_error is not None:
                self.emit_event("status", title="运行失败", body=str(run_error), tone=TONE_DANGER)
                self.emit_event("toast", message=f"执行失败: {run_error}", color="#b04b44")

    def _finalize_run_actions(self) -> bool:
        """当天全部任务结束后统一收尾：最多发一封日报、最多调度一次关机。"""
        today = time.strftime("%Y-%m-%d")
        generation = self._run_generation
        email_thread = None
        if self._email_pending:
            if self._email_sent_date == today:
                self._email_pending = False
                self._email_report.clear()
                self._email_warnings.clear()
                logger.info("[Bridge] 今日日报已发送，本批次不再重复发送")
            else:
                report = list(dict.fromkeys(self._email_report))
                warnings = list(dict.fromkeys(self._email_warnings))
                self._email_pending = False
                self._email_report.clear()
                self._email_warnings.clear()
                self._email_sent_date = today
                logger.info("[Bridge] 全部任务结束，统一发送日报（累积报告 %d 行）", len(report))

                def _send_email_async(report=report, warnings=warnings):
                    try:
                        email_ok, email_error = self._send_daily_email_report(
                            automation_report=report,
                            automation_warnings=warnings,
                        )
                        if not email_ok:
                            self._email_sent_date = ""
                            self._request_toast(f"邮件发送失败：{email_error}", "#b66f1f")
                            logger.warning("[Bridge] 邮件发送失败: %s", email_error)
                    except Exception:
                        self._email_sent_date = ""
                        logger.exception("[Bridge] 邮件发送异常")
                    finally:
                        # 发信前会实时刷新进度（最长 120s），关机必须等它结束，
                        # 否则 60 秒关机可能抢在日报发出之前。
                        self._schedule_pending_shutdown(generation)

                email_thread = threading.Thread(target=_send_email_async, daemon=True)
                email_thread.start()

        if not self._shutdown_pending:
            return False
        if email_thread is not None:
            logger.info("[Bridge] 日报正在发送，发信完成后再调度关机")
            return False
        return self._schedule_pending_shutdown()

    def _schedule_pending_shutdown(self, generation: Optional[int] = None) -> bool:
        if not self._shutdown_pending:
            return False
        if generation is not None and generation != self._run_generation:
            logger.info("[Bridge] 新一轮任务已启动，取消旧批次的推迟关机")
            self._shutdown_pending = False
            self._shutdown_deferred = False
            return False
        if self._shutdown_deferred:
            logger.info("[Bridge] 推迟的关机现在执行（定时任务/更新补跑已全部结束）")
        self._shutdown_pending = False
        self._shutdown_deferred = False
        if self._shutdown_scheduled:
            return True
        if self._schedule_shutdown():
            self._request_toast("任务已完成，60 秒后将自动关机。", "#b66f1f")
            return True
        logger.warning("[Bridge] 自动关机调度失败，请检查权限或系统设置")
        return False

    def _schedule_shutdown(self, delay_sec: int = 60) -> bool:
        """调用 Windows shutdown 计划 60 秒后关机；成功返回 True。"""
        try:
            # 用 subprocess.run 替代 os.system：
            # 1) CREATE_NO_WINDOW 避免弹出 cmd.exe 黑框抢焦点
            # 2) subprocess 等待子进程期间释放 GIL，os.system 不释放，会卡住 UI 线程
            completed = subprocess.run(
                # /f 强制结束未响应程序，避免弹窗阻塞无人值守关机
                ["shutdown", "/s", "/f", "/t", str(int(delay_sec))],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                capture_output=True,
                timeout=10,
            )
            rc = completed.returncode
        except Exception as exc:
            logger.warning("[Bridge] 调用 shutdown /s 失败: %s", exc)
            return False
        # shutdown 返回 0 表示成功提交，1116/1190 表示已存在计划
        if rc not in (0, 1116, 1190):
            logger.warning("[Bridge] shutdown /s 返回非预期 rc=%s", rc)
            return False
        self._shutdown_scheduled = True
        self.shutdownScheduledChanged.emit()
        logger.info("[Bridge] 已计划 %s 秒后自动关机 (rc=%s)", delay_sec, rc)
        return True

    def _refresh_daily_snapshot_for_report(self, tracker_factory, timeout_sec: float = 120.0):
        """发报告前实时拉取各游戏每日进度，写入 tracker 并返回最新快照。

        返回 (snapshot, fresh)。
          fresh=True  → 本次实时查询成功，数据是新的
          fresh=False → 查询超时/失败，已降级到数据库缓存（邮件里会标注）

        设计要点：
          - 实时查询要访问多个外网 API，必须放子线程 + join(timeout)，
            否则会卡住随后的关机流程；超时就退回缓存，绝不阻塞发信。
          - check_games_progress(tracker=...) 会把结果写回库，
            所以之后 load_today() 拿到的就是新鲜数据。
        """
        try:
            tracker = tracker_factory()
        except Exception as exc:
            logger.warning("[Bridge] 打开进度 tracker 失败: %s", exc)
            return None, False

        box: Dict[str, Any] = {}

        def _run():
            try:
                from ..clients.daily_checker import check_games_progress
                box["result"] = check_games_progress(tracker=tracker)
            except Exception as exc:
                box["error"] = exc

        try:
            t = threading.Thread(target=_run, daemon=True, name="daily-refresh-for-email")
            t.start()
            t.join(timeout=timeout_sec)
        except Exception as exc:
            logger.warning("[Bridge] 实时刷新每日进度线程异常: %s", exc)
            box["error"] = exc

        if threading.current_thread() is not t and t.is_alive():
            logger.warning(
                "[Bridge] 实时刷新每日进度超时（%.0fs），邮件改用数据库缓存数据", timeout_sec)
            try:
                return tracker.load_today(), False
            except Exception as exc:
                logger.warning("[Bridge] 读取进度缓存失败: %s", exc)
                return None, False

        err = box.get("error")
        if err is not None:
            logger.warning("[Bridge] 实时刷新每日进度失败，改用缓存: %s", err)
            try:
                return tracker.load_today(), False
            except Exception as exc:
                logger.warning("[Bridge] 读取进度缓存失败: %s", exc)
                return None, False

        result = box.get("result")
        if result is not None:
            ok = sum(1 for g in result.games if g.is_daily_completed)
            logger.info(
                "[Bridge] 发信前实时刷新每日进度完成: %d 款游戏，其中 %d 款已完成",
                len(result.games), ok)
        try:
            return tracker.load_today(), True
        except Exception as exc:
            logger.warning("[Bridge] 读取刷新后进度失败: %s", exc)
            return None, True

    def _send_daily_email_report(
        self,
        automation_report: List[str],
        automation_warnings: List[str],
    ) -> tuple:
        """读取 SMTP 配置 + tracker 数据，发送一条龙报告邮件。返回 (成功, 错误信息)。"""
        from ..services.email_reporter import (
            build_daily_report,
            is_email_configured,
            load_email_config,
            send_email,
        )
        from ..services.daily_progress_tracker import get_tracker
        from ..settings.paths import WORKFLOW_DB_PATH

        if not is_email_configured():
            return False, "app_config.json 的 email 段未配置或 enabled=false"

        config = load_email_config()
        if config is None:
            return False, "SMTP 配置不完整（缺少 host/sender/password/recipients）"

        # 发信前**实时**查一遍各游戏每日进度并写回 tracker。
        # 不能用 load_today() 直接读缓存：一条龙通常跑几十分钟到几小时，
        # 库里可能还是跑之前（甚至早上）的数据，邮件会发出过时进度。
        daily_snapshot, snapshot_fresh = self._refresh_daily_snapshot_for_report(
            tracker_factory=lambda: get_tracker(str(WORKFLOW_DB_PATH))
        )

        # 简单统计：本轮 success / failed 计数
        automation_total = len(automation_report)
        automation_success = sum(1 for line in automation_report if "✅" in line or "已完成" in line)
        automation_failed = sum(1 for line in automation_report if "❌" in line or "失败" in line)

        subject, body = build_daily_report(
            automation_report=automation_report,
            automation_warnings=automation_warnings,
            automation_total=automation_total,
            automation_success=automation_success,
            automation_failed=automation_failed,
            daily_snapshot=daily_snapshot,
            snapshot_fresh=snapshot_fresh,
        )

        try:
            ok, err = send_email(config, subject, body)
        except Exception as exc:
            logger.exception("[Bridge] 邮件发送异常")
            return False, str(exc)
        if not ok:
            return False, err or "未知 SMTP 错误"
        self._request_toast("每日进度邮件已发送", "#2f7d4a")
        return True, ""

    def _save_preferences_backend(self, prefs: List[Dict]):
        """后台保存启动偏好。"""
        try:
            db = self._get_preference_db()
            for pref in prefs:
                app_key = pref.get("app_key", "")
                enabled = bool(pref.get("enabled", False))
                script_choice = pref.get("script_choice", "")
                db.save_app_launch_preference(app_key, enabled, script_choice, False)
            self.emit_event("toast", message="启动偏好已保存", color="#2f7d4a")
            logger.info("[Bridge] 已保存 %d 项启动偏好", len(prefs))
        except Exception as exc:
            logger.exception("[Bridge] 保存偏好失败")
            self.emit_event("toast", message=f"保存失败: {exc}", color="#b04b44")

    def _run_daily_check_backend(self, game_keys: List[str]):
        """后台查询每日进度。"""
        try:
            self.emit_event("status", title="正在查询", body="正在获取每日进度...", tone=TONE_RUNNING)

            from ..clients.daily_checker import check_games_progress
            from ..services.daily_progress_tracker import get_tracker
            from ..settings.paths import WORKFLOW_DB_PATH

            # 同步写入 tracker，让 loadDailyProgress 下次能拿到今日记录
            tracker = None
            try:
                tracker = get_tracker(str(WORKFLOW_DB_PATH))
            except Exception as exc:
                logger.warning("[Bridge] 初始化 tracker 失败，跳过持久化: %s", exc)

            result = check_games_progress(
                game_keys=game_keys if game_keys else None,
                tracker=tracker,
            )

            games_data = []
            for game in result.games:
                tasks_data = []
                for task in game.tasks:
                    # DailyTaskItem 字段：name/current/total/is_completed
                    t_status = "success" if task.is_completed else "pending"
                    if task.total and task.total > 0:
                        t_message = f"{task.current}/{task.total}"
                    elif task.is_completed:
                        t_message = "已完成"
                    else:
                        t_message = "未完成"
                    tasks_data.append({
                        "task_key": task.name,
                        "task_name": task.name,
                        "status": t_status,
                        "message": t_message,
                        "current": task.current,
                        "total": task.total,
                    })
                games_data.append({
                    "game_key": game.game_key,
                    "game_name": game.game_name,
                    "uid": game.uid,
                    "nickname": game.nickname,
                    # GameDailyStatus 用 is_daily_completed，没有 is_all_success/has_failed
                    # has_failed 仅当 API 报错时为真；任务未完成属于 pending 不算 failed
                    "is_all_success": game.is_daily_completed,
                    "has_failed": bool(game.errors),
                    "progress_percent": game.progress_percent,
                    "tasks": tasks_data,
                    "errors": game.errors,
                })

            self._inject_external_log_progress(games_data)
            self._inject_sign_task_into_games(games_data)
            self._ensure_baijing_placeholder(games_data)
            self._attach_completion_state(games_data)
            self.emit_event("daily_progress", games=games_data, date_key="")
            self.emit_event("status", title="查询完成",
                            body=f"已查询 {len(games_data)} 款游戏",
                            tone=TONE_SUCCESS)
            self.emit_event("toast", message="每日进度已刷新", color="#2f7d4a")
        except Exception as exc:
            logger.exception("[Bridge] 每日进度查询失败")
            self.emit_event("status", title="查询失败", body=str(exc), tone=TONE_DANGER)
            self.emit_event("toast", message=f"查询失败: {exc}", color="#b04b44")

    def _serialize_snapshot_games(self, snapshot) -> List[Dict[str, Any]]:
        """把 DailyProgressSnapshot 序列化成 QML 可直接绑定的列表。"""
        from ..settings import resolve_game_key
        from ..settings.defaults import GAME_KEYS

        games_data: List[Dict[str, Any]] = []
        for game in snapshot.games:
            gk = resolve_game_key(game.game_key)
            tasks_data = []
            for task in game.tasks:
                t_extra = task.extra if isinstance(task.extra, dict) else {}
                t_current, t_total = self._extract_task_counts(t_extra)
                tasks_data.append({
                    "task_key": task.task_key,
                    "task_name": task.task_name,
                    "status": task.status,
                    "message": task.message,
                    "current": t_current,
                    "total": t_total,
                    "extra": t_extra,
                })
            games_data.append({
                "game_key": gk,
                "game_name": game.game_name or GAME_KEYS.get(gk, gk),
                "uid": game.uid,
                "nickname": game.nickname,
                "is_all_success": game.is_all_success,
                "has_failed": game.has_failed,
                "progress_percent": game.progress_percent,
                "completion_state": game.completion_state(),
                "tasks": tasks_data,
                "errors": list(game.errors or []),
            })
        games_data.sort(key=lambda g: (-g["progress_percent"], g["game_name"]))
        return games_data

    def _load_daily_dates_backend(self):
        """列出有每日进度记录的日期（倒序）。"""
        try:
            from ..services.daily_progress_tracker import get_tracker
            from ..settings.paths import WORKFLOW_DB_PATH

            tracker = get_tracker(str(WORKFLOW_DB_PATH))
            dates = tracker.list_available_dates(limit=120)
            payload = {"ok": True, "dates": dates}
        except Exception as exc:
            logger.exception("[Bridge] 加载历史日期列表失败")
            payload = {"ok": False, "dates": [], "message": str(exc)}
        self._daily_dates_json = json.dumps(payload, ensure_ascii=False)
        self.dailyDatesChanged.emit(self._daily_dates_json)

    def _load_daily_history_backend(self, date_key: str):
        """回看指定日期的每日进度。"""
        if not date_key:
            self._daily_history_json = json.dumps(
                {"ok": False, "date_key": "", "message": "未指定日期", "games": []},
                ensure_ascii=False)
            self.dailyHistoryChanged.emit(self._daily_history_json)
            return
        try:
            from ..services.daily_progress_tracker import get_tracker
            from ..settings.paths import WORKFLOW_DB_PATH

            tracker = get_tracker(str(WORKFLOW_DB_PATH))
            snapshot = tracker.load_date(date_key)
            games = self._serialize_snapshot_games(snapshot)
            payload = {
                "ok": True,
                "date_key": snapshot.date_key,
                "games": games,
                "message": "" if games else f"{date_key} 没有记录",
            }
            logger.info("[Bridge] 回看历史进度 %s：%d 款游戏", date_key, len(games))
        except Exception as exc:
            logger.exception("[Bridge] 回看历史进度失败: %s", date_key)
            payload = {"ok": False, "date_key": date_key, "games": [], "message": str(exc)}
        self._daily_history_json = json.dumps(payload, ensure_ascii=False)
        self.dailyHistoryChanged.emit(self._daily_history_json)

    def _load_daily_stats_backend(self, days: int):
        """统计最近 days 天各游戏完成率。"""
        try:
            from ..services.daily_progress_tracker import get_tracker
            from ..settings.paths import WORKFLOW_DB_PATH

            tracker = get_tracker(str(WORKFLOW_DB_PATH))
            stats = tracker.stats_recent_days(days)
            dates = tracker.list_available_dates(limit=days)
            payload = {
                "ok": True,
                "days": days,
                "date_from": dates[-1] if dates else "",
                "date_to": dates[0] if dates else "",
                "stats": stats,
            }
            logger.info("[Bridge] 统计最近 %d 天完成率：%d 款游戏", days, len(stats))
        except Exception as exc:
            logger.exception("[Bridge] 统计完成率失败")
            payload = {"ok": False, "days": days, "stats": [], "message": str(exc)}
        self._daily_stats_json = json.dumps(payload, ensure_ascii=False)
        self.dailyStatsChanged.emit(self._daily_stats_json)

    def _load_daily_progress_backend(self):
        """从 SQLite 加载今日持久化进度。"""
        try:
            from ..services.daily_progress_tracker import get_tracker
            from ..settings.paths import WORKFLOW_DB_PATH
            from ..settings import resolve_game_key
            from ..settings.defaults import GAME_KEYS
            tracker = get_tracker(str(WORKFLOW_DB_PATH))
            snapshot = tracker.load_today()

            # 合并同游戏的卡片：tracker 里可能同时存在大驼峰式（旧 task_batch_runner）
            # 和小写式（daily_checker / parser）两种 game_key，需要统一合并。
            merged_games: Dict[str, Dict[str, Any]] = {}
            for game in snapshot.games:
                gk = resolve_game_key(game.game_key)
                tasks_data = []
                for task in game.tasks:
                    t_extra = task.extra if isinstance(task.extra, dict) else {}
                    t_current, t_total = self._extract_task_counts(t_extra)
                    # 白荆验证截图：extra.verify_image 为相对路径，需转 file:// 供 QML Image
                    v_rel = t_extra.get("verify_image") if isinstance(t_extra.get("verify_image"), str) else None
                    v_url = None
                    if v_rel:
                        try:
                            from pathlib import Path as _P
                            from ..settings.paths import BASE_DIR as _BD
                            # 兼容相对/绝对两种写法
                            p = _P(v_rel)
                            if not p.is_absolute():
                                p = _BD / v_rel
                            if p.exists():
                                v_url = p.as_uri()
                            else:
                                v_url = None
                        except Exception:
                            v_url = None
                    tasks_data.append({
                        "task_key": task.task_key,
                        "task_name": task.task_name,
                        "status": task.status,
                        "message": task.message,
                        "current": t_current,
                        "total": t_total,
                        "verify_image": v_rel,
                        "verify_image_url": v_url,
                        "extra": t_extra,
                    })
                if gk in merged_games:
                    # 合并到已有卡片
                    existing = merged_games[gk]
                    existing["tasks"].extend(tasks_data)
                    existing["errors"].extend(game.errors or [])
                    # 取更优状态
                    if game.is_all_success:
                        existing["is_all_success"] = True
                    if game.has_failed:
                        existing["has_failed"] = True
                else:
                    merged_games[gk] = {
                        "game_key": gk,
                        "game_name": game.game_name or GAME_KEYS.get(gk, gk),
                        "uid": game.uid,
                        "nickname": game.nickname,
                        "is_all_success": game.is_all_success,
                        "has_failed": game.has_failed,
                        "progress_percent": game.progress_percent,
                        "tasks": tasks_data,
                        "errors": list(game.errors or []),
                    }
            games_data = list(merged_games.values())

            self._inject_external_log_progress(games_data)
            self._inject_sign_task_into_games(games_data)
            self._ensure_baijing_placeholder(games_data)
            self._attach_completion_state(games_data)
            self.emit_event("daily_progress", games=games_data, date_key=snapshot.date_key)
            logger.info("[Bridge] 加载持久化进度: date_key=%s games=%d",
                        snapshot.date_key, len(games_data))
        except Exception as exc:
            logger.exception("[Bridge] 加载持久化进度失败")
            self.emit_event("toast", message=f"加载失败: {exc}", color="#b04b44")

    @staticmethod
    def _real_completion_percent(game_key: str, tasks: List[Dict[str, Any]]):
        """从任务列表里取"真实游玩进度"百分比，而非脚本子任务比例。

        返回 (percent, source_key) 或 (None, None) 表示无可用真实计数。
        动机：ok-ww 的 progress_percent 只是"脚本跑了几步"，全跑完就是 100%，
        但玩家活跃度可能只有 90/100 —— 直接展示会严重误导。
        """
        # 复用 tracker 里的统一映射，避免两处各写一份逐渐走样
        try:
            from ..services.daily_progress_tracker import KEY_PROGRESS_TASKS
        except Exception:
            return None, None
        names = KEY_PROGRESS_TASKS.get(game_key)
        if not names:
            return None, None
        for want in names:
            for t in tasks or []:
                tname = str(t.get("task_name") or t.get("task_key") or "")
                if tname != want:
                    continue
                cur = t.get("current")
                total = t.get("total")
                try:
                    cur_i = int(cur)
                    total_i = int(total)
                except (TypeError, ValueError):
                    continue
                if total_i <= 0:
                    continue
                # 官方 API 未刷新时 cur 会是 0，此时不谎报 0%，交给调用方兜底
                if cur_i <= 0:
                    return None, None
                pct = int(cur_i / total_i * 100)
                return min(pct, 100), want
        return None, None

    @staticmethod
    def _attach_completion_state(games_data: List[Dict[str, Any]]):
        """给合并后的 games_data 逐项补 completion_state（供卡片徽标用）。

        合并后的 dict 不是 GameDailyRecord，按字段重建一个再判定，
        兼容两种任务形状：tracker 侧 extra.current/total 与查询侧顶层 current/total。
        失败不抛，缺字段就跳过该项（QML 回退旧逻辑）。
        """
        try:
            from ..services.daily_progress_tracker import DailyTaskRecord, GameDailyRecord
        except Exception:
            return
        for g in games_data or []:
            try:
                tasks = []
                for t in (g.get("tasks") or []):
                    if not isinstance(t, dict):
                        continue
                    extra = t.get("extra") if isinstance(t.get("extra"), dict) else {}
                    if "current" not in extra and t.get("current") is not None:
                        extra = dict(extra)
                        extra["current"] = t.get("current")
                        extra["total"] = t.get("total")
                    tasks.append(DailyTaskRecord(
                        task_key=str(t.get("task_key") or t.get("task_name") or ""),
                        task_name=str(t.get("task_name") or t.get("task_key") or ""),
                        source=str(t.get("source") or "script"),
                        status=str(t.get("status") or "pending"),
                        message=str(t.get("message") or ""),
                        extra=extra,
                    ))
                rec = GameDailyRecord(
                    game_key=str(g.get("game_key") or ""),
                    game_name=str(g.get("game_name") or ""),
                    tasks=tasks,
                    errors=list(g.get("errors") or []),
                )
                g["completion_state"] = rec.completion_state()
                # 完成度口径标签：拿得到关键任务真实计数（活跃度等）时，
                # progress_percent 就是游玩进度，必须标 non-script，否则白荆
                # 活跃度 15/100 会被误显示成"脚本 15%"（2026-10-19）。
                # 取不到真实计数时才是脚本子任务完成比例。
                # wuwa/endfield 合并路径已自带更精确的来源名，不覆盖。
                if not g.get("progress_source"):
                    g["progress_source"] = (
                        "real" if rec.real_progress_percent() is not None else "script"
                    )
            except Exception:
                continue

    @staticmethod
    def _extract_task_counts(t_extra: Dict[str, Any]):
        """从任务 extra 里取真实计数 (current, total)。

        优先顶层 current/total；缺失时回退 extra.summary 的
        done/tasks_total——白荆"日常任务"把 2/15 这类真实完成度放在
        summary 里，顶层没有计数。不提取的话 UI 只能看到
        status=success 的 ✅，实际只做了 2/15 也会打勾误导用户
        （2026-10-19 事故）。
        """
        if not isinstance(t_extra, dict):
            return None, None
        current = t_extra.get("current")
        total = t_extra.get("total")
        if current is None or total is None:
            summary = t_extra.get("summary")
            if isinstance(summary, dict):
                if current is None:
                    current = summary.get("done")
                if total is None:
                    total = summary.get("tasks_total")
        return current, total

    def _inject_external_log_progress(self, games_data: List[Dict[str, Any]]):
        """读取外部自动化工具日志，将日常执行情况注入 games_data。

        支持的工具与映射：
          - ok-ef（终末地）→ game_key="endfield"，只要关键任务（⭐活动奖励、⭐日常奖励）成功就算成功
          - ok-nte（异环）→ game_key="nte"，只要关键任务（领取活跃度奖励）成功就算成功
          - ok-ww（鸣潮）→ game_key="wuwa"，只要出现 Successfully Executed Task 就算成功
          - whimbox（无限暖暖）→ game_key="nikki"，白名单任务 + 末尾"一条龙结束"算成功
          - BetterGI（原神）→ game_key="genshin"，每日委托奖励 + 探索派遣 都完成才算成功

        行为：
          - whimbox：保留原有 tasks，**前置插入**白名单任务（同名不重复），用于细化任务细节
          - 其他：如果今天的日志存在，整体替换原有的对应 game 项；否则保持原数据不变
        """
        from ..services.ok_ef_daily_parser import parse_latest_ok_ef_report
        from ..services.ok_nte_daily_parser import parse_latest_ok_nte_log
        from ..services.ok_ww_daily_parser import parse_latest_ok_ww_log
        from ..services.whimbox_daily_parser import parse_latest_whimbox_log
        from ..services.bettergi_daily_parser import parse_latest_bettergi_log
        from ..services.zzz_daily_parser import parse_latest_zzz_log
        from ..services.starrail_daily_parser import parse_latest_starrail_log
        from ..settings import resolve_game_key

        parsers = (
            ("endfield", "ok-ef", parse_latest_ok_ef_report),
            ("nte", "ok-nte", parse_latest_ok_nte_log),
            ("wuwa", "ok-ww", parse_latest_ok_ww_log),
            ("nikki", "whimbox", parse_latest_whimbox_log),
            ("genshin", "BetterGI", parse_latest_bettergi_log),
            ("zzz", "OneDragon", parse_latest_zzz_log),
            ("honkai_sr", "March7th", parse_latest_starrail_log),
        )
        for game_key, tool_name, parser_func in parsers:
            try:
                data = parser_func()
                if data is None:
                    if game_key == "nte":
                        # 异环：ok-nte 今天若未执行，仍保留“游戏内每日”占位任务，
                        # 进度由 ok-nte 执行情况判定（未执行 = 待执行），避免游戏内进度整体缺失。
                        data = {
                            "game_key": "nte",
                            "game_name": "异环",
                            "uid": "",
                            "nickname": "ok-nte",
                            "is_all_success": False,
                            "has_failed": False,
                            "progress_percent": 0,
                            "tasks": [{
                                "task_key": "游戏内每日",
                                "task_name": "游戏内每日",
                                "status": "pending",
                                "message": "待 ok-nte 执行",
                            }],
                            "errors": [],
                            "report_time": "",
                            "log_date": "",
                        }
                    else:
                        continue
                resolved_key = resolve_game_key(game_key)
                replaced = False
                for g_idx, game in enumerate(games_data):
                    if resolve_game_key(game.get("game_key", "")) == resolved_key:
                        if game_key == "nikki":
                            # 无限暖暖特殊处理：whimbox 的子任务前置插入，保留 Pearpal 基线
                            existing_tasks = list(game.get("tasks") or [])
                            whimbox_tasks = data.get("tasks") or []
                            # 用 task_key 去重：whimbox 的 task_key 与 Pearpal 同名则取最新
                            existing_keys = {t.get("task_key") or t.get("task_name"): idx
                                             for idx, t in enumerate(existing_tasks)}
                            merged = []
                            for wt in whimbox_tasks:
                                wkey = wt.get("task_key") or wt.get("task_name")
                                if wkey in existing_keys:
                                    e_idx = existing_keys[wkey]
                                    # 用 whimbox 数据覆盖同名（更精细的进度）
                                    merged.append({**existing_tasks[e_idx], **wt})
                                else:
                                    merged.append(wt)
                            for j, t in enumerate(existing_tasks):
                                tkey = t.get("task_key") or t.get("task_name")
                                if tkey not in {(wt.get("task_key") or wt.get("task_name")) for wt in whimbox_tasks}:
                                    merged.append(t)
                            games_data[g_idx]["tasks"] = merged
                            # 整体成功/失败由 whimbox 决定（更精确）
                            games_data[g_idx]["is_all_success"] = data.get("is_all_success", games_data[g_idx].get("is_all_success"))
                            games_data[g_idx]["has_failed"] = data.get("has_failed", games_data[g_idx].get("has_failed"))
                            if data.get("errors"):
                                games_data[g_idx]["errors"] = data["errors"]
                            games_data[g_idx]["nickname"] = data.get("nickname", games_data[g_idx].get("nickname", ""))
                            logger.info("[Bridge] 已用 %s 日志合并 %s 任务细节: tasks=%d",
                                        tool_name, game_key, len(merged))
                        else:
                            if game_key == "nte":
                                # 异环特殊处理：游戏内进度由 ok-nte 执行情况判定。
                                # 保留塔吉多社区任务（社区签到/浏览帖子/点赞帖子/分享）
                                # 与 BBS 签到成果；仅丢弃"游戏签到"任务（其完成情况
                                # 由 ok-nte 进入游戏后的真实执行结果代表）。
                                # 历史 bug：过滤条件写成"endswith(签到)"，把 BBS签到
                                # （+80金币 API 成果）和异环签到也误删，导致 DailyView
                                # 签到行走 last_sign_* 兜底显示"今日尚未签到"。
                                existing_tasks = list(game.get("tasks") or [])
                                ok_nte_tasks = list(data.get("tasks") or [])
                                kept = [
                                    t for t in existing_tasks
                                    if str(t.get("task_name", "")) != "游戏签到"
                                ]
                                merged = list(kept) + ok_nte_tasks
                                # 社区任务全部完成（无社区任务时视为满足，不阻塞 ok-nte 判定）
                                community_done = all(
                                    t.get("status") == "success" for t in kept
                                ) if kept else True
                                game["tasks"] = merged
                                game["is_all_success"] = bool(data.get("is_all_success")) and community_done
                                game["has_failed"] = bool(data.get("has_failed")) or bool(game.get("has_failed"))
                                if data.get("errors"):
                                    game["errors"] = (game.get("errors") or []) + list(data.get("errors"))
                                game["nickname"] = data.get("nickname", game.get("nickname", ""))
                                game["progress_percent"] = data.get("progress_percent", game.get("progress_percent"))
                                logger.info(
                                    "[Bridge] 异环：合并 ok-nte 游戏内进度(%d 项) + 社区任务(%d 项)，成功=%s",
                                    len(ok_nte_tasks), len(kept), game["is_all_success"])
                            elif game_key in ("wuwa", "endfield"):
                                # 鸣潮/终末地：脚本日志与 API 便笺数据**互补**，合并而非替换。
                                # API 行（daily_checker）带体力/活跃度等便笺数据，
                                # 日志行带脚本执行进度；同名任务以日志为准，日志任务前置。
                                existing_tasks = list(game.get("tasks") or [])
                                log_tasks = list(data.get("tasks") or [])
                                log_keys = {(t.get("task_key") or t.get("task_name")) for t in log_tasks}
                                sign_tasks = [
                                    t for t in existing_tasks
                                    if (t.get("task_key") or "").startswith("sign_")
                                ]
                                kept_api_tasks = [
                                    t for t in existing_tasks
                                    if (t.get("task_key") or t.get("task_name")) not in log_keys
                                    and not (t.get("task_key") or "").startswith("sign_")
                                ]
                                game["tasks"] = sign_tasks + log_tasks + kept_api_tasks
                                game["is_all_success"] = data.get(
                                    "is_all_success", game.get("is_all_success"))
                                game["has_failed"] = bool(data.get("has_failed")) or bool(game.get("has_failed"))
                                if data.get("errors"):
                                    game["errors"] = (game.get("errors") or []) + list(data.get("errors"))
                                game["nickname"] = data.get("nickname") or game.get("nickname", "")

                                # 完成度必须反映真实游玩进度，不能用脚本子任务比例代替：
                                # ok-ww 的 progress_percent 是"脚本跑了几步"（全跑完就 100%），
                                # 与活跃度 90/100 无关。这里优先取 API 便笺的关键计数。
                                real_pct, real_src = self._real_completion_percent(
                                    game_key, sign_tasks + log_tasks + kept_api_tasks)
                                if real_pct is not None:
                                    game["progress_percent"] = real_pct
                                    game["progress_source"] = real_src
                                else:
                                    game["progress_percent"] = data.get(
                                        "progress_percent", game.get("progress_percent"))
                                    game["progress_source"] = "script"
                                logger.info(
                                    "[Bridge] %s：合并脚本日志进度(%d 项) + API 便笺数据(%d 项)，成功=%s 完成度=%s%%(%s)",
                                    game_key, len(log_tasks), len(kept_api_tasks),
                                    game["is_all_success"],
                                    game["progress_percent"], game.get("progress_source"))
                            else:
                                # 保留 tracker 里已有的签到任务（sign_ 前缀），
                                # 避免被外部日志整体替换后丢失签到状态。
                                existing_sign_tasks = [
                                    t for t in (game.get("tasks") or [])
                                    if (t.get("task_key") or "").startswith("sign_")
                                ]
                                if existing_sign_tasks:
                                    data_tasks = list(data.get("tasks") or [])
                                    data["tasks"] = existing_sign_tasks + data_tasks
                                games_data[g_idx] = data
                                logger.info("[Bridge] 已用 %s 日志替换 %s 进度: success=%s sign_tasks=%d",
                                            tool_name, game_key, data.get("is_all_success"), len(existing_sign_tasks))
                        replaced = True
                        break
                if not replaced:
                    games_data.append(data)
                    logger.info("[Bridge] 已注入 %s %s 进度: success=%s",
                                tool_name, game_key, data.get("is_all_success"))
            except Exception as exc:
                logger.warning("[Bridge] 读取 %s 日志失败: %s", tool_name, exc)

    def _ensure_baijing_placeholder(self, games_data: List[Dict[str, Any]]):
        """确保白荆回廊卡片始终可见：无记录时注入待执行占位，避免每日进度里看不到。"""
        try:
            from ..settings import resolve_game_key
            has = any(resolve_game_key(g.get("game_key", "")) == "baijing" for g in games_data)
            if has:
                return
            games_data.append({
                "game_key": "baijing",
                "game_name": "白荆回廊",
                "uid": "",
                "nickname": "",
                "is_all_success": False,
                "has_failed": False,
                "progress_percent": 0,
                "tasks": [
                    {"task_key": "enter_game", "task_name": "进入游戏", "status": "pending", "message": "待一条龙执行", "current": None, "total": None},
                    {"task_key": "daily", "task_name": "日常任务", "status": "pending", "message": "待执行", "current": None, "total": None},
                    {"task_key": "verify", "task_name": "验证", "status": "pending", "message": "待验证截图", "current": None, "total": None, "verify_image": None, "verify_image_url": None, "extra": {}},
                    {"task_key": "weekly", "task_name": "每周商店购买", "status": "pending", "message": "待周常（每周一次）", "current": None, "total": None},
                    {"task_key": "monthly", "task_name": "每月有猫兑换", "status": "pending", "message": "待月度（每月一次，购买清单待定）", "current": None, "total": None},
                ],
                "errors": [],
            })
            logger.info("[Bridge] 已注入白荆回廊占位进度（今日无记录）")
        except Exception as exc:
            logger.debug("[Bridge] 注入白荆占位失败: %s", exc)

    def _load_gacha_accounts_backend(self, game_code: str):
        """从 SQLite 加载指定游戏的抽卡账号列表。"""
        try:
            db = self._get_preference_db()
            accounts = db.get_gacha_accounts(game_code)
            accounts_data = []
            for acc in accounts:
                accounts_data.append({
                    "uid": acc.get("uid", ""),
                    "imported_at": acc.get("imported_at", ""),
                    "import_count": acc.get("import_count", 0),
                    "total_records": acc.get("total_records", 0),
                })
            self.emit_event("gacha_accounts", accounts=accounts_data)
            logger.info("[Bridge] 加载抽卡账号: game=%s count=%d", game_code, len(accounts_data))
        except Exception as exc:
            logger.exception("[Bridge] 加载抽卡账号失败")
            self.emit_event("toast", message=f"加载失败: {exc}", color="#b04b44")

    def _load_gacha_analysis_backend(self, game_code: str, uid: str):
        """从 SQLite 加载抽卡记录并生成分析数据。"""
        try:
            if not uid:
                self.emit_event("gacha_analysis", overview={}, pools=[], history=[], pity={})
                return

            db = self._get_preference_db()
            records = db.get_gacha_records(game_code, uid)
            if not records:
                self.emit_event("gacha_analysis", overview={}, pools=[], history=[], pity={})
                self.emit_event("toast", message="该 UID 暂无抽卡记录", color="#b66f1f")
                return

            latest = db.get_latest_gacha_import(game_code, uid)
            accounts = db.get_gacha_accounts(game_code)
            import_count = len(accounts)

            from ..gacha.hoyo_analysis import (
                build_overview,
                build_pool_analysis,
                build_high_rarity_history,
                build_pity_summary,
            )
            from ..gacha.luck_analysis import build_hoyo_luck

            raw_overview = build_overview(records, game_code, latest, import_count)
            raw_pools = build_pool_analysis(records, game_code)
            history = build_high_rarity_history(records, game_code)
            raw_pity = build_pity_summary(records, game_code)
            luck = build_hoyo_luck(records, game_code)

            # 转换 overview → 扁平化字段供 QML 使用
            total = raw_overview.get("total_records", 0)
            high_count = raw_overview.get("high_rarity_count", 0)
            overview = {
                "total_pulls": total,
                "high_rarity_count": high_count,
                "high_rarity_rate": f"{high_count / total * 100:.1f}%" if total > 0 else "0%",
                "latest_import": raw_overview.get("latest_imported_at", ""),
                "import_count": raw_overview.get("import_count", 0),
                "earliest_time": raw_overview.get("earliest_time", ""),
                "latest_time": raw_overview.get("latest_time", ""),
            }

            # 转换 pools dict → list
            pools = []
            for pool_name, pool_data in raw_pools.items():
                pool_total = pool_data.get("total", 0)
                pool_high = pool_data.get("high_rarity_count", 0)
                pools.append({
                    "pool_name": pool_name,
                    "total_pulls": pool_total,
                    "high_rarity_count": pool_high,
                    "high_rarity_rate": pool_data.get("high_rarity_pct", "0%"),
                    "current_pity": pool_data.get("current_pity", 0),
                    "high_rarity_history": pool_data.get("high_rarity_list", []),
                })

            # 转换 pity list → dict
            pity = {}
            if isinstance(raw_pity, list):
                for item in raw_pity:
                    group_name = item.get("group_name", "")
                    pity[group_name] = {
                        "avg_pity": item.get("avg_pity", 0),
                        "min_pity": item.get("min_pity", 0),
                        "max_pity": item.get("max_pity", 0),
                        "current_pity": item.get("current_pity", 0),
                    }
            elif isinstance(raw_pity, dict):
                pity = raw_pity

            # 提取全局保底摘要
            all_pities = []
            for v in pity.values():
                if isinstance(v, dict) and v.get("avg_pity"):
                    all_pities.append(v)
            pity_summary = {
                "avg_pity": int(sum(v["avg_pity"] for v in all_pities) / len(all_pities)) if all_pities else 0,
                "min_pity": min((v["min_pity"] for v in all_pities), default=0),
                "max_pity": max((v["max_pity"] for v in all_pities), default=0),
                "current_pity": max((v["current_pity"] for v in all_pities), default=0),
            }

            self.emit_event(
                "gacha_analysis",
                overview=overview,
                pools=pools,
                history=history,
                pity=pity_summary,
                luck=luck,
            )
            logger.info("[Bridge] 抽卡分析完成: game=%s uid=%s records=%d pools=%d",
                        game_code, uid, len(records), len(pools))
        except Exception as exc:
            logger.exception("[Bridge] 加载抽卡分析失败")
            self.emit_event("toast", message=f"分析失败: {exc}", color="#b04b44")

    def _import_gacha_backend(self, file_path: str):
        """后台导入 UIGF JSON 文件。"""
        try:
            self.emit_event("status", title="正在导入", body="正在解析 UIGF 文件...", tone=TONE_RUNNING)
            from ..gacha.hoyo_importer import import_uigf
            from ..settings.paths import WORKFLOW_DB_PATH
            summaries = import_uigf(file_path, str(WORKFLOW_DB_PATH))

            total_count = sum(s.get("total_records", 0) for s in summaries)
            game_code = summaries[0].get("game_code", "hk4e") if summaries else "hk4e"

            self.emit_event("status", title="导入完成",
                            body=f"已导入 {total_count} 条记录",
                            tone=TONE_SUCCESS)
            self.emit_event("toast", message=f"导入完成: {total_count} 条", color="#2f7d4a")
            # 自动刷新账号列表
            self._load_gacha_accounts_backend(game_code)
        except Exception as exc:
            logger.exception("[Bridge] 导入抽卡文件失败")
            self.emit_event("status", title="导入失败", body=str(exc), tone=TONE_DANGER)
            self.emit_event("toast", message=f"导入失败: {exc}", color="#b04b44")

    def _import_hoyogacha_db_backend(self, db_path: str, game_label: str):
        """后台从 HoYo.Gacha 数据库导入（只读拷临时文件，不锁原库）。"""
        try:
            self.emit_event("status", title="正在导入", body="正在读取 HoYo.Gacha 数据库...", tone=TONE_RUNNING)
            from ..gacha.hoyo_importer import import_hoyogacha_db
            from ..settings.paths import WORKFLOW_DB_PATH
            summaries = import_hoyogacha_db(db_path, str(WORKFLOW_DB_PATH))

            total_count = sum(s.get("total_records", 0) for s in summaries)
            codes = {s.get("game_code", "") for s in summaries}
            want = self._GACHA_GAME_MAP.get((game_label or "").strip(), "")
            refresh_code = want if want in codes else (summaries[0].get("game_code", "hk4e") if summaries else "hk4e")

            self.emit_event("status", title="导入完成",
                            body=f"HoYo.Gacha 已导入 {total_count} 条记录",
                            tone=TONE_SUCCESS)
            self.emit_event("toast", message=f"导入完成: {total_count} 条", color="#2f7d4a")
            # 只刷新当前选中游戏的账号列表（切游戏下拉会自动加载其它游戏）
            self._load_gacha_accounts_backend(refresh_code)
        except Exception as exc:
            logger.exception("[Bridge] HoYo.Gacha 数据库导入失败")
            self.emit_event("status", title="导入失败", body=str(exc), tone=TONE_DANGER)
            self.emit_event("toast", message=f"导入失败: {exc}", color="#b04b44")

    def _load_nikki_gacha_backend(self):
        """从数据库加载无限暖暖抽卡数据并分析。"""
        try:
            db = self._get_preference_db()
            accounts = db.get_gacha_accounts("nikki")
            if not accounts:
                self.emit_event("nikki_gacha", overview={}, pools=[])
                self.emit_event("toast", message="暂无无限暖暖抽卡记录", color="#b66f1f")
                return

            uid = accounts[0].get("uid", "")
            db_rows = db.get_gacha_records("nikki", uid)
            if not db_rows:
                self.emit_event("nikki_gacha", overview={}, pools=[])
                return

            from ..gacha.nikki_resonance_analysis import (
                db_to_nikki_document,
                build_overview as build_nikki_overview,
                build_pool_summary as build_nikki_pool_summary,
                build_pearpal_overview as build_nikki_pearpal_overview,
                build_pearpal_pool_summary as build_nikki_pearpal_pool_summary,
            )
            from ..gacha.nikki_resonance_fetch import GACHA_POOLS as NIKKI_GACHA_POOLS

            document = db_to_nikki_document(db_rows)
            records_by_pool = document.get("records_by_pool", {})
            pearpal_by_pool = document.get("pearpal_by_pool", {})

            has_raw = any(bool(rows) for rows in records_by_pool.values())
            has_pearpal = any(bool(rows) for rows in pearpal_by_pool.values())

            pools = []
            if has_raw:
                raw_overview = build_nikki_overview(records_by_pool)
                overview = {
                    "total_records": raw_overview.get("total_records", 0),
                    "total_pulls": raw_overview.get("total_records", 0),
                    "pool_count": raw_overview.get("pool_count", 0),
                    "earliest_time": raw_overview.get("earliest_time", ""),
                    "latest_time": raw_overview.get("latest_time", ""),
                    "mode": "raw",
                }
                for pool_id, pool_name in NIKKI_GACHA_POOLS.items():
                    rows = records_by_pool.get(pool_id, [])
                    if not rows:
                        continue
                    summary = build_nikki_pool_summary(pool_id, rows)
                    pools.append({
                        "pool_id": pool_id,
                        "pool_name": pool_name,
                        "total_pulls": summary.get("total_records", 0),
                        "total_records": summary.get("total_records", 0),
                        "unique_item_ids": summary.get("unique_item_ids", 0),
                        "earliest_time": summary.get("earliest_time", ""),
                        "latest_time": summary.get("latest_time", ""),
                        "top_items": summary.get("top_items", []),
                        "mode": "raw",
                    })
            elif has_pearpal:
                pp_overview = build_nikki_pearpal_overview(pearpal_by_pool)
                overview = {
                    "total_records": pp_overview.get("total_events", 0),
                    "total_pulls": pp_overview.get("total_pulls", 0),
                    "pool_count": pp_overview.get("pool_count", 0),
                    "four_count": pp_overview.get("four_count", 0),
                    "five_count": pp_overview.get("five_count", 0),
                    "earliest_time": "",
                    "latest_time": "",
                    "mode": "pearpal",
                }

                def _aggregate_outfits(five_hist: List[Dict]) -> List[Dict]:
                    """把 5★ 出货历史按套装聚合：套装名 → 已获得部件（含部位）。

                    PearPal 的五星都是套装部件（outfit_name=套装、item_type_label=部位），
                    玩家关心的是"集齐了哪个套装的哪几件"，不是一串单件名。
                    """
                    groups: Dict[str, Dict[str, Any]] = {}
                    order: List[str] = []
                    for r in five_hist:
                        outfit = (r.get("outfit_name") or "").strip() or "未知套装"
                        if outfit not in groups:
                            groups[outfit] = {"outfit_name": outfit, "parts": [], "min_pity": None}
                            order.append(outfit)
                        groups[outfit]["parts"].append({
                            "name": r.get("name") or "",
                            "part_type": (r.get("part_type") or "").strip(),
                            "pity": r.get("pity", 0),
                            "draw_index": r.get("draw_index", 0),
                        })
                        p = r.get("pity", 0)
                        if isinstance(p, int) and p > 0:
                            cur = groups[outfit]["min_pity"]
                            groups[outfit]["min_pity"] = p if cur is None else min(cur, p)
                    return [groups[k] for k in order]

                # 全局套装汇总（跨池）：先收集全部 5★ 再聚合
                all_five_hist: List[Dict] = []
                for pool_id, pool_name in NIKKI_GACHA_POOLS.items():
                    rows = pearpal_by_pool.get(pool_id, [])
                    if not rows:
                        continue
                    summary = build_nikki_pearpal_pool_summary(pool_id, rows)
                    # 完整 5★ 出货历史（无30条限制；recent_records 只保留30条会漏）
                    five_star_history = [
                        {
                            "name": r.get("display_name_with_type")
                            or r.get("display_name")
                            or r.get("item_name")
                            or ("ID " + str(r.get("item_id", ""))),
                            "pity": r.get("five_gap", 0),
                            "draw_index": r.get("draw_index", 0),
                            "time": "",
                            "pool_name": pool_name,
                            # 套装聚合用（PearPal 数据自带套装归属）
                            "outfit_name": (r.get("outfit_name") or "").strip(),
                            "part_type": (r.get("item_type_label") or r.get("item_type") or "").strip(),
                        }
                        for r in summary.get("five_star_history", [])
                    ]
                    all_five_hist.extend(five_star_history)
                    pools.append({
                        "pool_id": pool_id,
                        "pool_name": pool_name,
                        "total_pulls": summary.get("estimated_total_pulls", 0),
                        "total_records": summary.get("event_count", 0),
                        "unique_item_ids": summary.get("unique_item_ids", 0),
                        "four_count": summary.get("four_count", 0),
                        "five_count": summary.get("five_count", 0),
                        "current_four_pity": summary.get("current_four_pity", 0),
                        "current_five_pity": summary.get("current_five_pity", 0),
                        "average_four_gap": summary.get("average_four_gap", 0),
                        "average_five_gap": summary.get("average_five_gap", 0),
                        "top_items": summary.get("top_items", []),
                        "high_rarity_history": five_star_history,
                        "outfit_summary": _aggregate_outfits(five_star_history),
                        "mode": "pearpal",
                    })
                overview["outfit_summary"] = _aggregate_outfits(all_five_hist)
            else:
                overview = {}

            self.emit_event("nikki_gacha", overview=overview, pools=pools, uid=uid)
            logger.info("[Bridge] 暖暖抽卡分析完成: uid=%s pools=%d", uid, len(pools))
        except Exception as exc:
            logger.exception("[Bridge] 加载暖暖抽卡失败")
            self.emit_event("toast", message=f"加载失败: {exc}", color="#b04b44")

    def _load_wuwa_daily_backend(self):
        """拉取鸣潮库街区每日数据（体力/活跃度/周本/深塔等）供 QML 展示。"""
        try:
            from ..services import credential_store
            from ..clients import wuwa_kujiequ

            profile = credential_store.load_wuwa_kujiequ_profile()
            if profile is None:
                self.emit_event(
                    "wuwa_daily",
                    ok=False,
                    message="库街区 Token 或 DID 未配置，请先 ADB 抓取或手动填写",
                )
                return

            logger.info("[Bridge] 拉取库街区每日数据: role=%s", profile.role_name)
            daily = wuwa_kujiequ.fetch_daily_data(profile)
            try:
                base = wuwa_kujiequ.fetch_base_data(profile)
            except Exception as exc:  # 基础数据失败不影响主数据展示
                logger.warning("[Bridge] 拉取库街区基础数据失败，忽略: %s", exc)
                base = {}

            def _fmt(detail, label):
                if not isinstance(detail, dict):
                    return None
                cur = detail.get("cur")
                total = detail.get("total")
                if cur is None and total is None:
                    return None
                return {
                    "name": detail.get("name") or label,
                    "cur": cur if cur is not None else 0,
                    "total": total if total is not None else 0,
                }

            items = []
            for key, label in (
                ("energy", "结晶波片"),
                ("liveness", "活跃度"),
                ("weekly", "战歌重奏"),
                ("weekly_rouge", "千道门扉的异想"),
                ("store_energy", "结晶单质"),
                ("tower", "逆境深塔"),
                ("slash_tower", "冥歌海墟"),
            ):
                item = _fmt(daily.get(key), label)
                if item:
                    items.append(item)

            payload = {
                "ok": True,
                "role_name": daily.get("role_name") or profile.role_name or "",
                "server_name": daily.get("server_name") or "",
                "has_signed": bool(daily.get("has_signed")),
                "sign_text": daily.get("sign_text") or "",
                "items": items,
                "active_days": (base or {}).get("active_days", 0),
                # 官方限制：活跃度需进入游戏后才会刷新，未刷新时为前一日数据
                "stale_hint": "活跃度需进入游戏后才会更新，未更新时展示前一日数据",
                "cached_at": time.time(),
            }
            self.emit_event("wuwa_daily", **payload)
            # 仅成功结果落地缓存；失败保留上次好缓存
            self._save_wuwa_daily_cache(payload)
        except Exception as exc:
            logger.exception("[Bridge] 拉取库街区每日数据失败")
            self.emit_event("wuwa_daily", ok=False, message=str(exc))

    def _load_wuwa_gacha_backend(self):
        """从数据库加载鸣潮抽卡数据并分析。"""
        try:
            db = self._get_preference_db()
            accounts = db.get_gacha_accounts("wuwa")
            if not accounts:
                self.emit_event("wuwa_gacha", overview={}, pools=[])
                self.emit_event("toast", message="暂无鸣潮抽卡记录", color="#b66f1f")
                return

            uid = accounts[0].get("uid", "")
            db_rows = db.get_gacha_records("wuwa", uid)
            if not db_rows:
                self.emit_event("wuwa_gacha", overview={}, pools=[])
                return

            from ..gacha.wuwa_analysis import (
                db_to_wuwa_records,
                build_wuwa_overview,
                build_wuwa_pool_analysis,
                build_wuwa_five_star_history,
            )
            from ..gacha.luck_analysis import build_wuwa_luck

            records = db_to_wuwa_records(db_rows)
            overview = build_wuwa_overview(records)
            raw_pools = build_wuwa_pool_analysis(records)
            history = build_wuwa_five_star_history(records)
            luck = build_wuwa_luck(records)

            # 转换 pools dict → list
            pools = []
            for pool_name, pool_data in raw_pools.items():
                pools.append({
                    "pool_name": pool_name,
                    "total_pulls": pool_data.get("total", 0),
                    "five_star_count": pool_data.get("five_star_count", 0),
                    "five_star_rate": pool_data.get("five_star_pct", "0%"),
                    "high_rarity_history": pool_data.get("five_star_list", []),
                })

            self.emit_event("wuwa_gacha", overview=overview, pools=pools, history=history, uid=uid, luck=luck)
            logger.info("[Bridge] 鸣潮抽卡分析完成: uid=%s pools=%d", uid, len(pools))
        except Exception as exc:
            logger.exception("[Bridge] 加载鸣潮抽卡失败")
            self.emit_event("toast", message=f"加载失败: {exc}", color="#b04b44")

    def _import_wuwa_gacha_from_json_backend(self, file_paths_json: str):
        """从 WutheringWavesTool 的 pool.json 导入抽卡记录到数据库。

        支持多文件合并去重。导入前清空该 uid 的旧 wuwa 记录，避免不同
        record_id 格式造成重复。
        """
        try:
            # 解析 file_paths_json → list[str]
            file_paths: List[str] = []
            if file_paths_json:
                try:
                    parsed = json.loads(file_paths_json)
                    if isinstance(parsed, list):
                        file_paths = [str(p) for p in parsed if str(p).strip()]
                    elif isinstance(parsed, str):
                        file_paths = [parsed] if parsed.strip() else []
                    else:
                        file_paths = []
                except (json.JSONDecodeError, TypeError):
                    # 当作单路径字符串
                    file_paths = [file_paths_json] if file_paths_json.strip() else []

            if not file_paths:
                self.emit_event("toast", message="未选择文件", color="#b04b44")
                return

            # 检查文件存在性
            missing = [p for p in file_paths if not os.path.exists(p)]
            if missing:
                self.emit_event(
                    "toast",
                    message=f"文件不存在: {missing[0]}",
                    color="#b04b44",
                )
                return

            # 从配置取 uid（优先 role_id，没有则用 user_id）
            from ..settings import manager as cfg_manager
            cfg = cfg_manager.load()
            profile = (cfg.get("wuwa_kujiequ") or {}).get("profile") or {}
            uid = str(profile.get("role_id") or profile.get("user_id") or "")
            if not uid:
                self.emit_event(
                    "toast",
                    message="未找到鸣潮角色 ID，请先在库街区凭据里登录或 ADB 抓取",
                    color="#b04b44",
                )
                return

            self.emit_event(
                "toast",
                message=f"开始解析 {len(file_paths)} 个 pool.json...",
                color="#295d86",
            )

            from ..gacha.wuwa_analysis import (
                load_wuwa_pool_json,
                wuwa_pool_json_to_db_records,
            )

            records = load_wuwa_pool_json(file_paths)
            if not records:
                self.emit_event(
                    "toast", message="pool.json 中未找到有效记录", color="#b66f1f"
                )
                return

            db_rows = wuwa_pool_json_to_db_records(records, uid)

            db = self._get_preference_db()
            # 清旧记录，避免业务键 record_id 与旧 xlsx 导入（用 index 后缀）重复
            deleted = db.delete_gacha_records_by_uid("wuwa", uid)
            logger.info(
                "[Bridge] 清空旧 wuwa 抽卡记录: uid=%s 删除 %d 条", uid, deleted,
            )

            source_paths = "|".join(os.path.basename(p) for p in file_paths)
            import_id = db.start_gacha_import({
                "game_code": "wuwa",
                "uid": uid,
                "source_path": source_paths,
                "source_hash": "",
                "uigf_version": "",
                "export_app": "WutheringWavesTool",
                "export_app_version": "",
                "export_timestamp": "",
                "lang": "zh-cn",
                "timezone": "UTC+8",
                "total_records": len(db_rows),
                "notes": "pool.json 合并去重导入",
            })
            stats = db.upsert_gacha_records("wuwa", uid, import_id, db_rows)

            self.emit_event(
                "toast",
                message=(
                    f"鸣潮抽卡导入完成：共 {stats['total_count']} 条 "
                    f"(新增 {stats['inserted_count']}, 更新 {stats['updated_count']})"
                ),
                color="#2f7d4a",
            )
            logger.info(
                "[Bridge] 鸣潮抽卡导入完成: uid=%s total=%d inserted=%d updated=%d",
                uid, stats["total_count"], stats["inserted_count"],
                stats["updated_count"],
            )

            # 触发加载刷新 UI
            self._load_wuwa_gacha_backend()
        except Exception as exc:
            logger.exception("[Bridge] 鸣潮抽卡 pool.json 导入失败")
            self.emit_event(
                "toast",
                message=f"导入失败: {exc}",
                color="#b04b44",
            )

    def _load_endfield_gacha_backend(self, file_path: str):
        """从文件加载终末地抽卡数据并分析，同时写入数据库。"""
        try:
            from ..gacha.endfield_analysis import (
                load_all_records as load_endfield_records,
                build_overview as build_endfield_overview,
                build_six_star_history,
                build_pool_analysis as build_endfield_pool_analysis,
                endfield_to_db_records,
                extract_uid_from_filename,
            )

            records = load_endfield_records(file_path)
            if not records:
                self.emit_event("toast", message="文件中无抽卡记录", color="#b66f1f")
                return

            # 写入数据库
            from ..settings.paths import WORKFLOW_DB_PATH
            db = self._get_preference_db()
            # 从文件名 `<uid>_<roleId>.json` 提取真实终末地 UID，
            # 历史遗留的 uid="" 写法会让多账号场景下不同来源的数据无法合并。
            uid = extract_uid_from_filename(file_path)
            # 若本次导入有真实 uid，先把 DB 中遗留的旧 uid（含空字符串）迁移到该 uid
            if uid:
                self._migrate_endfield_uids_to(db, uid)
            db_records = endfield_to_db_records(records, uid)
            import_id = db.start_gacha_import({
                "game_code": "endfield",
                "uid": uid,
                "source_path": file_path,
                "source_hash": "",
                "uigf_version": "",
                "export_app": "",
                "export_app_version": "",
                "export_timestamp": "",
                "lang": "zh-cn",
                "timezone": "8",
                "total_records": len(records),
                "notes": "Endfield 文件导入",
            })
            db.upsert_gacha_records("endfield", uid, import_id, db_records)
            logger.info(
                "[Bridge] 终末地抽卡数据已写入数据库: uid=%s records=%d 条",
                uid or "<空>", len(records),
            )

            overview = build_endfield_overview(records)
            raw_pools = build_endfield_pool_analysis(records)
            history = build_six_star_history(records)

            # 转换 pools dict → list
            pools = []
            for pool_name, pool_data in raw_pools.items():
                pools.append({
                    "pool_name": pool_name,
                    "total_pulls": pool_data.get("total", 0),
                    "six_star_count": pool_data.get("six_star_count", 0),
                    "six_star_rate": f"{pool_data.get('six_star_pct', 0):.1f}%",
                    "high_rarity_history": pool_data.get("six_star_list", []),
                })

            self.emit_event("endfield_gacha", overview=overview, pools=pools, history=history)
            self.emit_event("toast", message=f"导入完成: {len(records)} 条", color="#2f7d4a")
            logger.info("[Bridge] 终末地抽卡分析完成: records=%d pools=%d", len(records), len(pools))
        except Exception as exc:
            logger.exception("[Bridge] 加载终末地抽卡失败")
            self.emit_event("toast", message=f"分析失败: {exc}", color="#b04b44")

    def _migrate_endfield_uids_to(self, db, uid: str) -> None:
        """把 DB 里 endfield 所有旧 uid 的记录迁移到本次真实 uid。

        用于：
        - 文件导入路径（从文件名提取的真实 uid）
        - token 同步路径（用户填写的真实 uid）

        迁移会把旧 record_id（如 `endfield__<ts>_<seq>`）改写成
        `endfield_<uid>_<ts>_<seq>`，使后续 upsert 能正确合并去重。
        """
        if not uid:
            return
        try:
            accounts = db.get_gacha_accounts("endfield")
        except Exception:
            accounts = []
        for acct in accounts:
            old_uid = str(acct.get("uid", "") or "")
            if old_uid == uid:
                continue
            try:
                migrated = db.migrate_gacha_uid(
                    "endfield",
                    old_uid=old_uid,
                    new_uid=uid,
                    record_id_prefix="endfield_",
                )
                if migrated > 0:
                    logger.info(
                        "[Bridge] 终末地 uid 迁移: %r → %r (%d rows)",
                        old_uid, uid, migrated,
                    )
            except Exception:
                logger.exception(
                    "[Bridge] 终末地 uid 迁移失败: %r → %r", old_uid, uid,
                )

    def _load_endfield_db_backend(self):
        """从数据库加载终末地抽卡数据。无数据则不报错，等用户手动导入文件。"""
        try:
            db = self._get_preference_db()
            accounts = db.get_gacha_accounts("endfield")
            if not accounts:
                logger.debug("[Bridge] 数据库中无终末地抽卡记录，等待用户导入文件")
                return

            uid = accounts[0].get("uid", "")
            db_rows = db.get_gacha_records("endfield", uid)
            if not db_rows:
                logger.debug("[Bridge] 数据库中无终末地抽卡记录 (uid=%s)", uid)
                return

            from ..gacha.endfield_analysis import (
                db_to_endfield_records,
                build_overview as build_endfield_overview,
                build_six_star_history,
                build_pool_analysis as build_endfield_pool_analysis,
            )

            records = db_to_endfield_records(db_rows)
            if not records:
                logger.debug("[Bridge] 终末地 raw_json 解析失败，跳过")
                return

            overview = build_endfield_overview(records)
            raw_pools = build_endfield_pool_analysis(records)
            history = build_six_star_history(records)

            pools = []
            for pool_name, pool_data in raw_pools.items():
                pools.append({
                    "pool_name": pool_name,
                    "total_pulls": pool_data.get("total", 0),
                    "six_star_count": pool_data.get("six_star_count", 0),
                    "six_star_rate": f"{pool_data.get('six_star_pct', 0):.1f}%",
                    "high_rarity_history": pool_data.get("six_star_list", []),
                })

            self.emit_event("endfield_gacha", overview=overview, pools=pools, history=history)
            logger.info("[Bridge] 终末地抽卡分析完成(DB): records=%d pools=%d", len(records), len(pools))
        except Exception as exc:
            logger.exception("[Bridge] 从数据库加载终末地抽卡失败")
            self.emit_event("toast", message=f"加载失败: {exc}", color="#b04b44")

    def _fetch_endfield_gacha_by_token_backend(self):
        """用 user 填写的 token 直接拉取终末地抽卡记录并入库。

        流程：app_config.json[endfield_gacha] → clients.endfield_gacha.fetch_gacha_records
        → gacha.endfield_analysis.load_records_from_dict → DB upsert → emit endfield_gacha。

        凭据安全：token 仅在内存中传递给客户端，日志只输出脱敏长度。
        """
        try:
            from ..settings import manager as cfg_manager
            from ..clients import endfield_gacha
            from ..gacha.endfield_analysis import (
                load_records_from_dict,
                build_overview as build_endfield_overview,
                build_six_star_history,
                build_pool_analysis as build_endfield_pool_analysis,
                endfield_to_db_records,
            )

            cfg = cfg_manager.load(force=True)
            gacha_cfg = cfg.get("endfield_gacha") or {}
            token = str(gacha_cfg.get("token") or "").strip()
            uid = str(gacha_cfg.get("uid") or "").strip()
            provider = str(gacha_cfg.get("provider") or "hypergryph").strip()
            server_id = str(gacha_cfg.get("server_id") or "1").strip() or "1"

            if not token:
                self.emit_event(
                    "toast",
                    message="终末地抽卡 Token 为空，请先填写",
                    color="#b04b44",
                )
                return
            if not uid:
                self.emit_event(
                    "toast",
                    message="终末地 UID 为空，请先填写",
                    color="#b04b44",
                )
                return

            logger.info(
                "[Bridge] 终末地抽卡 token 同步开始: provider=%s uid=%s token_len=%d",
                provider, uid, len(token),
            )
            self.emit_event(
                "toast",
                message="正在通过 token 同步终末地抽卡记录...",
                color="#295d86",
            )

            api_data = endfield_gacha.fetch_gacha_records(
                token=token, uid=uid, provider=provider, server_id=server_id,
            )
            stats = endfield_gacha.summarize_records(api_data)
            logger.info(
                "[Bridge] 终末地 API 抓取完成: char=%d weapon=%d total=%d",
                stats["character_records"], stats["weapon_records"],
                stats["total_records"],
            )

            if stats["total_records"] == 0:
                self.emit_event(
                    "toast",
                    message="未抓到任何抽卡记录（可能 token 过期或无数据）",
                    color="#b66f1f",
                )
                return

            records = load_records_from_dict(api_data)
            db_records = endfield_to_db_records(records, uid)

            db = self._get_preference_db()
            # 先把 DB 里 endfield 旧 uid（含历史遗留的 uid=""）迁移到本次的真实 uid，
            # 这样旧记录的 record_id 也会被改写成 endfield_<uid>_<ts>_<seq> 形式，
            # 后续 upsert 能与本次新拉取的记录正确合并去重。
            self._migrate_endfield_uids_to(db, uid)
            import_id = db.start_gacha_import({
                "game_code": "endfield",
                "uid": uid,
                "source_path": f"token_sync:{provider}",
                "source_hash": "",
                "uigf_version": "",
                "export_app": "endfield-gacha (token sync)",
                "export_app_version": "",
                "export_timestamp": "",
                "lang": "zh-cn",
                "timezone": "8",
                "total_records": len(records),
                "notes": f"Token sync provider={provider} server_id={server_id}",
            })
            upsert_stats = db.upsert_gacha_records("endfield", uid, import_id, db_records)
            logger.info(
                "[Bridge] 终末地抽卡已写入数据库: uid=%s total=%d inserted=%d updated=%d",
                uid, upsert_stats["total_count"], upsert_stats["inserted_count"],
                upsert_stats["updated_count"],
            )

            overview = build_endfield_overview(records)
            raw_pools = build_endfield_pool_analysis(records)
            history = build_six_star_history(records)
            pools = []
            for pool_name, pool_data in raw_pools.items():
                pools.append({
                    "pool_name": pool_name,
                    "total_pulls": pool_data.get("total", 0),
                    "six_star_count": pool_data.get("six_star_count", 0),
                    "six_star_rate": f"{pool_data.get('six_star_pct', 0):.1f}%",
                    "high_rarity_history": pool_data.get("six_star_list", []),
                })
            self.emit_event("endfield_gacha", overview=overview, pools=pools, history=history)
            self.emit_event(
                "toast",
                message=(
                    f"终末地抽卡同步完成：共 {stats['total_records']} 条 "
                    f"(角色 {stats['character_records']} / 武器 {stats['weapon_records']})"
                ),
                color="#2f7d4a",
            )
        except Exception as exc:
            # EndfieldGachaError 与其他异常都走这里，避免 NameError（import 失败时
            # except 子句引用未定义的名字会再次抛错）
            from ..clients.endfield_gacha import EndfieldGachaError
            if isinstance(exc, EndfieldGachaError):
                logger.warning("[Bridge] 终末地抽卡 token 同步失败: %s", exc)
            else:
                logger.exception("[Bridge] 终末地抽卡 token 同步异常")
            self.emit_event(
                "toast",
                message=f"同步失败: {exc}",
                color="#b04b44",
            )

    # ═══════════════════════════════════════════════════════════════
    #  Credentials backend (app_config.json)
    # ═══════════════════════════════════════════════════════════════

    # 顶层凭据键 → 应返回给 QML 的子键列表；空列表表示整块返回
    _CREDENTIAL_KEYS = (
        "mihoyo_sign",
        "wuwa_kujiequ",
        "tajiduo_sign",
        "skendfield_sign",
        "endfield_protocol",
        "endfield_gacha",
        "nikki",
    )

    def _load_credentials_backend(self):
        """读取所有凭据并推送到 QML。

        库街区 token/did 存数据库（DPAPI 加密），这里不回灌明文，
        只补 `profile.token_configured` / `did_configured` 布尔标志供界面提示。
        """
        try:
            from ..settings import manager as cfg_manager
            from ..services import credential_store

            cfg = cfg_manager.load(force=True)
            credentials: Dict[str, Any] = {}
            for top_key in self._CREDENTIAL_KEYS:
                section = cfg.get(top_key)
                credentials[top_key] = dict(section) if isinstance(section, dict) else {}

            # 库街区：剥离密钥字段（配置里可能残留迁移前的旧值），换成布尔标志
            wuwa = credentials.get(credential_store.WUWA_CATEGORY) or {}
            profile = dict(wuwa.get("profile") or {})
            for secret_key in credential_store.WUWA_SECRET_KEYS:
                profile.pop(secret_key, None)
            profile["token_configured"] = credential_store.has_secret(
                credential_store.WUWA_CATEGORY, "token",
            )
            profile["did_configured"] = credential_store.has_secret(
                credential_store.WUWA_CATEGORY, "did",
            )
            wuwa["profile"] = profile
            credentials[credential_store.WUWA_CATEGORY] = wuwa

            self.emit_event("credentials", credentials=credentials)
            logger.info(
                "[Bridge] 凭据已加载: %s",
                ", ".join(k for k, v in credentials.items() if v),
            )
        except Exception as exc:
            logger.exception("[Bridge] 加载凭据失败")
            self.emit_event("toast", message=f"加载凭据失败: {exc}", color="#b04b44")

    def _save_credential_backend(self, category: str, key: str, value: Any):
        """写回单个凭据。key 支持点分多级，例如 "profile.token"。

        库街区 token/did 属密钥，改存数据库（DPAPI 加密）；
        其余凭据仍写 app_config.json（由 settings.manager 加密敏感字段）。
        """
        try:
            if not category or not key:
                self.emit_event("toast", message="参数无效", color="#b04b44")
                return

            # 密钥路由：加密入库，配置里不再保留
            from ..services import credential_store

            if category == credential_store.WUWA_CATEGORY and key.startswith("profile."):
                sub_key = key.split(".", 1)[1]
                if sub_key in credential_store.WUWA_SECRET_KEYS:
                    credential_store.set_secret(category, sub_key, str(value or ""))
                    logger.info("[Bridge] 库街区密钥已加密入库: %s.%s", category, sub_key)
                    self._load_credentials_backend()
                    return

            from ..settings import manager as cfg_manager

            cfg = cfg_manager.load(force=True)
            section = cfg.setdefault(category, {})

            keys = key.split(".")
            target = section
            for k in keys[:-1]:
                if not isinstance(target.get(k), dict):
                    target[k] = {}
                target = target[k]
            target[keys[-1]] = value

            cfg_manager.save(cfg)
            logger.info(
                "[Bridge] 凭据已保存: %s.%s (类型=%s)",
                category, key, type(value).__name__,
            )
            # 重新推送凭据到 QML 以便刷新所有视图
            self._load_credentials_backend()
        except Exception as exc:
            logger.exception("[Bridge] 保存凭据失败")
            self.emit_event("toast", message=f"保存失败: {exc}", color="#b04b44")

    def _save_mihoyo_device_backend(self, raw_text: str):
        """识别粘贴的设备字段集合，写回 app_config.json 的 mihoyo_sign.device。"""
        try:
            from ..clients.daily_checker import recognize_device_input

            device, info = recognize_device_input(raw_text)
            if device is None:
                self.emit_event(
                    "toast",
                    message=info.get("error") or "设备字段识别失败",
                    color="#b04b44",
                )
                return

            from ..settings import manager as cfg_manager

            cfg = cfg_manager.load(force=True)
            section = cfg.setdefault("mihoyo_sign", {})
            old = section.get("device") if isinstance(section.get("device"), dict) else {}

            # 设备身份没变时保留旧缓存（fp/device_id/seed/registered），换设备则丢弃重新生成。
            # 只比较本次粘贴提供的字段：旧 dict 里的缓存键（fp/device_id 等）不参与比较。
            keep_cache = bool(device) and all(
                str(old.get(k) or "") == str(v or "") for k, v in device.items()
            )
            if "fp" not in device and keep_cache and old.get("fp"):
                device["fp"] = old["fp"]
            for cache_key in ("device_id", "seed_id", "seed_time", "registered"):
                if cache_key not in device and keep_cache and old.get(cache_key) is not None:
                    device[cache_key] = old[cache_key]

            section["device"] = device
            cfg_manager.save(cfg)
            source_label = "抓包 fp" if info.get("format") == "captured_fp" else "APK 参数"
            fp_suffix = f", fp={str(device.get('fp'))[:8]}…" if device.get("fp") else ""
            logger.info(
                "[Bridge] 设备指纹已保存: 格式=%s, 字段=%d 个%s",
                info.get("format"), len(info.get("fields") or []), fp_suffix,
            )
            self.emit_event(
                "toast",
                message=f"已识别并保存设备指纹（{source_label}，识别到 {len(info.get('fields') or [])} 个字段）",
                color="#2f7d4a",
            )
            self._load_credentials_backend()
        except Exception as exc:
            logger.exception("[Bridge] 保存设备指纹失败")
            self.emit_event("toast", message=f"保存设备指纹失败: {exc}", color="#b04b44")

    def _clear_mihoyo_device_backend(self):
        """从 app_config.json 移除 mihoyo_sign.device 绑定。"""
        try:
            from ..settings import manager as cfg_manager

            cfg = cfg_manager.load(force=True)
            section = cfg.get("mihoyo_sign")
            if isinstance(section, dict) and "device" in section:
                section.pop("device")
                cfg_manager.save(cfg)
                logger.info("[Bridge] 设备指纹绑定已清除")
                self.emit_event("toast", message="已清除设备指纹绑定", color="#2f7d4a")
                self._load_credentials_backend()
            else:
                self.emit_event("toast", message="当前没有设备指纹绑定", color="#b66f1f")
        except Exception as exc:
            logger.exception("[Bridge] 清除设备指纹失败")
            self.emit_event("toast", message=f"清除失败: {exc}", color="#b04b44")

    def _query_tajiduo_daily_backend(self):
        """后台查询塔吉多（异环）每日任务进度。

        复用签到用的 token + deviceid，走 getUserTasks 的
        limitTimes / completeTimes 计算完成度。
        """
        try:
            from ..settings import manager as cfg_manager
            from ..clients.daily_checker import check_tajiduo_daily

            cfg = cfg_manager.load(force=True)
            tj = cfg.get("tajiduo_sign", {}) or {}
            token = str(tj.get("token", "") or "").strip()
            deviceid = str(tj.get("deviceid", "") or "").strip()

            if not token or not deviceid:
                self.emit_event("toast", message="请先填写塔吉多 Token 和 Device ID",
                                color="#b66f1f")
                self.emit_event("tajiduo_daily_result", result={
                    "ok": False,
                    "message": "缺少 token 或 deviceid（Device ID 在签到配置里，"
                               "QML 未暴露输入框，需手动写进 app_config.json）",
                    "items": [],
                })
                return

            self.emit_event("toast", message="正在查询塔吉多每日任务...", color="#295d86")

            status = check_tajiduo_daily(
                token=token,
                deviceid=deviceid,
                uid=str(tj.get("uid", "0") or "0"),
                phone=str(tj.get("phone", "") or ""),
                password=str(tj.get("password", "") or ""),
            )

            if status.errors:
                msg = "；".join(map(str, status.errors))
                self.emit_event("toast", message="塔吉多查询失败", color="#b66f1f")
                self.emit_event("tajiduo_daily_result", result={
                    "ok": False, "message": msg, "items": [],
                })
                return

            items = [
                {"name": t.name, "current": t.current,
                 "total": t.total, "done": bool(t.is_completed)}
                for t in status.tasks
            ]
            self.emit_event("toast", message="塔吉多查询成功", color="#2f7d4a")
            self.emit_event("tajiduo_daily_result", result={
                "ok": True,
                "message": "",
                "items": items,
                "all_done": bool(status.is_daily_completed),
                "game_cards": (status.extra or {}).get("game_cards", []),
            })
            logger.info("[Bridge] 塔吉多每日查询完成: %d 项任务", len(items))
        except Exception as exc:
            logger.exception("[Bridge] 塔吉多每日查询异常")
            self.emit_event("toast", message=f"查询异常: {exc}", color="#b04b44")
            self.emit_event("tajiduo_daily_result", result={
                "ok": False, "message": f"查询异常: {exc}", "items": [],
            })

    def _query_endfield_skland_backend(self):
        """执行终末地每日数据查询（森空岛官方 API）。

        复用 skendfield_sign 里已填的 token，不需要 api_key / framework_token。
        结果沿用 endfield_query_result 事件，QML 侧可复用同一套结果展示。
        成功结果会写入本地缓存（供进页面即时展示）。
        """
        try:
            from ..settings import manager as cfg_manager
            from ..clients.skendfield_data import (
                fetch_endfield_data,
                format_endfield_data_report,
            )

            cfg = cfg_manager.load(force=True)
            sk_cfg = cfg.get("skendfield_sign", {}) or {}
            token = str(sk_cfg.get("token", "") or "").strip()
            server = str(sk_cfg.get("server", "cn") or "cn").strip() or "cn"

            if not token:
                self.emit_event("toast", message="请先在下方「森空岛签到」填写 Token",
                                color="#b66f1f")
                self.emit_event("endfield_query_result", result={
                    "query_type": "skland",
                    "success": False,
                    "message": "未配置森空岛 token。请在「森空岛签到」卡片填写，"
                               "取自 web-api.skland.com/account/info/hg 的 data.content",
                    "data": None,
                })
                return

            self.emit_event("toast", message="正在通过森空岛查询终末地数据...",
                            color="#295d86")

            data = fetch_endfield_data(token, server=server)

            if data.get("ok"):
                summary = "；".join(format_endfield_data_report(data))
                result = {
                    "query_type": "skland",
                    "success": True,
                    "message": summary,
                    "data": {
                        "uid": data.get("uid", ""),
                        "role_name": data.get("role_name", ""),
                        "server_name": data.get("server_name", ""),
                        "daily": data.get("daily"),
                        "extras": data.get("extras", {}),
                        "raw_keys": data.get("raw_keys", []),
                        "raw": data.get("raw", {}),
                    },
                }
                self.emit_event("toast", message="森空岛查询成功", color="#2f7d4a")
                # 仅成功结果落地缓存；失败保留上次好缓存
                self._save_endfield_skland_cache(result)
                # 给 UI 打上 cached_at（不写进磁盘缓存），用于显示"更新于"
                emitted = dict(result)
                emitted["cached_at"] = time.time()
                self.emit_event("endfield_query_result", result=emitted)
            else:
                msg = data.get("message") or "查询失败"
                self.emit_event("toast", message=f"森空岛查询失败：{msg}", color="#b66f1f")
                self.emit_event("endfield_query_result", result={
                    "query_type": "skland",
                    "success": False,
                    "message": msg,
                    "data": None,
                })
            logger.info("[Bridge] 森空岛终末地查询完成: ok=%s", data.get("ok"))
        except Exception as exc:
            logger.exception("[Bridge] 森空岛终末地查询异常")
            self.emit_event("toast", message=f"查询异常: {exc}", color="#b04b44")
            self.emit_event("endfield_query_result", result={
                "query_type": "skland",
                "success": False,
                "message": f"查询异常: {exc}",
                "data": None,
            })
        finally:
            self._endfield_skland_refreshing = False

    def _query_endfield_center_backend(self, query_type: str):
        """执行终末地数据中心查询。"""
        try:
            if not query_type:
                self.emit_event("toast", message="未知查询类型", color="#b04b44")
                return

            from ..settings import manager as cfg_manager
            from ..clients.endfield_protocol import perform_query

            cfg = cfg_manager.load(force=True)
            proto_cfg = cfg.get("endfield_protocol", {}) or {}
            api_key = proto_cfg.get("api_key", "")
            framework_token = proto_cfg.get("framework_token", "")
            role_id = proto_cfg.get("role_id", "")
            server_id = proto_cfg.get("server_id", "")

            if not api_key:
                self.emit_event("toast", message="请先填写 API Key", color="#b66f1f")
                return

            self.emit_event("toast", message=f"正在执行 {query_type} 查询...", color="#295d86")

            result = perform_query(
                api_key=api_key,
                framework_token=framework_token,
                role_id=role_id,
                server_id=server_id,
                query_type=query_type,
            )

            if result is None:
                self.emit_event("toast", message=f"{query_type} 查询失败或无数据", color="#b66f1f")
                self.emit_event("endfield_query_result", result={
                    "query_type": query_type,
                    "success": False,
                    "message": "查询失败或缺少必要参数（role_id/server_id 等）",
                    "data": None,
                })
            else:
                self.emit_event("toast", message=f"{query_type} 查询成功", color="#2f7d4a")
                self.emit_event("endfield_query_result", result={
                    "query_type": query_type,
                    "success": True,
                    "message": "查询成功",
                    "data": result,
                })
            logger.info("[Bridge] 终末地查询完成: type=%s success=%s",
                        query_type, result is not None)
        except Exception as exc:
            logger.exception("[Bridge] 终末地查询异常")
            self.emit_event("toast", message=f"查询异常: {exc}", color="#b04b44")
            self.emit_event("endfield_query_result", result={
                "query_type": query_type,
                "success": False,
                "message": f"查询异常: {exc}",
                "data": None,
            })

    def _refresh_tone_badge(self):
        badge_text, badge_bg, badge_fg = TONE_MAP.get(
            self._status_tone, TONE_MAP[TONE_INFO]
        )
        self._status_badge = badge_text
        self._status_badge_bg = badge_bg
        self._status_badge_fg = badge_fg
        self._progress_visible = self._status_tone == TONE_RUNNING
        self.statusBadgeChanged.emit(badge_text)
        self.statusBadgeBgChanged.emit(badge_bg)
        self.statusBadgeFgChanged.emit(badge_fg)
        self.progressVisibleChanged.emit(self._progress_visible)

    def _set_status(self, title: str, body: str, tone: str = TONE_INFO):
        self.statusTitle = title
        self.statusBody = body
        self.statusTone = tone

    def _append_log(self, message: str):
        self._log_lines.append(message)
        if len(self._log_lines) > 250:
            self._log_lines = self._log_lines[-250:]
        self._log_dirty = True
        # 节流：300ms 内最多 emit 一次，250 行上限减少大字符串 join 开销，缓解切页卡顿
        now = time.time()
        if now - self._last_log_emit >= 0.30:
            self._flush_log()

    def _flush_log(self):
        """强制刷新日志到 QML（任务结束/视图切换时调用，确保最后几条日志能显示）。"""
        if not self._log_dirty:
            return
        self._log_dirty = False
        self._last_log_emit = time.time()
        self.logLinesChanged.emit()
        self.logTextChanged.emit("\n".join(self._log_lines))

    def _request_toast(self, message: str, color: str = "#2f7d4a"):
        self.toastRequested.emit(message, color)

    def _update_app_list(self, apps: List[Dict]):
        self._app_list_json = json.dumps(apps, ensure_ascii=False)
        self.appListChanged.emit(self._app_list_json)

    def _update_daily_progress(self, games: List[Dict], date_key: str = ""):
        data = {"date_key": date_key, "games": games}
        self._daily_progress_json = json.dumps(data, ensure_ascii=False)
        self.dailyProgressChanged.emit(self._daily_progress_json)

    def _update_queue_list(self, items: List[Dict]):
        self._queue_list_json = json.dumps(items, ensure_ascii=False)
        self.queueListChanged.emit(self._queue_list_json)

    def _update_queue_item_state(self, app_key: str, state: str):
        """更新队列中单个 app 的状态。"""
        try:
            items = json.loads(self._queue_list_json)
            for item in items:
                if item.get("app_key") == app_key:
                    item["state"] = state
            self._queue_list_json = json.dumps(items, ensure_ascii=False)
            self.queueListChanged.emit(self._queue_list_json)
        except Exception:
            pass

    def _update_gacha_accounts(self, accounts: List[Dict]):
        self._gacha_accounts_json = json.dumps(accounts, ensure_ascii=False)
        self.gachaAccountsChanged.emit(self._gacha_accounts_json)

    def _update_gacha_analysis(self, overview: Dict, pools: List, history: List, pity: Dict):
        data = {
            "overview": overview,
            "pools": pools,
            "history": history,
            "pity": pity,
        }
        self._gacha_analysis_json = json.dumps(data, ensure_ascii=False, default=str)
        self.gachaAnalysisChanged.emit(self._gacha_analysis_json)

    # ── Sign action backends (run in background thread) ─────────────

    def _run_skendfield_sign_backend(self):
        """森空岛签到：从 app_config.json 读 token + server，调 perform_sign。"""
        try:
            from ..settings import manager as cfg_manager
            from ..clients import skendfield_sign

            cfg = cfg_manager.load(force=True)
            sk = cfg.get("skendfield_sign") or {}
            token = str(sk.get("token") or "").strip()
            server = str(sk.get("server") or "cn").strip()
            if not token:
                self.emit_event("toast", message="森空岛 Token 为空，请先填写", color="#b04b44")
                return

            logger.info("[Bridge] 森空岛签到开始: server=%s token_len=%d", server, len(token))
            result = skendfield_sign.perform_sign(token=token, server=server)
            for line in skendfield_sign.format_sign_report(result):
                logger.info("[Bridge] 森空岛签到: %s", line)
            # 摘要用 toast 推回 QML
            status = str(result.get("status") or "")
            success = status in ("success", "already")
            head = "森空岛签到成功" if success else "森空岛签到失败"
            color = "#2f7d4a" if success else "#b04b44"
            award = result.get("award_name") or result.get("message") or ""
            msg = f"{head}：{award}" if award else head
            self.emit_event("toast", message=msg, color=color)
            # 写入签到状态供 DailyView 终末地卡片读取
            record_status = status if status in ("success", "already") else "failed"
            self._update_sign_status(
                "skendfield_sign", record_status, award,
            )
        except Exception as exc:
            logger.exception("[Bridge] 森空岛签到失败")
            self.emit_event("toast", message=f"森空岛签到失败: {exc}", color="#b04b44")
            self._update_sign_status("skendfield_sign", "failed", str(exc))

    def _run_wuwa_kujiequ_sign_backend(self):
        """库街区签到：token/did 从数据库（DPAPI 加密）读取，调 perform_sign。"""
        try:
            from ..services import credential_store
            from ..clients import wuwa_kujiequ

            profile = credential_store.load_wuwa_kujiequ_profile()
            if profile is None:
                self.emit_event(
                    "toast",
                    message="库街区 Token 或 DID 未配置，请先 ADB 抓取或手动填写",
                    color="#b04b44",
                )
                return

            logger.info(
                "[Bridge] 库街区签到开始: role=%s user_id=%s",
                profile.role_name, profile.user_id,
            )
            result = wuwa_kujiequ.perform_sign(profile)
            code = result.get("code")
            msg_text = str(result.get("message") or result.get("msg") or "").strip()
            already = "请勿重复签到" in msg_text
            success = code == 200 and (not msg_text or "请求成功" in msg_text or already)
            head = "库街区签到成功" if success else "库街区签到失败"
            color = "#2f7d4a" if success else "#b04b44"
            self.emit_event("toast", message=f"{head}：{msg_text}" if msg_text else head, color=color)
            self._update_sign_status(
                "wuwa_kujiequ",
                "success" if success else "failed",
                msg_text or ("请求成功" if success else ""),
            )
        except Exception as exc:
            logger.exception("[Bridge] 库街区签到失败")
            self.emit_event("toast", message=f"库街区签到失败: {exc}", color="#b04b44")
            self._update_sign_status("wuwa_kujiequ", "failed", str(exc))

    def _run_wuwa_kujiequ_adb_grab_backend(self):
        """通过 ADB + WebView CDP 从库街区 APP 抓取 token/did 并加密入库。

        前置条件：手机已启用 USB 调试并连接电脑、库街区 APP 已登录并进入"养成计算器"。
        token/did 以 DPAPI 密文写入 workflow_db 的 credentials 表，角色信息写回
        app_config.json；可被 _run_wuwa_kujiequ_sign_backend / fetch_* 直接使用。
        """
        from ..clients import wuwa_kujiequ_adb
        from ..clients.wuwa_kujiequ_adb import KujiequAdbError

        def _progress(msg: str) -> None:
            self.emit_event("toast", message=msg, color="#295d86")

        try:
            self.emit_event(
                "toast",
                message="开始 ADB 抓取库街区 Token...",
                color="#295d86",
            )
            capture, profile = wuwa_kujiequ_adb.grab_and_persist(progress_cb=_progress)
            self.emit_event(
                "toast",
                message=(
                    f"库街区 Token 抓取成功（已加密入库）：角色 {profile.role_name} "
                    f"(UID {profile.role_id})"
                ),
                color="#2f7d4a",
            )
            self._update_sign_status(
                "wuwa_kujiequ",
                "success",
                f"ADB 抓取成功：{profile.role_name}",
            )
        except KujiequAdbError as exc:
            logger.warning("[Bridge] 库街区 ADB 抓取失败: %s", exc)
            self.emit_event(
                "toast",
                message=f"库街区 ADB 抓取失败: {exc}",
                color="#b04b44",
            )
        except Exception as exc:
            logger.exception("[Bridge] 库街区 ADB 抓取异常")
            self.emit_event(
                "toast",
                message=f"库街区 ADB 抓取异常: {exc}",
                color="#b04b44",
            )

    def _run_tajiduo_sign_backend(self):
        """塔吉多签到：从 app_config.json 读 token/deviceid/uid/options，调 perform_sign。"""
        try:
            from ..settings import manager as cfg_manager
            from ..clients import tajiduo_sign

            cfg = cfg_manager.load(force=True)
            tj = cfg.get("tajiduo_sign") or {}
            token = str(tj.get("token") or "").strip()
            deviceid = str(tj.get("deviceid") or "").strip()
            uid = str(tj.get("uid") or "0").strip()
            if not token or not deviceid:
                self.emit_event(
                    "toast",
                    message="塔吉多 Token 或 deviceid 为空，请先填写",
                    color="#b04b44",
                )
                return

            logger.info("[Bridge] 塔吉多签到开始: uid=%s token_len=%d", uid, len(token))
            result = tajiduo_sign.perform_sign(
                token=token,
                deviceid=deviceid,
                uid=uid,
                do_bbs_signin=bool(tj.get("do_bbs_signin", True)),
                do_game_signin=bool(tj.get("do_game_signin", True)),
                do_browse=bool(tj.get("do_browse", False)),
                do_like=bool(tj.get("do_like", False)),
                do_share=bool(tj.get("do_share", False)),
                phone=str(tj.get("phone") or ""),
                password=str(tj.get("password") or ""),
            )
            for line in tajiduo_sign.format_sign_report(result):
                logger.info("[Bridge] 塔吉多签到: %s", line)
            # 塔吉多返回 {"bbs": {...}, "game": [...], ...}，逐项判断
            bbs = result.get("bbs") or {}
            game = result.get("game") or []
            bbs_ok = bbs.get("status") in ("success", "already")
            game_ok = bool(game) and all(
                g.get("status") in ("success", "already") for g in game
            )
            success = bbs_ok and game_ok
            head = "塔吉多签到成功" if success else "塔吉多签到失败"
            color = "#2f7d4a" if success else "#b04b44"
            detail = bbs.get("message") or ""
            self.emit_event(
                "toast",
                message=f"{head}：{detail}" if detail else head,
                color=color,
            )
            self._update_sign_status(
                "tajiduo_sign", "success" if success else "failed",
                detail or (game[0].get("message", "") if game else ""),
            )
        except Exception as exc:
            logger.exception("[Bridge] 塔吉多签到失败")
            self.emit_event("toast", message=f"塔吉多签到失败: {exc}", color="#b04b44")
            self._update_sign_status("tajiduo_sign", "failed", str(exc))

    # ── Sign status recording & DailyView integration ─────────────
    # 把签到结果（成功/失败/消息）写到 app_config.json 对应 sign 段的
    # last_sign_date / last_sign_status / last_sign_message 字段，供
    # DailyView 在每个游戏卡片里展示"签到"行；该行不参与 is_all_success 计算。

    def _update_sign_status(self, section_name: str, status: str, message: str = ""):
        """在 app_config.json 顶层 {section_name}.last_sign_* 写入本次签到结果。"""
        try:
            from ..settings.paths import APP_CONFIG_PATH
            try:
                cfg = json.loads(APP_CONFIG_PATH.read_text(encoding="utf-8"))
            except FileNotFoundError:
                cfg = {}
            section = cfg.get(section_name) or {}
            if not isinstance(section, dict):
                section = {}
            section["last_sign_date"] = time.strftime("%Y-%m-%d")
            section["last_sign_status"] = status
            section["last_sign_message"] = message or ""
            cfg[section_name] = section
            from ..settings.manager import atomic_write_json
            from ..settings.paths import APP_CONFIG_PATH
            atomic_write_json(APP_CONFIG_PATH, cfg)
            logger.info(
                "[Bridge] sign 状态已写入: section=%s status=%s",
                section_name, status,
            )
        except Exception as exc:
            logger.warning("[Bridge] 写 sign 状态失败: %s", exc)

    def _build_sign_task_for_game(self, game_key: str) -> Optional[Dict[str, Any]]:
        """根据 game_key 生成一条"签到"附属任务。

        返回 None 表示该游戏没有可用的签到源（不在显示范围内）。
        返回的 task 带 is_sign_task=True 标记，前端识别后展示为附属项，
        不参与 is_all_success / has_failed / progress_percent 计算。

        来源说明：
          - endfield / wuwa / nte：签到结果写到 app_config.json 的
            skendfield_sign / wuwa_kujiequ / tajiduo_sign.last_sign_*
          - genshin / honkai_sr / zzz：mihoyo_sign 一键三签，结果写到
            tracker（task_key='sign_米游社'），所以这里要从 tracker 反查
        """
        from ..settings.defaults import GAME_KEY_TO_SIGN_SECTION
        section_name = GAME_KEY_TO_SIGN_SECTION.get(game_key)
        if not section_name:
            return None

        today = time.strftime("%Y-%m-%d")

        # 米游社三件套：状态在 tracker 里
        if section_name == "mihoyo_sign":
            try:
                from ..services.daily_progress_tracker import get_tracker
                from ..settings.paths import WORKFLOW_DB_PATH
                from ..settings import resolve_game_key
                tracker = get_tracker(str(WORKFLOW_DB_PATH))
                snap = tracker.load_today()
                target_key = resolve_game_key(game_key)
                for rec in (snap.games or []):
                    if resolve_game_key(rec.game_key) != target_key:
                        continue
                    for t in rec.tasks:
                        if t.task_key.startswith("sign_"):
                            return {
                                "task_key": "__sign__",
                                "task_name": "签到",
                                "status": t.status,
                                "message": t.message or "已签到",
                                "is_sign_task": True,
                                "sign_source": "mihoyo_sign",
                            }
            except Exception as exc:
                logger.warning("[Bridge] 读取 mihoyo_sign tracker 状态失败: %s", exc)

            # tracker 里没有 → 兜底显示"尚未签到"
            try:
                from ..settings.paths import APP_CONFIG_PATH
                cfg = json.loads(APP_CONFIG_PATH.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError):
                cfg = {}
            ms = cfg.get("mihoyo_sign") or {}
            cookie = str(ms.get("cookie") or ms.get("stoken_cookie") or "").strip()
            if not cookie:
                return {
                    "task_key": "__sign__",
                    "task_name": "签到",
                    "status": "pending",
                    "message": "未配置米游社 cookie",
                    "is_sign_task": True,
                    "sign_source": "mihoyo_sign",
                }
            return {
                "task_key": "__sign__",
                "task_name": "签到",
                "status": "pending",
                "message": "今日尚未签到",
                "is_sign_task": True,
                "sign_source": "mihoyo_sign",
            }

        # 其余来源：状态在 app_config.json 的 last_sign_*
        try:
            from ..settings.paths import APP_CONFIG_PATH
            cfg = json.loads(APP_CONFIG_PATH.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return None
        section = cfg.get(section_name) or {}
        if not isinstance(section, dict):
            return None

        token = ""
        if section_name == "wuwa_kujiequ":
            # 密钥已迁到数据库加密存储，配置文件里不再有 token，需查库判断是否已配置
            from ..services import credential_store
            if credential_store.has_wuwa_credentials():
                token = "configured"
        else:
            token = str(section.get("token") or "").strip()
            if section_name == "tajiduo_sign" and not token:
                token = str(section.get("deviceid") or "").strip()

        last_date = str(section.get("last_sign_date") or "")
        last_status = str(section.get("last_sign_status") or "")
        last_message = str(section.get("last_sign_message") or "")

        if not token:
            status = "pending"
            message = "未配置 token，无法签到"
        elif last_date == today and last_status in ("success", "already"):
            status = "success"
            message = last_message or "今日已签到"
        elif last_date == today and last_status == "failed":
            status = "failed"
            message = last_message or "今日签到失败"
        elif last_date == today and last_status:
            status = last_status
            message = last_message or "今日签到状态未知"
        else:
            status = "pending"
            message = "今日尚未签到"

        return {
            "task_key": "__sign__",
            "task_name": "签到",
            "status": status,
            "message": message,
            "is_sign_task": True,
            "sign_source": section_name,
        }

    def _inject_sign_task_into_games(self, games_data: List[Dict[str, Any]]):
        """对每个 game，在其 tasks 列表前插入"签到"任务。

        已经在 tracker 里以 sign_ 前缀存在的签到任务会被统一标记为
        is_sign_task=True 并重命名为"签到"（避免和这里注入的重复）。
        这里只对没有签到任务的游戏补一条（pending / 今日尚未签到）。
        """
        from ..settings import resolve_game_key
        for game in games_data:
            gk = resolve_game_key(game.get("game_key", ""))

            # 1) 把 tracker 里残留的 sign_* 任务标记为附属项
            tasks = list(game.get("tasks") or [])
            upgraded = False
            for t in tasks:
                if (t.get("task_key") or "").startswith("sign_") and not t.get("is_sign_task"):
                    t["is_sign_task"] = True
                    t["task_key"] = "__sign__"
                    t["task_name"] = "签到"
                    upgraded = True
            if upgraded:
                game["tasks"] = tasks

            # 2) 已经存在 is_sign_task 的，不再注入
            if any(t.get("is_sign_task") for t in (game.get("tasks") or [])):
                continue

            sign_task = self._build_sign_task_for_game(gk)
            if sign_task is None:
                continue
            tasks = list(game.get("tasks") or [])
            tasks.insert(0, sign_task)
            game["tasks"] = tasks

    # ═══════════════════════════════════════════════════════════════
    #  Event dispatcher (runs on main thread via queued connection)
    # ═══════════════════════════════════════════════════════════════

    def _handle_ui_event(self, kind: str, payload: dict):
        """Dispatch uiEvent signals on the main thread."""
        if kind == "log":
            self._append_log(payload.get("message", ""))
            return
        if kind == "status":
            self._set_status(
                payload.get("title", ""),
                payload.get("body", ""),
                payload.get("tone", TONE_INFO),
            )
            return
        if kind == "running":
            self.isRunning = payload.get("value", False)
            return
        if kind == "queue_state":
            app_key = payload.get("app_key", "")
            state = payload.get("state", "待执行")
            self._queue_state[app_key] = state
            self.queueStateChanged.emit()
            return
        if kind == "queue_list":
            self._update_queue_list(payload.get("items", []))
            return
        if kind == "queue_list_update":
            self._update_queue_item_state(
                payload.get("app_key", ""),
                payload.get("state", "待执行"),
            )
            return
        if kind == "app_list":
            self._update_app_list(payload.get("apps", []))
            return
        if kind == "daily_progress":
            self._update_daily_progress(
                payload.get("games", []),
                payload.get("date_key", ""),
            )
            return
        if kind == "finished":
            self.isRunning = False
            self._flush_log()  # 确保节流期间积攒的最后几条日志能显示
            self._set_status("运行结束", "本轮任务已全部执行完毕。", TONE_SUCCESS)
            if payload.get("shutdown_scheduled"):
                self._request_toast("任务已完成，60 秒后将自动关机。", "#b66f1f")
            elif payload.get("toast"):
                self._request_toast("本轮任务已完成。", "#2f7d4a")
            # 排队中的定时任务：上一轮刚结束，现在接着跑
            if self._pending_scheduled:
                nxt = self._pending_scheduled.pop(0)
                logger.info("[定时任务] 上轮结束，执行排队的定时任务: %s", nxt.get("name"))
                self._fire_scheduled_task(nxt)
            return
        if kind == "toast":
            self._request_toast(
                payload.get("message", ""),
                payload.get("color", "#2f7d4a"),
            )
            return
        if kind == "gacha_accounts":
            self._update_gacha_accounts(payload.get("accounts", []))
            return
        if kind == "gacha_analysis":
            self._update_gacha_analysis(
                payload.get("overview", {}),
                payload.get("pools", []),
                payload.get("history", []),
                payload.get("pity", {}),
            )
            return
        if kind == "nikki_gacha":
            self._nikki_gacha_json = json.dumps(
                {"overview": payload.get("overview", {}), "pools": payload.get("pools", []),
                 "uid": payload.get("uid", "")},
                ensure_ascii=False, default=str,
            )
            self.nikkiGachaChanged.emit(self._nikki_gacha_json)
            return
        if kind == "wuwa_gacha":
            self._wuwa_gacha_json = json.dumps(
                {"overview": payload.get("overview", {}), "pools": payload.get("pools", []),
                 "history": payload.get("history", []), "uid": payload.get("uid", "")},
                ensure_ascii=False, default=str,
            )
            self.wuwaGachaChanged.emit(self._wuwa_gacha_json)
            return
        if kind == "wuwa_daily":
            self._wuwa_daily_json = json.dumps(payload, ensure_ascii=False, default=str)
            self.wuwaDailyChanged.emit(self._wuwa_daily_json)
            if not payload.get("ok", False):
                self._request_toast(
                    f"库街区每日数据获取失败: {payload.get('message', '未知错误')}",
                    "#b04b44",
                )
            return
        if kind == "endfield_gacha":
            self._endfield_gacha_json = json.dumps(
                {"overview": payload.get("overview", {}), "pools": payload.get("pools", []),
                 "history": payload.get("history", [])},
                ensure_ascii=False, default=str,
            )
            self.endfieldGachaChanged.emit(self._endfield_gacha_json)
            return
        if kind == "credentials":
            self._credentials_json = json.dumps(
                payload.get("credentials", {}),
                ensure_ascii=False, default=str,
            )
            self.credentialsChanged.emit(self._credentials_json)
            return
        if kind == "endfield_query_result":
            self._endfield_query_result_json = json.dumps(
                payload.get("result", {}),
                ensure_ascii=False, default=str,
            )
            self.endfieldQueryResultChanged.emit(self._endfield_query_result_json)
            return
        if kind == "tajiduo_daily_result":
            self._tajiduo_daily_json = json.dumps(
                payload.get("result", {}),
                ensure_ascii=False, default=str,
            )
            self.tajiduoDailyChanged.emit(self._tajiduo_daily_json)
            return
        logger.debug("[Bridge] Unhandled uiEvent kind=%s", kind)
