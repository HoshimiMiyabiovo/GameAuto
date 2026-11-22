# -*- coding: utf-8 -*-
"""「打开一次再关闭」型羊毛流程的收尾动作（2026-11-22 起）。

职责
----
给「奖励只认当天打开过客户端、不需要点任何按钮」的应用做统一的
**停留 + 关闭** 收尾。首个用户是商汤小浣熊桌面端（用户口径：
「每天打开一次再关闭就行了」）。启动、找窗口仍由
`foreground_flow_runner.run_flow` 既有链路负责（它是所有流程的公共骨架），
本模块只接管「窗口已经在了，接下来要不要关、怎么关」。

数据流
------
`run_flow` 启动 exe → 等窗口 → 命中内建动作 `wool_open_close_app`
→ 本模块 `open_close_app_once` → 返回 (ok, 说明) → 由流程末步记账。

安全约束（比"关掉它"重要）
--------------------------
1. **复用已有实例时绝不关闭**：目标应用正在被用户使用（或本身就是承载
   本程序的宿主，例如商汤小浣熊桌面端里跑着本自动化）时，关闭等于打断
   用户会话。这种情况按「今天已经打开过了」记成功并原样保留进程。
2. **祖先进程红线**：目标 pid 是本进程祖先链上的一环 → 说明它是宿主，
   只告警不关闭（双保险，防 `_wait_window_for_flow` 的陈旧窗口兜底把
   宿主 pid 当成"本轮新进程"交回来）。
3. **只关本轮自己拉起的进程**：pid 的创建时间早于 `max_process_age_sec`
   （默认 10 分钟）视为"本来就在跑"，不关；关闭只按 pid 精确结束进程树，
   **不按进程名批量清理**（同批兄弟流程的 by-name 清理会误杀宿主）。
   因此 flow 配置必须 `close_process_on_finish/fail=false`，否则
   `background_template_runner._close_wool_flow` 会按进程名兜底开杀。

失败模式
--------
返回 False 只在「拿不到可关闭的进程句柄」时发生（理论上不会：run_flow
已经等到窗口了）。关闭本身失败只降级为"进程保留"告警，不算流程失败——
奖励口径是"打开过"，进程多留一会儿不影响。
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable, Dict, Optional, Tuple

import psutil
import win32con
import win32gui

from ..services.task_heartbeat_watchdog import beat_thread_heartbeat

DEFAULT_HOLD_SEC = 10.0
DEFAULT_CLOSE_TIMEOUT_SEC = 20.0
DEFAULT_MAX_PROCESS_AGE_SEC = 600.0
_POLL_SEC = 0.5


def _pid_is_self_ancestor(pid: int) -> bool:
    """目标 pid 是否在本进程的祖先链上（=宿主应用，绝不能关）。"""
    if not pid:
        return False
    try:
        current = psutil.Process(os.getpid())
        for ancestor in current.parents():
            if ancestor.pid == int(pid):
                return True
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False
    except Exception:  # noqa: BLE001 - 护栏判定失败按"不是祖先"处理，另有年龄闸
        logging.getLogger("GameAutomation").debug("祖先链判定失败 pid=%s", pid, exc_info=True)
    return False


def _process_age_sec(pid: int) -> Optional[float]:
    """目标进程已存活秒数；读不到返回 None（调用方按"可关闭"继续，另有年龄判断）。"""
    try:
        return max(0.0, time.time() - psutil.Process(int(pid)).create_time())
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None
    except Exception:  # noqa: BLE001
        return None


def _process_alive(pid: int) -> bool:
    try:
        return psutil.pid_exists(int(pid)) and not psutil.Process(int(pid)).status() == psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False
    except Exception:  # noqa: BLE001
        return False


def _sleep_watch(sec: float, pid: Optional[int], stop_check: Optional[Callable[[], bool]] = None) -> bool:
    """停留等待，期间每 0.5s 探一次目标进程。

    返回 False 表示目标进程已自行退出（Electron 单实例交接、启动即崩等），
    调用方据此跳过关闭、直接记成功。可被 stop_check 提前打断（GUI「停止」）。
    """
    end = time.time() + max(0.0, float(sec))
    while time.time() < end:
        if stop_check is not None and stop_check():
            return True
        if pid and not _process_alive(int(pid)):
            return False
        # 看门狗续命：本模块的停留/等待循环是新增的长等待点（默认 10s，
        # 可配到分钟级），按 06-robustness 的约定每轮都 beat，避免被误判卡死。
        beat_thread_heartbeat()
        time.sleep(_POLL_SEC if end - time.time() > _POLL_SEC else max(0.0, end - time.time()))
    return True


def _request_graceful_close(hwnd: Optional[int], logger: logging.Logger, flow_key: str) -> bool:
    """向主窗口发 WM_CLOSE，让 Electron 自己走退出流程（存档/埋点有落盘机会）。"""
    if not hwnd:
        return False
    try:
        win32gui.PostMessage(int(hwnd), win32con.WM_CLOSE, 0, 0)
        logger.info(f"[{flow_key}] 已发送 WM_CLOSE 请求关闭窗口 HWND={hwnd}")
        return True
    except Exception as exc:  # noqa: BLE001 - 关不掉会走强制结束兜底
        logger.warning(f"[{flow_key}] WM_CLOSE 发送失败: {exc}")
        return False


def _force_close_tree(pid: int, logger: logging.Logger, flow_key: str) -> bool:
    """兜底：按 pid 精确结束进程树（**不**按进程名批量清理）。"""
    from .process_supervisor import ProcessSupervisor

    try:
        ProcessSupervisor.safe_kill_process(int(pid), timeout=3)
        logger.info(f"[{flow_key}] 已强制结束进程树 PID={pid}")
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[{flow_key}] 强制结束进程 PID={pid} 失败: {exc}")
        return False


def _created_before_today(pid: int) -> bool:
    """进程是否在今天 0 点之前就已启动（跨天未关的旧会话）。

    「打开一次」型奖励按启动事件发放：昨天开着没关的进程今天不一定会重新入账。
    这类实例同样**不能**自动关闭（可能是宿主或用户正在用的会话），只提示用户
    手动重启一次。读不到创建时间按"不是旧会话"处理，不打扰。
    """
    try:
        started = psutil.Process(int(pid)).create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False
    except Exception:  # noqa: BLE001
        return False
    import datetime as _dt

    today_start = _dt.datetime.combine(_dt.date.today(), _dt.time.min).timestamp()
    return started < today_start


def open_close_app_once(
    logger: logging.Logger,
    flow_key: str,
    *,
    hwnd: Optional[int],
    pid: Optional[int],
    reused_window: bool = False,
    step: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, str]:
    """停留若干秒后关闭本轮拉起的客户端；复用已有实例时原样保留。

    参数
    ----
    hwnd / pid : run_flow 已定位到的目标窗口与进程（pid 可能被窗口承载进程改写）。
    reused_window : run_flow 是否复用了"执行前就已存在"的窗口。True 时本函数
        只记账不动作（今天已经被打开过，关掉等于打断用户）。
    step : 流程配置里的步骤字典，可含 hold_sec / close_timeout_sec /
        graceful_close / max_process_age_sec / app_label。

    返回 (ok, 说明)。ok=False 仅在「没有可关闭的进程句柄」时出现。
    """
    step = step or {}
    hold_sec = float(step.get("hold_sec", DEFAULT_HOLD_SEC))
    close_timeout = float(step.get("close_timeout_sec", DEFAULT_CLOSE_TIMEOUT_SEC))
    graceful = bool(step.get("graceful_close", True))
    max_age = float(step.get("max_process_age_sec", DEFAULT_MAX_PROCESS_AGE_SEC))
    app_label = str(step.get("app_label") or flow_key).strip()

    if reused_window:
        logger.info(
            f"[{flow_key}] 执行前已有 {app_label} 实例（今日已打开过），"
            "按需保留、不做打开/关闭动作"
        )
        if pid and _created_before_today(int(pid)):
            logger.warning(
                f"[{flow_key}] 该 {app_label} 实例是今天 0 点前启动的（跨天未关），"
                "「打开一次」型奖励可能未入账；需要时请手动重启一次客户端"
            )
        return True, f"复用已开启的 {app_label}，无需打开"

    if not pid:
        return False, f"未拿到 {app_label} 进程句柄，无法确认打开状态"

    pid = int(pid)
    if _pid_is_self_ancestor(pid):
        logger.warning(
            f"[{flow_key}] 目标进程 PID={pid} 是本程序的宿主（祖先进程），"
            "为避免打断当前会话改为不关闭"
        )
        return True, f"{app_label} 由本程序宿主承载，跳过关闭"

    age = _process_age_sec(pid)
    if age is not None and age > max_age:
        logger.warning(
            f"[{flow_key}] 目标进程 PID={pid} 已运行 {age:.0f}s（> {max_age:.0f}s），"
            "判定为执行前就已存在，跳过关闭"
        )
        return True, f"{app_label} 为既有实例，跳过关闭"

    logger.info(f"[{flow_key}] {app_label} 已打开，停留 {hold_sec:.0f}s 等待奖励入账后关闭")
    still_running = _sleep_watch(hold_sec, pid)
    if not still_running:
        logger.info(f"[{flow_key}] 停留期间 {app_label} 已自行退出（单实例交接/启动即退），不再关闭")
        return True, f"{app_label} 已打开（停留期间自行退出）"

    if graceful:
        _request_graceful_close(hwnd, logger, flow_key)
        # 先立即查一次再进等待循环：close_timeout_sec=0 表示"只查一次"，
        # 不能因为循环条件直接不成立就跳过优雅退出的确认（否则永远走强杀）。
        end = time.time() + close_timeout
        while True:
            if not _process_alive(pid):
                logger.info(f"[{flow_key}] {app_label} 已正常退出（PID={pid}）")
                return True, f"{app_label} 已打开并关闭"
            if time.time() >= end:
                break
            beat_thread_heartbeat()
            time.sleep(_POLL_SEC)

    _force_close_tree(pid, logger, flow_key)
    exited = not _process_alive(pid)
    if not exited:
        logger.warning(f"[{flow_key}] {app_label} 进程仍在 PID={pid}，本次按已打开记成功（不影响奖励口径）")
        return True, f"{app_label} 已打开（关闭未完成，进程保留）"
    return True, f"{app_label} 已打开并关闭"
