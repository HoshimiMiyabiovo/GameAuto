import psutil
import logging
import ctypes
import time
import re
from dataclasses import dataclass
from typing import List, Optional


@dataclass(frozen=True)
class ProcessSnapshot:
    pid: int
    name: str
    exe_path: str = ""
    cmdline: str = ""
    create_time: float = 0.0

class ProcessSupervisor:
    @staticmethod
    def snapshot_process(proc: psutil.Process) -> Optional[ProcessSnapshot]:
        try:
            info = getattr(proc, 'info', {}) or {}
            name = str(info.get('name') or "")
            exe_path = str(info.get('exe') or "")
            cmdline_parts = info.get('cmdline') or []
            create_time = float(info.get('create_time') or 0.0)

            if not name:
                name = proc.name()

            if not exe_path:
                try:
                    exe_path = proc.exe() or ""
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    exe_path = ""

            if not cmdline_parts:
                try:
                    cmdline_parts = proc.cmdline() or []
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    cmdline_parts = []

            if not create_time:
                try:
                    create_time = float(proc.create_time() or 0.0)
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    create_time = 0.0

            return ProcessSnapshot(
                pid=proc.pid,
                name=name,
                exe_path=exe_path,
                cmdline=" ".join(str(part) for part in cmdline_parts),
                create_time=create_time,
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            return None

    @staticmethod
    def snapshot_pid(pid: int) -> Optional[ProcessSnapshot]:
        try:
            return ProcessSupervisor.snapshot_process(psutil.Process(pid))
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            return None

    @staticmethod
    def describe_process(snapshot: Optional[ProcessSnapshot]) -> str:
        if snapshot is None:
            return "未知进程"
        details = [f"PID: {snapshot.pid}"]
        if snapshot.exe_path:
            details.append(f"路径: {snapshot.exe_path}")
        return f"{snapshot.name or '未知进程'} ({', '.join(details)})"

    @staticmethod
    def _sort_key(proc: psutil.Process) -> float:
        try:
            return float(proc.create_time() or 0.0)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            return 0.0

    @staticmethod
    def find_processes(name_pattern: str, cmd_pattern: str = None, exact_name: bool = False) -> List[psutil.Process]:
        logger = logging.getLogger("GameAutomation")
        matching_procs = []
        pattern = (name_pattern or "").lower()
        for proc in psutil.process_iter(['name', 'exe', 'cmdline', 'create_time']):
            try:
                proc_name = proc.info.get('name') or ""
                cmdline = " ".join(proc.info.get('cmdline') or [])
                name_matched = proc_name.lower() == pattern if exact_name else pattern in proc_name.lower()
                if not name_matched:
                    continue
                if cmd_pattern and not re.search(cmd_pattern, cmdline, re.IGNORECASE):
                    continue
                matching_procs.append(proc)
                snapshot = ProcessSupervisor.snapshot_process(proc)
                if snapshot is not None:
                    logger.debug(f"找到匹配进程: {ProcessSupervisor.describe_process(snapshot)}")
                else:
                    logger.debug(f"找到匹配进程: {proc_name} (PID: {proc.pid})")
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        if not matching_procs:
            # 进程轮询阶段频繁 miss 属于常态，使用 debug 避免制造误导性告警
            logger.debug(f"未找到进程: {name_pattern}")
        return sorted(matching_procs, key=ProcessSupervisor._sort_key, reverse=True)

    @staticmethod
    def find_process(name_pattern: str, cmd_pattern: str = None, exact_name: bool = False) -> Optional[psutil.Process]:
        matching_procs = ProcessSupervisor.find_processes(
            name_pattern,
            cmd_pattern=cmd_pattern,
            exact_name=exact_name,
        )
        return matching_procs[0] if matching_procs else None

    @staticmethod
    def find_process_snapshot(name_pattern: str, cmd_pattern: str = None, exact_name: bool = False) -> Optional[ProcessSnapshot]:
        proc = ProcessSupervisor.find_process(
            name_pattern,
            cmd_pattern=cmd_pattern,
            exact_name=exact_name,
        )
        return ProcessSupervisor.snapshot_process(proc) if proc is not None else None

    @staticmethod
    def wait_for_process(pattern: str, timeout: int, cmd_pattern: str = None) -> bool:
        start = time.time()
        logger = logging.getLogger("GameAutomation")
        while time.time() - start < timeout:
            if ProcessSupervisor.find_process(pattern, cmd_pattern):
                logger.info(f"进程 {pattern} 启动成功 (等待时间: {int(time.time() - start)} 秒)")
                return True
            elapsed = int(time.time() - start)
            remaining = timeout - elapsed
            logger.debug(f"等待进程 {pattern}... (已等待: {elapsed}s, 剩余: {remaining}s)")
            time.sleep(2)
        logger.warning(f"进程 {pattern} 启动超时 ({timeout} 秒)")
        return False

    @staticmethod
    def _terminate_process(proc: psutil.Process, timeout: int, logger: logging.Logger, role: str = "进程"):
        pid = proc.pid
        try:
            name = proc.name()
        except psutil.NoSuchProcess:
            return

        try:
            logger.info(f"尝试温和终止{role}: {name} (PID: {pid})")
            proc.terminate()
            proc.wait(timeout=timeout)
        except psutil.NoSuchProcess:
            return
        except psutil.TimeoutExpired:
            try:
                logger.warning(f"温和终止失败，强杀{role}: {name} (PID: {pid})")
                proc.kill()
                proc.wait(timeout=timeout)
            except psutil.NoSuchProcess:
                return
        except Exception as e:
            logger.error(f"终止{role} {pid} 异常: {str(e)}")
            return

        try:
            if proc.is_running():
                logger.warning(f"强杀失败，使用 Windows API 强制终止{role}: {name} (PID: {pid})")
                handle = ctypes.windll.kernel32.OpenProcess(1, False, pid)
                ctypes.windll.kernel32.TerminateProcess(handle, -1)
                ctypes.windll.kernel32.CloseHandle(handle)
        except psutil.NoSuchProcess:
            return
        except Exception as e:
            logger.error(f"Windows API 终止{role} {pid} 异常: {str(e)}")
            return

        logger.info(f"{role}已终止: {name} (PID: {pid})")

    @staticmethod
    def safe_kill_process(pid: int, timeout: int = 10):
        logger = logging.getLogger("GameAutomation")
        try:
            proc = psutil.Process(pid)
        except psutil.NoSuchProcess:
            return
        except Exception as e:
            logger.error(f"终止进程 {pid} 异常: {str(e)}")
            return

        try:
            children = sorted(proc.children(recursive=True), key=lambda p: p.create_time(), reverse=True)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            children = []

        for child in children:
            ProcessSupervisor._terminate_process(child, timeout, logger, role="子进程")

        ProcessSupervisor._terminate_process(proc, timeout, logger)