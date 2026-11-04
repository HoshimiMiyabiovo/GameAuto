"""Client for the headless ok-ww module runner.

The runner lives in ``tools/okww_module/runner.py`` and is executed with the
Python interpreter bundled inside the ok-ww installation.  This module owns the
process lifecycle and the loopback JSON-lines control channel.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from ..settings.paths import DATA_DIR, LOG_DIR, TOOLS_DIR


class OkwwModuleError(RuntimeError):
    """Raised when the ok-ww runner cannot start or a command fails."""


@dataclass
class ModuleTaskResult:
    task: str
    success: bool
    stopped: bool = False
    error: str = ""
    duration: float = 0.0
    saw_running: bool = False
    info: Dict[str, Any] = field(default_factory=dict)
    humanize: Dict[str, Any] = field(default_factory=dict)


@dataclass
class OkwwModulePaths:
    python_exe: Path
    runtime_root: Path
    runner_path: Path
    humanize_config: Path
    log_path: Path
    ready_path: Path


def _as_module_config(app_config: Dict[str, Any]) -> Dict[str, Any]:
    raw = app_config.get("module_mode")
    if isinstance(raw, dict):
        return dict(raw)
    if raw is True:
        return {"enabled": True}
    return {}


def _find_app_dir(install_root: Path) -> Path:
    direct = install_root / "data" / "apps" / "ok-ww"
    if (direct / "python" / "python.exe").is_file():
        return direct
    apps_dir = install_root / "data" / "apps"
    if apps_dir.is_dir():
        for child in apps_dir.iterdir():
            if (child / "python" / "python.exe").is_file():
                return child
    return direct


def _resolve_paths(app_config: Dict[str, Any], module_cfg: Dict[str, Any]) -> OkwwModulePaths:
    discovery = app_config.get("_discovery")
    discovery = discovery if isinstance(discovery, dict) else {}
    script_exe = str(
        module_cfg.get("script_exe")
        or discovery.get("script_exe_path")
        or app_config.get("launcher")
        or ""
    ).strip()
    if not script_exe:
        raise OkwwModuleError("ok-ww 可执行文件路径缺失（app_config._discovery.script_exe_path）")
    install_root = Path(script_exe).resolve().parent
    app_dir = _find_app_dir(install_root)

    python_exe = Path(
        str(module_cfg.get("python_exe") or app_dir / "python" / "python.exe")
    ).resolve()
    if not python_exe.is_file():
        raise OkwwModuleError(f"ok-ww bundled python not found: {python_exe}")

    runtime_root = Path(
        str(module_cfg.get("runtime_root") or DATA_DIR / "okww" / "src")
    ).resolve()
    if not (runtime_root / "main.py").is_file():
        fallback = (app_dir / "working").resolve()
        if (fallback / "main.py").is_file():
            runtime_root = fallback
        else:
            raise OkwwModuleError(
                f"ok-ww runtime not found: {runtime_root} (also tried {fallback})"
            )

    runner_path = Path(
        str(module_cfg.get("runner_path") or TOOLS_DIR / "okww_module" / "runner.py")
    ).resolve()
    if not runner_path.is_file():
        raise OkwwModuleError(f"ok-ww module runner not found: {runner_path}")

    humanize_config = Path(
        str(module_cfg.get("humanize_config") or DATA_DIR / "okww" / "humanize.json")
    ).resolve()
    log_path = Path(
        str(module_cfg.get("log_path") or LOG_DIR / "okww_module_runner.log")
    ).resolve()
    ready_path = Path(
        str(
            module_cfg.get("ready_path")
            or DATA_DIR / "okww" / "run" / f"runner_{os.getpid()}_{int(time.time() * 1000)}.json"
        )
    ).resolve()
    return OkwwModulePaths(
        python_exe=python_exe,
        runtime_root=runtime_root,
        runner_path=runner_path,
        humanize_config=humanize_config,
        log_path=log_path,
        ready_path=ready_path,
    )


class OkwwModuleSession:
    def __init__(
        self,
        paths: OkwwModulePaths,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        startup_timeout: float = 120.0,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        self.paths = paths
        self.host = host
        self.port = int(port)
        self.startup_timeout = float(startup_timeout)
        self.event_callback = event_callback

        self.token = secrets.token_urlsafe(18)
        self._proc: Optional[subprocess.Popen] = None
        self._sock: Optional[socket.socket] = None
        self._reader: Optional[threading.Thread] = None
        self._reader_file = None
        self._pending: Dict[int, Dict[str, Any]] = {}
        self._pending_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._next_id = 0
        self._closed = threading.Event()
        self.ready: Dict[str, Any] = {}

    # ------------------------------------------------------------------ build
    @classmethod
    def from_app_config(
        cls,
        app_config: Dict[str, Any],
        *,
        event_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        require_enabled: bool = True,
    ) -> "OkwwModuleSession":
        module_cfg = _as_module_config(app_config)
        if require_enabled and not bool(module_cfg.get("enabled", False)):
            raise OkwwModuleError("ok-ww module_mode is not enabled")
        paths = _resolve_paths(app_config, module_cfg)
        return cls(
            paths,
            host=str(module_cfg.get("host") or "127.0.0.1"),
            port=int(module_cfg.get("port") or 0),
            startup_timeout=float(module_cfg.get("startup_timeout") or 120.0),
            event_callback=event_callback,
        )

    # --------------------------------------------------------------- lifecycle
    def start(
        self, stop_check: Optional[Callable[[], bool]] = None
    ) -> "OkwwModuleSession":
        if self._proc is not None and self._proc.poll() is None:
            return self
        self._closed.clear()
        self.paths.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.paths.ready_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.paths.ready_path.unlink()
        except FileNotFoundError:
            pass

        command = [
            str(self.paths.python_exe),
            "-X",
            "utf8",
            "-u",
            str(self.paths.runner_path),
            "--okww-root",
            str(self.paths.runtime_root),
            "--host",
            self.host,
            "--port",
            str(self.port),
            "--token",
            self.token,
            "--ready-file",
            str(self.paths.ready_path),
            "--log",
            str(self.paths.log_path),
            "--humanize-config",
            str(self.paths.humanize_config),
        ]
        env = os.environ.copy()
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.setdefault("PYTHONUTF8", "1")
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self._proc = subprocess.Popen(
            command,
            cwd=str(self.paths.runtime_root),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            creationflags=creationflags,
        )
        try:
            self.ready = self._wait_ready(stop_check=stop_check)
            self._connect()
            pong = self.request("ping", timeout=10)
            if not pong.get("pong"):
                raise OkwwModuleError(f"unexpected ping response: {pong}")
        except Exception:
            self.stop()
            raise
        return self

    def _wait_ready(
        self, stop_check: Optional[Callable[[], bool]] = None
    ) -> Dict[str, Any]:
        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            if stop_check is not None and stop_check():
                raise OkwwModuleError("user stopped during ok-ww runner startup")
            if self._proc is not None and self._proc.poll() is not None:
                code = self._proc.returncode
                raise OkwwModuleError(
                    f"ok-ww module runner exited during startup (code={code})\n{self.log_tail(40)}"
                )
            if self.paths.ready_path.is_file():
                try:
                    payload = json.loads(self.paths.ready_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    time.sleep(0.2)
                    continue
                if payload.get("ok"):
                    return payload
                if payload.get("state") == "error":
                    raise OkwwModuleError(
                        f"ok-ww module runner init failed: {payload.get('error')}\n{self.log_tail(40)}"
                    )
            time.sleep(0.2)
        raise OkwwModuleError(
            f"ok-ww module runner startup timed out after {self.startup_timeout:.0f}s\n"
            f"{self.log_tail(40)}"
        )

    def _connect(self) -> None:
        port = int(self.ready.get("port") or 0)
        if not port:
            raise OkwwModuleError(f"runner did not publish a port: {self.ready}")
        self.token = str(self.ready.get("token") or self.token)
        last_error: Optional[Exception] = None
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                sock = socket.create_connection((self.host, port), timeout=10)
                sock.settimeout(None)
                self._sock = sock
                self._reader_file = sock.makefile("r", encoding="utf-8", newline="\n")
                self._reader = threading.Thread(
                    target=self._reader_loop,
                    name="okww-module-session-reader",
                    daemon=True,
                )
                self._reader.start()
                return
            except OSError as exc:
                last_error = exc
                time.sleep(0.2)
        raise OkwwModuleError(f"cannot connect to ok-ww runner on {self.host}:{port}: {last_error}")

    def stop(self) -> None:
        if self._sock is not None and not self._closed.is_set():
            try:
                self.request("shutdown", timeout=5)
            except Exception:
                pass
        self._closed.set()
        if self._sock is not None:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        proc = self._proc
        if proc is not None:
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self._terminate_process_tree(proc)
            self._proc = None
        self._fail_pending(OkwwModuleError("ok-ww module session stopped"))

    def _terminate_process_tree(self, proc: subprocess.Popen) -> None:
        try:
            import psutil

            parent = psutil.Process(proc.pid)
            children = parent.children(recursive=True)
            for child in children:
                try:
                    child.terminate()
                except psutil.Error:
                    pass
            parent.terminate()
            try:
                parent.wait(timeout=5)
            except psutil.TimeoutExpired:
                for child in children:
                    try:
                        child.kill()
                    except psutil.Error:
                        pass
                parent.kill()
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    @property
    def pid(self) -> Optional[int]:
        return self._proc.pid if self._proc is not None else None

    def is_alive(self) -> bool:
        return (
            self._proc is not None
            and self._proc.poll() is None
            and not self._closed.is_set()
        )

    # ----------------------------------------------------------------- protocol
    def _reader_loop(self) -> None:
        try:
            assert self._reader_file is not None
            for line in self._reader_file:
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                request_id = message.get("id")
                if request_id is not None:
                    with self._pending_lock:
                        waiter = self._pending.pop(int(request_id), None)
                    if waiter is not None:
                        waiter["response"] = message
                        waiter["event"].set()
                    continue
                if self.event_callback is not None:
                    try:
                        self.event_callback(message)
                    except Exception:
                        pass
        except (OSError, ValueError):
            pass
        finally:
            self._closed.set()
            self._fail_pending(OkwwModuleError("ok-ww module runner connection closed"))

    def _fail_pending(self, error: Exception) -> None:
        with self._pending_lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for waiter in pending:
            waiter["error"] = error
            waiter["event"].set()

    def request(self, cmd: str, *, timeout: float = 30.0, **kwargs: Any) -> Dict[str, Any]:
        if self._sock is None or self._closed.is_set():
            raise OkwwModuleError("ok-ww module runner is not connected")
        with self._pending_lock:
            self._next_id += 1
            request_id = self._next_id
            waiter: Dict[str, Any] = {
                "event": threading.Event(),
                "response": None,
                "error": None,
            }
            self._pending[request_id] = waiter
        payload = {"id": request_id, "token": self.token, "cmd": cmd}
        payload.update(kwargs)
        data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            with self._send_lock:
                self._sock.sendall(data)
        except OSError as exc:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise OkwwModuleError(f"send command failed: {exc}") from exc

        if not waiter["event"].wait(max(1.0, float(timeout))):
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise OkwwModuleError(f"command timed out after {timeout:.0f}s: {cmd}")
        if waiter["error"] is not None:
            raise waiter["error"]
        response = waiter["response"] or {}
        if not response.get("ok"):
            raise OkwwModuleError(str(response.get("error") or f"command failed: {cmd}"))
        return response.get("result") or {}

    # -------------------------------------------------------------------- api
    def run_task(
        self,
        task: str = "DailyTask",
        *,
        timeout: float = 1800.0,
        close_game: bool = True,
    ) -> ModuleTaskResult:
        result = self.request(
            "run_task",
            timeout=float(timeout) + 120.0,
            task=task,
            task_timeout=float(timeout),
            close_game=bool(close_game),
        )
        return ModuleTaskResult(
            task=str(result.get("task") or task),
            success=bool(result.get("success")),
            stopped=bool(result.get("stopped")),
            error=str(result.get("error") or ""),
            duration=float(result.get("duration") or 0.0),
            saw_running=bool(result.get("saw_running")),
            info=result.get("info") if isinstance(result.get("info"), dict) else {},
            humanize=result.get("humanize") if isinstance(result.get("humanize"), dict) else {},
        )

    def status(self) -> Dict[str, Any]:
        return self.request("status", timeout=10)

    def list_tasks(self) -> Dict[str, Any]:
        return self.request("list_tasks", timeout=15)

    def stop_task(self) -> Dict[str, Any]:
        return self.request("stop_task", timeout=15)

    def log_tail(self, lines: int = 40) -> str:
        try:
            with self.paths.log_path.open("r", encoding="utf-8", errors="replace") as handle:
                return "".join(deque(handle, maxlen=max(1, int(lines))))
        except OSError:
            return ""
