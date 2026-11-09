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
import threading
import time
from typing import Dict, List, Optional

from PySide6.QtCore import QObject, Signal, Property, Slot, QTimer

from .bridge_views.automation import AutomationView
from .bridge_views.click_sequence import ClickSequenceView
from .bridge_views.common import (
    TONE_DANGER,
    TONE_INFO,
    TONE_MAP,
    TONE_RUNNING,
    TONE_SUCCESS,
    TONE_WARNING,
    _mask_address,
    logger,
)
from .bridge_views.daily import DailyView
from .bridge_views.disk_manager import DiskManagerView
from .bridge_views.gacha import GachaView
from .bridge_views.scheduler import SchedulerView
from .bridge_views.setup import SetupWizardView
from .bridge_views.tool_manager import ToolManagerView

# 全局 Bridge 单例，供 WebUI 同步运行状态（WebUI 与 GUI 同进程时可见）
_active_bridge: "UiBridge | None" = None

def get_active_bridge() -> "UiBridge | None":
    return _active_bridge


class UiBridge(
    QObject,
    ClickSequenceView,
    SetupWizardView,
    ToolManagerView,
    DiskManagerView,
    AutomationView,
    SchedulerView,
    DailyView,
    GachaView,
):
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
    scriptVersionsChanged = Signal(str)
    scriptCheckRunningChanged = Signal()
    toolInstallChanged = Signal(str)
    diskScanChanged = Signal(str)
    flowFailuresChanged = Signal(str)

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
        # 用户主动停止后置 True：定时任务/更新补跑不再自动触发，直到用户
        # 下次手动点"开始"。2026-11-04 19:37 用户停止后 19:38 更新补跑又自己
        # 启动原神，19:43 批次重试再启动一次，用户以为"关不掉"。
        self._auto_run_paused = False
        threading.Thread(target=self._prefetch_update_calendar, daemon=True).start()

        # ── 第三方脚本版本静默检查：启动 5s 后跑一次（TTL 内不联网）──
        # 新发现有更新才 toast，同一版本只提醒一次（去重记录在
        # data/cache/script_versions.json 的 notified，见 script_version_check）
        QTimer.singleShot(5_000, self._auto_check_script_versions)

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
        self._script_versions_json = "[]"
        self._script_checking = False
        # ── 脚本管理页：安装任务状态（全局同时一个）──
        self._tool_install_json = "{}"
        self._tool_install_running = False
        self._tool_install_cancel = None
        # ── 磁盘管理页：扫描任务状态（全局同时一个）──
        self._disk_scan_running = False
        self._disk_scan_cancel = None

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

    @Property(str, notify=scriptVersionsChanged)
    def scriptVersionsJson(self) -> str:
        return self._script_versions_json

    @Property(bool, notify=scriptCheckRunningChanged)
    def scriptCheckRunning(self) -> bool:
        return self._script_checking

    @Property(str, notify=toolInstallChanged)
    def toolInstallJson(self) -> str:
        return self._tool_install_json

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
        from ..games import view_keys

        valid = {
            "automation", "stamina_config", "wool", "daily", "gacha",
            "setup_wizard", "click_sequence", "webui", "tool_manager",
            "disk_manager",
        } | set(view_keys())
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
        except Exception as exc:
            logger.debug("[Bridge] 推送队列列表失败: %s", exc)

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
        if kind == "script_versions":
            # 后台脚本版本检查的回包：rows=报告 JSON，running=检查中状态，
            # toasts=启动静默检查新发现的更新提醒（手动检查时为空）
            rows = payload.get("rows") or ""
            if rows:
                self._script_versions_json = rows
                self.scriptVersionsChanged.emit(self._script_versions_json)
            running = bool(payload.get("running", False))
            if running != self._script_checking:
                self._script_checking = running
                self.scriptCheckRunningChanged.emit()
            for msg in payload.get("toasts") or []:
                self._request_toast(msg, "#b66f1f")
            return
        if kind == "tool_install":
            # 脚本安装任务的进度/阶段回包（下载→解压→换血→注册）
            data = {
                "tool_key": payload.get("tool_key", ""),
                "phase": payload.get("phase", ""),
                "received": payload.get("received", 0),
                "total": payload.get("total", 0),
                "message": payload.get("message", ""),
            }
            self._tool_install_json = json.dumps(data, ensure_ascii=False)
            self.toolInstallChanged.emit(self._tool_install_json)
            if data["phase"] == "done":
                self._request_toast(
                    data["message"] or "安装完成", "#2f7d4a")
            elif data["phase"] == "error":
                self._request_toast(f"安装失败: {data['message']}", "#b04b44")
            return
        if kind == "disk_scan":
            # 磁盘扫描的进度/结果回包（starting → item×N → done/idle/error）
            data = {
                "phase": payload.get("phase", ""),
                "item": payload.get("item"),
                "items": payload.get("items"),
                "overview": payload.get("overview"),
                "done": payload.get("done", 0),
                "total": payload.get("total", 0),
                "scanned_at": payload.get("scanned_at", ""),
                "message": payload.get("message", ""),
            }
            self.diskScanChanged.emit(json.dumps(data, ensure_ascii=False))
            if data["phase"] == "done":
                self._request_toast("磁盘扫描完成", "#2f7d4a")
            elif data["phase"] == "error":
                self._request_toast(f"磁盘扫描失败: {data['message']}", "#b04b44")
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
