import os
import time
import logging
import queue
import subprocess
import ctypes
import atexit
import psutil
import pyautogui
import win32gui
import win32process
from logging.handlers import TimedRotatingFileHandler
from typing import Tuple, Optional, Dict, List

from .foreground_flow_runner import run_flow
from .process_supervisor import ProcessSnapshot, ProcessSupervisor
from .ok_ww_log_monitor import run_nte_post_launch_ocr_click
from .script_error_log_monitor import build_script_error_watchers
from .window_detector import WindowDetector
from ..settings import (
    APPS,
    CHECK_INTERVAL,
    DEBUG_MODE,
    BACKUP_COUNT,
    GAME_CLOSE_GRACE_SEC,
    LOG_FILE,
    MIN_RUN_TIME,
    POST_CLOSE_LAUNCHER_WAIT,
    get_app_display_name,
    get_script_display_name,
    resolve_app_key,
)
from ..settings.discovery import remember_process_snapshot
from ..services.utils import clear_keyboard_buffer, get_saved_script_choice


WRAPPER_PROCESS_NAMES = {
    "cmd.exe",
    "conhost.exe",
    "explorer.exe",
    "powershell.exe",
    "pwsh.exe",
    "python.exe",
    "pythonw.exe",
}

# 游戏更新中跳过哨兵：等待游戏进程时检测到官方启动器在更新（如异环），
# 不等超时直接结束本轮。task_batch_runner 认这个字符串记为 skipped（不失败、不重试）。
GAME_UPDATING_SKIP = "游戏更新中，跳过"

# 日志队列化：GameAutomation logger 只挂 QueueHandler，写盘/转发由
# QueueListener 消费线程执行。E 盘/USB 盘 IO 卡住时仅消费线程阻塞，
# 任务线程的 logger.* 调用只是内存入队，永不钉死在坏盘上
# （2026-10-15 蓝屏复盘：同步写盘把任务线程挂到系统崩溃）。
_LOG_LISTENER = None
_LOG_LISTENER_ATEXIT = False


def _stop_log_listener():
    global _LOG_LISTENER
    listener = _LOG_LISTENER
    _LOG_LISTENER = None
    if listener is not None:
        try:
            listener.stop()
        except Exception:
            pass


class AutomationController:
    def __init__(self):
        self.logger = self._init_logger()
        self.stop_current_task = False
        # 任务线程心跳看门狗：任务线程卡死（IO 挂起/死锁）时留现场并止损。
        # 与 hang_watchdog（只盯主线程）互补，见 services/task_heartbeat_watchdog。
        from ..services.task_heartbeat_watchdog import TaskHeartbeatWatchdog
        from ..settings.paths import WATCHDOG_TASK_LOG_FILE
        self._heartbeat = TaskHeartbeatWatchdog(
            self.logger,
            WATCHDOG_TASK_LOG_FILE,
            stall_timeout=float(
                (APPS.get("_global", {}) or {}).get("task_heartbeat_timeout_sec", 600)
                or 600
            ),
        )
        self.logger.info("键盘检测与快捷键监听功能已禁用")

    def _init_logger(self):
        logger = logging.getLogger("GameAutomation")
        logger.setLevel(logging.DEBUG if DEBUG_MODE else logging.INFO)
        logger.propagate = False

        # 保留 WebUI / QML 的内存转发 handler，避免初始化时清掉导致日志不可见
        preserved = []
        for handler in logger.handlers[:]:
            cname = handler.__class__.__name__
            if cname in ("WebLogHandler", "UILogHandler"):
                preserved.append(handler)
                continue
            logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass
        formatter = logging.Formatter(
            '[%(asctime)s.%(msecs)03d] [%(levelname)-8s] %(name)s:%(funcName)s:%(lineno)d - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        from ..settings.manager import get_log_config
        backup_count = max(0, int(get_log_config().get("backup_count", BACKUP_COUNT) or 0))
        file_handler = TimedRotatingFileHandler(
            LOG_FILE, when='midnight', interval=1, backupCount=backup_count, encoding='utf-8'
        )
        file_handler.setFormatter(formatter)

        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)

        # 日志队列化：logger 只挂 QueueHandler（入队即返回），file/console/UI
        # 转发全部交给 QueueListener 消费线程。IO 卡住时只卡消费线程。
        global _LOG_LISTENER, _LOG_LISTENER_ATEXIT
        if _LOG_LISTENER is not None:
            try:
                _LOG_LISTENER.stop()
            except Exception:
                pass
            _LOG_LISTENER = None
        log_queue: "queue.Queue" = queue.Queue(-1)
        logger.addHandler(logging.handlers.QueueHandler(log_queue))
        _LOG_LISTENER = logging.handlers.QueueListener(
            log_queue, file_handler, console_handler, *preserved,
            respect_handler_level=True,
        )
        _LOG_LISTENER.start()
        if not _LOG_LISTENER_ATEXIT:
            atexit.register(_stop_log_listener)
            _LOG_LISTENER_ATEXIT = True

        return logger

    def _start_key_listener(self):
        return

    def _cleanup_key_listener(self):
        clear_keyboard_buffer()

    def _is_wrapper_process_name(self, name: str) -> bool:
        return str(name or "").strip().lower() in WRAPPER_PROCESS_NAMES

    def _is_wrapper_snapshot(self, snapshot: Optional[ProcessSnapshot]) -> bool:
        if snapshot is None:
            return False
        if self._is_wrapper_process_name(snapshot.name):
            return True
        return self._is_wrapper_process_name(os.path.basename(str(snapshot.exe_path or "")))

    def _configured_game_process_name(self, config: dict) -> str:
        game_cfg = config.get("game")
        if not isinstance(game_cfg, dict):
            return ""

        configured_name = str(game_cfg.get("process_name", "") or "").strip()
        if configured_name and not self._is_wrapper_process_name(configured_name):
            return configured_name

        exe_name = os.path.basename(str(game_cfg.get("exe_path", "") or "").strip())
        if exe_name and not self._is_wrapper_process_name(exe_name):
            return exe_name

        return ""

    def _foreground_window_pid(self) -> Optional[int]:
        try:
            hwnd = win32gui.GetForegroundWindow()
            if not hwnd:
                return None
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            return pid or None
        except Exception:
            return None

    def _find_visible_window_by_pid(self, pid: Optional[int]) -> Optional[int]:
        if not pid:
            return None
        for _, hwnd, candidate_pid in self._visible_window_candidates():
            if candidate_pid == pid:
                return hwnd
        return None

    def _game_family_has_visible_window(self, root_pid: Optional[int]) -> bool:
        """判断游戏进程及其全部子进程是否仍拥有可见窗口。

        仅当整个进程族都没有可见窗口时，才认为游戏已进入关闭/退出流程
        （避免把'主进程无窗口但子进程持窗'的正常游戏误判为已关闭）。
        """
        if not root_pid:
            return False
        visible_pids = {pid for _, _, pid in self._visible_window_candidates()}
        try:
            root = psutil.Process(root_pid)
            family_pids = {root.pid, *(child.pid for child in root.children(recursive=True))}
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False
        return bool(family_pids & visible_pids)

    def _activate_window(self, hwnd: int) -> bool:
        activation_errors: List[str] = []

        try:
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, 9)
        except Exception as exc:
            activation_errors.append(f"ShowWindow: {exc}")

        for action_name, action in (
            ("BringWindowToTop", win32gui.BringWindowToTop),
            ("SetForegroundWindow", win32gui.SetForegroundWindow),
            ("SetActiveWindow", win32gui.SetActiveWindow),
        ):
            try:
                action(hwnd)
            except Exception as exc:
                activation_errors.append(f"{action_name}: {exc}")

        try:
            foreground_hwnd = win32gui.GetForegroundWindow()
        except Exception:
            foreground_hwnd = None

        activated = foreground_hwnd == hwnd
        if activation_errors and not activated:
            self.logger.debug(f"窗口激活未完全成功 HWND={hwnd}: {'; '.join(activation_errors)}")
        return activated

    def _is_pid_running(self, pid: int) -> bool:
        try:
            proc = psutil.Process(pid)
            return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False

    def _log_detected_process(self, role: str, snapshot: Optional[ProcessSnapshot], *, source: str = ""):
        if snapshot is None:
            return
        suffix = f" [来源: {source}]" if source else ""
        self.logger.info(f"{role}识别结果{suffix}: {ProcessSupervisor.describe_process(snapshot)}")

    def _collect_process_family_snapshots(self, root_pid: Optional[int]) -> List[ProcessSnapshot]:
        if not root_pid:
            return []

        snapshots: List[ProcessSnapshot] = []
        try:
            root = psutil.Process(root_pid)
            for proc in [root, *root.children(recursive=True)]:
                snapshot = ProcessSupervisor.snapshot_process(proc)
                if snapshot is not None:
                    snapshots.append(snapshot)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return []

        snapshots.sort(key=lambda item: item.create_time, reverse=True)
        return snapshots

    def _log_script_process_context(self, script_snapshot: Optional[ProcessSnapshot], launch_started_at: float) -> Optional[ProcessSnapshot]:
        self._log_detected_process("脚本进程", script_snapshot, source="启动后识别")
        if script_snapshot is None:
            return None

        if not self._is_wrapper_snapshot(script_snapshot):
            return script_snapshot

        descendants = []
        preferred_snapshot = None
        for snapshot in self._collect_process_family_snapshots(script_snapshot.pid):
            if snapshot.pid == script_snapshot.pid or snapshot.pid == os.getpid():
                continue
            if snapshot.create_time and snapshot.create_time < launch_started_at - 2:
                continue
            descendants.append(ProcessSupervisor.describe_process(snapshot))
            if preferred_snapshot is None and not self._is_wrapper_snapshot(snapshot):
                preferred_snapshot = snapshot

        if descendants:
            self.logger.info(f"脚本入口进程为包装器，当前子进程: {'; '.join(descendants[:5])}")

        if preferred_snapshot is None:
            self.logger.warning("脚本入口进程仍是包装器，跳过将包装器当作脚本本体")
            return None

        return preferred_snapshot

    def _visible_window_candidates(self) -> List[tuple[int, int, int]]:
        candidates: List[tuple[int, int, int]] = []

        def callback(hwnd, _):
            if not win32gui.IsWindowVisible(hwnd):
                return
            try:
                left, top, right, bottom = win32gui.GetWindowRect(hwnd)
                if right <= left or bottom <= top:
                    return
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                if not pid or pid == os.getpid():
                    return
                area = (right - left) * (bottom - top)
            except Exception:
                return

            candidates.append((area, hwnd, pid))

        win32gui.EnumWindows(callback, None)
        candidates.sort(reverse=True)
        return candidates

    def _detect_game_process_snapshot(
        self,
        configured_name: str,
        launch_started_at: float,
        *,
        script_snapshot: Optional[ProcessSnapshot] = None,
        launcher_name: str = "",
    ) -> Optional[ProcessSnapshot]:
        preferred_name = str(configured_name or "").strip().lower()
        script_name = (script_snapshot.name.lower() if script_snapshot and script_snapshot.name else "")
        launcher_name = str(launcher_name or "").strip().lower()
        best_snapshot = None
        best_score = None
        for area, _, pid in self._visible_window_candidates():
            snapshot = ProcessSupervisor.snapshot_pid(pid)
            if snapshot is None or snapshot.pid == os.getpid():
                continue

            name_lower = snapshot.name.lower()
            if preferred_name and name_lower == preferred_name:
                return snapshot
            if snapshot.create_time and snapshot.create_time < launch_started_at - 2:
                continue
            if self._is_wrapper_snapshot(snapshot):
                continue
            if script_name and name_lower == script_name:
                continue
            if launcher_name and name_lower == launcher_name:
                continue

            score = (
                area,
                snapshot.create_time,
            )
            if best_score is None or score > best_score:
                best_score = score
                best_snapshot = snapshot

        return best_snapshot

    def _wait_for_game_process_snapshot(
        self,
        app_name: str,
        config: dict,
        launch_config: dict,
        launch_started_at: float,
        runtime_processes: Dict[str, ProcessSnapshot],
        script_choice: Optional[str] = None,
        progress_hint=None,
    ) -> Tuple[Optional[ProcessSnapshot], str]:
        """等待游戏进程，返回 (snapshot, reason)。

        双预算：平时按 timeouts.launch 找进程；progress_hint 命中（更新中）时
        改按 timeouts.update 等更新结束（更新完游戏出来照常跑，不跳过）。
        更新行超过 180 秒没再出现视为更新停滞，回到 launch 预算收尾。
        reason ""=正常，"updating"=更新预算也耗尽，调用方记跳过而非失败。
        """
        game_cfg = config.get("game") if isinstance(config.get("game"), dict) else {}
        raw_configured_name = str(game_cfg.get("process_name", "") or "").strip()
        raw_game_exe = str(game_cfg.get("exe_path", "") or "").strip()
        configured_name = self._configured_game_process_name(config)
        timeout = int(config["timeouts"]["launch"])
        update_timeout = int((config.get("timeouts") or {}).get("update", 0) or 0)
        start = time.time()

        if raw_configured_name and not configured_name:
            self.logger.warning(f"[{app_name}] 当前 game.process_name 为包装器进程 {raw_configured_name}，本轮忽略该配置并改用真实游戏识别")
        elif raw_configured_name and configured_name and configured_name.lower() != raw_configured_name.lower():
            self.logger.warning(
                f"[{app_name}] 当前 game.process_name 为包装器进程 {raw_configured_name}，本轮改用 game.exe_path 推导出的精确进程名 {configured_name}"
            )
        elif not raw_configured_name and raw_game_exe and configured_name:
            self.logger.info(f"[{app_name}] 未配置 game.process_name，本轮改用 game.exe_path 推导出的精确进程名 {configured_name}")

        if not configured_name:
            self.logger.error(f"[{app_name}] 未配置可精确识别的游戏进程名，已禁用未知窗口/未知进程兜底")
            return None, ""

        self.logger.info(f"等待游戏进程启动: {configured_name} (超时: {timeout}秒)")

        last_log = 0.0
        polls = 0
        last_update_seen = 0.0
        updating_announced = False
        while True:
            now = time.time()
            if self.stop_current_task:
                return None, ""

            snapshot = ProcessSupervisor.find_process_snapshot(configured_name, exact_name=True)

            if snapshot is not None:
                runtime_processes["game"] = snapshot
                self._log_detected_process("游戏进程", snapshot, source="精确进程名")
                remember_process_snapshot(app_name, "game", snapshot, script_choice=script_choice, logger=self.logger)
                return snapshot, ""

            # 更新感知：约每 10 秒查一次。更新中则按 update 预算等（等完照常跑日常），
            # 更新行停滞超 180 秒则回到 launch 预算收尾；update 未配置才直接跳过。
            polls += 1
            if progress_hint is not None and polls % 5 == 0:
                try:
                    if progress_hint():
                        last_update_seen = now
                        if update_timeout <= 0:
                            self.logger.warning(f"[{app_name}] 检测到游戏正在更新，跳过本次等待")
                            return None, "updating"
                        if not updating_announced:
                            self.logger.info(
                                f"[{app_name}] 游戏正在更新，按更新预算等待"
                                f"（最长 {update_timeout // 60} 分钟），更新完继续跑")
                            updating_announced = True
                except Exception:
                    pass

            updating = last_update_seen and (now - last_update_seen) <= 180
            limit = update_timeout if updating else timeout
            if now - start >= limit:
                break

            elapsed = int(now - start)
            remaining = int(limit - (now - start))
            if time.time() - last_log >= 2:
                if updating:
                    self.logger.debug(f"等待游戏更新完成... (已等待: {elapsed}s，更新预算剩余: {remaining}s)")
                else:
                    self.logger.debug(f"等待游戏进程中... (已等待: {elapsed}s, 剩余: {remaining}s)")
                last_log = time.time()
            time.sleep(2)

        # 预算耗尽时如果更新行还新鲜（超大更新超过 update 上限），记跳过而非失败，
        # 免得失败重试再烧几个小时
        if last_update_seen and (time.time() - last_update_seen) <= 180:
            self.logger.warning(f"[{app_name}] 更新预算耗尽仍未见游戏进程，本次跳过")
            return None, "updating"
        self.logger.warning(f"未检测到目标游戏进程 (等待超时: {timeout}秒)")
        return None, ""

    def _kill_processes_by_name(
        self,
        process_name: str,
        reason: str,
        *,
        exact_name: bool = True,
        skip_pids: Optional[set] = None,
        cmd_pattern: Optional[str] = None,
    ):
        if not process_name:
            return

        skip_pids = skip_pids or set()
        matched = ProcessSupervisor.find_processes(process_name, cmd_pattern, exact_name=exact_name)
        if not matched:
            return

        for proc in matched:
            if proc.pid in skip_pids:
                continue
            try:
                proc_name = proc.name()
            except psutil.NoSuchProcess:
                continue
            self.logger.info(f"{reason}: {proc_name} (PID: {proc.pid})")
            ProcessSupervisor.safe_kill_process(proc.pid)

    def _run_prelaunch_flow(self, config: dict, game_display: str) -> Tuple[bool, str]:
        flow_key = config.get("prelaunch_flow")
        if not flow_key:
            return True, ""

        self.logger.info(f"[{game_display}] 执行前置启动流程: {flow_key}")
        ok, msg = run_flow(self.logger, flow_key, stop_check=lambda: self.stop_current_task)
        if ok:
            self.logger.info(f"[{game_display}] 前置启动流程完成: {msg}")
        else:
            self.logger.error(f"[{game_display}] 前置启动流程失败: {msg}")
        return ok, msg

    def _ok_ww_daily_completed(self) -> bool:
        """ok-ww（鸣潮）今日日常是否已完成：当天脚本日志有 Successfully Executed Task。

        ok-ww 日常 2-4 分钟即可跑完（账号已清空时更快），全局 MIN_RUN_TIME=300s 的
        "运行时间过短"判定对它全是误报（2026-10-23 实例：3 次运行全部实际完成，
        却被记了两次失败）。解析器与 Bridge 日报同源（ok_ww_daily_parser），
        今天没跑过或读取异常都返回 False，按原时长判定（保守，不放大成功面）。
        """
        try:
            from ..services.ok_ww_daily_parser import parse_latest_ok_ww_log

            data = parse_latest_ok_ww_log()
            return bool(data and data.get("is_all_success"))
        except Exception as exc:
            self.logger.warning(f"[鸣潮] ok-ww 日志复核失败，按原时长判定: {exc}")
            return False

    def _resolve_script_choice(self, app_name: str, config: dict, script_choice: Optional[str]) -> Optional[str]:
        if script_choice:
            return script_choice
        if "launcher_options" not in config:
            return None
        saved_choice = get_saved_script_choice(app_name)
        if saved_choice and any(opt.get("name") == saved_choice for opt in config.get("launcher_options", [])):
            return saved_choice
        return None

    def _find_visible_window_by_process_names(self, process_names: List[str]) -> Optional[int]:
        names = {(name or "").strip().lower() for name in process_names if str(name).strip()}
        if not names:
            return None

        candidates = []
        for area, hwnd, pid in self._visible_window_candidates():
            try:
                proc_name = psutil.Process(pid).name().lower()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            if proc_name in names:
                candidates.append((area, hwnd))
        if not candidates:
            return None

        candidates.sort(reverse=True)
        return candidates[0][1]

    def _wait_game_window_ready(self, config: dict, game_display: str,
                                timeout: int = 120, min_area: int = 307200) -> bool:
        """拉脚本前等游戏窗口稳定（防脚本误判游戏没起而重复启动客户端）。

        条件：游戏进程名下出现面积达标的可见窗口，且句柄+位置连续 2 轮不变。
        超时返回 True（不断路，保持旧行为）；用户停止返回 False。
        """
        proc_name = self._configured_game_process_name(config)
        if not proc_name:
            return True
        self.logger.info(f"[{game_display}] 等游戏窗口就绪再拉脚本（防重复启动客户端，超时{timeout}s）")
        last_key = None
        stable = 0
        end = time.time() + max(5, int(timeout))
        while time.time() < end:
            if self.stop_current_task:
                return False
            key = None
            hwnd = self._find_visible_window_by_process_names([proc_name])
            if hwnd:
                try:
                    l, t, r, b = win32gui.GetWindowRect(hwnd)
                    if max(0, r - l) * max(0, b - t) >= max(1, int(min_area)):
                        key = (hwnd, l, t, r, b)
                except Exception:
                    key = None
            if key is not None and key == last_key:
                stable += 1
                if stable >= 2:
                    self.logger.info(f"[{game_display}] 游戏窗口已稳定，可以拉脚本了")
                    return True
            else:
                stable = 0
                last_key = key
            time.sleep(1.0)
        self.logger.warning(f"[{game_display}] 游戏窗口未就绪（超时{timeout}s），仍继续拉脚本")
        return True

    def _find_best_visible_window(self, ignored_names: Optional[set] = None) -> Optional[int]:
        ignored_names = {str(name).strip().lower() for name in (ignored_names or set()) if str(name).strip()}
        for _, hwnd, pid in self._visible_window_candidates():
            snapshot = ProcessSupervisor.snapshot_pid(pid)
            if snapshot is None:
                continue
            if snapshot.name.lower() in ignored_names:
                continue
            return hwnd
        return None

    def _click_game_window_after_launch(
        self,
        config: dict,
        game_display: str,
        *,
        preferred_snapshot: Optional[ProcessSnapshot] = None,
        click_reason: str = "脚本启动后尝试点回游戏窗口，避免脚本遮挡",
    ):
        if not config.get("post_launch_click_game"):
            return

        raw_process_names = config.get("post_launch_click_process_names") or [config.get("game", {}).get("process_name", "")]
        process_names = [
            str(name).strip()
            for name in raw_process_names
            if str(name).strip() and not self._is_wrapper_process_name(str(name).strip())
        ]
        timeout = max(1.0, float(config.get("post_launch_click_timeout", 8)))
        end_time = time.time() + timeout
        hwnd = None
        ignored_names = {
            str(config.get("script_process_name", "") or "").strip().lower(),
            os.path.basename(str(config.get("launcher", "") or "")).strip().lower(),
            *WRAPPER_PROCESS_NAMES,
        }

        self.logger.info(f"[{game_display}] {click_reason}")
        while time.time() < end_time:
            if preferred_snapshot is not None and not self._is_wrapper_snapshot(preferred_snapshot):
                hwnd = self._find_visible_window_by_pid(preferred_snapshot.pid)
            if not hwnd and process_names:
                hwnd = self._find_visible_window_by_process_names(process_names)
            if not hwnd:
                hwnd = self._find_best_visible_window(ignored_names)
            if hwnd:
                break
            time.sleep(0.4)

        if not hwnd:
            self.logger.warning(f"[{game_display}] 未找到可点击的游戏窗口，跳过防遮挡点击")
            return

        try:
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            click_x = left + max(1, (right - left) // 2)
            click_y = top + max(1, (bottom - top) // 2)
            original_pos = None

            try:
                original_pos = pyautogui.position()
            except Exception:
                original_pos = None

            activated = self._activate_window(hwnd)
            time.sleep(0.35)
            pyautogui.click(click_x, click_y, button="left")

            if original_pos is not None:
                pyautogui.moveTo(original_pos.x, original_pos.y, duration=0)

            if not activated and preferred_snapshot is not None:
                self.logger.warning(f"[{game_display}] 置前游戏窗口未确认成功，但已尝试直接点击窗口中心")
            self.logger.info(f"[{game_display}] 已点击游戏窗口中心，防止脚本界面遮挡")
        except Exception as e:
            self.logger.warning(f"[{game_display}] 防遮挡点击失败: {str(e)}")

    def _launch_normal(
        self,
        config: dict,
        script_type: str,
        *,
        launch_started_at: Optional[float] = None,
        game_display: str = "",
    ) -> Tuple[bool, Optional[int]]:
        script_pid = None
        try:
            start_time = float(launch_started_at or time.time())
            if "launcher_options" in config:
                launcher = config.get("launcher", config["launcher_options"][0]["launcher"])
                params = config.get("launcher_params", config["launcher_options"][0]["launcher_params"])
            else:
                launcher = config["launcher"]
                params = config.get("launcher_params", [])
           
            self.logger.debug(f"启动程序: {launcher} {params} (类型: {script_type})")
           
            param_str = ' '.join(f'"{p}"' if ' ' in p else p for p in params) if params else ''
            exe_dir = os.path.dirname(launcher)
            self.logger.debug(f"启动工作目录: {exe_dir}")
            result = ctypes.windll.shell32.ShellExecuteW(
                None, "open", launcher, param_str, exe_dir, 1
            )
            if result <= 32:
                self.logger.error(f"ShellExecuteW 失败 (错误码: {result})")
                return False, None
            self.logger.info(f"程序通过 ShellExecuteW 启动成功 (工作目录: {exe_dir})")
           
            launcher_name = os.path.basename(launcher)
            is_script_file = launcher_name.lower().endswith((".bat", ".cmd", ".py"))
            while time.time() - start_time < 10:
                if is_script_file:
                    # 仅接受 launch 之后新出现的 cmd/python，避免误抓长期后台进程
                    if proc := (ProcessSupervisor.find_process("cmd.exe") or ProcessSupervisor.find_process("python.exe")):
                        if proc.create_time() < start_time - 1:
                            time.sleep(0.5)
                            continue
                        script_pid = proc.pid
                        snapshot = ProcessSupervisor.snapshot_pid(script_pid)
                        if snapshot is not None:
                            self.logger.info(
                                f"脚本子进程启动成功 (类型: {script_type}): {ProcessSupervisor.describe_process(snapshot)}"
                            )
                        else:
                            self.logger.info(f"脚本子进程启动成功 PID: {script_pid} (类型: {script_type})")
                        break
                else:
                    if proc := ProcessSupervisor.find_process(launcher_name):
                        script_pid = proc.pid
                        snapshot = ProcessSupervisor.snapshot_process(proc)
                        if snapshot is not None:
                            self.logger.info(f"脚本进程启动成功: {ProcessSupervisor.describe_process(snapshot)}")
                        else:
                            self.logger.info(f"脚本进程启动成功 PID: {script_pid}")
                        break
                time.sleep(0.5)
            if not script_pid:
                self.logger.warning("未找到精确脚本进程 PID，将继续但终止时使用通用匹配")

            if config.get("post_launch_click_before_wait") and game_display:
                early_delay = max(0.2, float(config.get("post_launch_click_initial_delay_sec", 1.5) or 1.5))
                self.logger.info(f"[{game_display}] 脚本刚启动，{early_delay:.1f} 秒后提前切回游戏窗口")
                time.sleep(early_delay)
                self._click_game_window_after_launch(
                    config,
                    game_display,
                    click_reason="脚本刚启动，提前尝试点回游戏窗口",
                )
           
            initial_wait = config.get("initial_wait", 20)
            self.logger.debug(f"程序已启动，等待 {initial_wait} 秒初始化")
            time.sleep(initial_wait)
            return True, script_pid
        except Exception as e:
            self.logger.error(f"启动失败: {str(e)}", exc_info=DEBUG_MODE)
            return False, None

    def _launch_special(self, config: dict) -> Tuple[bool, Optional[int]]:
        try:
            self.logger.debug(f"启动特殊程序: {config['launcher']}")
            subprocess.Popen(
                f'cmd /c start "" "{config['launcher']}"',
                shell=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW
            )
        except Exception as e:
            self.logger.error(f"特殊程序启动失败: {str(e)}", exc_info=DEBUG_MODE)
            return False, None
        start_time = time.time()
        timeout = config['timeouts']['launch']
       
        while time.time() - start_time < timeout:
            if pid := WindowDetector.find_window_process(config["launcher_profile"]["window_title"]):
                self.logger.info(f"窗口检测成功 PID: {pid}")
                return True, pid
            for pattern in config["launcher_profile"]["process_patterns"]:
                if proc := ProcessSupervisor.find_process(pattern["name"], pattern.get("cmdline")):
                    self.logger.info(f"进程匹配成功 PID: {proc.pid}")
                    return True, proc.pid
           
            elapsed = int(time.time() - start_time)
            remaining = timeout - elapsed
            self.logger.debug(f"等待目标程序启动... (已等待: {elapsed}s, 剩余: {remaining}s)")
            time.sleep(CHECK_INTERVAL)
           
            if self.stop_current_task:
                self.logger.info("检测到终止请求，停止启动过程")
                return False, None
        self.logger.warning(f"未检测到目标窗口或进程 (等待超时: {timeout}秒)")
        return False, None

    def _monitor_game(
        self,
        app_name: str,
        config: dict,
        script_pid: Optional[int] = None,
        *,
        launch_started_at: Optional[float] = None,
        launch_config: Optional[dict] = None,
        runtime_processes: Optional[Dict[str, ProcessSnapshot]] = None,
        script_choice: Optional[str] = None,
    ) -> Tuple[bool, int, str]:
        runtime_processes = runtime_processes if runtime_processes is not None else {}
        # 游戏收到关闭指令后若优雅关闭过久，超过宽限则强制结束（避免进程在后台残留）。默认开启。
        force_kill_game_on_close = bool(config.get("force_kill_game_on_close", True))
        game_close_grace = max(1, int(config.get("game_close_grace_sec", GAME_CLOSE_GRACE_SEC)))
        launch_started_at = float(launch_started_at or time.time())
        launch_config = launch_config or config
        game_display = get_app_display_name(app_name)
        focus_guard_deadline = time.time() + max(0.0, float(launch_config.get("post_launch_focus_guard_sec", 0) or 0.0))
        focus_retry_interval = max(1.0, float(launch_config.get("post_launch_focus_retry_interval_sec", 2.0) or 2.0))
        last_focus_retry = 0.0
       
        start_time = time.time()
        # 异环：ok-nte 日志出现更新行时不等超时，直接跳过（更新日等俩小时没意义）
        # 米游三件套：官方公告显示当前在维护窗口内时同样快速跳过；桥接层会按公告
        # 开服时间（+5 分钟）安排动态补跑，不再靠固定 11:05 猜。
        progress_hint = None
        if app_name == "NevernessToEverness":
            try:
                from ..services.ok_nte_daily_parser import is_update_in_progress
                from ..services.game_update_calendar import get_maintenance_state

                def _nte_update_hint():
                    if is_update_in_progress():
                        return True
                    return bool(get_maintenance_state(app_name))

                progress_hint = _nte_update_hint
            except Exception:
                progress_hint = None
        elif app_name in ("GenshinImpact", "HonkaiStarRail", "ZenlessZoneZero", "WutheringWaves", "InfinityNikki"):
            try:
                from ..services.game_update_calendar import get_maintenance_state

                def _mihoyo_maintenance_hint():
                    state = get_maintenance_state(app_name)
                    if not state:
                        return False
                    open_dt = state.get("open_at_dt")
                    open_txt = open_dt.strftime("%H:%M") if open_dt else "?"
                    self.logger.info(
                        f"[{app_name}] 官方公告维护中"
                        f"（{state.get('version') or '版本更新'}），预计 {open_txt} 开服，"
                        f"本次跳过等待"
                    )
                    return True

                progress_hint = _mihoyo_maintenance_hint
            except Exception:
                progress_hint = None
        game_snapshot, wait_reason = self._wait_for_game_process_snapshot(
            app_name,
            config,
            launch_config,
            launch_started_at,
            runtime_processes,
            script_choice=script_choice,
            progress_hint=progress_hint,
        )
        if game_snapshot is None:
            if self.stop_current_task:
                self._kill_related_processes(config, script_pid, runtime_processes=runtime_processes)
                return False, 0, "user_stop"
            if wait_reason == "updating":
                self._kill_related_processes(config, script_pid, runtime_processes=runtime_processes)
                return False, 0, "updating"
            configured_name = str(config.get("game", {}).get("process_name", "") or "").strip()
            if configured_name:
                self.logger.error(f"进程未启动: {configured_name}")
            else:
                self.logger.error("自动识别游戏进程失败")
            self._kill_related_processes(config, script_pid, runtime_processes=runtime_processes)
            return False, 0, "launch_timeout"

        if launch_config.get("post_launch_click_game"):
            self._click_game_window_after_launch(
                launch_config,
                game_display,
                preferred_snapshot=game_snapshot,
                click_reason="检测到游戏进程后，再次尝试切回游戏窗口",
            )
        last_log = start_time
        monitor_timeout = config["timeouts"]["monitor"]
        game_closing_since = 0.0
        game_window_seen = False

        # 脚本报错监控：脚本（如 March7th Assistant）任务失败后可能停留在界面不再退出，
        # 若只等游戏退出会空转到监控超时。通过日志关键字提前止损。
        error_watchers = build_script_error_watchers(config, self.logger)
        for watcher in error_watchers:
            self.logger.info(
                f"[脚本日志监控] 开始监控 {watcher.name} 报错日志: {watcher.log_dir} "
                f"(关键字: {'、'.join(watcher.error_keywords)})"
            )

        while time.time() - start_time < monitor_timeout:
            if self.stop_current_task:
                self.logger.info("检测到快捷键终止请求，结束当前任务")
                self._kill_related_processes(config, script_pid, runtime_processes=runtime_processes)
                duration = int(time.time() - start_time)
                return False, duration, "user_stop"

            for watcher in error_watchers:
                error_line = watcher.poll_new_errors()
                if error_line:
                    self.logger.error(
                        f"[{game_display}] [{watcher.name}] 检测到脚本报错，提前结束任务: {error_line[:160]}"
                    )
                    self._kill_related_processes(
                        config,
                        script_pid=script_pid,
                        include_game=True,
                        include_script=True,
                        runtime_processes=runtime_processes,
                    )
                    duration = int(time.time() - start_time)
                    return False, duration, "script_error"

            current_game = runtime_processes.get("game")
            if current_game and not self._is_pid_running(current_game.pid):
                replacement = ProcessSupervisor.find_process_snapshot(current_game.name, exact_name=True)
                if replacement is not None and replacement.pid != current_game.pid:
                    runtime_processes["game"] = replacement
                    self._log_detected_process("游戏进程", replacement, source="PID切换后重新识别")
                    remember_process_snapshot(app_name, "game", replacement, script_choice=script_choice, logger=self.logger)
                    current_game = replacement

            # 游戏关闭宽限优化：仅当游戏曾可见、当前整个进程族已无可见窗口但进程仍存活时，
            # 才认为收到关闭指令后正在优雅关闭（存盘/清理）。超过宽限仍不退出则强制结束游戏与脚本进程树，
            # 避免进程在后台残留很久。启动期尚未出窗口（game_window_seen=False）不触发，防止误杀正在启动的游戏。
            duration = int(time.time() - start_time)
            if current_game and self._is_pid_running(current_game.pid):
                has_window = self._game_family_has_visible_window(current_game.pid)
                if has_window:
                    if game_closing_since != 0.0:
                        self.logger.info(f"[{game_display}] 游戏窗口再次出现，取消关闭宽限")
                    game_closing_since = 0.0
                    game_window_seen = True
                elif game_window_seen and force_kill_game_on_close:
                    if game_closing_since == 0.0:
                        game_closing_since = time.time()
                        self.logger.info(
                            f"[{game_display}] 检测到游戏窗口已关闭，进入关闭宽限期 "
                            f"({game_close_grace}s)，超时将强制结束进程"
                        )
                    elif time.time() - game_closing_since >= game_close_grace:
                        self.logger.warning(
                            f"[{game_display}] 游戏收到关闭指令后超过 {game_close_grace}s 仍未退出，"
                            f"强制结束游戏与脚本进程树"
                        )
                        self._kill_related_processes(
                            config,
                            script_pid=script_pid,
                            include_game=True,
                            include_script=True,
                            runtime_processes=runtime_processes,
                        )
                        return duration >= MIN_RUN_TIME, duration, "game_closing_forced"

            if current_game and launch_config.get("post_launch_click_game") and time.time() < focus_guard_deadline:
                foreground_pid = self._foreground_window_pid()
                if foreground_pid != current_game.pid and time.time() - last_focus_retry >= focus_retry_interval:
                    self._click_game_window_after_launch(
                        launch_config,
                        game_display,
                        preferred_snapshot=current_game,
                        click_reason="脚本启动后的焦点守护，尝试再次切回游戏窗口",
                    )
                    last_focus_retry = time.time()

            if not current_game or not self._is_pid_running(current_game.pid):
                duration = int(time.time() - start_time)
                self.logger.info(f"游戏进程正常退出 (运行时间: {duration//60}分{duration%60}秒)")
                self._kill_related_processes(
                    config,
                    script_pid,
                    include_game=False,
                    runtime_processes=runtime_processes,
                )
                return True, duration, "game_exited"
           
            if time.time() - last_log > 300:
                elapsed = int((time.time() - start_time) / 60)
                self.logger.info(f"游戏运行中 (已运行: {elapsed}分钟)")
                last_log = time.time()

            # 任务线程心跳：本循环每轮上报，看门狗据此判断任务线程是否卡死
            self._heartbeat.beat()

            time.sleep(CHECK_INTERVAL)
        duration = int(time.time() - start_time)
        self.logger.warning(f"游戏监控超时 (运行: {duration//60}分{duration%60}秒)")
        self._kill_related_processes(config, script_pid, runtime_processes=runtime_processes)
        return False, duration, "timeout"

    def _kill_related_processes(
        self,
        config: dict,
        script_pid: Optional[int] = None,
        *,
        include_game: bool = True,
        include_script: bool = True,
        runtime_processes: Optional[Dict[str, ProcessSnapshot]] = None,
    ):
        runtime_processes = runtime_processes or {}
        game_process = self._configured_game_process_name(config)
        game_snapshot = runtime_processes.get("game")
        script_snapshot = runtime_processes.get("script")
        killed_pids = set()

        if include_game and game_snapshot is not None and game_snapshot.pid != os.getpid() and not self._is_wrapper_snapshot(game_snapshot):
            if self._is_pid_running(game_snapshot.pid):
                self.logger.info(f"强制终止自动识别游戏进程: {ProcessSupervisor.describe_process(game_snapshot)}")
                ProcessSupervisor.safe_kill_process(game_snapshot.pid)
                killed_pids.add(game_snapshot.pid)
        elif include_game and game_snapshot is not None and self._is_wrapper_snapshot(game_snapshot):
            self.logger.warning(f"检测到包装器被标记为游戏进程，跳过回收以避免误杀: {ProcessSupervisor.describe_process(game_snapshot)}")

        if include_game and game_process:
            self._kill_processes_by_name(
                game_process,
                f"强制终止游戏进程: {game_process}",
                exact_name=True,
                skip_pids=killed_pids,
            )

        if include_script and script_pid:
            current_pid = os.getpid()
            if script_pid == current_pid:
                self.logger.warning(f"检测到脚本PID与当前主进程相同，跳过终止以防程序退出 (PID: {script_pid})")
            else:
                self.logger.info(f"强制终止脚本进程 PID: {script_pid}")
                ProcessSupervisor.safe_kill_process(script_pid)

        # 额外杀 launcher 主进程（彻底清理）
        if "launcher" in config:
            launcher_name = os.path.basename(config["launcher"])
            self._kill_processes_by_name(
                launcher_name,
                f"额外终止启动器主进程: {launcher_name}",
                exact_name=True,
                skip_pids={os.getpid(), *killed_pids},
            )

        for process_name in config.get("cleanup_processes", []):
            self._kill_processes_by_name(
                process_name,
                f"终止附加清理进程: {process_name}",
                exact_name=True,
                skip_pids={os.getpid(), *killed_pids},
            )

        # 按命令行补刀：包装器提前退出后，按名杀不到真正的引擎子进程
        # （如 ok-nte.exe 包装器死后残留的 pythonw 引擎），用 {name, cmdline} 精确匹配。
        # 自身进程永远跳过（skip_pids 含 os.getpid()），cmdline 必须带任务特征串，
        # 禁止只写 pythonw.exe 这种裸名。
        for pattern in config.get("cleanup_cmd_patterns", []) or []:
            if not isinstance(pattern, dict):
                continue
            name = str(pattern.get("name") or "").strip()
            cmd = str(pattern.get("cmdline") or "").strip()
            if not name or not cmd:
                continue
            self._kill_processes_by_name(
                name,
                f"终止附加清理进程(命令行匹配 {cmd}): {name}",
                exact_name=True,
                skip_pids={os.getpid(), *killed_pids},
                cmd_pattern=cmd,
            )

        # special 类型处理
        if config.get("type") == "special":
            for pattern in config.get("launcher_profile", {}).get("process_patterns", []):
                self._kill_processes_by_name(
                    pattern["name"],
                    f"强制终止启动器进程: {pattern['name']}",
                    exact_name=False,
                )
        # normal 类型处理
        elif include_script and config.get("type") in {"normal", "script"}:
            script_name = config.get("script_process_name", os.path.basename(config.get("launcher", "")))
            if script_snapshot is not None and script_snapshot.name:
                script_name = script_snapshot.name
            self._kill_processes_by_name(
                script_name,
                f"强制终止脚本进程: {script_name}",
                exact_name=True,
                skip_pids={os.getpid(), *killed_pids},
            )

        # 收尾阶段扫杀游戏关闭后弹出的原生启动器/更新窗（其进程不在游戏树内，会干扰下一个脚本）
        self._sweep_post_close_launcher(config, runtime_processes)

    def _sweep_post_close_launcher(
        self,
        config: dict,
        runtime_processes: Optional[Dict[str, ProcessSnapshot]] = None,
    ) -> int:
        """游戏关闭后某些游戏会重新弹出原生启动器/更新窗，该进程不在游戏进程树内，
        若残留会抢焦点/挡点击，干扰下一个脚本。此处统一扫杀这类残留窗口与进程。

        持续轮询 wait 秒（默认 POST_CLOSE_LAUNCHER_WAIT），覆盖'游戏关掉后过几秒才弹窗'的延迟场景。
        命中信号（任一即杀）：
        1) 配置里的启动器主进程名（config.launcher / launcher_options[*].launcher）与显式 close_launcher_processes；
        2) 与游戏 exe 同目录、且进程名含 launcher 的兄弟进程；
        3) 与游戏 exe 同目录、且非游戏/非脚本/非包装器的可见窗口（兜底：任意残留启动器/更新窗）。
        """
        if not config.get("sweep_post_close_launcher", True):
            return 0
        wait = float(config.get("post_close_launcher_wait", POST_CLOSE_LAUNCHER_WAIT))
        deadline = time.time() + max(0.0, wait)
        total = 0
        while time.time() < deadline:
            total += self._sweep_post_close_launcher_once(config, runtime_processes)
            if time.time() < deadline:
                time.sleep(1)
        return total

    def _sweep_post_close_launcher_once(
        self,
        config: dict,
        runtime_processes: Optional[Dict[str, ProcessSnapshot]] = None,
    ) -> int:
        """单次扫杀：按候选名/同目录窗口清掉残留启动器，返回本次杀死数量。
        供收尾阶段持续轮询与任务间隔冷却期复用。"""
        if not config.get("sweep_post_close_launcher", True):
            return 0

        runtime_processes = runtime_processes or {}
        game_snap = runtime_processes.get("game")
        game_exe = (getattr(game_snap, "exe_path", None)
                    or (config.get("game") or {}).get("exe_path")
                    or "")
        game_dir = os.path.dirname(game_exe).lower() if game_exe else ""
        game_name = (game_snap.name if game_snap else "") or self._configured_game_process_name(config)
        game_name = (game_name or "").lower()
        script_snap = runtime_processes.get("script")
        script_name = (script_snap.name if script_snap else "") or config.get("script_process_name", "")
        script_name = (script_name or "").lower()

        candidate_names: set = set()
        for src in [config.get("launcher")] + [o.get("launcher") for o in config.get("launcher_options", []) or []]:
            if src:
                b = os.path.basename(str(src)).strip().lower()
                if b:
                    candidate_names.add(b)
        for p in config.get("close_launcher_processes", []) or []:
            if p:
                candidate_names.add(str(p).strip().lower())
        if game_dir and os.path.isdir(game_dir):
            try:
                for fn in os.listdir(game_dir):
                    fl = fn.lower()
                    if "launcher" in fl and fl.endswith(".exe"):
                        candidate_names.add(fl)
            except OSError:
                pass

        # 既无候选名又无游戏目录可扫，直接返回避免空转
        if not candidate_names and not game_dir:
            return 0

        myself = os.getpid()
        killed: set = set()

        # 1) 按候选名杀残留启动器进程
        for name in list(candidate_names):
            try:
                snap = ProcessSupervisor.find_process_snapshot(name, exact_name=True)
            except Exception:
                snap = None
            if snap and snap.pid != myself and not self._is_wrapper_snapshot(snap):
                self.logger.info(f"收尾阶段终止残留启动器进程: {ProcessSupervisor.describe_process(snap)}")
                ProcessSupervisor.safe_kill_process(snap.pid)
                killed.add(snap.pid)

        # 2) 枚举游戏目录下的可见窗口（兜底：任意启动器/更新窗）
        if game_dir:
            for _, hwnd, pid in self._visible_window_candidates():
                if pid in killed or pid == myself:
                    continue
                try:
                    proc = psutil.Process(pid)
                    pname = proc.name()
                    ppath = proc.exe() or ""
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
                if self._is_wrapper_process_name(pname):
                    continue
                if game_name and pname.lower() == game_name:
                    continue
                if script_name and pname.lower() == script_name:
                    continue
                if os.path.dirname(ppath).lower() == game_dir:
                    self.logger.info(f"收尾阶段关闭游戏目录残留窗口(启动器): {pname} (PID: {pid})")
                    ProcessSupervisor.safe_kill_process(pid)
                    killed.add(pid)

        return len(killed)

    def _cooldown_before_next_task(self, app_key: str, delay_sec: float) -> None:
        """上一个游戏任务收尾后、下一个脚本启动前的静默冷却期。

        在等待期间持续监测并按名/同目录窗口清掉慢关游戏弹出的启动器，
        避免其抢焦点/挡点击干扰下一个脚本；用户请求停止则提前结束。
        """
        if delay_sec <= 0:
            return

        app_cfg = (APPS or {}).get(app_key) or {}
        self.logger.info(f"[{app_key}] 任务间隔冷却 {int(delay_sec)} 秒，期间持续清理残留启动器窗口")
        elapsed = 0.0
        while elapsed < delay_sec:
            if self.stop_current_task:
                self.logger.info(f"[{app_key}] 冷却期间用户请求停止，提前结束间隔")
                break
            self._sweep_post_close_launcher_once(app_cfg, {})
            time.sleep(1)
            elapsed += 1.0

    def execute_task(self, app_name: str, script_choice: str = None) -> Tuple[bool, str, int]:
        original_app_name = app_name
        app_name = resolve_app_key(app_name)
        config = APPS.get(app_name)
        if not config:
            self.logger.error(f"无效的应用名称: {app_name}")
            return False, "无效配置", 0
        if original_app_name != app_name:
            self.logger.info(f"应用名称已重定向: {original_app_name} -> {app_name}")
        game_display = get_app_display_name(app_name)
        script_choice = self._resolve_script_choice(app_name, config, script_choice)
        script_display = get_script_display_name(app_name, script_choice)
        self.logger.info(
            f"[任务] enter app={app_name} display={game_display} script={script_display} pid={os.getpid()}"
        )
        
        script_pid = None
        runtime_processes: Dict[str, ProcessSnapshot] = {}
        # 任务线程心跳看门狗：卡死时 dump 栈 + 置停止标志 + 杀进程树
        def _on_task_stall(task_key: str, task_label: str) -> None:
            self.logger.error(
                f"[{game_display}] 任务线程心跳超时，强制结束任务"
                "（现场栈已写入 watchdog_task_heartbeat.log）"
            )
            self.stop_current_task = True
            try:
                self._kill_related_processes(
                    config, script_pid, runtime_processes=runtime_processes
                )
            except Exception as exc:
                self.logger.warning(f"[{game_display}] 看门狗止损杀进程失败: {exc}")

        self._heartbeat.start(
            app_name, _on_task_stall, task_label=f"[{game_display}] {script_display}"
        )
        try:
            self.logger.info(f"\n{'='*40}\n开始执行 [{game_display}] 任务 (脚本: {script_display})\n{'='*40}")
            self.stop_current_task = False
            self._cleanup_key_listener()

            # 终止已有游戏进程
            configured_game_process = self._configured_game_process_name(config)
            if configured_game_process:
                self._kill_processes_by_name(
                    configured_game_process,
                    f"终止已有进程: {configured_game_process}",
                    exact_name=True,
                )
            else:
                raw_game_process = str(config.get("game", {}).get("process_name", "") or "").strip()
                if raw_game_process:
                    self.logger.warning(f"[{game_display}] 当前 game.process_name 为包装器进程 {raw_game_process}，跳过启动前清理以避免误杀")

            # 终止已有脚本进程（避免 ok-nte 等工具因旧进程残留导致更新失败/无法启动）
            script_process_name = str(config.get("script_process_name", "") or "").strip()
            if script_process_name and not self._is_wrapper_process_name(script_process_name):
                self._kill_processes_by_name(
                    script_process_name,
                    f"终止已有脚本进程: {script_process_name}",
                    exact_name=True,
                )
            elif script_process_name:
                self.logger.warning(f"[{game_display}] 脚本进程 {script_process_name} 为包装器进程，跳过启动前清理以避免误杀")

            prelaunch_ok, prelaunch_message = self._run_prelaunch_flow(config, game_display)
            if not prelaunch_ok:
                self._kill_related_processes(config, include_script=False)
                return False, f"前置启动失败: {prelaunch_message}", 0

            # 拉脚本前先等游戏窗口稳定，防止脚本误判游戏没起而重复启动客户端
            # （只对配了 wait_game_window_before_script 的游戏生效，默认关闭）
            if config.get("wait_game_window_before_script", False):
                if not self._wait_game_window_ready(
                    config, game_display,
                    timeout=int(config.get("game_window_wait_sec", 120)),
                    min_area=int(config.get("game_window_min_area", 307200)),
                ):
                    return False, "用户终止", 0

            # 定义 script_type（必须在这里定义！）
            script_type = config.get("type", "normal")
            launch_success = False
            script_pid = None
            launch_config = config
            launch_started_at = time.time()

            if "launcher_options" in config:
                if not script_choice:
                    script_choice = config["launcher_options"][0]["name"]
                script_config = next((opt for opt in config["launcher_options"] if opt["name"] == script_choice), None)
                if not script_config:
                    self.logger.error(f"[{game_display}] 无效脚本配置: {script_choice}")
                    return False, "无效脚本配置", 0
                temp_config = {**config, "launcher": script_config["launcher"], "launcher_params": script_config["launcher_params"]}
                if "script_process_name" in script_config:
                    temp_config["script_process_name"] = script_config["script_process_name"]
                script_type = script_config["type"]
                launch_config = temp_config
                launch_success, script_pid = self._launch_normal(
                    temp_config,
                    script_type,
                    launch_started_at=launch_started_at,
                    game_display=game_display,
                )
            else:
                launch_success, script_pid = self._launch_normal(
                    config,
                    script_type,
                    launch_started_at=launch_started_at,
                    game_display=game_display,
                )

            if not launch_success:
                self.logger.error(f"[{game_display}] 启动失败")
                self._kill_related_processes(config, include_script=False, runtime_processes=runtime_processes)
                return False, "启动失败", 0

            if script_pid:
                script_snapshot = ProcessSupervisor.snapshot_pid(script_pid)
                preferred_script_snapshot = self._log_script_process_context(script_snapshot, launch_started_at)
                if preferred_script_snapshot is not None:
                    runtime_processes["script"] = preferred_script_snapshot
                    remember_process_snapshot(
                        app_name,
                        "script",
                        preferred_script_snapshot,
                        script_choice=script_choice,
                        logger=self.logger,
                    )

            self._click_game_window_after_launch(launch_config, game_display)

            if launch_config.get("post_launch_ocr_flow") == "nte_start":
                self.logger.info(f"[{game_display}] 启动后 OCR 点击启动 ok-nte 任务...")
                run_nte_post_launch_ocr_click(launch_config, self.logger)

            if launch_config.get("post_launch_ingame_flow") == "baijing":
                from .baijing_flow import run_baijing_full_flow
                self.logger.info(f"[{game_display}] 启动后执行白荆回廊一条龙（进入游戏→日常→验证截图→周常→关闭）...")
                ok_full = run_baijing_full_flow(launch_config, self.logger, controller=self)
                # 白荆一条龙已含关闭，跳过 _monitor_game 的长轮询，直接按时长判定
                duration = int(time.time() - launch_started_at)
                # 兜底清理残留（full_flow 已尽力关闭，这里仅清 script/launcher）
                try:
                    self._kill_related_processes(config, script_pid, runtime_processes=runtime_processes)
                except Exception:
                    pass
                if self.stop_current_task:
                    status = "用户终止"
                    self.logger.info(f"\n{'='*40}\n[{game_display}] 任务结束: {status} (脚本: {script_display}, 运行时间: {duration//60}分{duration%60}秒)\n{'='*40}")
                    return False, status, duration
                if not ok_full:
                    status = "启动失败"
                    self.logger.info(f"\n{'='*40}\n[{game_display}] 任务结束: {status} (脚本: {script_display}, 运行时间: {duration//60}分{duration%60}秒)\n{'='*40}")
                    return False, status, duration
                if duration < MIN_RUN_TIME:
                    status = f"运行时间过短(小于{MIN_RUN_TIME//60}分钟)"
                else:
                    status = "成功"
                self.logger.info(f"\n{'='*40}\n[{game_display}] 任务结束: {status} (脚本: {script_display}, 运行时间: {duration//60}分{duration%60}秒)\n{'='*40}")
                success = duration >= MIN_RUN_TIME and not self.stop_current_task
                return success, status, duration

            # 监控游戏进程
            monitor_result, duration, end_reason = self._monitor_game(
                app_name,
                config,
                script_pid,
                launch_started_at=launch_started_at,
                launch_config=launch_config,
                runtime_processes=runtime_processes,
                script_choice=script_choice,
            )
            
            # ok-ww（鸣潮）特例：日常 2-4 分钟就能跑完，MIN_RUN_TIME(300s) 的
            # "运行时间过短"全是误报。游戏是自己退的（end_reason=game_exited，
            # 即脚本日志 "Exiting Game and App" 签名）且时长不足时，查一次当天
            # ok-ww 日志：有 Successfully Executed Task 即判成功。
            wuwa_script_done = False
            if (
                resolve_app_key(app_name) == "WutheringWaves"
                and end_reason == "game_exited"
                and duration < MIN_RUN_TIME
            ):
                wuwa_script_done = self._ok_ww_daily_completed()
                if wuwa_script_done:
                    self.logger.info(
                        f"[{game_display}] 脚本日志确认今日日常已完成"
                        f"（Successfully Executed Task），运行时间 {duration}s 不计过短"
                    )

            status = ""
            if self.stop_current_task:
                status = "用户终止"
            elif end_reason == "launch_timeout":
                status = "启动超时"
            elif end_reason == "updating":
                status = GAME_UPDATING_SKIP
            elif end_reason == "script_error":
                status = "脚本报错"
            elif not monitor_result:
                status = "超时"
            elif duration < MIN_RUN_TIME and not wuwa_script_done:
                status = f"运行时间过短(小于{MIN_RUN_TIME//60}分钟)"
            else:
                status = "成功"

            self.logger.info(
                f"\n{'='*40}\n[{game_display}] 任务结束: {status} (脚本: {script_display}, 运行时间: {duration//60}分{duration%60}秒)\n{'='*40}"
            )
            success = (
                monitor_result
                and (duration >= MIN_RUN_TIME or wuwa_script_done)
                and not self.stop_current_task
            )
            return success, status, duration
        
        except Exception as e:
            self.logger.error(f"任务执行异常: {str(e)}", exc_info=DEBUG_MODE)
            return False, f"执行异常: {str(e)}", 0
        
        finally:
            self._heartbeat.stop()
            self._cleanup_key_listener()
            _started = locals().get("launch_started_at")
            _elapsed = int(time.time() - _started) if _started else 0
            self.logger.info(
                f"[任务] exit app={app_name} display={game_display} "
                f"script={script_display} duration={_elapsed}s"
            )