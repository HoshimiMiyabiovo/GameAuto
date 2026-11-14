import logging
import time
from datetime import date, datetime
from typing import Callable, List, Optional, Tuple

from .foreground_flow_runner import run_flow

LOGGER = logging.getLogger("GameAutomation")


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
WOOL_FLOW_KEYS: List[str] = ["MINIMAX", "WORKBUDDY", "TRAE", "QODER", "TELEAGENT"]


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


def monthly_flow_meta_key(flow_key: str) -> str:
    """每月流程的门控键（workflow.sqlite3 的 task_meta）。"""
    return f"monthly_flow:{flow_key}"


def is_monthly_flow_done(flow_key: str, month_key: Optional[str] = None) -> bool:
    """本月是否已成功跑过该每月流程。

    供每日一条龙调度器复用同一套月度标记：批次里判断"要不要顺手把月商店买了"
    时只看标记，不重复实现口径。读库失败按"未完成"处理（宁可多跑一次）。
    """
    from ..storage.workflow_db import WorkflowDB
    from ..settings.paths import WORKFLOW_DB_PATH

    month = month_key or time.strftime("%Y-%m")
    try:
        db = WorkflowDB(str(WORKFLOW_DB_PATH))
        return db.get_task_meta(monthly_flow_meta_key(flow_key)) == month
    except Exception as exc:  # noqa: BLE001 - 读不到标记就当没做过，不影响主流程
        LOGGER.warning("[每月] 读取月度标记失败(%s): %s", flow_key, exc)
        return False


def _run_monthly_flow(logger: logging.Logger, flow_key: str, label: str) -> Tuple[bool, str]:
    """每月流程通用门控：自然月记录在 workflow.sqlite3，本月已执行则跳过。"""
    from ..storage.workflow_db import WorkflowDB
    from ..settings.paths import WORKFLOW_DB_PATH

    month_key = time.strftime("%Y-%m")
    meta_key = monthly_flow_meta_key(flow_key)
    db = WorkflowDB(str(WORKFLOW_DB_PATH))
    if db.get_task_meta(meta_key) == month_key:
        logger.info("[%s] 本月已执行，跳过", label)
        return True, "本月已执行，跳过"

    ok, msg = run_flow(logger, flow_key)
    if ok:
        db.set_task_meta(meta_key, month_key)
        # 2026-11-04：月商店成败以前只进邮件日报，runner 日志里查不到；
        # 至少这里要落一行，`_run_pending_monthly_flow` 的报告行才会跟着进日志。
        logger.info("[%s] ✅ %s（本月已记录，下月再执行）", label, msg)
        return True, f"✅ {msg}（本月已记录，下月再执行）"
    logger.warning("[%s] ❌ %s（下次运行重试）", label, msg)
    return False, f"❌ {msg}（下次运行重试）"


def weekly_flow_meta_key(flow_key: str) -> str:
    """每周流程的门控键（workflow.sqlite3 的 task_meta）。"""
    return f"weekly_flow:{flow_key}"


def weekly_flow_week_key(now=None) -> str:
    """「本周」的键：ISO 周（%G-W%V），整体后移 4 小时。

    绝区零是**周一 04:00** 刷新。周一 01:00 跑的时候游戏里还是上一周的数据，
    直接按自然周记会把标记写到新的一周，真正的新一周反而被跳过。后移 4 小时
    让 00:00-04:00 仍算上一周。ISO 周（%G-W%V）以周一为一周起点，与此一致。
    """
    from datetime import datetime, timedelta

    stamp = (now or datetime.now()) - timedelta(hours=4)
    return stamp.strftime("%G-W%V")


def is_weekly_flow_done(flow_key: str, week_key: Optional[str] = None) -> bool:
    """本周是否已成功跑过该每周流程。读库失败按「没做过」处理（宁可多跑一次）。"""
    from ..storage.workflow_db import WorkflowDB
    from ..settings.paths import WORKFLOW_DB_PATH

    week = week_key or weekly_flow_week_key()
    try:
        db = WorkflowDB(str(WORKFLOW_DB_PATH))
        return db.get_task_meta(weekly_flow_meta_key(flow_key)) == week
    except Exception as exc:  # noqa: BLE001 - 读不到标记就当没做过，不影响主流程
        LOGGER.warning("[每周] 读取周标记失败(%s): %s", flow_key, exc)
        return False


def _run_weekly_flow(logger: logging.Logger, flow_key: str, label: str) -> Tuple[bool, str]:
    """每周流程通用门控：ISO 周（周一 04:00 刷新）记录，本周已跑过则跳过。

    注意「本周已打完」有两种情况都算成功：
    1. 标记已写过（本周跑过）；
    2. 流程自己读到游戏内剩余次数为 0（比如手动打过了），流程内部按成功返回，
       这里照常写标记，避免每天白启动一次客户端。
    """
    from ..storage.workflow_db import WorkflowDB
    from ..settings.paths import WORKFLOW_DB_PATH

    week_key = weekly_flow_week_key()
    meta_key = weekly_flow_meta_key(flow_key)
    db = WorkflowDB(str(WORKFLOW_DB_PATH))
    if db.get_task_meta(meta_key) == week_key:
        logger.info("[%s] 本周已执行，跳过", label)
        return True, "本周已执行，跳过"

    ok, msg = run_flow(logger, flow_key)
    if ok:
        db.set_task_meta(meta_key, week_key)
        return True, f"✅ {msg}（本周已记录，下周再执行）"
    return False, f"❌ {msg}（下次运行重试）"


def run_zzz_notorious_hunt_background(logger: logging.Logger) -> Tuple[bool, str]:
    """绝区零恶名狩猎（每周三次）：_run_weekly_flow 门控 + run_flow。"""
    return _run_weekly_flow(logger, "ZZZ_NOTORIOUS_HUNT", "绝区零恶名狩猎")



def run_hsr_monthly_background(logger: logging.Logger) -> Tuple[bool, str]:
    return _run_monthly_flow(logger, "HSR_MONTHLY", "崩铁每月")


def run_genshin_monthly_background(logger: logging.Logger) -> Tuple[bool, str]:
    return _run_monthly_flow(logger, "GENSHIN_MONTHLY", "原神每月")


def run_zzz_monthly_background(logger: logging.Logger) -> Tuple[bool, str]:
    return _run_monthly_flow(logger, "ZZZ_MONTHLY", "绝区零每月")


def run_zzz_logistics_monthly_background(logger: logging.Logger) -> Tuple[bool, str]:
    """绝区零后勤商店：与「信号残响」互相独立的第二条月流程。

    2026-11-06 用户拍板：两个商店都是每月刷新，不该串在一条 run_flow 里共用
    一个 `monthly_flow:ZZZ_MONTHLY` 标记 —— 信号残响失败会直接 return False，
    后勤商店连跑都跑不到。拆开后各自一个标记、各自成败、各自重试。
    """
    return _run_monthly_flow(logger, "ZZZ_LOGISTICS_MONTHLY", "绝区零后勤商店")


def run_nte_monthly_background(logger: logging.Logger) -> Tuple[bool, str]:
    return _run_monthly_flow(logger, "NTE_MONTHLY", "异环每月")


# ── 版本周期流程（崩铁 42 天/版本更新对齐，2026-11-12）────────────────
# 与 monthly_flow（自然月）/weekly_flow（ISO 周）并列的第三种刷新节奏：
# 崩铁版本 42 天一更，常驻商店「戏言成真」页签随版本重置可兑换次数——自然月
# 门控会漏掉"一个月两个版本更新"的场景，ISO 周门控则一天到晚重跑，都不对口径。
# 门控键 `version_cycle:<KEY>` = "<版本号>|<YYYY-MM-DD>"（成功才写）。

VERSION_CYCLE_CD_DAYS = 42   # 用户口径：每 42 天更新、注意 CD（日历不可用时的兜底）


def version_cycle_meta_key(flow_key: str) -> str:
    """版本周期流程的门控键（workflow.sqlite3 的 task_meta）。"""
    return f"version_cycle:{flow_key}"


def _parse_version_cycle_meta(stored: Optional[str]) -> Tuple[str, Optional[date]]:
    """meta 值 "<版本号>|<YYYY-MM-DD>" → (版本号, 成功日期)。

    容错：无 | 的旧值整串当版本号；日期解析失败给 None（门控按"无成功记录"走
    首跑/CD 逻辑，宁可多跑一次也不静默跳过）。
    """
    from datetime import date as _date

    if not stored:
        return "", None
    if "|" not in stored:
        return stored.strip(), None
    ver, _, day = stored.partition("|")
    try:
        return ver.strip(), _date.fromisoformat(day.strip())
    except ValueError:
        return ver.strip(), None


def version_cycle_due(
    stored: Optional[str],
    update_info: Optional[dict],
    today=None,
) -> Tuple[bool, str]:
    """版本周期门控判定（纯函数，便于测试）。

    - 日历有版本号：与已存版本不同 → 到期（含首跑无记录）；相同 → 未到期。
    - 日历不可用（update_info 为 None / 无 version 字段）：距上次成功
      ≥ VERSION_CYCLE_CD_DAYS 天兜底到期；无成功记录 → 到期。
    开服校验不在这里——公告已出未开服要顺延，由 runner 层用 open_at 判。
    """
    from datetime import date as _date

    today = today or _date.today()
    stored_ver, stored_day = _parse_version_cycle_meta(stored)
    cur_ver = str((update_info or {}).get("version") or "").strip()
    if cur_ver:
        if stored_ver and stored_ver == cur_ver:
            return False, f"当前版本 {cur_ver} 已购"
        return True, f"新版本 {cur_ver}（已购 {stored_ver or '无'}）"
    if stored_day is None:
        return True, "无购买记录，到期"
    days = (today - stored_day).days
    if days >= VERSION_CYCLE_CD_DAYS:
        return True, f"日历不可用，距上次成功 {days} 天 ≥ {VERSION_CYCLE_CD_DAYS} 天 CD"
    return False, f"日历不可用，距上次成功 {days} 天（CD {VERSION_CYCLE_CD_DAYS} 天未满）"


def _fetch_hsr_update_info() -> Optional[dict]:
    """更新日历取崩铁当前版本公告（模块缓存 6h）；不可用返回 None 走 CD 兜底。

    键必须是 app_key「HonkaiStarRail」——日历的 APP_KEY_TO_GAME 由注册表
    app_key/game_key 生成，不含公告解析 key「hsr」（2026-11-14 实锤：传 "hsr"
    恒得 None，版本商店上线起就没读到过日历，首跑标记版本号落成了 "?"）。
    """
    try:
        from ..services.game_update_calendar import get_update_info

        return get_update_info("HonkaiStarRail")
    except Exception as exc:  # noqa: BLE001 - 日历不可用不影响门控兜底
        LOGGER.warning("[版本商店] 读取更新日历失败: %s", exc)
        return None


def _version_shop_open(info: dict, now: Optional[datetime] = None) -> bool:
    """新版本公告已出但未开服 → False（顺延，别抢在关服时启动客户端）。

    open_at + 5min 的口径与更新日历 buffer 一致；解析失败宁可放行（由流程
    内部的失败取证兜底）。
    """
    from datetime import datetime as _dt, timedelta as _td

    open_at = str((info or {}).get("open_at") or "").strip()
    if not open_at:
        return True
    try:
        open_dt = _dt.fromisoformat(open_at)
    except ValueError:
        return True
    if now is not None:
        now_dt = now
    elif open_dt.tzinfo is not None:
        now_dt = _dt.now(open_dt.tzinfo)
    else:
        now_dt = _dt.now()
    try:
        if (open_dt.tzinfo is None) != (now_dt.tzinfo is None):
            # naive/aware 混比只发生在注入时钟的测试/工具路径：统一去 tz 按挂钟比较，
            # 不能"保守放行"——那会把"未开服顺延"整个语义吞掉（套件实锤）。
            return now_dt.replace(tzinfo=None) >= open_dt.replace(tzinfo=None) + _td(minutes=5)
        return now_dt >= open_dt + _td(minutes=5)
    except (TypeError, ValueError):  # noqa: BLE001 - 仍比不了的极端情况才放行
        return True


def _run_version_cycle_flow(
    logger: logging.Logger,
    flow_key: str,
    label: str,
    *,
    update_info: Optional[dict] = None,
    now: Optional[datetime] = None,
) -> Tuple[bool, str]:
    """版本周期流程通用门控 + 执行：到期才 run_flow，成功才写标记。

    `update_info`/`now` 可注入（测试用）；不注入走真实日历与时钟。
    """
    from datetime import date as _date, datetime as _dt

    from ..storage.workflow_db import WorkflowDB
    from ..settings.paths import WORKFLOW_DB_PATH

    db = WorkflowDB(str(WORKFLOW_DB_PATH))
    meta_key = version_cycle_meta_key(flow_key)
    stored = db.get_task_meta(meta_key)
    info = update_info if update_info is not None else _fetch_hsr_update_info()
    now_dt = now or _dt.now()
    today = now_dt.date() if isinstance(now_dt, _dt) else now_dt
    due, reason = version_cycle_due(stored, info, today=today)
    if not due:
        logger.info("[%s] %s，跳过", label, reason)
        return True, reason
    if not _version_shop_open(info or {}, now=now_dt if isinstance(now_dt, _dt) else None):
        logger.info("[%s] 新版本未开服，顺延（open_at=%s）", label, (info or {}).get("open_at"))
        return True, "新版本未开服，顺延"
    ok, msg = run_flow(logger, flow_key)
    if ok:
        ver = str((info or {}).get("version") or "")
        db.set_task_meta(meta_key, f"{ver}|{today.isoformat()}")
        logger.info("[%s] ✅ %s（已记录版本 %s|%s）", label, msg, ver or "?", today.isoformat())
        return True, f"✅ {msg}"
    logger.warning("[%s] ❌ %s（下次运行重试）", label, msg)
    return False, f"❌ {msg}（下次运行重试）"


def run_hsr_version_shop_background(logger: logging.Logger) -> Tuple[bool, str]:
    """崩铁戏言成真版本商店（金品质三件，随版本重置）。

    门控：更新日历版本号变化 → 跑（开服后才跑）；同版本 → 跳过；
    日历不可用 → 42 天 CD 兜底。清单与导航在 flow_configs 的 HSR_VERSION_SHOP。
    """
    return _run_version_cycle_flow(logger, "HSR_VERSION_SHOP", "崩铁戏言成真商店")

