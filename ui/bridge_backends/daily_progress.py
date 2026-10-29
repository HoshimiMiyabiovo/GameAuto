"""每日进度、失败复核与邮件日报的后台逻辑（从 ui/bridge.py 拆出）。

数据流：WorkflowDB(每日快照) + 外部日志解析 → 合并完成度口径 → QML JSON 属性；
邮件日报走实时快照刷新。函数接收 bridge 实例以访问控制器、信号与共享状态。
"""


import atexit
import json
import logging
import os
import subprocess
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from PySide6.QtCore import QObject, Signal, Property, Slot, QTimer

from ...settings.paths import CACHE_DIR, ENDFIELD_SKLAND_CACHE, PROTECTED_CLICK_SEQUENCES, WUWA_DAILY_CACHE
from ...services.game_update_calendar import (
    CN_TZ,
    OPEN_BUFFER_MIN,
    UPDATE_RETRY_FILE,
    get_maintenance_state,
    get_update_info,
)

logger = logging.getLogger("GameAutomation.Bridge")

# ── Status tone constants ────────────────────────────────────────
TONE_INFO = "info"
TONE_RUNNING = "running"
TONE_SUCCESS = "success"
TONE_WARNING = "warning"
TONE_DANGER = "danger"

# 没有官方公告接口/公告尚未发布时，按常规维护窗口兜底（仅用于失败/跳过后排补跑）
_FALLBACK_UPDATE_WINDOWS = {
    "WutheringWaves": ("04:00", "11:00"),
    "InfinityNikki": ("10:00", "11:00"),
    "NevernessToEverness": ("06:00", "11:00"),
}

TONE_MAP: Dict[str, tuple] = {
    TONE_INFO:    ("待命中", "#dff1e4", "#2f7d4a"),
    TONE_RUNNING: ("进行中", "#e9f3fb", "#295d86"),
    TONE_SUCCESS: ("已完成", "#dff1e4", "#2f7d4a"),
    TONE_WARNING: ("请留意", "#faebd7", "#b66f1f"),
    TONE_DANGER:  ("已中断", "#f7dfdd", "#b04b44"),
}

# 终末地森空岛缓存新鲜度窗口（秒）：超过后才在进页面时后台自动刷新
ENDFIELD_SKLAND_TTL = 300
WUWA_DAILY_TTL = 300


def _mask_address(addr: str) -> str:
    """日志中脱敏邮箱地址：a*c@example.com。"""
    if not addr or "@" not in addr:
        return "***"
    name, domain = addr.split("@", 1)
    if len(name) <= 2:
        masked = "*" * len(name)
    else:
        masked = name[0] + "*" * (len(name) - 2) + name[-1]
    return f"{masked}@{domain}"


def _refresh_daily_snapshot_for_report(bridge, tracker_factory, timeout_sec: float = 120.0):
    """发报告前实时拉取各游戏每日进度，写入 tracker 并返回最新快照。

    返回 (snapshot, fresh)。
      fresh=True  → 本次实时查询成功，数据是新的
      fresh=False → 查询超时/失败，已降级到数据库缓存（邮件里会标注）

    设计要点：
      - 实时查询要访问多个外网 API，必须放子线程 + join(timeout)，
        否则会卡住随后的关机流程；超时就退回缓存，绝不阻塞发信。
      - check_games_progress(tracker=...) 会把结果写回库，
        所以之后 load_today() 拿到的就是新鲜数据。
    """
    try:
        tracker = tracker_factory()
    except Exception as exc:
        logger.warning("[Bridge] 打开进度 tracker 失败: %s", exc)
        return None, False

    box: Dict[str, Any] = {}

    def _run():
        try:
            from ...clients.daily_checker import check_games_progress
            box["result"] = check_games_progress(tracker=tracker)
        except Exception as exc:
            box["error"] = exc

    try:
        t = threading.Thread(target=_run, daemon=True, name="daily-refresh-for-email")
        t.start()
        t.join(timeout=timeout_sec)
    except Exception as exc:
        logger.warning("[Bridge] 实时刷新每日进度线程异常: %s", exc)
        box["error"] = exc

    if threading.current_thread() is not t and t.is_alive():
        logger.warning(
            "[Bridge] 实时刷新每日进度超时（%.0fs），邮件改用数据库缓存数据", timeout_sec)
        try:
            return tracker.load_today(), False
        except Exception as exc:
            logger.warning("[Bridge] 读取进度缓存失败: %s", exc)
            return None, False

    err = box.get("error")
    if err is not None:
        logger.warning("[Bridge] 实时刷新每日进度失败，改用缓存: %s", err)
        try:
            return tracker.load_today(), False
        except Exception as exc:
            logger.warning("[Bridge] 读取进度缓存失败: %s", exc)
            return None, False

    result = box.get("result")
    if result is not None:
        ok = sum(1 for g in result.games if g.is_daily_completed)
        logger.info(
            "[Bridge] 发信前实时刷新每日进度完成: %d 款游戏，其中 %d 款已完成",
            len(result.games), ok)
    try:
        return tracker.load_today(), True
    except Exception as exc:
        logger.warning("[Bridge] 读取刷新后进度失败: %s", exc)
        return None, True


def _send_daily_email_report(
    bridge,
    automation_report: List[str],
    automation_warnings: List[str],
) -> tuple:
    """读取 SMTP 配置 + tracker 数据，发送一条龙报告邮件。返回 (成功, 错误信息)。"""
    from ...services.email_reporter import (
        build_daily_report,
        is_email_configured,
        load_email_config,
        send_email,
    )
    from ...services.daily_progress_tracker import get_tracker
    from ...settings.paths import WORKFLOW_DB_PATH

    if not is_email_configured():
        return False, "app_config.json 的 email 段未配置或 enabled=false"

    config = load_email_config()
    if config is None:
        return False, "SMTP 配置不完整（缺少 host/sender/password/recipients）"

    # 发信前**实时**查一遍各游戏每日进度并写回 tracker。
    # 不能用 load_today() 直接读缓存：一条龙通常跑几十分钟到几小时，
    # 库里可能还是跑之前（甚至早上）的数据，邮件会发出过时进度。
    daily_snapshot, snapshot_fresh = bridge._refresh_daily_snapshot_for_report(
        tracker_factory=lambda: get_tracker(str(WORKFLOW_DB_PATH))
    )

    # 简单统计：本轮 success / failed 计数
    automation_total = len(automation_report)
    automation_success = sum(1 for line in automation_report if "✅" in line or "已完成" in line)
    automation_failed = sum(1 for line in automation_report if "❌" in line or "失败" in line)

    subject, body = build_daily_report(
        automation_report=automation_report,
        automation_warnings=automation_warnings,
        automation_total=automation_total,
        automation_success=automation_success,
        automation_failed=automation_failed,
        daily_snapshot=daily_snapshot,
        snapshot_fresh=snapshot_fresh,
    )

    try:
        ok, err = send_email(config, subject, body)
    except Exception as exc:
        logger.exception("[Bridge] 邮件发送异常")
        return False, str(exc)
    if not ok:
        return False, err or "未知 SMTP 错误"
    bridge._request_toast("每日进度邮件已发送", "#2f7d4a")
    return True, ""


def _save_preferences_backend(bridge, prefs: List[Dict]):
    """后台保存启动偏好。"""
    try:
        db = bridge._get_preference_db()
        for pref in prefs:
            app_key = pref.get("app_key", "")
            enabled = bool(pref.get("enabled", False))
            script_choice = pref.get("script_choice", "")
            db.save_app_launch_preference(app_key, enabled, script_choice, False)
        bridge.emit_event("toast", message="启动偏好已保存", color="#2f7d4a")
        logger.info("[Bridge] 已保存 %d 项启动偏好", len(prefs))
    except Exception as exc:
        logger.exception("[Bridge] 保存偏好失败")
        bridge.emit_event("toast", message=f"保存失败: {exc}", color="#b04b44")


def _run_daily_check_backend(bridge, game_keys: List[str]):
    """后台查询每日进度。"""
    try:
        bridge.emit_event("status", title="正在查询", body="正在获取每日进度...", tone=TONE_RUNNING)

        from ...clients.daily_checker import check_games_progress
        from ...services.daily_progress_tracker import get_tracker
        from ...settings.paths import WORKFLOW_DB_PATH

        # 同步写入 tracker，让 loadDailyProgress 下次能拿到今日记录
        tracker = None
        try:
            tracker = get_tracker(str(WORKFLOW_DB_PATH))
        except Exception as exc:
            logger.warning("[Bridge] 初始化 tracker 失败，跳过持久化: %s", exc)

        result = check_games_progress(
            game_keys=game_keys if game_keys else None,
            tracker=tracker,
        )

        games_data = []
        for game in result.games:
            tasks_data = []
            for task in game.tasks:
                # DailyTaskItem 字段：name/current/total/is_completed
                t_status = "success" if task.is_completed else "pending"
                if task.total and task.total > 0:
                    t_message = f"{task.current}/{task.total}"
                elif task.is_completed:
                    t_message = "已完成"
                else:
                    t_message = "未完成"
                tasks_data.append({
                    "task_key": task.name,
                    "task_name": task.name,
                    "status": t_status,
                    "message": t_message,
                    "current": task.current,
                    "total": task.total,
                })
            games_data.append({
                "game_key": game.game_key,
                "game_name": game.game_name,
                "uid": game.uid,
                "nickname": game.nickname,
                # GameDailyStatus 用 is_daily_completed，没有 is_all_success/has_failed
                # has_failed 仅当 API 报错时为真；任务未完成属于 pending 不算 failed
                "is_all_success": game.is_daily_completed,
                "has_failed": bool(game.errors),
                "progress_percent": game.progress_percent,
                "tasks": tasks_data,
                "errors": game.errors,
            })

        bridge._inject_external_log_progress(games_data)
        bridge._inject_sign_task_into_games(games_data)
        bridge._ensure_baijing_placeholder(games_data)
        bridge._attach_completion_state(games_data)
        bridge.emit_event("daily_progress", games=games_data, date_key="")
        bridge.emit_event("status", title="查询完成",
                        body=f"已查询 {len(games_data)} 款游戏",
                        tone=TONE_SUCCESS)
        bridge.emit_event("toast", message="每日进度已刷新", color="#2f7d4a")
    except Exception as exc:
        logger.exception("[Bridge] 每日进度查询失败")
        bridge.emit_event("status", title="查询失败", body=str(exc), tone=TONE_DANGER)
        bridge.emit_event("toast", message=f"查询失败: {exc}", color="#b04b44")


def _serialize_snapshot_games(bridge, snapshot) -> List[Dict[str, Any]]:
    """把 DailyProgressSnapshot 序列化成 QML 可直接绑定的列表。"""
    from ...settings import resolve_game_key
    from ...settings.defaults import GAME_KEYS

    games_data: List[Dict[str, Any]] = []
    for game in snapshot.games:
        gk = resolve_game_key(game.game_key)
        tasks_data = []
        for task in game.tasks:
            t_extra = task.extra if isinstance(task.extra, dict) else {}
            t_current, t_total = bridge._extract_task_counts(t_extra)
            tasks_data.append({
                "task_key": task.task_key,
                "task_name": task.task_name,
                "status": task.status,
                "message": task.message,
                "current": t_current,
                "total": t_total,
                "extra": t_extra,
            })
        games_data.append({
            "game_key": gk,
            "game_name": game.game_name or GAME_KEYS.get(gk, gk),
            "uid": game.uid,
            "nickname": game.nickname,
            "is_all_success": game.is_all_success,
            "has_failed": game.has_failed,
            "progress_percent": game.progress_percent,
            "completion_state": game.completion_state(),
            "tasks": tasks_data,
            "errors": list(game.errors or []),
        })
    games_data.sort(key=lambda g: (-g["progress_percent"], g["game_name"]))
    return games_data


def _load_daily_dates_backend(bridge):
    """列出有每日进度记录的日期（倒序）。"""
    try:
        from ...services.daily_progress_tracker import get_tracker
        from ...settings.paths import WORKFLOW_DB_PATH

        tracker = get_tracker(str(WORKFLOW_DB_PATH))
        dates = tracker.list_available_dates(limit=120)
        payload = {"ok": True, "dates": dates}
    except Exception as exc:
        logger.exception("[Bridge] 加载历史日期列表失败")
        payload = {"ok": False, "dates": [], "message": str(exc)}
    bridge._daily_dates_json = json.dumps(payload, ensure_ascii=False)
    bridge.dailyDatesChanged.emit(bridge._daily_dates_json)


def _load_daily_history_backend(bridge, date_key: str):
    """回看指定日期的每日进度。"""
    if not date_key:
        bridge._daily_history_json = json.dumps(
            {"ok": False, "date_key": "", "message": "未指定日期", "games": []},
            ensure_ascii=False)
        bridge.dailyHistoryChanged.emit(bridge._daily_history_json)
        return
    try:
        from ...services.daily_progress_tracker import get_tracker
        from ...settings.paths import WORKFLOW_DB_PATH

        tracker = get_tracker(str(WORKFLOW_DB_PATH))
        snapshot = tracker.load_date(date_key)
        games = bridge._serialize_snapshot_games(snapshot)
        payload = {
            "ok": True,
            "date_key": snapshot.date_key,
            "games": games,
            "message": "" if games else f"{date_key} 没有记录",
        }
        logger.info("[Bridge] 回看历史进度 %s：%d 款游戏", date_key, len(games))
    except Exception as exc:
        logger.exception("[Bridge] 回看历史进度失败: %s", date_key)
        payload = {"ok": False, "date_key": date_key, "games": [], "message": str(exc)}
    bridge._daily_history_json = json.dumps(payload, ensure_ascii=False)
    bridge.dailyHistoryChanged.emit(bridge._daily_history_json)


def _load_daily_stats_backend(bridge, days: int):
    """统计最近 days 天各游戏完成率。"""
    try:
        from ...services.daily_progress_tracker import get_tracker
        from ...settings.paths import WORKFLOW_DB_PATH

        tracker = get_tracker(str(WORKFLOW_DB_PATH))
        stats = tracker.stats_recent_days(days)
        dates = tracker.list_available_dates(limit=days)
        payload = {
            "ok": True,
            "days": days,
            "date_from": dates[-1] if dates else "",
            "date_to": dates[0] if dates else "",
            "stats": stats,
        }
        logger.info("[Bridge] 统计最近 %d 天完成率：%d 款游戏", days, len(stats))
    except Exception as exc:
        logger.exception("[Bridge] 统计完成率失败")
        payload = {"ok": False, "days": days, "stats": [], "message": str(exc)}
    bridge._daily_stats_json = json.dumps(payload, ensure_ascii=False)
    bridge.dailyStatsChanged.emit(bridge._daily_stats_json)


def _load_daily_progress_backend(bridge):
    """从 SQLite 加载今日持久化进度。"""
    try:
        from ...services.daily_progress_tracker import get_tracker
        from ...settings.paths import WORKFLOW_DB_PATH
        from ...settings import resolve_game_key
        from ...settings.defaults import GAME_KEYS
        tracker = get_tracker(str(WORKFLOW_DB_PATH))
        snapshot = tracker.load_today()

        # 合并同游戏的卡片：tracker 里可能同时存在大驼峰式（旧 task_batch_runner）
        # 和小写式（daily_checker / parser）两种 game_key，需要统一合并。
        merged_games: Dict[str, Dict[str, Any]] = {}
        for game in snapshot.games:
            gk = resolve_game_key(game.game_key)
            tasks_data = []
            for task in game.tasks:
                t_extra = task.extra if isinstance(task.extra, dict) else {}
                t_current, t_total = bridge._extract_task_counts(t_extra)
                # 白荆验证截图：extra.verify_image 为相对路径，需转 file:// 供 QML Image
                v_rel = t_extra.get("verify_image") if isinstance(t_extra.get("verify_image"), str) else None
                v_url = None
                if v_rel:
                    try:
                        from pathlib import Path as _P
                        from ...settings.paths import BASE_DIR as _BD
                        # 兼容相对/绝对两种写法
                        p = _P(v_rel)
                        if not p.is_absolute():
                            p = _BD / v_rel
                        if p.exists():
                            v_url = p.as_uri()
                        else:
                            v_url = None
                    except Exception:
                        v_url = None
                tasks_data.append({
                    "task_key": task.task_key,
                    "task_name": task.task_name,
                    "status": task.status,
                    "message": task.message,
                    "current": t_current,
                    "total": t_total,
                    "verify_image": v_rel,
                    "verify_image_url": v_url,
                    "extra": t_extra,
                })
            if gk in merged_games:
                # 合并到已有卡片
                existing = merged_games[gk]
                existing["tasks"].extend(tasks_data)
                existing["errors"].extend(game.errors or [])
                # 取更优状态
                if game.is_all_success:
                    existing["is_all_success"] = True
                if game.has_failed:
                    existing["has_failed"] = True
            else:
                merged_games[gk] = {
                    "game_key": gk,
                    "game_name": game.game_name or GAME_KEYS.get(gk, gk),
                    "uid": game.uid,
                    "nickname": game.nickname,
                    "is_all_success": game.is_all_success,
                    "has_failed": game.has_failed,
                    "progress_percent": game.progress_percent,
                    "tasks": tasks_data,
                    "errors": list(game.errors or []),
                }
        games_data = list(merged_games.values())

        bridge._inject_external_log_progress(games_data)
        bridge._inject_sign_task_into_games(games_data)
        bridge._ensure_baijing_placeholder(games_data)
        bridge._attach_completion_state(games_data)
        bridge.emit_event("daily_progress", games=games_data, date_key=snapshot.date_key)
        logger.info("[Bridge] 加载持久化进度: date_key=%s games=%d",
                    snapshot.date_key, len(games_data))
    except Exception as exc:
        logger.exception("[Bridge] 加载持久化进度失败")
        bridge.emit_event("toast", message=f"加载失败: {exc}", color="#b04b44")



def _real_completion_percent(game_key: str, tasks: List[Dict[str, Any]]):
    """从任务列表里取"真实游玩进度"百分比，而非脚本子任务比例。

    返回 (percent, source_key) 或 (None, None) 表示无可用真实计数。
    动机：ok-ww 的 progress_percent 只是"脚本跑了几步"，全跑完就是 100%，
    但玩家活跃度可能只有 90/100 —— 直接展示会严重误导。
    """
    # 复用 tracker 里的统一映射，避免两处各写一份逐渐走样
    try:
        from ...services.daily_progress_tracker import KEY_PROGRESS_TASKS
    except Exception:
        return None, None
    names = KEY_PROGRESS_TASKS.get(game_key)
    if not names:
        return None, None
    for want in names:
        for t in tasks or []:
            tname = str(t.get("task_name") or t.get("task_key") or "")
            if tname != want:
                continue
            cur = t.get("current")
            total = t.get("total")
            try:
                cur_i = int(cur)
                total_i = int(total)
            except (TypeError, ValueError):
                continue
            if total_i <= 0:
                continue
            # 官方 API 未刷新时 cur 会是 0，此时不谎报 0%，交给调用方兜底
            if cur_i <= 0:
                return None, None
            pct = int(cur_i / total_i * 100)
            return min(pct, 100), want
    return None, None



def _attach_completion_state(games_data: List[Dict[str, Any]]):
    """给合并后的 games_data 逐项补 completion_state（供卡片徽标用）。

    合并后的 dict 不是 GameDailyRecord，按字段重建一个再判定，
    兼容两种任务形状：tracker 侧 extra.current/total 与查询侧顶层 current/total。
    失败不抛，缺字段就跳过该项（QML 回退旧逻辑）。
    """
    try:
        from ...services.daily_progress_tracker import DailyTaskRecord, GameDailyRecord
    except Exception:
        return
    for g in games_data or []:
        try:
            tasks = []
            for t in (g.get("tasks") or []):
                if not isinstance(t, dict):
                    continue
                extra = t.get("extra") if isinstance(t.get("extra"), dict) else {}
                if "current" not in extra and t.get("current") is not None:
                    extra = dict(extra)
                    extra["current"] = t.get("current")
                    extra["total"] = t.get("total")
                tasks.append(DailyTaskRecord(
                    task_key=str(t.get("task_key") or t.get("task_name") or ""),
                    task_name=str(t.get("task_name") or t.get("task_key") or ""),
                    source=str(t.get("source") or "script"),
                    status=str(t.get("status") or "pending"),
                    message=str(t.get("message") or ""),
                    extra=extra,
                ))
            rec = GameDailyRecord(
                game_key=str(g.get("game_key") or ""),
                game_name=str(g.get("game_name") or ""),
                tasks=tasks,
                errors=list(g.get("errors") or []),
            )
            g["completion_state"] = rec.completion_state()
            # 完成度口径标签：拿得到关键任务真实计数（活跃度等）时，
            # progress_percent 就是游玩进度，必须标 non-script，否则白荆
            # 活跃度 15/100 会被误显示成"脚本 15%"（2026-10-19）。
            # 取不到真实计数时才是脚本子任务完成比例。
            # wuwa/endfield 合并路径已自带更精确的来源名，不覆盖。
            if not g.get("progress_source"):
                g["progress_source"] = (
                    "real" if rec.real_progress_percent() is not None else "script"
                )
        except Exception:
            continue



def _extract_task_counts(t_extra: Dict[str, Any]):
    """从任务 extra 里取真实计数 (current, total)。

    优先顶层 current/total；缺失时回退 extra.summary 的
    done/tasks_total——白荆"日常任务"把 2/15 这类真实完成度放在
    summary 里，顶层没有计数。不提取的话 UI 只能看到
    status=success 的 ✅，实际只做了 2/15 也会打勾误导用户
    （2026-10-19 事故）。
    """
    if not isinstance(t_extra, dict):
        return None, None
    current = t_extra.get("current")
    total = t_extra.get("total")
    if current is None or total is None:
        summary = t_extra.get("summary")
        if isinstance(summary, dict):
            if current is None:
                current = summary.get("done")
            if total is None:
                total = summary.get("tasks_total")
    return current, total


def _inject_external_log_progress(bridge, games_data: List[Dict[str, Any]]):
    """读取外部自动化工具日志，将日常执行情况注入 games_data。

    支持的工具与映射：
      - ok-ef（终末地）→ game_key="endfield"，只要关键任务（⭐活动奖励、⭐日常奖励）成功就算成功
      - ok-nte（异环）→ game_key="nte"，只要关键任务（领取活跃度奖励）成功就算成功
      - ok-ww（鸣潮）→ game_key="wuwa"，只要出现 Successfully Executed Task 就算成功
      - whimbox（无限暖暖）→ game_key="nikki"，白名单任务 + 末尾"一条龙结束"算成功
      - BetterGI（原神）→ game_key="genshin"，每日委托奖励 + 探索派遣 都完成才算成功

    行为：
      - whimbox：保留原有 tasks，**前置插入**白名单任务（同名不重复），用于细化任务细节
      - 其他：如果今天的日志存在，整体替换原有的对应 game 项；否则保持原数据不变
    """
    from ...services.ok_ef_daily_parser import parse_latest_ok_ef_report
    from ...services.ok_nte_daily_parser import parse_latest_ok_nte_log
    from ...services.ok_ww_daily_parser import parse_latest_ok_ww_log
    from ...services.whimbox_daily_parser import parse_latest_whimbox_log
    from ...services.bettergi_daily_parser import parse_latest_bettergi_log
    from ...services.zzz_daily_parser import parse_latest_zzz_log
    from ...services.starrail_daily_parser import parse_latest_starrail_log
    from ...settings import resolve_game_key

    parsers = (
        ("endfield", "ok-ef", parse_latest_ok_ef_report),
        ("nte", "ok-nte", parse_latest_ok_nte_log),
        ("wuwa", "ok-ww", parse_latest_ok_ww_log),
        ("nikki", "whimbox", parse_latest_whimbox_log),
        ("genshin", "BetterGI", parse_latest_bettergi_log),
        ("zzz", "OneDragon", parse_latest_zzz_log),
        ("honkai_sr", "March7th", parse_latest_starrail_log),
    )
    for game_key, tool_name, parser_func in parsers:
        try:
            data = parser_func()
            if data is None:
                if game_key == "nte":
                    # 异环：ok-nte 今天若未执行，仍保留“游戏内每日”占位任务，
                    # 进度由 ok-nte 执行情况判定（未执行 = 待执行），避免游戏内进度整体缺失。
                    data = {
                        "game_key": "nte",
                        "game_name": "异环",
                        "uid": "",
                        "nickname": "ok-nte",
                        "is_all_success": False,
                        "has_failed": False,
                        "progress_percent": 0,
                        "tasks": [{
                            "task_key": "游戏内每日",
                            "task_name": "游戏内每日",
                            "status": "pending",
                            "message": "待 ok-nte 执行",
                        }],
                        "errors": [],
                        "report_time": "",
                        "log_date": "",
                    }
                else:
                    continue
            resolved_key = resolve_game_key(game_key)
            replaced = False
            for g_idx, game in enumerate(games_data):
                if resolve_game_key(game.get("game_key", "")) == resolved_key:
                    if game_key == "nikki":
                        # 无限暖暖特殊处理：whimbox 的子任务前置插入，保留 Pearpal 基线
                        existing_tasks = list(game.get("tasks") or [])
                        whimbox_tasks = data.get("tasks") or []
                        # 用 task_key 去重：whimbox 的 task_key 与 Pearpal 同名则取最新
                        existing_keys = {t.get("task_key") or t.get("task_name"): idx
                                         for idx, t in enumerate(existing_tasks)}
                        merged = []
                        for wt in whimbox_tasks:
                            wkey = wt.get("task_key") or wt.get("task_name")
                            if wkey in existing_keys:
                                e_idx = existing_keys[wkey]
                                # 用 whimbox 数据覆盖同名（更精细的进度）
                                merged.append({**existing_tasks[e_idx], **wt})
                            else:
                                merged.append(wt)
                        for j, t in enumerate(existing_tasks):
                            tkey = t.get("task_key") or t.get("task_name")
                            if tkey not in {(wt.get("task_key") or wt.get("task_name")) for wt in whimbox_tasks}:
                                merged.append(t)
                        games_data[g_idx]["tasks"] = merged
                        # 整体成功/失败由 whimbox 决定（更精确）
                        games_data[g_idx]["is_all_success"] = data.get("is_all_success", games_data[g_idx].get("is_all_success"))
                        games_data[g_idx]["has_failed"] = data.get("has_failed", games_data[g_idx].get("has_failed"))
                        if data.get("errors"):
                            games_data[g_idx]["errors"] = data["errors"]
                        games_data[g_idx]["nickname"] = data.get("nickname", games_data[g_idx].get("nickname", ""))
                        logger.info("[Bridge] 已用 %s 日志合并 %s 任务细节: tasks=%d",
                                    tool_name, game_key, len(merged))
                    else:
                        if game_key == "nte":
                            # 异环特殊处理：游戏内进度由 ok-nte 执行情况判定。
                            # 保留塔吉多社区任务（社区签到/浏览帖子/点赞帖子/分享）
                            # 与 BBS 签到成果；仅丢弃"游戏签到"任务（其完成情况
                            # 由 ok-nte 进入游戏后的真实执行结果代表）。
                            # 历史 bug：过滤条件写成"endswith(签到)"，把 BBS签到
                            # （+80金币 API 成果）和异环签到也误删，导致 DailyView
                            # 签到行走 last_sign_* 兜底显示"今日尚未签到"。
                            existing_tasks = list(game.get("tasks") or [])
                            ok_nte_tasks = list(data.get("tasks") or [])
                            kept = [
                                t for t in existing_tasks
                                if str(t.get("task_name", "")) != "游戏签到"
                            ]
                            merged = list(kept) + ok_nte_tasks
                            # 社区任务全部完成（无社区任务时视为满足，不阻塞 ok-nte 判定）
                            community_done = all(
                                t.get("status") == "success" for t in kept
                            ) if kept else True
                            game["tasks"] = merged
                            game["is_all_success"] = bool(data.get("is_all_success")) and community_done
                            game["has_failed"] = bool(data.get("has_failed")) or bool(game.get("has_failed"))
                            if data.get("errors"):
                                game["errors"] = (game.get("errors") or []) + list(data.get("errors"))
                            game["nickname"] = data.get("nickname", game.get("nickname", ""))
                            game["progress_percent"] = data.get("progress_percent", game.get("progress_percent"))
                            logger.info(
                                "[Bridge] 异环：合并 ok-nte 游戏内进度(%d 项) + 社区任务(%d 项)，成功=%s",
                                len(ok_nte_tasks), len(kept), game["is_all_success"])
                        elif game_key in ("wuwa", "endfield"):
                            # 鸣潮/终末地：脚本日志与 API 便笺数据**互补**，合并而非替换。
                            # API 行（daily_checker）带体力/活跃度等便笺数据，
                            # 日志行带脚本执行进度；同名任务以日志为准，日志任务前置。
                            existing_tasks = list(game.get("tasks") or [])
                            log_tasks = list(data.get("tasks") or [])
                            log_keys = {(t.get("task_key") or t.get("task_name")) for t in log_tasks}
                            sign_tasks = [
                                t for t in existing_tasks
                                if (t.get("task_key") or "").startswith("sign_")
                            ]
                            kept_api_tasks = [
                                t for t in existing_tasks
                                if (t.get("task_key") or t.get("task_name")) not in log_keys
                                and not (t.get("task_key") or "").startswith("sign_")
                            ]
                            game["tasks"] = sign_tasks + log_tasks + kept_api_tasks
                            game["is_all_success"] = data.get(
                                "is_all_success", game.get("is_all_success"))
                            game["has_failed"] = bool(data.get("has_failed")) or bool(game.get("has_failed"))
                            if data.get("errors"):
                                game["errors"] = (game.get("errors") or []) + list(data.get("errors"))
                            game["nickname"] = data.get("nickname") or game.get("nickname", "")

                            # 完成度必须反映真实游玩进度，不能用脚本子任务比例代替：
                            # ok-ww 的 progress_percent 是"脚本跑了几步"（全跑完就 100%），
                            # 与活跃度 90/100 无关。这里优先取 API 便笺的关键计数。
                            real_pct, real_src = bridge._real_completion_percent(
                                game_key, sign_tasks + log_tasks + kept_api_tasks)
                            if real_pct is not None:
                                game["progress_percent"] = real_pct
                                game["progress_source"] = real_src
                            else:
                                game["progress_percent"] = data.get(
                                    "progress_percent", game.get("progress_percent"))
                                game["progress_source"] = "script"
                            logger.info(
                                "[Bridge] %s：合并脚本日志进度(%d 项) + API 便笺数据(%d 项)，成功=%s 完成度=%s%%(%s)",
                                game_key, len(log_tasks), len(kept_api_tasks),
                                game["is_all_success"],
                                game["progress_percent"], game.get("progress_source"))
                        else:
                            # 保留 tracker 里已有的签到任务（sign_ 前缀），
                            # 避免被外部日志整体替换后丢失签到状态。
                            existing_sign_tasks = [
                                t for t in (game.get("tasks") or [])
                                if (t.get("task_key") or "").startswith("sign_")
                            ]
                            if existing_sign_tasks:
                                data_tasks = list(data.get("tasks") or [])
                                data["tasks"] = existing_sign_tasks + data_tasks
                            games_data[g_idx] = data
                            logger.info("[Bridge] 已用 %s 日志替换 %s 进度: success=%s sign_tasks=%d",
                                        tool_name, game_key, data.get("is_all_success"), len(existing_sign_tasks))
                    replaced = True
                    break
            if not replaced:
                games_data.append(data)
                logger.info("[Bridge] 已注入 %s %s 进度: success=%s",
                            tool_name, game_key, data.get("is_all_success"))
        except Exception as exc:
            logger.warning("[Bridge] 读取 %s 日志失败: %s", tool_name, exc)


def _ensure_baijing_placeholder(bridge, games_data: List[Dict[str, Any]]):
    """确保白荆回廊卡片始终可见：无记录时注入待执行占位，避免每日进度里看不到。"""
    try:
        from ...settings import resolve_game_key
        has = any(resolve_game_key(g.get("game_key", "")) == "baijing" for g in games_data)
        if has:
            return
        games_data.append({
            "game_key": "baijing",
            "game_name": "白荆回廊",
            "uid": "",
            "nickname": "",
            "is_all_success": False,
            "has_failed": False,
            "progress_percent": 0,
            "tasks": [
                {"task_key": "enter_game", "task_name": "进入游戏", "status": "pending", "message": "待一条龙执行", "current": None, "total": None},
                {"task_key": "daily", "task_name": "日常任务", "status": "pending", "message": "待执行", "current": None, "total": None},
                {"task_key": "verify", "task_name": "验证", "status": "pending", "message": "待验证截图", "current": None, "total": None, "verify_image": None, "verify_image_url": None, "extra": {}},
                {"task_key": "weekly", "task_name": "每周商店购买", "status": "pending", "message": "待周常（每周一次）", "current": None, "total": None},
                {"task_key": "monthly", "task_name": "每月有猫兑换", "status": "pending", "message": "待月度（每月一次，购买清单待定）", "current": None, "total": None},
            ],
            "errors": [],
        })
        logger.info("[Bridge] 已注入白荆回廊占位进度（今日无记录）")
    except Exception as exc:
        logger.debug("[Bridge] 注入白荆占位失败: %s", exc)
