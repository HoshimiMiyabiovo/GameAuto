import logging
import time
from typing import Callable, List, Optional, Tuple

from .foreground_flow_runner import run_flow


def run_xm_background(
    logger: logging.Logger,
    stop_check: Optional[Callable[[], bool]] = None,
) -> Tuple[bool, str]:
    return run_flow(logger, "XM", stop_after_step="运行中确认", stop_check=stop_check)


def run_cj_background(
    logger: logging.Logger,
    stop_check: Optional[Callable[[], bool]] = None,
) -> Tuple[bool, str]:
    return run_flow(logger, "CJ", stop_check=stop_check)


# 薅羊毛集合：依次执行的子流程 key（flow_configs.json），新增羊毛任务在此追加
WOOL_FLOW_KEYS: List[str] = ["MINIMAX", "WORKBUDDY", "TRAE", "QODER"]


def _close_wool_flow(logger: logging.Logger, flow_key: str, cfg, ok: bool) -> bool:
    """薅羊毛子流程收尾：按配置关闭对应软件（含子进程），避免残留窗口挡住下一个任务。

    成功看 close_process_on_finish、失败看 close_process_on_fail（都默认 True）。
    2026-10-29 修正：原实现只认 finish 开关，WorkBuddy 配置 fail=false 仍被关掉——
    它是用户日常聊天软件，必须尊重失败时"保留进程"的配置。
    """
    cfg = cfg or {}
    key = "close_process_on_finish" if ok else "close_process_on_fail"
    if not bool(cfg.get(key, True)):
        logger.info("[%s] 配置为不关闭程序，保留进程（%s）", flow_key, "成功" if ok else "失败")
        return False

    from .foreground_flow_runner import close_flow_processes

    try:
        close_flow_processes(logger, flow_key, process_names=cfg.get("window_process_names", []))
        return True
    except Exception as exc:
        logger.warning("[%s] 收尾关闭程序失败: %s", flow_key, exc)
        return False


def _flow_time_allowed(cfg, now_hm: Optional[str] = None) -> bool:
    """flow_configs.json 的 not_before（HH:MM）时间门槛。"""
    not_before = str((cfg or {}).get("not_before") or "").strip()
    if not not_before:
        return True
    return (now_hm or time.strftime("%H:%M")) >= not_before


def run_wool_flow_background(logger: logging.Logger, flow_keys: List[str]) -> Tuple[bool, str]:
    """按给定子流程顺序执行；支持 not_before 时间门槛（如 Qoder 10:00 刷新）。"""
    from .foreground_flow_runner import _load_flow_config

    messages = []
    all_ok = True
    ran_any = False
    for flow_key in flow_keys:
        cfg = _load_flow_config(flow_key)
        if cfg is not None and not cfg.get("enabled", True):
            messages.append(f"{flow_key} ⏭")
            continue
        not_before = str((cfg or {}).get("not_before") or "").strip()
        if not_before and not _flow_time_allowed(cfg):
            messages.append(f"{flow_key} ⏭ 未到 {not_before}")
            continue
        ran_any = True
        ok, msg = run_flow(logger, flow_key)
        _close_wool_flow(logger, flow_key, cfg, ok)
        # 每日进度仅需精简状态，详细 msg 已在日志中，避免卡片文字过长
        messages.append(f"{flow_key} {'✅' if ok else '❌'}")
        if not ok:
            all_ok = False
    if not ran_any:
        return True, "已跳过（全部禁用或未到刷新时间）"
    return all_ok, " ".join(messages)


def run_wool_background(logger: logging.Logger) -> Tuple[bool, str]:
    """薅羊毛集合：依次执行各羊毛流程，全部成功才算成功。

    flow_configs.json 中 enabled=false 的流程自动跳过（GUI 薅羊毛页可切换）。
    每个子流程结束后强制关闭对应软件（含子进程），失败也不例外。
    """
    return run_wool_flow_background(logger, WOOL_FLOW_KEYS)


def _run_monthly_flow(logger: logging.Logger, flow_key: str, label: str) -> Tuple[bool, str]:
    """每月流程通用门控：自然月记录在 workflow.sqlite3，本月已执行则跳过。"""
    from ..storage.workflow_db import WorkflowDB
    from ..settings.paths import WORKFLOW_DB_PATH

    month_key = time.strftime("%Y-%m")
    meta_key = f"monthly_flow:{flow_key}"
    db = WorkflowDB(str(WORKFLOW_DB_PATH))
    if db.get_task_meta(meta_key) == month_key:
        logger.info("[%s] 本月已执行，跳过", label)
        return True, "本月已执行，跳过"

    ok, msg = run_flow(logger, flow_key)
    if ok:
        db.set_task_meta(meta_key, month_key)
        return True, f"✅ {msg}（本月已记录，下月再执行）"
    return False, f"❌ {msg}（下次运行重试）"


def run_hsr_monthly_background(logger: logging.Logger) -> Tuple[bool, str]:
    return _run_monthly_flow(logger, "HSR_MONTHLY", "崩铁每月")


def run_genshin_monthly_background(logger: logging.Logger) -> Tuple[bool, str]:
    return _run_monthly_flow(logger, "GENSHIN_MONTHLY", "原神每月")


def run_zzz_monthly_background(logger: logging.Logger) -> Tuple[bool, str]:
    return _run_monthly_flow(logger, "ZZZ_MONTHLY", "绝区零每月")

