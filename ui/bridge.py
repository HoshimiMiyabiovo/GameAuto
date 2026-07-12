"""Central bridge object exposed to QML.

A single QObject that carries all UI-facing state as Qt properties.
QML binds to these properties declaratively; Python updates them
and change-notify signals trigger automatic re-renders.

Thread safety: Qt's cross-thread signal delivery (QueuedConnection)
ensures that background threads can emit uiEvent and the handler
slot executes on the main thread — no polling needed.
"""

import json
import logging
import os
import threading
from typing import Any, Dict, List, Optional

from PySide6.QtCore import QObject, Signal, Property, Slot

logger = logging.getLogger("GameAutomation.Bridge")

# ── Status tone constants ────────────────────────────────────────
TONE_INFO = "info"
TONE_RUNNING = "running"
TONE_SUCCESS = "success"
TONE_WARNING = "warning"
TONE_DANGER = "danger"

TONE_MAP: Dict[str, tuple] = {
    TONE_INFO:    ("待命中", "#dff1e4", "#2f7d4a"),
    TONE_RUNNING: ("进行中", "#e9f3fb", "#295d86"),
    TONE_SUCCESS: ("已完成", "#dff1e4", "#2f7d4a"),
    TONE_WARNING: ("请留意", "#faebd7", "#b66f1f"),
    TONE_DANGER:  ("已中断", "#f7dfdd", "#b04b44"),
}


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
    queueListChanged = Signal(str)
    gachaAccountsChanged = Signal(str)
    gachaAnalysisChanged = Signal(str)
    nikkiGachaChanged = Signal(str)
    wuwaGachaChanged = Signal(str)
    endfieldGachaChanged = Signal(str)
    credentialsChanged = Signal(str)
    endfieldQueryResultChanged = Signal(str)

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)

        # ── Private state ──
        self._current_view = "automation"
        self._is_running = False
        self._status_title = "准备就绪"
        self._status_body = "先勾选任务，再点击开始运行。"
        self._status_tone = TONE_INFO
        self._progress_visible = False
        self._selection_summary = "还没有选择任务"
        self._log_lines: List[str] = []

        # ── Tone-derived badge state ──
        self._refresh_tone_badge()

        # ── Queue state (app_key → "待执行" | "运行中" | "已完成" | "失败" | "已停止") ──
        self._queue_state: Dict[str, str] = {}
        self._queue_order: List[str] = []

        # ── Data properties (JSON strings for QML) ──
        self._app_list_json = "[]"
        self._daily_progress_json = "{}"
        self._queue_list_json = "[]"
        self._gacha_accounts_json = "[]"
        self._gacha_analysis_json = "{}"
        self._nikki_gacha_json = "{}"
        self._wuwa_gacha_json = "{}"
        self._endfield_gacha_json = "{}"
        self._credentials_json = "{}"
        self._endfield_query_result_json = "{}"

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

    @Property(str, notify=endfieldGachaChanged)
    def endfieldGachaJson(self) -> str:
        return self._endfield_gacha_json

    @Property(str, notify=credentialsChanged)
    def credentialsJson(self) -> str:
        return self._credentials_json

    @Property(str, notify=endfieldQueryResultChanged)
    def endfieldQueryResultJson(self) -> str:
        return self._endfield_query_result_json

    # ═══════════════════════════════════════════════════════════════
    #  Slots — QML → Python
    # ═══════════════════════════════════════════════════════════════

    @Slot(str)
    def switchView(self, view_key: str):
        valid = {
            "automation", "daily", "gacha", "nikki",
            "wuwa", "tajiduo", "endfield_center", "setup_wizard"
        }
        if view_key not in valid:
            logger.warning("[UI] 未知视图: %s", view_key)
            return
        self.currentView = view_key

    @Slot()
    def clearLog(self):
        """清空日志缓冲区。"""
        self._log_lines.clear()
        self.logLinesChanged.emit()
        self.logTextChanged.emit("")

    # ── Setup wizard slots ─────────────────────────────────────────

    @Slot()
    def isSetupCompleted(self) -> bool:
        from ..settings.manager import is_setup_completed
        return is_setup_completed()

    @Slot()
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

        return json.dumps(items, ensure_ascii=False)

    @Slot(str)
    def checkFileExists(self, path: str) -> bool:
        import os
        return bool(path) and os.path.isfile(path)

    @Slot(str)
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

            if not app_key:
                continue

            app_cfg = apps.setdefault(app_key, {})
            discovery = app_cfg.setdefault("_discovery", {})

            if field == "launcher":
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

    @Slot(str)
    def startAutomation(self, apps_json: str):
        """启动自动化任务。apps_json: [{"app_key":"genshin","script_choice":"ok"}, ...]"""
        try:
            targets = json.loads(apps_json)
        except json.JSONDecodeError:
            self._request_toast("参数解析失败", "#b04b44")
            return
        if not targets:
            self._request_toast("请先勾选至少一个任务", "#b66f1f")
            return
        if self._is_running:
            self._request_toast("已有任务在运行中", "#b66f1f")
            return
        # 新任务开始时清空旧日志，避免多任务日志混杂导致时间错乱
        self.clearLog()
        threading.Thread(
            target=self._run_automation_backend,
            args=(targets,),
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

    @Slot()
    def loadNikkiGacha(self):
        """加载无限暖暖抽卡数据（从数据库）。"""
        threading.Thread(target=self._load_nikki_gacha_backend, daemon=True).start()

    @Slot()
    def loadWuwaGacha(self):
        """加载鸣潮抽卡数据（从数据库）。"""
        threading.Thread(target=self._load_wuwa_gacha_backend, daemon=True).start()

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
    def queryEndfieldCenter(self, query_type: str):
        """终末地数据中心查询。
        query_type: note | stamina | search_chars | spaceship | achieve | gacha_records | ...
        """
        threading.Thread(
            target=self._query_endfield_center_backend,
            args=(query_type,),
            daemon=True,
        ).start()

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
                enabled = pref.get("enabled", False)
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

    def _run_automation_backend(self, targets: List[Dict]):
        """后台执行自动化任务。"""
        try:
            controller = self._get_controller()
            self.emit_event("status", title="正在运行", body="自动化任务已启动", tone=TONE_RUNNING)
            self.emit_event("running", value=True)

            target_apps = [(t["app_key"], t.get("script_choice") or None) for t in targets]

            # 构建队列列表
            from ..settings import get_app_display_name, get_script_display_name
            queue_items = []
            for app_key, script_choice in target_apps:
                display = get_app_display_name(app_key)
                script_label = get_script_display_name(app_key, script_choice)
                queue_items.append({
                    "app_key": app_key,
                    "task_name": f"{display}({script_label})",
                    "script_label": script_label,
                    "state": "待执行",
                })
            self.emit_event("queue_list", items=queue_items)

            from ..runtime.task_batch_runner import run_task_batch, TaskBatchHooks

            def on_before_app(app_key, script_choice, display_name):
                self.emit_event("queue_state", app_key=app_key, state="运行中")
                self.emit_event("queue_list_update", app_key=app_key, state="运行中")
                self.emit_event("status", title="正在运行",
                                body=f"正在执行: {display_name}",
                                tone=TONE_RUNNING)

            def on_after_app(result):
                app_key = result.app_key if hasattr(result, "app_key") else ""
                if result.success:
                    self.emit_event("queue_state", app_key=app_key, state="已完成")
                    self.emit_event("queue_list_update", app_key=app_key, state="已完成")
                else:
                    self.emit_event("queue_state", app_key=app_key, state="失败")
                    self.emit_event("queue_list_update", app_key=app_key, state="失败")

            hooks = TaskBatchHooks(
                on_before_app=on_before_app,
                on_after_app=on_after_app,
            )

            result = run_task_batch(
                controller,
                target_apps,
                hooks=hooks,
            )

            for line in result.report:
                self.emit_event("log", message=line)
            for warning in result.warnings:
                self.emit_event("log", message=f"[警告] {warning}")

            self.emit_event("finished", toast=True)
        except Exception as exc:
            logger.exception("[Bridge] 自动化执行失败")
            self.emit_event("status", title="运行失败", body=str(exc), tone=TONE_DANGER)
            self.emit_event("toast", message=f"执行失败: {exc}", color="#b04b44")
            self.emit_event("running", value=False)

    def _save_preferences_backend(self, prefs: List[Dict]):
        """后台保存启动偏好。"""
        try:
            db = self._get_preference_db()
            for pref in prefs:
                app_key = pref.get("app_key", "")
                enabled = pref.get("enabled", False)
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
            result = check_games_progress(game_keys=game_keys if game_keys else None)

            games_data = []
            for game in result.games:
                tasks_data = []
                for task in game.tasks:
                    tasks_data.append({
                        "task_key": task.task_key,
                        "task_name": task.task_name,
                        "status": task.status,
                        "message": task.message,
                    })
                games_data.append({
                    "game_key": game.game_key,
                    "game_name": game.game_name,
                    "uid": game.uid,
                    "nickname": game.nickname,
                    "is_all_success": game.is_all_success,
                    "has_failed": game.has_failed,
                    "progress_percent": game.progress_percent,
                    "tasks": tasks_data,
                    "errors": game.errors,
                })

            self._inject_external_log_progress(games_data)
            self.emit_event("daily_progress", games=games_data, date_key="")
            self.emit_event("status", title="查询完成",
                            body=f"已查询 {len(games_data)} 款游戏",
                            tone=TONE_SUCCESS)
            self.emit_event("toast", message="每日进度已刷新", color="#2f7d4a")
        except Exception as exc:
            logger.exception("[Bridge] 每日进度查询失败")
            self.emit_event("status", title="查询失败", body=str(exc), tone=TONE_DANGER)
            self.emit_event("toast", message=f"查询失败: {exc}", color="#b04b44")

    def _load_daily_progress_backend(self):
        """从 SQLite 加载今日持久化进度。"""
        try:
            from ..services.daily_progress_tracker import get_tracker
            from ..settings.paths import WORKFLOW_DB_PATH
            tracker = get_tracker(str(WORKFLOW_DB_PATH))
            snapshot = tracker.load_today()

            games_data = []
            for game in snapshot.games:
                tasks_data = []
                for task in game.tasks:
                    tasks_data.append({
                        "task_key": task.task_key,
                        "task_name": task.task_name,
                        "status": task.status,
                        "message": task.message,
                    })
                games_data.append({
                    "game_key": game.game_key,
                    "game_name": game.game_name,
                    "uid": game.uid,
                    "nickname": game.nickname,
                    "is_all_success": game.is_all_success,
                    "has_failed": game.has_failed,
                    "progress_percent": game.progress_percent,
                    "tasks": tasks_data,
                    "errors": game.errors,
                })

            self._inject_external_log_progress(games_data)
            self.emit_event("daily_progress", games=games_data, date_key=snapshot.date_key)
            logger.info("[Bridge] 加载持久化进度: date_key=%s games=%d",
                        snapshot.date_key, len(games_data))
        except Exception as exc:
            logger.exception("[Bridge] 加载持久化进度失败")
            self.emit_event("toast", message=f"加载失败: {exc}", color="#b04b44")

    def _inject_external_log_progress(self, games_data: List[Dict[str, Any]]):
        """读取外部自动化工具日志，将日常执行情况注入 games_data。

        支持的工具与映射：
          - ok-ef（终末地）→ game_key="endfield"，只要关键任务（⭐活动奖励、⭐日常奖励）成功就算成功
          - ok-nte（异环）→ game_key="nte"，只要关键任务（领取活跃度奖励）成功就算成功
          - ok-ww（鸣潮）→ game_key="wuwa"，只要出现 Successfully Executed Task 就算成功
          - whimbox（无限暖暖）→ game_key="nikki"，没有 ❌ 未完成任务就算成功
          - BetterGI（原神）→ game_key="genshin"，每日委托奖励 + 探索派遣 都完成才算成功

        如果今天的日志存在，替换原有的对应 game 项；否则保持原数据不变。
        """
        from ..services.ok_ef_daily_parser import parse_latest_ok_ef_report
        from ..services.ok_nte_daily_parser import parse_latest_ok_nte_log
        from ..services.ok_ww_daily_parser import parse_latest_ok_ww_log
        from ..services.whimbox_daily_parser import parse_latest_whimbox_log
        from ..services.bettergi_daily_parser import parse_latest_bettergi_log
        from ..settings import resolve_app_key

        parsers = (
            ("endfield", "ok-ef", parse_latest_ok_ef_report),
            ("nte", "ok-nte", parse_latest_ok_nte_log),
            ("wuwa", "ok-ww", parse_latest_ok_ww_log),
            ("nikki", "whimbox", parse_latest_whimbox_log),
            ("genshin", "BetterGI", parse_latest_bettergi_log),
        )
        for game_key, tool_name, parser_func in parsers:
            try:
                data = parser_func()
                if data is None:
                    continue
                resolved_key = resolve_app_key(game_key)
                replaced = False
                for i, game in enumerate(games_data):
                    if resolve_app_key(game.get("game_key", "")) == resolved_key:
                        games_data[i] = data
                        replaced = True
                        logger.info("[Bridge] 已用 %s 日志替换 %s 进度: success=%s",
                                    tool_name, game_key, data.get("is_all_success"))
                        break
                if not replaced:
                    games_data.append(data)
                    logger.info("[Bridge] 已注入 %s %s 进度: success=%s",
                                tool_name, game_key, data.get("is_all_success"))
            except Exception as exc:
                logger.warning("[Bridge] 读取 %s 日志失败: %s", tool_name, exc)

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

            raw_overview = build_overview(records, game_code, latest, import_count)
            raw_pools = build_pool_analysis(records, game_code)
            history = build_high_rarity_history(records, game_code)
            raw_pity = build_pity_summary(records, game_code)

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
                        }
                        for r in summary.get("five_star_history", [])
                    ]
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
                        "mode": "pearpal",
                    })
            else:
                overview = {}

            self.emit_event("nikki_gacha", overview=overview, pools=pools, uid=uid)
            logger.info("[Bridge] 暖暖抽卡分析完成: uid=%s pools=%d", uid, len(pools))
        except Exception as exc:
            logger.exception("[Bridge] 加载暖暖抽卡失败")
            self.emit_event("toast", message=f"加载失败: {exc}", color="#b04b44")

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

            records = db_to_wuwa_records(db_rows)
            overview = build_wuwa_overview(records)
            raw_pools = build_wuwa_pool_analysis(records)
            history = build_wuwa_five_star_history(records)

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

            self.emit_event("wuwa_gacha", overview=overview, pools=pools, history=history, uid=uid)
            logger.info("[Bridge] 鸣潮抽卡分析完成: uid=%s pools=%d", uid, len(pools))
        except Exception as exc:
            logger.exception("[Bridge] 加载鸣潮抽卡失败")
            self.emit_event("toast", message=f"加载失败: {exc}", color="#b04b44")

    def _load_endfield_gacha_backend(self, file_path: str):
        """从文件加载终末地抽卡数据并分析，同时写入数据库。"""
        try:
            from ..gacha.endfield_analysis import (
                load_all_records as load_endfield_records,
                build_overview as build_endfield_overview,
                build_six_star_history,
                build_pool_analysis as build_endfield_pool_analysis,
                endfield_to_db_records,
            )

            records = load_endfield_records(file_path)
            if not records:
                self.emit_event("toast", message="文件中无抽卡记录", color="#b66f1f")
                return

            # 写入数据库
            from ..settings.paths import WORKFLOW_DB_PATH
            db = self._get_preference_db()
            uid = ""  # endfield 使用空 uid
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
            logger.info("[Bridge] 终末地抽卡数据已写入数据库: %d 条", len(records))

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
        "nikki",
    )

    def _load_credentials_backend(self):
        """从 app_config.json 读取所有凭据，打包为 JSON 推送到 QML。"""
        try:
            from ..settings import manager as cfg_manager

            cfg = cfg_manager.load(force=True)
            credentials: Dict[str, Any] = {}
            for top_key in self._CREDENTIAL_KEYS:
                section = cfg.get(top_key)
                if isinstance(section, dict):
                    credentials[top_key] = section
                else:
                    credentials[top_key] = {}

            self.emit_event("credentials", credentials=credentials)
            logger.info(
                "[Bridge] 凭据已加载: %s",
                ", ".join(k for k, v in credentials.items() if v),
            )
        except Exception as exc:
            logger.exception("[Bridge] 加载凭据失败")
            self.emit_event("toast", message=f"加载凭据失败: {exc}", color="#b04b44")

    def _save_credential_backend(self, category: str, key: str, value: Any):
        """写回单个凭据到 app_config.json。
        key 支持点分多级，例如 "profile.token"。
        """
        try:
            if not category or not key:
                self.emit_event("toast", message="参数无效", color="#b04b44")
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
        if len(self._log_lines) > 400:
            self._log_lines = self._log_lines[-400:]
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
            self._set_status("运行结束", "本轮任务已全部执行完毕。", TONE_SUCCESS)
            if payload.get("toast"):
                self._request_toast("本轮任务已完成。", "#2f7d4a")
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
        logger.debug("[Bridge] Unhandled uiEvent kind=%s", kind)
