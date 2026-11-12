"""定时启动（scheduled_tasks）与更新补跑队列。"""

import json
import logging
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
from .common import TONE_DANGER, TONE_INFO, TONE_SUCCESS, TONE_WARNING, logger


class SchedulerView:
    """每 30s 检查定时任务，并按公告开服时间安排更新补跑。"""
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
        from ...settings import manager as cfg_manager
        # 2026-11-09：配置写路径已全部收敛进 manager 锁内（update_config），
        # 进程内缓存与磁盘保持一致，不再需要 force=True 每 30s 全量重读 +
        # DPAPI 解密（审计 P2-6）。跨进程写（settings_gui）只动 qbittorrent/
        # log/global 段，不碰 scheduled_tasks。
        cfg = cfg_manager.load()
        tasks = cfg.get("scheduled_tasks") or []
        return tasks if isinstance(tasks, list) else []

    def _save_scheduled_tasks(self, tasks: List[Dict]) -> bool:
        from ...settings import manager as cfg_manager
        try:
            # 锁内原子读改写：30s 定时器与签到回写等并发写者不再互相覆盖
            def _apply(root_cfg) -> None:
                root_cfg["scheduled_tasks"] = tasks
            cfg_manager.update_config(_apply)
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
        if getattr(self, "_auto_run_paused", False):
            # 用户已主动停止：不再自动拉起定时任务，直到手动点"开始"
            return
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
        except Exception as exc:
            logger.debug("[Bridge] 读取今日已触发定时任务失败: %s", exc)
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
        except Exception as exc:
            logger.debug("[Bridge] 读取定时任务偏好失败: %s", exc)
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
            from ...services.game_update_calendar import refresh_all
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

    def _retry_scheduled_date(self, entry: Dict) -> str:
        """条目「该在哪天执行」：以 due_at 的日期为准，兼容只有 date 的旧条目。

        历史 bug（11-10/11-11 连续两天）：``date`` 存的是**创建当天**，而
        ``due_at`` 常指向公告开服的**次日**。旧代码一律拿 ``date == today``
        过滤，于是跨日条目在创建当天被当成"今天还有补跑没执行"，把日报邮件
        和自动关机无限推迟；到了真正该执行的那天，又因日期不匹配永远不触发。
        """
        due = self._parse_retry_dt(entry.get("due_at"))
        if due is not None:
            return due.astimezone(CN_TZ).strftime("%Y-%m-%d")
        return str(entry.get("date") or "")

    def _pending_update_retries(self, now: Optional[datetime] = None) -> List[Dict]:
        now = now or datetime.now(CN_TZ)
        today = now.strftime("%Y-%m-%d")
        with self._update_retry_lock:
            items = self._load_update_retries()
        out: List[Dict] = []
        for e in items:
            if self._retry_scheduled_date(e) != today:
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
        from ...services.launch_task_catalog import is_standalone_task

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
        """无公告时按常规维护窗口给出兜底开服时间（窗口来自游戏档案）。"""
        from ...games import get_profile

        profile = get_profile(app_key)
        window = profile.update_window if profile else ()
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
        due_day = due.astimezone(CN_TZ).strftime("%Y-%m-%d")
        with self._update_retry_lock:
            # 按「应执行日」清理：过期残留丢弃，今天/未来（含跨日）保留。
            items = [e for e in self._load_update_retries()
                     if self._retry_scheduled_date(e) >= today]
            entry = next(
                (e for e in items
                 if str(e.get("app_key") or "") == app_key
                 and self._retry_scheduled_date(e) == due_day),
                None,
            )
            if entry is None:
                entry = {
                    "app_key": app_key,
                    "script_choice": script_choice,
                    "date": due_day,
                    "attempt": 0,
                    "max_attempts": 3,
                    "created_at": now.isoformat(timespec="seconds"),
                }
                items.append(entry)
            entry.update({
                "script_choice": script_choice,
                # date 统一迁移成「应执行日」；旧条目（存创建日）在这里被纠正
                "date": due_day,
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
        with self._update_retry_lock:
            items = self._load_update_retries()
            # 补跑已完成/终止：该 app 的排队全部清掉（含跨日、旧格式残留），
            # 不再按日期挑，避免"删了今天的、明天的死条目还挂着挡收尾"。
            new_items = [e for e in items if str(e.get("app_key") or "") != app_key]
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
                 if str(e.get("app_key") or "") == app_key
                 and self._retry_scheduled_date(e) == today),
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

    def _check_update_retries(self, now: Optional[datetime] = None) -> None:
        """每 30s 由定时器调用：到点的更新补跑启动一批。

        ``now`` 可注入，便于跨日回归测试（定时器调用时不传）。
        """
        if getattr(self, "_auto_run_paused", False):
            return
        if self._is_running:
            return
        now = now or datetime.now(CN_TZ)
        today = now.strftime("%Y-%m-%d")
        with self._update_retry_lock:
            items = self._load_update_retries()
            # 应执行日已过的残留直接丢弃，不再挂在文件里
            fresh = [e for e in items if self._retry_scheduled_date(e) >= today]
            changed = len(fresh) != len(items)
            if changed:
                logger.info("[更新补跑] 清理 %d 条过期队列", len(items) - len(fresh))
                items = fresh
            due = []
            for e in items:
                if self._retry_scheduled_date(e) != today:
                    continue
                attempt = int(e.get("attempt") or 0)
                max_attempts = int(e.get("max_attempts") or 3)
                if attempt >= max_attempts:
                    continue
                due_at = self._parse_retry_dt(e.get("due_at"))
                if due_at is not None and due_at <= now:
                    due.append(e)
            if not due:
                if changed:
                    self._save_update_retries(items)
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
        """保存"完成后操作"偏好到 app_config.json（锁内原子读改写）。"""
        try:
            from ...settings import manager as cfg_manager

            def _apply(root_cfg) -> None:
                root_cfg["post_run"] = {
                    "email_on_finish": bool(email_on_finish),
                    "shutdown_on_finish": bool(shutdown_on_finish),
                }
            cfg_manager.update_config(_apply)
        except Exception as exc:
            logger.warning("[Bridge] 保存完成后操作偏好失败: %s", exc)
