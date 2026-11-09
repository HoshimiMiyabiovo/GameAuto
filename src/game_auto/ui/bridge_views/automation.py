"""自动化（一条龙）视图：启动/停止、定时任务、更新补跑、薅羊毛。"""

import json
import logging
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Slot

from ...services.game_update_calendar import (
    CN_TZ,
    OPEN_BUFFER_MIN,
    UPDATE_RETRY_FILE,
    get_maintenance_state,
    get_update_info,
)
from .common import TONE_DANGER, TONE_INFO, TONE_RUNNING, TONE_SUCCESS, TONE_WARNING, logger


class AutomationView:
    """任务批处理、定时启动与更新补跑。"""
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
                # 审计 P1-4：WebUI 共享状态此前要等后台线程跑到 backend 里才
                # 置位，中间隔着秒级维护预检——这段时间 WebUI /run 的空闲检查
                # 仍会通过，双通道并发双开。现在锁内同步占位（队列细节由
                # backend 稍后覆盖），失败只降级不阻断本批。
                try:
                    from game_auto.ui.webui.state import set_running as _ws_set_running_early
                    _ws_set_running_early(True, source="gui")
                except Exception as exc:
                    logger.debug("[Bridge] 提前同步 WebUI 运行状态失败: %s", exc)
        if busy:
            self._request_toast("已有任务在运行中", "#b66f1f")
            return
        # 用户手动点"开始"→解除停止闸门，恢复定时任务/更新补跑的自动触发
        self._auto_run_paused = False
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
        """请求停止当前自动化任务，并暂停后续自动补跑（直到用户再次点开始）。"""
        first_stop = not getattr(self, "_auto_run_paused", False)
        self._auto_run_paused = True
        # 排队中的定时任务一并取消：停止就应该停下来，不能等本轮结束再自己开
        try:
            self._pending_scheduled.clear()
        except Exception:
            pass
        if self._controller:
            self._controller.stop_current_task = True
            logger.info("[Bridge] 用户请求停止任务（已暂停定时任务/更新补跑）")
            if first_stop:
                self._request_toast("正在停止当前任务，并暂停后续自动补跑", "#b66f1f")
        else:
            self._request_toast("没有运行中的任务", "#6d6257")
        # 同步停止 WebUI 侧控制器，保证 WebUI 发起的任务也能被 GUI 停止
        try:
            from game_auto.ui.webui.state import get_controller as _ws_get_ctrl
            try:
                _ws_ctrl = _ws_get_ctrl()
                if _ws_ctrl is not None:
                    _ws_ctrl.stop_current_task = True
            except Exception as exc:
                logger.debug("[Bridge] 同步 WebUI 控制器停止标志失败: %s", exc)
        except Exception as exc:
            logger.debug("[Bridge] 停止 WebUI 侧任务失败: %s", exc)
        try:
            from game_auto.ui.webui.state import update_queue_state, get_run_status
            st = get_run_status()
            cur = st.get("current") or {}
            if cur.get("app_key"):
                update_queue_state(cur["app_key"], "stopped", "用户停止")
        except Exception as exc:
            logger.debug("[Bridge] 标记 WebUI 队列停止状态失败: %s", exc)

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
            from ...settings import APP_ORDER, manager as cfg_manager
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
            from ...settings import manager as cfg_manager
            cfg = cfg_manager.load()
            prefs = cfg.get("post_run") or {}
            return json.dumps({
                "email": bool(prefs.get("email_on_finish", False)),
                "shutdown": bool(prefs.get("shutdown_on_finish", False)),
            }, ensure_ascii=False)
        except Exception:
            return '{"email":false,"shutdown":false}'

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
        from ...runtime.foreground_flow_runner import _load_flow_config
        from ...runtime.background_template_runner import WOOL_FLOW_KEYS
        from ...settings import manager as cm

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
        """写本地 flow_configs.json 中薅羊毛子流程的布尔字段（读改写全程持锁）。"""
        try:
            from ...settings.paths import ensure_flow_config_path, FLOW_CONFIG_LOCK
            from ...settings.manager import atomic_write_json
            path = ensure_flow_config_path()
            with FLOW_CONFIG_LOCK:
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
        """运行薅羊毛集合（后台线程，仅执行启用的子流程）。

        审计 P1-4 同款修复：旧实现「无锁查 _is_running → spawn 线程 → 线程内
        才置位」，双击（或与正式批次）都能穿过检查；finally 又无条件清
        isRunning，wool 先结束会把正在跑的批次"关掉"。现在锁内进入即占位 +
        代际号持有者校验，只有自己仍是最新一轮时才复位。
        """
        with self._run_lock:
            if self._is_running:
                self.emit_event("toast", message="有任务正在运行，请先等待完成", color="#b66f1f")
                return
            self._is_running = True
            self._run_generation += 1
            wool_generation = self._run_generation
        self.isRunningChanged.emit(True)
        threading.Thread(
            target=self._run_wool_backend,
            args=(wool_generation,),
            daemon=True,
        ).start()

    def _run_wool_backend(self, wool_generation: int):
        try:
            self.emit_event("status", title="正在运行", body="薅羊毛集合执行中", tone=TONE_RUNNING)
            # isRunning 属性与 _is_running 同源：runWoolCollection 已在锁内置位并
            # 发过 isRunningChanged，这里不再重复置位（双击竞态的根因就是置位太晚）
            try:
                from game_auto.ui.webui.state import set_running as _ws_set_running_w, logger as _ws_logger_w
                _ws_set_running_w(True, queue=[{"app_key": "WoolFlow", "display_name": "薅羊毛集合", "script_choice": "", "state": "running"}], source="gui")
                _ws_logger_w.info("[WebUI][运行] GUI 发起薅羊毛")
            except Exception as exc:
                logger.debug("[Bridge] 同步 WebUI 薅羊毛启动状态失败: %s", exc)
            from ...runtime.background_template_runner import run_wool_background
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
                from game_auto.ui.webui.state import release_running as _ws_release_w2, set_last_result as _ws_set_last_w2
                _ws_set_last_w2([msg], [])
                _ws_release_w2("gui")
            except Exception as exc:
                logger.debug("[Bridge] 同步 WebUI 薅羊毛结果失败: %s", exc)
        except Exception as exc:
            logger.exception("[Bridge] 薅羊毛集合执行失败")
            self.emit_event("status", title="运行失败", body=str(exc), tone=TONE_DANGER)
            self.emit_event("toast", message=f"薅羊毛集合失败: {exc}", color="#b04b44")
            try:
                from game_auto.ui.webui.state import release_running as _ws_release_w3, set_last_result as _ws_set_last_w3
                _ws_set_last_w3([f"薅羊毛失败: {exc}"], [])
                _ws_release_w3("gui")
            except Exception as sync_exc:
                logger.debug("[Bridge] 同步 WebUI 薅羊毛失败状态失败: %s", sync_exc)
        finally:
            # 持有者校验复位（审计 P1-4）：若 wool 期间有新批次启动（代际号已
            # 前进），绝不把别人的运行态清掉——旧实现无条件清 isRunning，
            # wool 先结束会把正在跑的批次"关掉"
            with self._run_lock:
                is_owner = self._run_generation == wool_generation
                if is_owner:
                    self._is_running = False
            if is_owner:
                self.isRunningChanged.emit(False)
            try:
                from game_auto.ui.webui.state import release_running as _ws_release_w4
                # 仅当 WebUI 运行态仍归属本 GUI 会话时才清（source=webui 的
                # 批次不受影响）
                _ws_release_w4("gui")
            except Exception as exc:
                logger.debug("[Bridge] 复位 WebUI 运行状态失败: %s", exc)

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
            from ...settings import get_app_display_name, get_script_display_name

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
                from game_auto.ui.webui.state import set_running as _ws_set_running, update_queue_state as _ws_upd, set_last_result as _ws_set_last, logger as _ws_logger
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
            except Exception as exc:
                logger.debug("[Bridge] 同步 WebUI 任务启动状态失败: %s", exc)

            from ...runtime.task_batch_runner import run_task_batch, TaskBatchHooks

            def on_before_app(app_key, script_choice, display_name):
                self.emit_event("queue_state", app_key=app_key, state="运行中")
                self.emit_event("queue_list_update", app_key=app_key, state="运行中")
                self.emit_event("status", title="正在运行",
                                body=f"正在执行: {display_name}",
                                tone=TONE_RUNNING)
                try:
                    from game_auto.ui.webui.state import update_queue_state as _ws_upd2
                    _ws_upd2(app_key, "running", display_name)
                except Exception as exc:
                    logger.debug("[Bridge] 同步 WebUI 队列运行状态失败: %s", exc)

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
                    from game_auto.ui.webui.state import update_queue_state as _ws_upd3
                    # 映射到 WebUI 状态
                    ws_state = "completed" if result.success else "failed"
                    if result_state == "skipped":
                        ws_state = "skipped"
                    elif result_state == "stopped":
                        ws_state = "stopped"
                    _ws_upd3(app_key, ws_state, getattr(result, "message", ""))
                except Exception as exc:
                    logger.debug("[Bridge] 同步 WebUI 队列完成状态失败: %s", exc)
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
                from game_auto.ui.webui.state import set_running as _ws_set_running2, set_last_result as _ws_set_last2
                _ws_set_last2(report, warnings)
                _ws_set_running2(False)
            except Exception as exc:
                logger.debug("[Bridge] 同步 WebUI 任务结果失败: %s", exc)
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
