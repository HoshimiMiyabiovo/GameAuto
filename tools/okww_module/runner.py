#!/usr/bin/env python3
"""Headless ok-ww module runner.

This process is started by ``game_auto.runtime.okww_module_session`` with the
Python interpreter bundled inside the ok-ww installation.  It imports ok-ww's
own ``config`` and ``ok`` modules, keeps a single runtime alive, and exposes a
small JSON-lines control protocol on loopback.

Why a separate process?
  * ok-ww needs its own Python environment (PySide6 / OpenVINO / OCR).
  * A crash in ok-ww must not take down the game_auto GUI.
  * The runtime must be elevated together with the game window.

The protocol is intentionally tiny and does not depend on game_auto:

  request : {"id": 1, "token": "...", "cmd": "run_task", "task": "DailyTask"}
  response: {"id": 1, "ok": true, "result": {...}}
  event   : {"event": "task_started", "task": "DailyTask", ...}

Commands: ping, status, list_tasks, run_task, stop_task, shutdown.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("game_auto.okww.runner")


def _json_safe(value: Any, depth: int = 0) -> Any:
    """Best-effort conversion for task.info values."""
    if depth > 3:
        return repr(value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v, depth + 1) for v in value]
    return repr(value)


class ModuleRunner:
    def __init__(
        self,
        *,
        okww_root: str,
        host: str,
        port: int,
        token: str,
        ready_file: str,
        log_file: str,
        humanize_config: str,
    ) -> None:
        self.root = Path(okww_root).resolve()
        self.host = host
        self.port = int(port)
        self.token = token
        self.ready_file = Path(ready_file).resolve()
        self.log_file = Path(log_file).resolve()
        self.humanize_config = humanize_config

        self.ok = None
        self.app = None
        self.version = ""
        self.humanize_settings = None

        self.server: Optional[socket.socket] = None
        self.bound_port: Optional[int] = None
        self._shutdown = threading.Event()
        self._run_lock = threading.Lock()
        self._stop_requested = False
        self._current_task = None
        self._task_started_at = 0.0
        self._started_at = time.time()
        self._errors: List[str] = []
        self._active_client: Optional["ClientConnection"] = None
        self._client_lock = threading.Lock()
        self._log_handle = None

    # ------------------------------------------------------------------ setup
    def _setup_logging(self) -> None:
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        self._log_handle = open(self.log_file, "a", encoding="utf-8", buffering=1)
        sys.stdout = self._log_handle
        sys.stderr = self._log_handle
        # ok-script configures the root logger itself; basicConfig() would be a
        # no-op.  Give our own namespace an explicit handler so runner and
        # humanize messages always land in the runner log.
        package_logger = logging.getLogger("game_auto.okww")
        package_logger.setLevel(logging.INFO)
        package_logger.propagate = False
        for handler in list(package_logger.handlers):
            package_logger.removeHandler(handler)
        handler = logging.StreamHandler(self._log_handle)
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s %(threadName)s %(name)s: %(message)s"
            )
        )
        package_logger.addHandler(handler)

    def _init_okww(self) -> None:
        if not (self.root / "main.py").is_file():
            raise RuntimeError(f"ok-ww runtime not found: {self.root / 'main.py'}")
        if not (self.root / "configs").exists():
            raise RuntimeError(f"ok-ww configs not found: {self.root / 'configs'}")

        # ok-script derives its app path from sys.argv[0].  It must point at
        # ok-ww's main.py, otherwise assets/logs resolve under our runner dir.
        os.chdir(self.root)
        sys.path.insert(0, str(self.root))
        sys.argv = [str(self.root / "main.py")]

        import config as okww_config  # type: ignore[import-not-found]

        cfg = dict(okww_config.config)
        cfg.pop("gui", None)
        cfg["use_gui"] = False
        cfg["debug"] = False
        cfg["check_mutex"] = False
        windows = cfg.get("windows")
        if not isinstance(windows, dict):
            windows = {}
        windows = dict(windows)
        windows["start_exe"] = False
        cfg["windows"] = windows
        cfg["mcp"] = {"enabled": False}

        from ok import OK  # type: ignore[import-not-found]

        self.ok = OK(cfg)
        # Creating the facade also sets og.app, which StartController uses.
        self.app = self.ok.headless_app
        self.version = str(cfg.get("version") or "")

        # Keep the runtime idle until a command arrives.  do_start() enables
        # exactly the task it is given.
        for task in list(self.ok.task_executor.onetime_tasks):
            task._enabled = False
        for task in list(self.ok.task_executor.trigger_tasks):
            task._enabled = False
        logger.info(
            "ok-ww runtime ready: version=%s onetime=%d trigger=%d",
            self.version,
            len(self.ok.task_executor.onetime_tasks),
            len(self.ok.task_executor.trigger_tasks),
        )

    def _install_humanize(self) -> None:
        try:
            import humanize  # type: ignore[import-not-found]

            self.humanize_settings = humanize.install(
                self.humanize_config, logger_override=logger
            )
        except Exception as exc:  # noqa: BLE001 - never block the runtime
            logger.error("humanize install failed: %s", exc)
            logger.error(traceback.format_exc())

    # ----------------------------------------------------------------- ready
    def _write_ready(self, *, ok: bool, error: str = "", state: str = "ready") -> None:
        payload: Dict[str, Any] = {
            "ok": bool(ok),
            "state": state,
            "pid": os.getpid(),
            "host": self.host,
            "port": self.bound_port,
            "token": self.token,
            "version": self.version,
            "root": str(self.root),
            "started_at": self._started_at,
            "error": error,
        }
        if self.humanize_settings is not None:
            payload["humanize"] = {
                "enabled": self.humanize_settings.enabled,
                "target_cps": self.humanize_settings.target_cps,
                "jitter": self.humanize_settings.jitter,
                "teams": len(self.humanize_settings.teams),
            }
        try:
            self.ready_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.ready_file.with_suffix(self.ready_file.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.ready_file)
        except Exception as exc:  # noqa: BLE001
            logger.error("write ready file failed: %s", exc)

    # ---------------------------------------------------------------- server
    def _bind_server(self) -> None:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((self.host, self.port))
        server.listen(2)
        server.settimeout(1.0)
        self.server = server
        self.bound_port = int(server.getsockname()[1])
        logger.info("control server listening on %s:%s", self.host, self.bound_port)

    def _serve(self) -> None:
        assert self.server is not None
        heartbeat = threading.Thread(
            target=self._heartbeat_loop, name="okww-control-heartbeat", daemon=True
        )
        heartbeat.start()
        while not self._shutdown.is_set():
            try:
                conn, addr = self.server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with self._client_lock:
                if self._active_client is not None:
                    try:
                        conn.sendall(
                            (
                                json.dumps(
                                    {"ok": False, "error": "another client is already connected"}
                                )
                                + "\n"
                            ).encode("utf-8")
                        )
                    except OSError:
                        pass
                    conn.close()
                    continue
                client = ClientConnection(self, conn, addr)
                self._active_client = client
            client.start()

    def _heartbeat_loop(self) -> None:
        while not self._shutdown.wait(5.0):
            client = self._active_client
            if client is None:
                continue
            task = self._current_task
            payload = {
                "event": "heartbeat",
                "ts": time.time(),
                "uptime_sec": round(time.time() - self._started_at, 1),
                "task": task.name if task is not None else "",
                "task_running": bool(task is not None and task.running),
                "task_elapsed_sec": round(time.time() - self._task_started_at, 1)
                if self._task_started_at
                else 0,
            }
            client.send(payload)

    def _clear_client(self, client: "ClientConnection") -> None:
        with self._client_lock:
            if self._active_client is client:
                self._active_client = None
        if self._current_task is not None:
            logger.warning("control client disconnected while a task is running; stopping task")
            self.stop_task()

    # ------------------------------------------------------------------ api
    def _game_status(self) -> Dict[str, Any]:
        if self.ok is None:
            return {}
        try:
            hwnd_window = getattr(self.ok.device_manager, "hwnd_window", None)
            if hwnd_window is None:
                return {}
            return {
                "hwnd": getattr(hwnd_window, "hwnd", None),
                "title": getattr(hwnd_window, "hwnd_title", ""),
                "exe_name": getattr(hwnd_window, "exe_name", ""),
            }
        except Exception:
            return {}

    def ping(self) -> Dict[str, Any]:
        return {
            "pong": True,
            "pid": os.getpid(),
            "version": self.version,
            "uptime_sec": round(time.time() - self._started_at, 1),
        }

    def status(self) -> Dict[str, Any]:
        task = self._current_task
        payload: Dict[str, Any] = {
            "pid": os.getpid(),
            "version": self.version,
            "running": task is not None,
            "task": task.name if task is not None else "",
            "task_running": bool(task is not None and task.running),
            "task_enabled": bool(task is not None and task.enabled),
            "task_elapsed_sec": round(time.time() - self._task_started_at, 1)
            if self._task_started_at
            else 0,
            "game": self._game_status(),
            "uptime_sec": round(time.time() - self._started_at, 1),
        }
        if task is not None:
            try:
                import humanize  # type: ignore[import-not-found]

                payload["humanize"] = humanize.task_metrics(task)
            except Exception:
                pass
            try:
                payload["task_info"] = _json_safe(task.info)
            except Exception:
                pass
        return payload

    def list_tasks(self) -> Dict[str, Any]:
        if self.ok is None:
            return {"onetime": [], "trigger": []}

        def describe(task) -> Dict[str, Any]:
            return {
                "name": task.name,
                "class": task.__class__.__name__,
                "module": task.__class__.__module__,
                "enabled": bool(task.enabled),
                "running": bool(task.running),
                "status": task.get_status(),
                "description": getattr(task, "description", ""),
            }

        return {
            "onetime": [describe(task) for task in self.ok.task_executor.onetime_tasks],
            "trigger": [describe(task) for task in self.ok.task_executor.trigger_tasks],
        }

    def stop_task(self) -> Dict[str, Any]:
        self._stop_requested = True
        task = self._current_task
        if task is None:
            return {"stopped": False}
        try:
            self.ok.task_executor.stop_current_task()
        except Exception as exc:  # noqa: BLE001
            logger.warning("stop_current_task failed: %s", exc)
        return {"stopped": True, "task": task.name}

    def request_shutdown(self) -> None:
        self._shutdown.set()
        self.stop_task()

    def _close_game(self) -> None:
        if self.ok is None:
            return
        try:
            device_manager = self.ok.device_manager
            hwnd_window = getattr(device_manager, "hwnd_window", None)
            if hwnd_window is None:
                return
            logger.info("closing game window after module task")
            device_manager.stop_hwnd()
        except Exception as exc:  # noqa: BLE001
            logger.warning("close game failed: %s", exc)

    def run_task(self, request: Dict[str, Any]) -> Dict[str, Any]:
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError("another ok-ww task is already running")
        task = None
        callback = None
        try:
            if self.ok is None:
                raise RuntimeError("ok-ww runtime is not initialized")
            task_name = str(request.get("task") or "DailyTask")
            timeout = float(request.get("task_timeout") or request.get("timeout") or 1800)
            close_game = bool(request.get("close_game", False))
            task = self.ok.get_onetime_task(task_name)
            if task.running:
                raise RuntimeError(f"task is already running: {task.name}")

            from ok.core.events import communicate  # type: ignore[import-not-found]

            self._errors = []
            self._stop_requested = False
            self._current_task = task
            self._task_started_at = time.monotonic()
            task.start_time = 0
            if task.__class__.__name__ == "DailyTask":
                # 先写一次 run 边界：即使任务在自身 open_daily 之前失败，
                # 日报解析器也不会把上一轮的成功标记算到这一轮。
                try:
                    from ok import Logger  # type: ignore[import-not-found]

                    Logger.get_logger("ok").info("DailyTask:open_daily")
                except Exception:
                    pass

            def on_notification(
                message,
                title=None,
                error=False,
                tray=False,
                show_tab=None,
                params=None,
                images=None,
            ):
                if not error:
                    return
                # TaskExecutor 用 task.name 作为错误通知标题；GPU 驱动警告、
                # AutoHDR 警告等也是 error=True，但标题不是当前任务，不能算失败。
                title_text = str(title or "")
                if title_text:
                    if title_text != task.name and task.name not in title_text:
                        return
                elif task.name not in str(message or ""):
                    return
                text = f"{title_text or task.name}: {message}"
                if text not in self._errors:
                    self._errors.append(text)

            callback = on_notification
            communicate.notification.connect(callback)

            logger.info("run_task start: %s timeout=%ss close_game=%s", task.name, timeout, close_game)
            self._send_event("task_started", task=task.name, timeout=timeout)
            started = self.ok.headless_app.start_controller.do_start(task, exit_after=False)
            if not started:
                raise RuntimeError(f"start task failed: {task.name}")

            saw_running = False
            deadline = time.monotonic() + timeout
            while True:
                if self._stop_requested:
                    logger.info("run_task stop requested: %s", task.name)
                    try:
                        self.ok.task_executor.stop_current_task()
                    except Exception:
                        pass
                    break
                if task.running:
                    saw_running = True
                if saw_running and not task.running and not task.enabled:
                    break
                if not saw_running and not task.enabled and time.monotonic() - self._task_started_at > 5:
                    # The executor finished before we observed "running" (or
                    # do_start did not enqueue).  Error details, if any, are in
                    # task.info / the notification callback.
                    break
                if time.monotonic() > deadline:
                    logger.error("run_task timeout: %s", task.name)
                    try:
                        self.ok.task_executor.stop_current_task()
                    except Exception:
                        pass
                    raise TimeoutError(f"task timeout after {timeout:.0f}s: {task.name}")
                time.sleep(0.5)

            duration = time.monotonic() - self._task_started_at
            task_error = ""
            try:
                task_error = str(task.info.get("Error") or "")
            except Exception:
                task_error = ""
            error = "\n".join(self._errors) or task_error
            stopped = self._stop_requested
            completed = bool(getattr(task, "start_time", 0)) and not task.enabled and not task.running
            if not completed and not stopped and not error:
                error = "task did not start (game window not ready or task was not queued)"
            success = completed and not stopped and not error

            if success and not getattr(task, "exit_after_task", False):
                # ok-ww 的 TaskExecutor 只在 exit_after=True 时打这个标记。
                # 模块模式不退出 runner，但 game_auto 的日报解析器仍以它作为
                # 整体成功标志，所以成功时补一行兼容标记。
                try:
                    from ok import Logger  # type: ignore[import-not-found]

                    Logger.get_logger("ok").info(
                        "Successfully Executed Task, Exiting Game and App!"
                    )
                    # ok-ww 的日志是 QueueHandler + 后台消费者；给队列一点时间
                    # 落盘，避免 runner 立即关闭时日报解析器看不到这一行。
                    time.sleep(0.2)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("write compatibility success marker failed: %s", exc)

            if close_game:
                self._close_game()

            try:
                import humanize  # type: ignore[import-not-found]

                humanize_metrics = humanize.task_metrics(task)
            except Exception:
                humanize_metrics = {}
            result = {
                "task": task.name,
                "success": success,
                "stopped": stopped,
                "error": error,
                "duration": round(duration, 1),
                "saw_running": saw_running,
                "info": _json_safe(getattr(task, "info", {})),
                "humanize": humanize_metrics,
            }
            logger.info(
                "run_task finish: %s success=%s stopped=%s duration=%.1fs clicks=%s cps=%s error=%s",
                task.name,
                success,
                stopped,
                duration,
                humanize_metrics.get("clicks", 0),
                humanize_metrics.get("target_cps", 0),
                error[:300],
            )
            self._send_event(
                "task_finished",
                task=task.name,
                success=success,
                stopped=stopped,
                duration=round(duration, 1),
                error=error,
                humanize=humanize_metrics,
            )
            return result
        finally:
            if callback is not None:
                try:
                    from ok.core.events import communicate  # type: ignore[import-not-found]

                    communicate.notification.disconnect(callback)
                except Exception:
                    pass
            self._current_task = None
            self._task_started_at = 0.0
            self._run_lock.release()

    def _send_event(self, event: str, **kwargs: Any) -> None:
        client = self._active_client
        if client is None:
            return
        payload = {"event": event, "ts": time.time()}
        payload.update(kwargs)
        client.send(payload)

    # ---------------------------------------------------------------- entry
    def run(self) -> int:
        self._setup_logging()
        ok = False
        try:
            logger.info("runner starting: root=%s pid=%s", self.root, os.getpid())
            self._init_okww()
            self._install_humanize()
            self._bind_server()
            self._write_ready(ok=True)
            ok = True
            self._serve()
            return 0
        except Exception as exc:  # noqa: BLE001
            logger.error("runner fatal error: %s", exc)
            logger.error(traceback.format_exc())
            self._write_ready(ok=False, error=f"{type(exc).__name__}: {exc}", state="error")
            return 1
        finally:
            self._shutdown.set()
            with self._client_lock:
                client = self._active_client
                self._active_client = None
            if client is not None:
                client.close()
            if self.server is not None:
                try:
                    self.server.close()
                except OSError:
                    pass
            if self.ok is not None:
                try:
                    logger.info("runner shutting down ok-ww")
                    self.ok.quit()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("ok-ww quit failed: %s", exc)
                try:
                    thread = getattr(self.ok.task_executor, "thread", None)
                    if thread is not None and thread.is_alive():
                        thread.join(timeout=10)
                except Exception:
                    pass
            self._write_ready(ok=ok, state="stopped" if ok else "error")
            if self._log_handle is not None:
                try:
                    self._log_handle.flush()
                except Exception:
                    pass


class ClientConnection:
    def __init__(self, runner: ModuleRunner, conn: socket.socket, addr) -> None:
        self.runner = runner
        self.conn = conn
        self.addr = addr
        self._send_lock = threading.Lock()
        self._closed = False
        self._worker: Optional[threading.Thread] = None
        self._reader = threading.Thread(target=self._read_loop, name="okww-control-client", daemon=True)

    def start(self) -> None:
        self._reader.start()

    def close(self) -> None:
        self._closed = True
        try:
            self.conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.conn.close()
        except OSError:
            pass

    def send(self, payload: Dict[str, Any]) -> None:
        data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        with self._send_lock:
            if self._closed:
                return
            try:
                self.conn.sendall(data)
            except OSError:
                self._closed = True

    def _read_loop(self) -> None:
        try:
            file = self.conn.makefile("r", encoding="utf-8", newline="\n")
            for line in file:
                line = line.strip()
                if not line:
                    continue
                try:
                    request = json.loads(line)
                except ValueError:
                    self.send({"ok": False, "error": "invalid json"})
                    continue
                self._handle(request)
                if self._closed:
                    break
        except OSError:
            pass
        finally:
            self.runner._clear_client(self)

    def _handle(self, request: Dict[str, Any]) -> None:
        request_id = request.get("id")
        if request.get("token") != self.runner.token:
            self.send({"id": request_id, "ok": False, "error": "invalid token"})
            return
        command = str(request.get("cmd") or "")
        try:
            if command == "ping":
                self.send({"id": request_id, "ok": True, "result": self.runner.ping()})
            elif command == "status":
                self.send({"id": request_id, "ok": True, "result": self.runner.status()})
            elif command == "list_tasks":
                self.send({"id": request_id, "ok": True, "result": self.runner.list_tasks()})
            elif command == "stop_task":
                self.send({"id": request_id, "ok": True, "result": self.runner.stop_task()})
            elif command == "shutdown":
                self.send({"id": request_id, "ok": True, "result": {"shutdown": True}})
                self.runner.request_shutdown()
            elif command == "run_task":
                if self._worker is not None and self._worker.is_alive():
                    self.send({"id": request_id, "ok": False, "error": "task already running"})
                    return
                self._worker = threading.Thread(
                    target=self._run_task_worker,
                    args=(request_id, request),
                    name="okww-control-task",
                    daemon=True,
                )
                self._worker.start()
            else:
                self.send({"id": request_id, "ok": False, "error": f"unknown command: {command}"})
        except Exception as exc:  # noqa: BLE001
            logger.error("control command failed: %s", exc)
            logger.error(traceback.format_exc())
            self.send({"id": request_id, "ok": False, "error": f"{type(exc).__name__}: {exc}"})

    def _run_task_worker(self, request_id: Any, request: Dict[str, Any]) -> None:
        try:
            result = self.runner.run_task(request)
            self.send({"id": request_id, "ok": True, "result": result})
        except Exception as exc:  # noqa: BLE001
            logger.error("run_task failed: %s", exc)
            logger.error(traceback.format_exc())
            self.send({"id": request_id, "ok": False, "error": f"{type(exc).__name__}: {exc}"})


def main() -> int:
    parser = argparse.ArgumentParser(description="Headless ok-ww module runner")
    parser.add_argument("--okww-root", required=True, help="ok-ww working directory")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--token", required=True)
    parser.add_argument("--ready-file", required=True)
    parser.add_argument("--log", required=True)
    parser.add_argument("--humanize-config", default="")
    args = parser.parse_args()

    runner = ModuleRunner(
        okww_root=args.okww_root,
        host=args.host,
        port=args.port,
        token=args.token,
        ready_file=args.ready_file,
        log_file=args.log,
        humanize_config=args.humanize_config,
    )
    return runner.run()


if __name__ == "__main__":
    raise SystemExit(main())
