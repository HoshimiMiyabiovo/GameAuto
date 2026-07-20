from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

from .background_template_runner import run_cj_background, run_xm_background
from .bettergi_log_monitor import BetterGIDailyRewardSummary, format_bettergi_daily_reward_summary, start_bettergi_log_monitor
from .ok_ww_log_monitor import OkScriptAttemptResult, run_ok_script_with_retry
from .process_supervisor import ProcessSupervisor
from .whimbox_log_monitor import format_whimbox_task_summary, start_infinity_nikki_log_monitor
from ..settings import APPS, MIN_RUN_TIME, QBITTORRENT, get_app_display_name, get_script_display_name, refresh_runtime_config
from ..settings.discovery import prepare_app_for_runtime
from ..services.launch_task_catalog import is_standalone_task
from ..services.startup_sign_runner import run_startup_sign_tasks
from ..services.utils import format_duration, get_saved_script_choice


@dataclass
class TaskAppResult:
    app_key: str
    script_choice: Optional[str]
    effective_script_choice: Optional[str]
    display_name: str
    script_name: str
    label: str
    success: bool
    message: str
    duration: int
    time_str: str
    state: str
    report_lines: List[str] = field(default_factory=list)
    ok_script_attempt: Optional[OkScriptAttemptResult] = None
    bettergi_summary: Optional[BetterGIDailyRewardSummary] = None
    whimbox_lines: List[str] = field(default_factory=list)
    should_stop_batch: bool = False


@dataclass
class TaskBatchHooks:
    on_startup_lines: Optional[Callable[[List[str]], None]] = None
    on_before_app: Optional[Callable[[str, str, str], None]] = None
    on_before_ok_retry: Optional[
        Callable[[str, str, str, int, float, OkScriptAttemptResult], None]
    ] = None
    on_after_app: Optional[Callable[[TaskAppResult], None]] = None
    on_before_cj: Optional[Callable[[], None]] = None


@dataclass
class TaskBatchResult:
    report: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    startup_lines: List[str] = field(default_factory=list)


def ensure_qbittorrent_running(controller) -> bool:
    if not ProcessSupervisor.find_process(QBITTORRENT["process_name"]):
        controller.logger.info("启动 qBittorrent...")
        try:
            subprocess.Popen(
                [QBITTORRENT["path"]],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            controller.logger.info("qBittorrent 启动成功")
            return True
        except Exception as exc:
            controller.logger.error(f"qBittorrent 启动失败: {str(exc)}")
            return False
    controller.logger.debug("qBittorrent 已在运行")
    return True


def _run_single_task(
    controller,
    app_key: str,
    script_choice: Optional[str],
    *,
    cj_should_run: bool,
    before_ok_retry: Optional[
        Callable[[str, str, str, int, float, OkScriptAttemptResult], None]
    ] = None,
) -> TaskAppResult:
    effective_script_choice = script_choice or get_saved_script_choice(app_key)
    if not is_standalone_task(app_key) and prepare_app_for_runtime(app_key, effective_script_choice, controller.logger):
        refresh_runtime_config(force=True)

    app_config = APPS.get(app_key, {})
    display_name = get_app_display_name(app_key)
    script_name = get_script_display_name(app_key, effective_script_choice)
    label = f"{display_name}({script_name})"

    ok_script_attempt = None
    ok_retry_notice = ""
    bettergi_monitor = None
    nikki_completion_monitor = None

    if is_standalone_task(app_key):
        started_at = time.time()
        if app_key == "XMFlow":
            success, message = run_xm_background(controller.logger)
        else:
            success, message = run_cj_background(controller.logger)
        duration = max(int(time.time() - started_at), 1)
        if not message:
            message = f"{display_name}执行完成"
    elif app_config.get("ok_script_log_monitor") or app_config.get("ok_ww_log_monitor"):
        def _before_retry(next_attempt_no, restart_delay, attempt):
            if before_ok_retry is not None:
                before_ok_retry(app_key, display_name, script_name, next_attempt_no, restart_delay, attempt)

        attempts = run_ok_script_with_retry(
            controller,
            app_config,
            script_choice=script_choice,
            app_key=app_key,
            before_retry=_before_retry if before_ok_retry is not None else None,
        )
        final_attempt = attempts[-1]
        ok_script_attempt = final_attempt
        success, message, duration = final_attempt.success, final_attempt.message, final_attempt.duration
        if len(attempts) > 1:
            ok_retry_notice = (
                f"{display_name}({script_name})      ⚠️ 首次检测到 {app_config.get('script_name', app_key)} 日志异常停滞，已自动重试 1 次"
            )
        if (final_attempt.restart_requested or final_attempt.error_detected) and not controller.stop_current_task:
            success = False
            message = final_attempt.failure_reason or f"{app_config.get('script_name', app_key)} 日志异常停滞，自动重试后仍失败"
    else:
        if app_key == "GenshinImpact":
            bettergi_monitor = start_bettergi_log_monitor(app_config, controller.logger)
        if app_key == "InfinityNikki":
            nikki_completion_monitor = start_infinity_nikki_log_monitor(
                app_config,
                controller.logger,
                stop_game_on_completion=True,
            )

        try:
            success, message, duration = controller.execute_task(app_key, script_choice)
        finally:
            if bettergi_monitor is not None:
                bettergi_monitor.stop()
            if nikki_completion_monitor is not None:
                nikki_completion_monitor.wait_for_task_results(timeout=3.5)
                nikki_completion_monitor.stop()

    bettergi_summary = None
    if app_key == "GenshinImpact":
        bettergi_summary = bettergi_monitor.build_summary() if bettergi_monitor is not None else None
        if bettergi_summary is None or not bettergi_summary.all_completed:
            if not controller.stop_current_task:
                success = False
                message = bettergi_summary.failure_message() if bettergi_summary is not None else "BetterGI 日志监控不可用"

    whimbox_lines: List[str] = []
    if app_key == "InfinityNikki":
        whimbox_lines = format_whimbox_task_summary(nikki_completion_monitor)

    time_str = format_duration(duration)
    report_lines: List[str] = []
    if ok_retry_notice:
        report_lines.append(ok_retry_notice)

    stopped = controller.stop_current_task or "用户终止" in message or "快捷键" in message
    if stopped:
        state = "stopped"
        report_lines.append(f"{label:20} ⚠️ {message} (运行: {time_str})")
    elif not success:
        state = "failed"
        if "超时" in message:
            report_lines.append(f"{label:20} ❌ 失败: {message} (运行: {time_str}) [已终止进程]")
        else:
            report_lines.append(f"{label:20} ❌ 失败: {message} (运行: {time_str})")
    elif duration < MIN_RUN_TIME and not app_config.get("skip_min_run_warning"):
        state = "warning"
        report_lines.append(f"{label:20} ⚠️ 警告: {message} (仅运行: {time_str})")
    else:
        state = "success"
        report_lines.append(f"{label:20} ✅ 成功 (运行: {time_str})")

    if ok_script_attempt is not None and not success and ok_script_attempt.failure_reason:
        report_lines.append(f"  失败原因：{ok_script_attempt.failure_reason}")

    if app_key == "GenshinImpact":
        report_lines.extend(format_bettergi_daily_reward_summary(bettergi_summary))

    if app_key == "InfinityNikki":
        report_lines.extend(whimbox_lines)

    # 记录脚本执行结果到每日进度表（CLI/QML/Flet 通用，避免只在 Flet 里记录）
    try:
        from ..services.daily_progress_tracker import get_tracker
        from ..settings.paths import WORKFLOW_DB_PATH
        from ..settings import resolve_game_key
        _tracker = get_tracker(str(WORKFLOW_DB_PATH))
        # 映射 state → record_script_run status
        _progress_status_map = {
            "success": "success",
            "failed": "failed",
            "stopped": "skipped",
            "warning": "skipped",  # 运行时间过短，记为跳过
        }
        _progress_status = _progress_status_map.get(state, "failed")
        _tracker.record_script_run(
            game_key=resolve_game_key(app_key),
            game_name=display_name,
            script_name=script_name,
            status=_progress_status,
            message=message,
        )
    except Exception as _exc:
        controller.logger.warning("[任务] 记录每日进度失败: %s", _exc)

    return TaskAppResult(
        app_key=app_key,
        script_choice=script_choice,
        effective_script_choice=effective_script_choice,
        display_name=display_name,
        script_name=script_name,
        label=label,
        success=success,
        message=message,
        duration=duration,
        time_str=time_str,
        state=state,
        report_lines=report_lines,
        ok_script_attempt=ok_script_attempt,
        bettergi_summary=bettergi_summary,
        whimbox_lines=whimbox_lines,
        should_stop_batch=stopped,
    )


def run_task_batch(
    controller,
    target_apps: List[Tuple[str, Optional[str]]],
    *,
    xm_enabled: bool = False,
    cj_enabled: bool = False,
    hooks: Optional[TaskBatchHooks] = None,
    inter_task_delay_sec: float = 20.0,
) -> TaskBatchResult:
    hooks = hooks or TaskBatchHooks()
    report: List[str] = []
    warnings: List[str] = []

    has_regular_tasks = any(not is_standalone_task(app_key) for app_key, _ in target_apps)
    if has_regular_tasks:
        ensure_qbittorrent_running(controller)

    startup_lines = run_startup_sign_tasks([app_key for app_key, _ in target_apps])
    if startup_lines:
        report.extend(startup_lines)
        if hooks.on_startup_lines is not None:
            hooks.on_startup_lines(startup_lines)

    standalone_xm_selected = any(app_key == "XMFlow" for app_key, _ in target_apps)
    standalone_cj_selected = any(app_key == "CJFlow" for app_key, _ in target_apps)
    xm_result = {"ok": None, "msg": ""}
    nikki_selected = any(app_key == "InfinityNikki" for app_key, _ in target_apps)
    cj_should_run = cj_enabled and nikki_selected and not standalone_cj_selected
    xm_enabled = xm_enabled and not standalone_xm_selected

    for index, (app_key, script_choice) in enumerate(target_apps):
        display_name = get_app_display_name(app_key)
        effective_script_choice = script_choice or get_saved_script_choice(app_key)
        script_name = get_script_display_name(app_key, effective_script_choice)

        if hooks.on_before_app is not None:
            hooks.on_before_app(app_key, display_name, script_name)

        if app_key == "GenshinImpact" and xm_enabled and xm_result["ok"] is None:
            controller.logger.info("[XM流程] 在原神前执行 XM 前台流程")
            ok, msg = run_xm_background(controller.logger)
            xm_result["ok"] = ok
            xm_result["msg"] = msg
            if not ok:
                warnings.append(msg or "XM 前台流程失败")
            time.sleep(5)

        result = _run_single_task(
            controller,
            app_key,
            script_choice,
            cj_should_run=cj_should_run,
            before_ok_retry=hooks.on_before_ok_retry,
        )
        report.extend(result.report_lines)

        if hooks.on_after_app is not None:
            hooks.on_after_app(result)

        if result.should_stop_batch:
            break

        if index < len(target_apps) - 1:
            time.sleep(inter_task_delay_sec)

    if cj_should_run and not controller.stop_current_task:
        controller.logger.info("[CJ流程] 开始执行重聚前台流程（排在最后）")
        if hooks.on_before_cj is not None:
            hooks.on_before_cj()
        ok, msg = run_cj_background(controller.logger)
        if not ok:
            warnings.append(msg or "重聚前台流程失败")
        else:
            report.append(f"{'重聚前台流程':20} ✅ 成功")

    return TaskBatchResult(report=report, warnings=warnings, startup_lines=startup_lines)