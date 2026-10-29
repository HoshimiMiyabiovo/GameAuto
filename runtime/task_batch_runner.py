"""任务批次编排与事后判定（一条龙的大脑）。

一条龙（run_task_batch）把用户勾选的应用按顺序串行执行，每个应用经过：
  1. 预检：跳过今日已完成（_is_game_script_done_today，standalone 任务豁免、
     手动"强制重跑"勾选可无视）→ 维护窗口跳过（GAME_UPDATING_SKIP）；
  2. 前置流程：XM/白荆周常等挂在指定游戏之前/之后（xm_enabled/cj_enabled）；
  3. _run_single_task：真正启动游戏与脚本（execute_task），跑完后：
     - BetterGI 日志摘要（原神）、whimbox 任务摘要（暖暖）生成报告行；
     - 事后判定三兄弟：_apply_bettergi_verdict（原神双向）/
       _apply_starrail_score_verdict（崩铁超时兜底）/
       _apply_zzz_activity_verdict（绝区零超时兜底）；
     - 事后日志复核 _verify_script_log_result：success→failed 改判（永不反向），
       终末地/异环合并 API 关键计数兜底；
     - record_script_run 落库（script_ 前缀，"今日已完成跳过"的数据源）；
     - _auto_query_daily_progress 查便笺 API 补数据。
  4. 统一重试：首轮失败项等 8 秒重试一次（user 停止/FailSafe 除外）。

**收敛兜底铁律**：邮件、自动关机、每日进度刷新、WebUI 状态复位全部必须放在
调用方（bridge._run_automation_backend）的 finally 里——任何任务内未捕获异常
都会穿透 run_task_batch（2026-09-29 事故：挂机整夜没关机没发邮件）。

**FailSafe 契约**：pyautogui 触碰屏幕角落的紧急停止被 _pyautogui_click_guarded
转成 FailSafeAborted 抛出，本层收口成 should_stop_batch=True——优雅终止整批、
不重试不继续，绝不裸抛 traceback。

关键数据结构：TaskAppResult（单任务结果，state: success/failed/skipped/warning）、
TaskBatchHooks（bridge 注入的进度回调）、TaskBatchResult（report 行 + warnings）。
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .automation_controller import GAME_UPDATING_SKIP
from .background_template_runner import (
    run_cj_background,
    run_genshin_monthly_background,
    run_hsr_monthly_background,
    run_wool_background,
    run_wool_flow_background,
    run_xm_background,
    run_zzz_monthly_background,
)
from .bettergi_log_monitor import BetterGIDailyRewardSummary, format_bettergi_daily_reward_summary, start_bettergi_log_monitor
from .foreground_flow_runner import FailSafeAborted
from .ok_ww_log_monitor import OkScriptAttemptResult, run_ok_script_once
from .process_supervisor import ProcessSupervisor
from .whimbox_log_monitor import format_whimbox_task_summary, start_infinity_nikki_log_monitor
from ..settings import APPS, MIN_RUN_TIME, QBITTORRENT, get_app_display_name, get_script_display_name, refresh_runtime_config, resolve_game_key
from ..settings.discovery import prepare_app_for_runtime
from ..settings.paths import WORKFLOW_DB_PATH
from ..services.daily_progress_tracker import get_tracker
from ..services.launch_task_catalog import is_standalone_task
from ..services.startup_sign_runner import run_startup_sign_tasks
from ..services.utils import format_duration, get_saved_script_choice


def _interruptible_sleep(controller, seconds: float, step: float = 1.0):
    """可被 stop_current_task 中断的睡眠，每 step 秒检查一次停止标志。"""
    while seconds > 0 and not controller.stop_current_task:
        time.sleep(min(step, seconds))
        seconds -= step


def _is_game_script_done_today(app_key: str) -> bool:
    """检查今日该游戏的脚本任务是否已全部成功。

    只看 script_ 前缀任务（不看 sign_）。如果今日有 script_ 任务且全部
    success/already，返回 True；否则（有 failed 或无记录）返回 False。

    2026-10-24 修正：脚本行 success ≠ 当日已完成。白荆回廊把部分完成
    （如 1/15 任务、活跃度 5/100）也记 success（10-20 设计：避免失败重试），
    曾导致手动运行被"今日脚本任务已全部成功"跳过（实际远未完成）。
    现在脚本成功之外还要求关键指标确认完成（completion_state=completed）；
    unknown（关键计数未刷新，如官方 API 没给数）信任脚本结果，维持旧行为。
    """
    try:
        game_key = resolve_game_key(app_key)
        if not game_key:
            return False
        tracker = get_tracker(WORKFLOW_DB_PATH)
        snapshot = tracker.load_today()
        for game in snapshot.games:
            if game.game_key != game_key:
                continue
            script_tasks = [t for t in game.tasks if t.task_key.startswith("script_")]
            if not script_tasks:
                return False
            if not all(t.status in ("success", "already") for t in script_tasks):
                return False
            # 脚本成功 ≠ 当日完成：关键指标未满（白荆 1/15、活跃度 5/100）
            # 或有关键失败时视为未完成，允许手动/补跑重新执行
            return game.completion_state() in ("completed", "unknown")
        return False
    except Exception:
        return False


@dataclass
class _StopFlagAdapter:
    """把 controller.stop_current_task 布尔检查适配成 threading.Event.is_set 接口。"""

    check: Callable[[], bool]

    def is_set(self) -> bool:
        """threading.Event 接口兼容：让 Event.wait 轮询 controller 的停止标志。"""
        return bool(self.check())


# 白荆回廊一条龙：主流程结束（停在主界面）后追加执行的每周点击序列
BAIJING_WEEKLY_SEQUENCE = "白荆回廊-每周商店购买"


def run_weekly_click_sequence(
    name: str,
    logger,
    stop_check: Optional[Callable[[], bool]] = None,
) -> Optional[str]:
    """每周执行一次指定点击序列（ISO 周门控，成功后才记录本周已执行）。

    调用方必须保证目标窗口已就位——白荆回廊主流程的结束条件就是
    识别到主界面，所以挂在主流程成功之后即满足"必须在主界面才能运行"。
    本周已执行返回 None（跳过）；失败不记录，本周内下次运行会重试。
    返回报告行文本。
    """
    try:
        from .click_sequence import ClickSequencePlayer, _resolve_window
        from ..storage.workflow_db import WorkflowDB

        db = WorkflowDB(str(WORKFLOW_DB_PATH))
        week_key = time.strftime("%G-W%V")
        meta_key = f"weekly_sequence:{name}"
        if db.get_task_meta(meta_key) == week_key:
            logger.info("[每周序列] %s 本周已执行，跳过", name)
            return None

        player = ClickSequencePlayer(logger)
        seq = player.load(name)
        # 回放前把目标窗口还原/调回前台，保证点击落点正确
        hwnd, _title, _rect = _resolve_window(seq.window_title_keyword)
        if hwnd:
            try:
                import win32con
                import win32gui
                if win32gui.IsIconic(hwnd):
                    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                win32gui.SetForegroundWindow(hwnd)
            except Exception:
                logger.debug("[每周序列] 目标窗口置前失败（继续尝试回放）", exc_info=True)
        time.sleep(0.8)

        stop_event = _StopFlagAdapter(stop_check) if stop_check else None
        ok = player.play(seq, stop_event=stop_event)
        if ok:
            db.set_task_meta(meta_key, week_key)
            logger.info("[每周序列] %s 执行成功，本周不再重复", name)
            return "  🛒 白荆回廊每周商店购买: ✅ 已完成"
        logger.warning("[每周序列] %s 执行失败，下次运行将重试", name)
        return "  🛒 白荆回廊每周商店购买: ❌ 失败（下次运行重试）"
    except Exception as exc:
        logger.warning("[每周序列] %s 执行异常: %s", name, exc, exc_info=True)
        return f"  🛒 白荆回廊每周商店购买: ❌ 异常: {exc}"


@dataclass
class TaskAppResult:
    """单个应用的执行结果（_run_single_task 产出，run_task_batch 汇总上报）。

    state 五态：success / failed / skipped（维护跳过、今日已完成跳过、用户终止）
    / warning（运行时长不足 MIN_RUN_TIME 但脚本正常退出）/ stopped。
    should_stop_batch=True 表示发生不可恢复事件（FailSafe 紧急停止），整批终止。
    report_lines 是该任务的明细行（邮件正文与 UI 报告共用）。
    """
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
    """bridge 注入的进度回调（QML/WebUI 实时刷新用，全部可选）。

    on_startup_lines: 启动前签到输出；on_before_app: 单任务即将开始（UI 置
    running 态）；on_before_ok_retry: ok 系脚本重试前；on_after_app: 单任务
    结束（写报告行）；on_before_cj: CJ 流程开始前。
    """
    on_startup_lines: Optional[Callable[[List[str]], None]] = None
    on_before_app: Optional[Callable[[str, str, str], None]] = None
    on_before_ok_retry: Optional[
        Callable[[str, str, str, int, float, OkScriptAttemptResult], None]
    ] = None
    on_after_app: Optional[Callable[[TaskAppResult], None]] = None
    on_before_cj: Optional[Callable[[], None]] = None


@dataclass
@dataclass
class TaskBatchResult:
    """整批执行结果：report 为汇总报告行（邮件/UI 共用），warnings 为非致命提示，
    startup_lines 为启动前签到的输出段。"""
    report: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    startup_lines: List[str] = field(default_factory=list)


def _apply_bettergi_verdict(success: bool, message: str, summary, stop_requested: bool) -> Tuple[bool, str]:
    """原神最终判定以 BetterGI 日志为准（用户拍板 2026-10-24，双向覆盖）。

    监控跟踪的两项是「委托奖励」与「探索派遣」（BetterGILogMonitor._process_line）：
    - 日志未确认全部完成 → 失败（与旧口径一致，只降不升，避免超时漏判半途任务）；
    - 日志已确认全部完成 → 覆盖超时/退出异常等一票否决：BetterGI 实际已把委托
      奖励、探索派遣做完，监控超时只是收尾被杀——判成功，天然不触发统一重试；
    - 用户主动停止时不做任何覆盖，保持原判；
    - 监控不可用（None）保持旧的 fail-closed：成功也改判"日志监控不可用"。
    米游社便笺（每日委托计数）不再作为原神判定依据——cookie 过期时它整月无数据，
    曾把判定退化成"BetterGI 脚本行说了算"（2026-10-24 排查结论）。
    """
    if stop_requested:
        return success, message
    if summary is None:
        return False, "BetterGI 日志监控不可用"
    if not summary.all_completed:
        return False, summary.failure_message()
    if not success:
        return True, "BetterGI 日志确认今日委托奖励与探索派遣已完成"
    return success, message


def _apply_starrail_score_verdict(success: bool, message: str, logger, stop_requested: bool) -> Tuple[bool, str]:
    """崩铁（March7th）超时兜底：实训分数已达标的运行不再按超时一票否决。

    用户拍板 2026-10-24，口径与原神 `_apply_bettergi_verdict` 对齐：
    March7th 日志里的「当前累计分数：X/500」达到上限即当日实训（活跃度）拿满，
    超时只是收尾被杀——改判成功，天然不触发统一重试。分数没达标/读不到时
    保持原判（部分完成的重试仍有意义；分数行只在跑到实训检查时才出现）。
    仅在 script=March7th 时启用（StarRailOneDragon 无此日志）。
    """
    if stop_requested or success:
        return success, message
    try:
        from ..services.starrail_daily_parser import parse_latest_starrail_log
        data = parse_latest_starrail_log()
        if not data:
            return success, message
        for t in data.get("tasks") or []:
            if str(t.get("task_name") or "") != "活跃度":
                continue
            extra = t.get("extra") or {}
            try:
                cur, total = int(extra.get("current")), int(extra.get("total"))
            except (TypeError, ValueError):
                continue
            if total > 0 and cur >= total:
                logger.info("[星铁] 实训分数已达 %s/%s，超时运行改判成功", cur, total)
                return True, "March7th 日志确认实训分数已达标"
    except Exception as exc:
        logger.debug("[星铁] 实训分数兜底判定跳过: %s", exc)
    return success, message


def _apply_zzz_activity_verdict(success: bool, message: str, logger, stop_requested: bool) -> Tuple[bool, str]:
    """绝区零（OneDragon）超时兜底：活跃度已满的运行不再按超时一票否决。

    用户拍板 2026-10-24，口径与原神/崩铁对齐：OneDragon 日志里「活跃度奖励」
    节点的「活跃度已满」即当日活跃度拿满（等价于崩铁的分数达标），超时只是
    收尾被杀——改判成功，天然不触发统一重试。仅认「活跃度已满」这个显式信号
    （「日常奖励领取成功」可能是中途领箱，活跃度未必满）。日志未满/读不到时
    保持原判。OneDragon 只打枚举状态不打数值，因此没有崩铁那样的三态百分比。
    """
    if stop_requested or success:
        return success, message
    try:
        from ..services.zzz_daily_parser import parse_latest_zzz_log
        data = parse_latest_zzz_log()
        if not data:
            return success, message
        for t in data.get("tasks") or []:
            if str(t.get("task_name") or "") != "活跃度奖励":
                continue
            if t.get("status") == "success" and str(t.get("message") or "") == "活跃度已满":
                logger.info("[绝区零] 活跃度已满，超时运行改判成功")
                return True, "OneDragon 日志确认活跃度已满"
            break
    except Exception as exc:
        logger.debug("[绝区零] 活跃度兜底判定跳过: %s", exc)
    return success, message


def _load_api_key_progress_tasks(game_key: str, logger) -> List[Dict[str, Any]]:
    """从每日进度表取今天 API 侧的关键进度任务（命中 KEY_PROGRESS_TASKS 的非脚本项）。

    供事后日志复核合并进脚本记录：终末地的整体完成以查询接口的日活跃为准
    （用户拍板口径），⭐送礼 等边角脚本任务失败不应改判整轮失败。
    读不到（无记录/查询异常）时返回空表，复核退回纯脚本口径，保留兜底改判能力。
    """
    try:
        from ..services.daily_progress_tracker import KEY_PROGRESS_TASKS

        names = KEY_PROGRESS_TASKS.get(game_key) or ()
        if not names:
            return []
        snapshot = get_tracker(WORKFLOW_DB_PATH).load_today()
        for game in snapshot.games:
            if game.game_key != game_key:
                continue
            merged: List[Dict[str, Any]] = []
            for t in game.tasks:
                if (t.task_name or t.task_key) not in names or t.source == "script":
                    continue
                merged.append({
                    "task_key": t.task_key,
                    "task_name": t.task_name,
                    "status": t.status,
                    "message": t.message,
                    "extra": dict(t.extra or {}),
                })
            return merged
        return []
    except Exception as exc:
        logger.debug("[日志复核] 合并 %s API 关键进度任务失败: %s", game_key, exc)
        return []


def _verify_script_log_result(app_key: str, effective_script_choice: Optional[str],
                              display_name: str, logger) -> Optional[str]:
    """事后日志复核：只做 success→failed 改判，不做反向；无当日日志时保持原判。

    覆盖：绝区零 OneDragon / 星铁 March7th / 鸣潮 ok-ww / 终末地 ok-ef / 异环 ok-nte /
    无限暖暖 whimbox（关键判定=朝夕心愿）。
    判定口径与展示卡片一致：走 GameDailyRecord.completion_state()，
    关键任务（如异环"日常领取"）说了算，而不是"有任何失败就全盘失败"——
    否则边角任务（如异象家具）一挂就会触发整轮重试，而领取类任务不可重入，
    重跑反而把已领取项全判失败（2026-10-03 事故）。
    无任务可判时沿用旧逻辑（parser 自带的 is_all_success）。
    终末地额外把当日 API 侧关键进度（日活跃）合并进记录，见 _load_api_key_progress_tasks。
    返回失败原因字符串则改判，返回 None 保持原判。
    """
    try:
        from ..services.daily_progress_tracker import DailyTaskRecord, GameDailyRecord

        tag = ""
        data = None
        if app_key == "ZenlessZoneZero":
            from ..services.zzz_daily_parser import parse_latest_zzz_log
            data = parse_latest_zzz_log()
            tag = "OneDragon"
        elif app_key == "HonkaiStarRail" and (effective_script_choice or "") == "March7th":
            # 只校验 March7th 日志；StarRailOneDragon 无对应日志文件，不误判
            from ..services.starrail_daily_parser import parse_latest_starrail_log
            data = parse_latest_starrail_log()
            tag = "March7th"
        elif app_key == "WutheringWaves":
            from ..services.ok_ww_daily_parser import parse_latest_ok_ww_log
            data = parse_latest_ok_ww_log()
            tag = "ok-ww"
        elif app_key == "ArknightsEndfield":
            from ..services.ok_ef_daily_parser import parse_latest_ok_ef_report
            data = parse_latest_ok_ef_report()
            tag = "ok-ef"
            if data is not None:
                # 复核口径：合并查询接口的日活跃（KEY_PROGRESS_TASKS["endfield"]），
                # 有 API 关键任务时由真实活跃度说了算；无 API 数据则退回脚本口径
                data.setdefault("tasks", []).extend(
                    _load_api_key_progress_tasks(str(data.get("game_key") or "endfield"), logger))
        elif app_key == "NevernessToEverness":
            from ..services.ok_nte_daily_parser import parse_latest_ok_nte_log
            data = parse_latest_ok_nte_log()
            tag = "ok-nte"
            if data is not None:
                # dayvalue 复核兜底（2026-10-24）：塔吉多 roleHome 的活跃度满 100
                # → 当日完成，ok-nte 边角任务失败不再改判
                data.setdefault("tasks", []).extend(
                    _load_api_key_progress_tasks("nte", logger))
                for t in data["tasks"]:
                    if str(t.get("task_name") or "") != "活跃度":
                        continue
                    extra = t.get("extra") or {}
                    try:
                        cur, total = int(extra.get("current")), int(extra.get("total"))
                    except (TypeError, ValueError):
                        continue
                    if total > 0 and cur >= total:
                        return None
        elif app_key == "InfinityNikki":
            # 重试关键判定：朝夕心愿（KEY_STATUS_TASKS["nikki"]）成功即整体完成，
            # 美鸭梨挖掘/星海拾光等边角任务失败不改判（whimbox 解析器恒定输出 8 项）
            from ..services.whimbox_daily_parser import parse_latest_whimbox_log
            data = parse_latest_whimbox_log()
            tag = "whimbox"
        else:
            return None
        if data is None:
            return None
        tasks = data.get("tasks") or []
        if not tasks:
            # 解析不到任何任务：沿用 parser 自带结论（fail-closed，不 silent 成功）
            if data.get("is_all_success", False):
                return None
            errs = [str(e) for e in (data.get("errors") or []) if str(e).strip()]
            return "；".join(errs) if errs else f"{tag} 未正常结束"
        rec = GameDailyRecord(
            game_key=str(data.get("game_key") or ""),
            game_name=str(data.get("game_name") or ""),
            tasks=[DailyTaskRecord(
                task_key=str(t.get("task_key") or t.get("task_name") or ""),
                task_name=str(t.get("task_name") or t.get("task_key") or ""),
                source="script",
                status=str(t.get("status") or "pending"),
                message=str(t.get("message") or ""),
                extra=dict(t.get("extra") or {}),
            ) for t in tasks if isinstance(t, dict)],
            errors=list(data.get("errors") or []),
        )
        if rec.completion_state() != "failed":
            return None
        errs = [str(e) for e in (data.get("errors") or []) if str(e).strip()]
        return "；".join(errs) if errs else f"{tag} 未正常结束"
    except Exception as exc:
        logger.debug("[%s] 日志复核跳过: %s", display_name, exc)
        return None


def ensure_qbittorrent_running(controller) -> bool:
    """确保 qBittorrent 在运行（下载类任务依赖；已运行返回 False）。

    未配置/路径失效时静默跳过——它不是所有批次的硬依赖。
    """
    # 2026-10-24 修正：path 为空（默认值，用户未配置）时 Popen([""]) 抛
    # WinError 87 参数错误，每个批次都刷一条 ERROR——未配置就直接跳过
    qbt_path = str(QBITTORRENT.get("path") or "").strip()
    if not qbt_path:
        controller.logger.debug("qBittorrent 未配置路径，跳过启动")
        return False
    if not ProcessSupervisor.find_process(QBITTORRENT["process_name"]):
        controller.logger.info("启动 qBittorrent...")
        try:
            subprocess.Popen(
                [qbt_path],
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


def _auto_query_daily_progress(controller, game_key: str, display_name: str) -> None:
    """一条龙跑完后，自动查询该游戏的每日数据并写入每日进度表。

    只查刚跑完的这一个游戏，不整批全量查询，避免无谓的接口调用。
    任何异常都不影响主流程——一条龙已经跑完，结果不受影响。
    """
    try:
        from ..clients.daily_checker import check_games_progress
        from ..settings import manager as cfg_manager

        config = cfg_manager.load(force=True)
        tracker = get_tracker(str(WORKFLOW_DB_PATH))
        result = check_games_progress(config, game_keys=[game_key], tracker=tracker)

        for game in result.games:
            if game.errors:
                controller.logger.info("[任务] %s 每日数据未取到：%s",
                                       display_name, "; ".join(map(str, game.errors)))
            else:
                done = sum(1 for t in game.tasks if t.is_completed)
                controller.logger.info(
                    "[任务] %s 每日数据已自动填入：%s（%d/%d 项完成）",
                    display_name, game.game_name, done, len(game.tasks))
    except Exception as exc:  # noqa: BLE001 - 自动查询失败绝不能影响主流程
        controller.logger.warning("[任务] %s 自动查询每日数据失败: %s", display_name, exc)


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
    """执行单个应用并产出结果行（run_task_batch 的核心，承担全部 per-app 分支）。

    分支结构（按 app_key 分派，读时自上而下）：
    - standalone 任务（WoolFlow/QoderFlow/HsrMonthly/ZzzMonthly/XM/CJ）→
      对应 background/flow runner；
    - ok 系脚本（ok-ww/ok-ef/ok-nte）→ run_ok_script_once（只跑一次，
      重试交给 run_task_batch 的统一重试）；
    - 其余（原神 BetterGI/崩铁 March7th/绝区零 OneDragon/暖暖 whimbox）→
      controller.execute_task + 各自的日志监控器。
    收尾统一做：事后判定三兄弟 → 日志复核改判 → record_script_run 落库 →
    _auto_query_daily_progress 补便笺数据。任何分支都不允许抛异常穿透
    （FailSafeAborted 在此收口为 should_stop_batch）。
    """
    effective_script_choice = script_choice or get_saved_script_choice(app_key)
    # 官方公告维护窗口内先别启动：快速记 skipped，桥接层会按公告开服时间自动补跑。
    # 比让 launcher/脚本白等 5~60 分钟再失败更稳，也不会留下半开的启动器。
    if not is_standalone_task(app_key):
        try:
            from ..services.game_update_calendar import get_maintenance_state
            _maint = get_maintenance_state(app_key)
        except Exception:
            _maint = None
        if _maint:
            display_name = get_app_display_name(app_key)
            script_name = get_script_display_name(app_key, effective_script_choice)
            label = f"{display_name}({script_name})"
            open_dt = _maint.get("open_at_dt")
            open_txt = open_dt.strftime("%H:%M") if open_dt else "?"
            message = f"游戏维护中，预计 {open_txt} 开服（{_maint.get('version') or '版本更新'}）"
            controller.logger.info("[%s] %s，跳过本次启动并安排补跑", display_name, message)
            return TaskAppResult(
                app_key=app_key,
                script_choice=script_choice,
                effective_script_choice=effective_script_choice,
                display_name=display_name,
                script_name=script_name,
                label=label,
                success=False,
                message=message,
                duration=0,
                time_str="0秒",
                state="skipped",
                report_lines=[f"{label:20} ⏭️ {message}"],
                should_stop_batch=False,
            )
    if not is_standalone_task(app_key) and prepare_app_for_runtime(app_key, effective_script_choice, controller.logger):
        refresh_runtime_config(force=True)

    app_config = APPS.get(app_key, {})
    display_name = get_app_display_name(app_key)
    script_name = get_script_display_name(app_key, effective_script_choice)
    label = f"{display_name}({script_name})"

    ok_script_attempt = None
    bettergi_monitor = None
    nikki_completion_monitor = None
    weekly_seq_line = None

    if is_standalone_task(app_key):
        started_at = time.time()
        try:
            if app_key == "XMFlow":
                success, message = run_xm_background(
                    controller.logger,
                    stop_check=lambda: controller.stop_current_task,
                )
            elif app_key == "WoolFlow":
                success, message = run_wool_background(controller.logger)
            elif app_key == "QoderFlow":
                success, message = run_wool_flow_background(controller.logger, ["QODER"])
            elif app_key == "HsrMonthlyFlow":
                success, message = run_hsr_monthly_background(controller.logger)
            elif app_key == "ZzzMonthlyFlow":
                success, message = run_zzz_monthly_background(controller.logger)
            elif app_key == "GenshinMonthlyFlow":
                success, message = run_genshin_monthly_background(controller.logger)
            else:
                success, message = run_cj_background(
                    controller.logger,
                    stop_check=lambda: controller.stop_current_task,
                )
        except FailSafeAborted as exc:
            # 鼠标被放在屏幕左上角触发 pyautogui 紧急停止。
            # 视为用户想停：优雅失败 + 停止整批（不重试、不跑后续任务）。
            duration = max(int(time.time() - started_at), 1)
            controller.logger.warning("[%s] %s", display_name, exc)
            return TaskAppResult(
                app_key=app_key,
                script_choice=script_choice,
                effective_script_choice=script_choice,
                display_name=display_name,
                script_name=app_key,
                label=display_name,
                success=False,
                message=str(exc),
                duration=duration,
                time_str=time.strftime("%H:%M:%S"),
                state="failed",
                report_lines=[f"⏹ {exc}"],
                should_stop_batch=True,
            )
        duration = max(int(time.time() - started_at), 1)
        if not message:
            message = f"{display_name}执行完成"
    elif app_config.get("ok_script_log_monitor") or app_config.get("ok_ww_log_monitor"):
        # OK 系列（ok-ww / ok-ef）：只跑一次，不内部重试。
        # 重试统一由 run_task_batch 的失败重试逻辑处理。
        ok_script_attempt = run_ok_script_once(
            controller,
            app_config,
            script_choice=script_choice,
            app_key=app_key,
        )
        success, message, duration = ok_script_attempt.success, ok_script_attempt.message, ok_script_attempt.duration
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

        # 白荆回廊：周常已在 baijing_flow.run_baijing_full_flow 内执行（日常→验证→周常→关闭一条龙）
        # 此处保留兜底：若 full_flow 未执行周常（如旧版本/单测），则补跑一次；已执行则 meta 命中直接跳过
        weekly_seq_line = None
        if app_key == "BaiJing" and success and not controller.stop_current_task:
            weekly_seq_line = run_weekly_click_sequence(
                BAIJING_WEEKLY_SEQUENCE,
                controller.logger,
                stop_check=lambda: controller.stop_current_task,
            )

    bettergi_summary = None
    if app_key == "GenshinImpact":
        bettergi_summary = bettergi_monitor.build_summary() if bettergi_monitor is not None else None
        success, message = _apply_bettergi_verdict(
            success, message, bettergi_summary, bool(controller.stop_current_task))

    if (app_key == "HonkaiStarRail"
            and (effective_script_choice or "") == "March7th"):
        success, message = _apply_starrail_score_verdict(
            success, message, controller.logger, bool(controller.stop_current_task))

    if app_key == "ZenlessZoneZero":
        success, message = _apply_zzz_activity_verdict(
            success, message, controller.logger, bool(controller.stop_current_task))

    whimbox_lines: List[str] = []
    if app_key == "InfinityNikki":
        whimbox_lines = format_whimbox_task_summary(nikki_completion_monitor)

    # 事后日志复核（绝区零/星铁March7th/鸣潮/终末地/异环）：日志显示没跑完则改判失败；
    # 无当日日志时保持原判。只做 success→failed，不做反向。放在全部分支之后，
    # ok 脚本分支（elif）和常规分支（else）都能覆盖到。
    if success and not controller.stop_current_task:
        _verify_msg = _verify_script_log_result(
            app_key, effective_script_choice, display_name, controller.logger)
        if _verify_msg:
            success = False
            message = _verify_msg
            daily_failed = True
            controller.logger.warning("[%s] 日志复核不通过，改判失败: %s", display_name, message)

    time_str = format_duration(duration)
    report_lines: List[str] = []

    stopped = controller.stop_current_task or "用户终止" in message or "快捷键" in message
    if stopped:
        state = "stopped"
        report_lines.append(f"{label:20} ⚠️ {message} (运行: {time_str})")
    elif message == GAME_UPDATING_SKIP:
        # 游戏更新中：跳过而非失败，不计入失败统计、不触发统一重试
        state = "skipped"
        report_lines.append(f"{label:20} ⏭️ {message}")
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

    # 失败取证（结构化落库，供事后聚合分析"哪一步最常失败"）。
    # 这里拿不到窗口句柄（脚本层失败多在进程/接口层），所以不抓截图，只记文字证据。
    if state == "failed":
        try:
            from .flow_diagnostics import record_failure
            from ..settings import resolve_game_key

            _fail_reason = str(ok_script_attempt.failure_reason) if ok_script_attempt else ""
            record_failure(
                controller.logger,
                flow_key=app_key,
                step=script_name,
                error=f"{message}" + (f"｜{_fail_reason}" if _fail_reason else ""),
                game_key=resolve_game_key(app_key),
                save_image=False,
                extra={
                    "duration_sec": int(duration or 0),
                    "ok_script_failure_reason": _fail_reason,
                    "kind": "script_task",
                },
            )
        except Exception as exc:
            controller.logger.debug("[%s] 记录脚本失败诊断异常: %s", script_name, exc)

    if weekly_seq_line:
        report_lines.append(weekly_seq_line)

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

    # 只在脚本真正跑成功时回填每日数据；失败/跳过/用户终止时不查，
    # 免得给没跑完的游戏写入"未完成"的假数据。
    if state == "success":
        _auto_query_daily_progress(controller, resolve_game_key(app_key), display_name)

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
    force_rerun: bool = False,
) -> TaskBatchResult:
    """串行执行一批应用的每日任务（一条龙主入口）。

    controller: automation_controller 实例——提供 execute_task/stop_current_task/
    logger，是本模块与进程管理的唯一接口。
    target_apps: (app_key, script_choice) 列表，script_choice=None 时用上次保存的选择。
    force_rerun: True 时无视"今日已完成"跳过（手动勾选强制重跑）；
                 False 时按 _is_game_script_done_today 跳过已完成游戏（定时补跑模式）。
    返回 TaskBatchResult.report 为按行格式化的执行报告（邮件/UI 共用此文本）。
    失败项统一重试一次（force 停止/FailSafeAborted 除外）——重试前冷却 20 秒并
    清理残留启动器窗口。
    """
    hooks = hooks or TaskBatchHooks()
    report: List[str] = []
    warnings: List[str] = []
    app_results: List[TaskAppResult] = []

    # 修复：每次新批次开始时清除上一次手动停止（stopAutomation）遗留的停止标志。
    # 否则同一会话复用的 controller 会带着 stop_current_task=True 进入新批次，
    # 在下方 controller.stop_current_task 检查时直接返回空批次，表现为"刚启动就自动停止"；
    # 完整重启程序会新建 controller 从而恢复，历史上即"重启程序恢复"。
    controller.stop_current_task = False

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

    if controller.stop_current_task:
        controller.logger.info("[一条龙] 启动前签到期间用户已请求停止，跳过所有任务")
        return TaskBatchResult(report=report, warnings=warnings, startup_lines=startup_lines)

    for index, (app_key, script_choice) in enumerate(target_apps):
        if controller.stop_current_task:
            controller.logger.info("[一条龙] 用户已请求停止，终止后续任务")
            break

        display_name = get_app_display_name(app_key)
        effective_script_choice = script_choice or get_saved_script_choice(app_key)
        script_name = get_script_display_name(app_key, effective_script_choice)

        if hooks.on_before_app is not None:
            hooks.on_before_app(app_key, display_name, script_name)

        # 跳过今日脚本任务已全部成功的游戏（只对非 standalone 任务生效）
        if not force_rerun and not is_standalone_task(app_key) and _is_game_script_done_today(app_key):
            controller.logger.info("[%s] 今日脚本任务已全部成功，跳过", display_name)
            report.append(f"{display_name:20} ⏭️ 今日已完成，跳过")
            skip_result = TaskAppResult(
                app_key=app_key,
                script_choice=script_choice,
                effective_script_choice=effective_script_choice,
                display_name=display_name,
                script_name=script_name,
                label=f"{display_name}({script_name})",
                success=True,
                message="今日已完成，跳过",
                duration=0,
                time_str="0秒",
                state="skipped",
            )
            app_results.append(skip_result)
            if hooks.on_after_app is not None:
                hooks.on_after_app(skip_result)
            if index < len(target_apps) - 1:
                _interruptible_sleep(controller, inter_task_delay_sec)
            continue

        if app_key == "GenshinImpact" and xm_enabled and xm_result["ok"] is None:
            controller.logger.info("[XM流程] 在原神前执行 XM 前台流程")
            ok, msg = run_xm_background(
                controller.logger,
                stop_check=lambda: controller.stop_current_task,
            )
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
        # 首轮只记录不重试：失败项统一在批量结束后重试一次（见下方统一重试）
        app_results.append(result)
        report.extend(result.report_lines)

        if hooks.on_after_app is not None:
            hooks.on_after_app(result)

        if result.should_stop_batch:
            break

        if index < len(target_apps) - 1:
            # 任务间隔冷却：期间持续清理刚结束任务弹出的残留启动器，避免干扰下一个脚本
            controller._cooldown_before_next_task(app_key, inter_task_delay_sec)

    if cj_should_run and not controller.stop_current_task:
        controller.logger.info("[CJ流程] 开始执行重聚前台流程（排在最后）")
        if hooks.on_before_cj is not None:
            hooks.on_before_cj()
        ok, msg = run_cj_background(
            controller.logger,
            stop_check=lambda: controller.stop_current_task,
        )
        if not ok:
            warnings.append(msg or "重聚前台流程失败")
        else:
            report.append(f"{'重聚前台流程':20} ✅ 成功")

    # 统一重试：日常任务失败就重试一次（启动失败/超时/内容没做完都算失败，
    # 不失败不可能日常成功）。只限常规游戏任务：standalone 杂项、用户终止/熔断不管。
    # 关机/邮件在 run_task_batch 返回之后，天然在重试之后。
    retry_candidates = [
        r for r in app_results
        if r.state == "failed"
        and not is_standalone_task(r.app_key)
        and not r.should_stop_batch
    ]
    if retry_candidates and not controller.stop_current_task:
        controller.logger.info(
            "[一条龙] 首轮失败 %d 项（%s），8 秒后统一重试一次",
            len(retry_candidates),
            "、".join(r.display_name for r in retry_candidates),
        )
        report.append(f"—— 统一重试（{len(retry_candidates)} 项） ——")
        _interruptible_sleep(controller, 8)
        for r in retry_candidates:
            if controller.stop_current_task:
                controller.logger.info("[一条龙] 重试期间用户请求停止，剩余不再重试")
                break
            controller.logger.info("[%s] 统一重试开始", r.display_name)
            if hooks.on_before_app is not None:
                hooks.on_before_app(r.app_key, r.display_name, r.script_name)
            new_result = _run_single_task(
                controller,
                r.app_key,
                r.script_choice,
                cj_should_run=False,
                before_ok_retry=hooks.on_before_ok_retry,
            )
            if new_result.state == "success":
                new_result.report_lines.insert(0, f"{r.display_name:20} ℹ️ 首次失败后重试成功")
            report.extend(new_result.report_lines)
            if hooks.on_after_app is not None:
                hooks.on_after_app(new_result)
            # 用重试结果替换首轮结果，供调用方做最终统计
            try:
                app_results[app_results.index(r)] = new_result
            except ValueError:
                # 结果对象可能已被并发清理，找不到就保留原批次结果
                pass

    return TaskBatchResult(report=report, warnings=warnings, startup_lines=startup_lines)