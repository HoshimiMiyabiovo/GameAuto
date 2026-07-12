import os
import time
import logging
import subprocess
import ctypes
import psutil
import pyautogui
import win32gui
import win32process
from logging.handlers import TimedRotatingFileHandler
from typing import Tuple, Optional, Dict, List

from .foreground_flow_runner import run_flow
from .process_supervisor import ProcessSnapshot, ProcessSupervisor
from .ok_ww_log_monitor import run_nte_post_launch_ocr_click
from .window_detector import WindowDetector
from ..settings import (
    APPS,
    CHECK_INTERVAL,
    DEBUG_MODE,
    BACKUP_COUNT,
    LOG_FILE,
    MIN_RUN_TIME,
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

class AutomationController:
    def __init__(self):
        self.logger = self._init_logger()
        self.stop_current_task = False
        self.logger.info("键盘检测与快捷键监听功能已禁用")

    def _init_logger(self):
        logger = logging.getLogger("GameAutomation")
        logger.setLevel(logging.DEBUG if DEBUG_MODE else logging.INFO)
        logger.propagate = False
       
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
            handler.close()
        formatter = logging.Formatter(
            '[%(asctime)s.%(msecs)03d] [%(levelname)-8s] %(name)s:%(funcName)s:%(lineno)d - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        file_handler = TimedRotatingFileHandler(
            LOG_FILE, when='midnight', interval=1, backupCount=BACKUP_COUNT, encoding='utf-8'
        )
        file_handler.setFormatter(formatter)
       
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
       
        logger.addHandler(file_handler)
        logger.addHandler(console_handler)
       
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
    ) -> Optional[ProcessSnapshot]:
        game_cfg = config.get("game") if isinstance(config.get("game"), dict) else {}
        raw_configured_name = str(game_cfg.get("process_name", "") or "").strip()
        raw_game_exe = str(game_cfg.get("exe_path", "") or "").strip()
        configured_name = self._configured_game_process_name(config)
        timeout = int(config["timeouts"]["launch"])
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
            return None

        self.logger.info(f"等待游戏进程启动: {configured_name} (超时: {timeout}秒)")

        last_log = 0.0
        while time.time() - start < timeout:
            if self.stop_current_task:
                return None

            snapshot = ProcessSupervisor.find_process_snapshot(configured_name, exact_name=True)

            if snapshot is not None:
                runtime_processes["game"] = snapshot
                self._log_detected_process("游戏进程", snapshot, source="精确进程名")
                remember_process_snapshot(app_name, "game", snapshot, script_choice=script_choice, logger=self.logger)
                return snapshot

            elapsed = int(time.time() - start)
            remaining = timeout - elapsed
            if time.time() - last_log >= 2:
                self.logger.debug(f"等待游戏进程中... (已等待: {elapsed}s, 剩余: {remaining}s)")
                last_log = time.time()
            time.sleep(2)

        self.logger.warning(f"未检测到目标游戏进程 (等待超时: {timeout}秒)")
        return None

    def _kill_processes_by_name(
        self,
        process_name: str,
        reason: str,
        *,
        exact_name: bool = True,
        skip_pids: Optional[set] = None,
    ):
        if not process_name:
            return

        skip_pids = skip_pids or set()
        matched = ProcessSupervisor.find_processes(process_name, exact_name=exact_name)
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
        ok, msg = run_flow(self.logger, flow_key)
        if ok:
            self.logger.info(f"[{game_display}] 前置启动流程完成: {msg}")
        else:
            self.logger.error(f"[{game_display}] 前置启动流程失败: {msg}")
        return ok, msg

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
        monitor_script_exit = bool(config.get("end_when_script_exits"))
        launch_started_at = float(launch_started_at or time.time())
        launch_config = launch_config or config
        game_display = get_app_display_name(app_name)
        focus_guard_deadline = time.time() + max(0.0, float(launch_config.get("post_launch_focus_guard_sec", 0) or 0.0))
        focus_retry_interval = max(1.0, float(launch_config.get("post_launch_focus_retry_interval_sec", 2.0) or 2.0))
        last_focus_retry = 0.0
       
        start_time = time.time()
        game_snapshot = self._wait_for_game_process_snapshot(
            app_name,
            config,
            launch_config,
            launch_started_at,
            runtime_processes,
            script_choice=script_choice,
        )
        if game_snapshot is None:
            if self.stop_current_task:
                self._kill_related_processes(config, script_pid, runtime_processes=runtime_processes)
                return False, 0, "user_stop"
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
       
        while time.time() - start_time < monitor_timeout:
            if self.stop_current_task:
                self.logger.info("检测到快捷键终止请求，结束当前任务")
                self._kill_related_processes(config, script_pid, runtime_processes=runtime_processes)
                duration = int(time.time() - start_time)
                return False, duration, "user_stop"

            if monitor_script_exit and script_pid and not self._is_pid_running(script_pid):
                duration = int(time.time() - start_time)
                self.logger.warning("检测到脚本进程已退出，开始回收关联进程")
                self._kill_related_processes(config, script_pid=None, runtime_processes=runtime_processes)
                return duration >= MIN_RUN_TIME, duration, "script_exited"

            current_game = runtime_processes.get("game")
            if current_game and not self._is_pid_running(current_game.pid):
                replacement = ProcessSupervisor.find_process_snapshot(current_game.name, exact_name=True)
                if replacement is not None and replacement.pid != current_game.pid:
                    runtime_processes["game"] = replacement
                    self._log_detected_process("游戏进程", replacement, source="PID切换后重新识别")
                    remember_process_snapshot(app_name, "game", replacement, script_choice=script_choice, logger=self.logger)
                    current_game = replacement

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

            prelaunch_ok, prelaunch_message = self._run_prelaunch_flow(config, game_display)
            if not prelaunch_ok:
                self._kill_related_processes(config, include_script=False)
                return False, f"前置启动失败: {prelaunch_message}", 0

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
            
            status = ""
            if self.stop_current_task:
                status = "用户终止"
            elif end_reason == "script_exited":
                status = "脚本已退出"
            elif end_reason == "launch_timeout":
                status = "启动超时"
            elif not monitor_result:
                status = "超时"
            elif duration < MIN_RUN_TIME:
                status = f"运行时间过短(小于{MIN_RUN_TIME//60}分钟)"
            else:
                status = "成功"
            
            self.logger.info(
                f"\n{'='*40}\n[{game_display}] 任务结束: {status} (脚本: {script_display}, 运行时间: {duration//60}分{duration%60}秒)\n{'='*40}"
            )
            success = monitor_result and duration >= MIN_RUN_TIME and not self.stop_current_task
            if end_reason == "script_exited":
                success = duration >= MIN_RUN_TIME and not self.stop_current_task
            return success, status, duration
        
        except Exception as e:
            self.logger.error(f"任务执行异常: {str(e)}", exc_info=DEBUG_MODE)
            return False, f"执行异常: {str(e)}", 0
        
        finally:
            self._cleanup_key_listener()
            _started = locals().get("launch_started_at")
            _elapsed = int(time.time() - _started) if _started else 0
            self.logger.info(
                f"[任务] exit app={app_name} display={game_display} "
                f"script={script_display} duration={_elapsed}s"
            )