"""每日任务完成情况检测模块。

通过各游戏的官方/社区 API 查询每日任务完成状态，
用于脚本执行后的成功判定。

覆盖游戏：
- 原神 (Genshin Impact) — HoYoLab 实时便笺 API
- 崩坏：星穹铁道 (Honkai: Star Rail) — HoYoLab 实时便笺 API
- 绝区零 (Zenless Zone Zero) — HoYoLab 实时便笺 API
- 鸣潮 (Wuthering Waves) — 库街区每日数据 API
- 无限暖暖 (Infinity Nikki) — Pearpal notebook API
"""

from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .mihoyo_sign import (
    get_bound_accounts,
    tidy_cookie,
    GAME_DEFINITIONS as SIGN_GAME_DEFS,
    enrich_cookie_for_record_api,
)
from .wuwa_kujiequ import (
    WuwaKujiequProfile,
    fetch_daily_data,
    fetch_base_data,
    profile_from_dict,
)

LOGGER = logging.getLogger("GameAutomation")



def _extract_cookie_fields(cookie: str) -> Dict[str, str]:
    """从 cookie 字符串中提取所有字段。"""
    fields = {}
    for part in cookie.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            fields[k.strip()] = v.strip()
    return fields


def _hoyo_record_ds(query: str = "", body: str = "") -> str:
    """生成国服游戏记录 API 的 DS 签名（TeyvatGuide X4 格式）。"""
    from hashlib import md5 as _md5
    salt = "xV8v4Qu54lUKrEYFZkJhB8cuOh9Asafs"
    t = str(int(time.time()))
    r = str(random.randint(100000, 200000))
    raw = f"salt={salt}&t={t}&r={r}&b={body}&q={query}"
    return f"{t},{r},{_md5(raw.encode()).hexdigest()}"


def _query_hoyo_daily_note(cookie: str, uid: int, server: str, game: str) -> Dict[str, Any]:
    """直接调用米游社实时便笺 API（TeyvatGuide 方式：account_id + cookie_token）。"""
    from urllib.request import Request, urlopen

    fields = _extract_cookie_fields(cookie)
    account_id = fields.get("account_id") or fields.get("stuid") or ""
    cookie_token = fields.get("cookie_token") or fields.get("cookie_token_v2") or ""
    if not account_id or not cookie_token:
        raise RuntimeError("缺少 account_id/cookie_token")

    query_str = f"role_id={uid}&server={server}"
    ds = _hoyo_record_ds(query=query_str)

    paths = {
        "genshin": "/game_record/app/genshin/api/dailyNote",
        "honkai_sr": "/game_record/app/hkrpg/api/note",
        "zzz": "/event/game_record_zzz/api/zzz/note",
    }
    path = paths.get(game)
    if not path:
        raise RuntimeError(f"未知游戏: {game}")

    url = f"https://api-takumi-record.mihoyo.com{path}?{query_str}"
    ck = f"account_id={account_id};cookie_token={cookie_token}"
    headers = {
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) miHoYoBBS/2.106.2",
        "x-rpc-app_version": "2.106.2",
        "x-rpc-client_type": "5",
        "x-requested-with": "com.mihoyo.hyperion",
        "referer": "https://webstatic.mihoyo.com",
        "ds": ds,
        "cookie": ck,
    }

    req = Request(url, headers=headers, method="GET")
    with urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    retcode = data.get("retcode", -1)
    if retcode == 5003:
        raise RuntimeError("账号未在米游社开启「实时便笺」数据共享（请在米游社 App → 我的 → 设置 → 隐私设置中开启）")
    if retcode == 10001:
        raise RuntimeError("Cookie 已过期，请重新登录")
    if retcode == 10102:
        raise RuntimeError("数据不存在，请先在游戏内登录一次")
    if retcode != 0:
        raise RuntimeError(f"API 错误 (retcode={retcode}): {data.get('message', '')}")

    return data.get("data") or {}


def _query_genshin_notes(cookie: str, uid: int) -> Dict[str, Any]:
    """查询原神实时便笺。"""
    return _query_hoyo_daily_note(cookie, uid, "cn_gf01", "genshin")


def _query_starrail_notes(cookie: str, uid: int) -> Dict[str, Any]:
    """查询星铁实时便笺。"""
    return _query_hoyo_daily_note(cookie, uid, "prod_gf_cn", "honkai_sr")


def _query_zzz_notes(cookie: str, uid: int) -> Dict[str, Any]:
    """查询绝区零实时便笺。"""
    return _query_hoyo_daily_note(cookie, uid, "prod_gf_cn", "zzz")


def _check_hoyo_accounts(cookie: str, game_key: str) -> List[Dict[str, Any]]:
    missing = [f for f in ("stoken", "stoken_v2", "stuid", "stuid_v2") if f not in cookie]
    if missing:
        LOGGER.warning("[每日检查] Cookie 可能缺少游戏记录所需字段：%s", ", ".join(missing))
    return get_bound_accounts(cookie, game_key)


# ---------------------------------------------------------------------------
# 游戏每日状态数据结构
# ---------------------------------------------------------------------------

@dataclass
class DailyTaskItem:
    """单个每日任务项。"""
    name: str
    current: int
    total: int
    is_completed: bool

    @property
    def progress_percent(self) -> int:
        try:
            cur = int(self.current) if not isinstance(self.current, dict) else 0
            tot = int(self.total) if not isinstance(self.total, dict) else 1
        except (ValueError, TypeError):
            return 100 if self.is_completed else 0
        if tot <= 0:
            return 100 if self.is_completed else 0
        return int(cur / tot * 100)


@dataclass
class GameDailyStatus:
    """单个游戏的每日任务完成状态。"""
    game_key: str
    game_name: str
    uid: str
    nickname: str
    is_daily_completed: bool
    tasks: List[DailyTaskItem] = field(default_factory=list)
    extra: Dict[str, Any] = field(default_factory=dict)
    raw: Any = None
    errors: List[str] = field(default_factory=list)

    @property
    def progress_percent(self) -> int:
        if not self.tasks:
            return 100 if self.is_daily_completed else 0
        total_progress = sum(t.progress_percent for t in self.tasks)
        return int(total_progress / len(self.tasks))


# ---------------------------------------------------------------------------
# 原神
# ---------------------------------------------------------------------------

def check_genshin_daily(cookie: str) -> List[GameDailyStatus]:
    """检查原神每日任务完成情况。"""
    accounts = _check_hoyo_accounts(cookie, "genshin")
    results: List[GameDailyStatus] = []
    for account in accounts:
        uid = account.get("uid", "")
        nickname = account.get("nickname", "")
        try:
            result = _query_genshin_notes(cookie, int(uid))
            finished = int(result.get("finished_task_num") or 0)
            total = int(result.get("total_task_num") or 4)
            claimed = bool(result.get("is_extra_task_reward_received"))
            tasks: List[DailyTaskItem] = []
            tasks.append(DailyTaskItem(
                name="每日委托",
                current=finished,
                total=total,
                is_completed=claimed or finished >= total,
            ))
            results.append(GameDailyStatus(
                game_key="genshin", game_name="原神", uid=uid, nickname=nickname,
                is_daily_completed=claimed or finished >= total, tasks=tasks,
                extra={"resin": int(result.get("current_resin") or 0),
                       "max_resin": int(result.get("max_resin") or 160)},
                raw=result,
            ))
        except Exception as exc:
            msg = str(exc)
            LOGGER.error("[原神] %s", msg)
            results.append(GameDailyStatus(
                game_key="genshin", game_name="原神", uid=uid, nickname=nickname,
                is_daily_completed=False, errors=[msg],
            ))
    return results


# ---------------------------------------------------------------------------
# 崩坏：星穹铁道
# ---------------------------------------------------------------------------

def check_hsr_daily(cookie: str) -> List[GameDailyStatus]:
    """检查崩坏：星穹铁道每日任务完成情况。"""
    accounts = _check_hoyo_accounts(cookie, "honkai_sr")
    results: List[GameDailyStatus] = []
    for account in accounts:
        uid = account.get("uid", "")
        nickname = account.get("nickname", "")
        try:
            result = _query_starrail_notes(cookie, int(uid))
            score = int(result.get("current_train_score") or 0)
            max_score = int(result.get("max_train_score") or 500)
            tasks: List[DailyTaskItem] = []
            tasks.append(DailyTaskItem(
                name="每日实训",
                current=score,
                total=max_score,
                is_completed=score >= max_score,
            ))
            results.append(GameDailyStatus(
                game_key="honkai_sr", game_name="崩坏：星穹铁道", uid=uid, nickname=nickname,
                is_daily_completed=score >= max_score, tasks=tasks,
                extra={"stamina": int(result.get("current_stamina") or 0),
                       "max_stamina": int(result.get("max_stamina") or 240)},
                raw=result,
            ))
        except Exception as exc:
            msg = str(exc)
            LOGGER.error("[星铁] %s", msg)
            results.append(GameDailyStatus(
                game_key="honkai_sr", game_name="崩坏：星穹铁道", uid=uid, nickname=nickname,
                is_daily_completed=False, errors=[msg],
            ))
    return results


# ---------------------------------------------------------------------------
# 绝区零
# ---------------------------------------------------------------------------

def check_zzz_daily(cookie: str) -> List[GameDailyStatus]:
    """检查绝区零每日任务完成情况。"""
    accounts = _check_hoyo_accounts(cookie, "zzz")
    results: List[GameDailyStatus] = []
    for account in accounts:
        uid = account.get("uid", "")
        nickname = account.get("nickname", "")
        try:
            result = _query_zzz_notes(cookie, int(uid))
            engagement = result.get("engagement") or {}
            battery = result.get("battery_charge") or {}
            eng_current = int(engagement.get("current") or 0) if isinstance(engagement, dict) else 0
            eng_max = int(engagement.get("max") or 400) if isinstance(engagement, dict) else 400
            bat_current = int(battery.get("current") or 0) if isinstance(battery, dict) else 0
            bat_max = int(battery.get("max") or 240) if isinstance(battery, dict) else 240
            tasks: List[DailyTaskItem] = []
            tasks.append(DailyTaskItem(
                name="活跃度",
                current=eng_current,
                total=eng_max,
                is_completed=eng_current >= eng_max,
            ))
            results.append(GameDailyStatus(
                game_key="zzz", game_name="绝区零", uid=uid, nickname=nickname,
                is_daily_completed=eng_current >= eng_max, tasks=tasks,
                extra={"energy": bat_current, "max_energy": bat_max},
                raw=result,
            ))
        except Exception as exc:
            msg = str(exc)
            LOGGER.error("[绝区零] %s", msg)
            results.append(GameDailyStatus(
                game_key="zzz", game_name="绝区零", uid=uid, nickname=nickname,
                is_daily_completed=False, errors=[msg],
            ))
    return results


# ---------------------------------------------------------------------------
# 鸣潮
# ---------------------------------------------------------------------------

def check_wuwa_daily(profile: WuwaKujiequProfile) -> GameDailyStatus:
    """检查鸣潮每日任务完成情况。"""
    try:
        daily_data = fetch_daily_data(profile)
        base_data = fetch_base_data(profile)
        tasks: List[DailyTaskItem] = []

        def _detail_progress(detail: Any) -> tuple[int, int]:
            if not isinstance(detail, dict):
                return 0, 0
            current = _safe_int(detail.get("cur"), 0)
            total_value = _safe_int(detail.get("total"), 0)
            if total_value <= 0:
                value = str(detail.get("value") or "")
                if "/" in value:
                    left, right = value.split("/", 1)
                    current = _safe_int(left.strip(), current)
                    total_value = _safe_int(right.strip(), total_value)
            return current, total_value

        liveness = daily_data.get("liveness") or {}
        completed, total = _detail_progress(liveness)
        tasks.append(DailyTaskItem(
            name="今日活跃", current=completed, total=total, is_completed=completed >= total if total else False,
        ))
        energy = daily_data.get("energy") or {}
        current_energy, max_energy = _detail_progress(energy)
        if max_energy <= 0:
            current_energy = base_data.get("energy", 0) or 0
            max_energy = base_data.get("max_energy", 0) or 240
        weekly = daily_data.get("weekly") or {}
        weekly_current, weekly_total = _detail_progress(weekly)
        LOGGER.info("[鸣潮] 每日检查成功：%s", profile.role_name)
        return GameDailyStatus(
            game_key="wuwa", game_name="鸣潮", uid=str(profile.role_id), nickname=profile.role_name,
            is_daily_completed=completed >= total if total else False, tasks=tasks,
            extra={
                "energy": current_energy,
                "max_energy": max_energy,
                "active_days": base_data.get("active_days", 0),
                "liveness_int": completed,
                "liveness_max": total,
                "liveness_data": liveness,
                "weekly": {
                    "current": weekly_current,
                    "max": weekly_total,
                    "detail": weekly,
                },
            },
            raw={"daily": daily_data, "base": base_data},
        )
    except Exception as exc:
        LOGGER.error("[鸣潮] 检查失败: %s", exc, exc_info=True)
        return GameDailyStatus(
            game_key="wuwa", game_name="鸣潮", uid=str(profile.role_id), nickname=profile.role_name,
            is_daily_completed=False, errors=[str(exc)],
        )


# ---------------------------------------------------------------------------
# 无限暖暖（Pearpal notebook API）
# ---------------------------------------------------------------------------

import os as _os
import sys as _sys

# 先尝试相对导入（当 daily_checker 作为包的一部分被导入时）
try:
    from ..gacha.nikki_resonance_fetch import (
        fetch_pearpal_notebook_payload,
        fetch_pearpal_user_info,
        _get_pearpal_api_config,
    )
except ImportError:
    # 再尝试绝对导入（当项目根目录在 sys.path 上时）
    try:
        from gacha.nikki_resonance_fetch import (
            fetch_pearpal_notebook_payload,
            fetch_pearpal_user_info,
            _get_pearpal_api_config,
        )
    except ImportError:
        # 最后尝试动态添加项目根目录到 sys.path
        _pkg_dir = _os.path.dirname(_os.path.abspath(__file__))
        _project_root = _os.path.dirname(_pkg_dir)
        if _project_root not in _sys.path:
            _sys.path.insert(0, _project_root)
        from gacha.nikki_resonance_fetch import (
            fetch_pearpal_notebook_payload,
            fetch_pearpal_user_info,
            _get_pearpal_api_config,
        )


def _record_nikki_to_tracker(tracker, status: GameDailyStatus):
    """把无限暖暖的 DailyTaskItem 同步到 tracker。"""
    for task in status.tasks:
        task_status = "success" if task.is_completed else (
            "skipped" if task.name == "未配置凭据" else "pending"
        )
        tracker.record_task(
            game_key=status.game_key, game_name=status.game_name,
            task_key=task.name, task_name=task.name,
            status=task_status, source="pearpal_notebook",
            message=f"{task.current}/{task.total}" if task.total else "",
            uid=status.uid, nickname=status.nickname,
            extra={"current": task.current, "total": task.total},
        )


# 无限暖暖期望监控的 task 模板。即使 Pearpal API 不可用，也返回这组 task 让
# DailyView 卡片始终能展示出"监控框架"（每条标 pending 或基于 notbeook gm 数据填值）。
NIKKI_EXPECTED_TASKS: Dict[str, Dict[str, Any]] = {
    "每日心愿": {
        "gm_key": "daily_task",
        "max": 500,
        "is_required": True,   # 每日心愿全满 → 当日完成
    },
    "周常奖励": {
        "gm_key": "weekly_reward_status",
        "max": 1,
        "is_required": True,
    },
    "能量/体力": {
        "gm_key": "energy",
        "max": 240,
        "is_required": False,  # 不计入完成
    },
    "派遣进行中": {
        "gm_key": "dispatch_active",
        "max": 4,
        "is_required": False,
    },
    "登录天数": {
        "gm_key": "login_days",
        "max": 0,  # 0 表示仅展示，不参与完成判断
        "is_required": False,
    },
}


def _build_nikki_tasks_from_gm(gm: Dict[str, Any], info_self: Dict[str, Any]) -> List[DailyTaskItem]:
    """根据 Pearpal notebook 的 gm/info_self 字典，生成期望监控的 nikki task 列表。

    只要 gm 字典里某个 gm_key 存在，就用真实数据填 current/total；不存在的 task
    会以 0/0 + is_completed=False 占位（"尚未追踪"）。
    """
    tasks: List[DailyTaskItem] = []
    # 计算当前活跃派遣数
    active_dispatch = 0
    if isinstance(gm.get("dispatch"), list):
        try:
            import time as _t
            now_ts = _t.time()
            for d in gm["dispatch"]:
                if not isinstance(d, dict):
                    continue
                start = int(d.get("start_time") or 0)
                duration_h = int(d.get("spend_time") or 0)
                if now_ts < start + duration_h * 3600:
                    active_dispatch += 1
        except Exception:
            active_dispatch = 0

    for name, meta in NIKKI_EXPECTED_TASKS.items():
        gm_key = meta["gm_key"]
        if gm_key == "dispatch_active":
            current = active_dispatch
        elif gm_key == "login_days":
            current = int(info_self.get("login_days") or 0)
        else:
            try:
                current = int(gm.get(gm_key) or 0)
            except (TypeError, ValueError):
                current = 0
        total = int(meta.get("max") or 0)
        is_required = bool(meta.get("is_required"))
        if total > 0 and is_required:
            is_completed = current >= total
        elif total > 0 and not is_required:
            # 进度类指标（能量/派遣/登录天数）：只要 Pearpal 返回了非零数据就算"已追踪"
            is_completed = current > 0
        else:
            is_completed = current > 0
        tasks.append(DailyTaskItem(
            name=name, current=current, total=total, is_completed=is_completed,
        ))
    return tasks


def check_nikki_daily(
    momo_token: str,
    momo_id: str,
    uid: str = "",
    domain: str = "x6cn-clickhouse.nuanpaper.com",
) -> GameDailyStatus:
    """检查无限暖暖每日任务完成情况。

    通过 Pearpal notebook API 获取每日心愿/能量/派遣/登录天数等指标，
    按 `NIKKI_EXPECTED_TASKS` 模板生成 task 列表，使 DailyView 始终能看到
    期望监控的所有任务（每条 current/total 反映真实数据，没有则 0 占位）。

    判定标准：每日心愿 ≥ 500 + 周常奖励已领取。
    """
    resolved_uid = uid
    nickname = ""
    errors: List[str] = []
    extra: Dict[str, Any] = {}
    gm: Dict[str, Any] = {}
    info_self: Dict[str, Any] = {}

    if fetch_pearpal_notebook_payload is None:
        errors.append("nikki_resonance_fetch 模块不可用")
    elif not (momo_token and momo_id):
        errors.append("未配置 momo_token / momo_id，无法拉取 Pearpal notebook")
    else:
        try:
            notebook = fetch_pearpal_notebook_payload(momo_token, momo_id, domain=domain)
        except Exception as exc:
            LOGGER.warning("[nikki] 获取 notebook 数据失败：%s", exc)
            errors.append(f"获取 notebook 数据失败：{exc}")
            notebook = None
        if isinstance(notebook, dict):
            gm = notebook.get("info_from_gm") or {}
            info_self = notebook.get("info_from_self") or {}
            if not isinstance(gm, dict):
                gm = {}
            if not isinstance(info_self, dict):
                info_self = {}
            resolved_uid = str(gm.get("role_id") or uid or "")
            nickname = str(gm.get("name") or info_self.get("name") or "")
            extra["has_data"] = True
        elif notebook is not None:
            LOGGER.warning("[nikki] notebook 返回类型异常：%s", type(notebook).__name__)
            errors.append(f"notebook 返回类型异常：{type(notebook).__name__}")

    # 始终按 NIKKI_EXPECTED_TASKS 生成 task 列表，让 DailyView 总能看到监控框架
    tasks = _build_nikki_tasks_from_gm(gm, info_self)

    # 填充 extra
    if "daily_task" in gm:
        try:
            extra["daily_task"] = {"current": int(gm["daily_task"] or 0), "total": 500}
        except (TypeError, ValueError):
            pass
    if "weekly_reward_status" in gm:
        try:
            extra["weekly_reward_completed"] = int(gm["weekly_reward_status"] or 0) >= 1
        except (TypeError, ValueError):
            pass
    if "energy" in gm:
        try:
            extra["energy"] = int(gm["energy"] or 0)
        except (TypeError, ValueError):
            pass
    if "level" in gm:
        try:
            extra["level"] = int(gm["level"] or 0)
        except (TypeError, ValueError):
            pass
    if "login_days" in info_self:
        try:
            extra["login_days"] = int(info_self["login_days"] or 0)
        except (TypeError, ValueError):
            pass
    if isinstance(gm.get("dispatch"), list):
        active = 0
        total_d = 0
        import time as _t
        now_ts = _t.time()
        for d in gm["dispatch"]:
            if not isinstance(d, dict):
                continue
            total_d += 1
            try:
                start = int(d.get("start_time") or 0)
                duration_h = int(d.get("spend_time") or 0)
                if now_ts < start + duration_h * 3600:
                    active += 1
            except (TypeError, ValueError):
                continue
        extra["dispatch"] = {"total": total_d, "active": active}

    # 完成判断：每日心愿 + 周常奖励（is_required=True 的任务）
    daily_completed = all(
        t.is_completed for t in tasks
        if NIKKI_EXPECTED_TASKS.get(t.name, {}).get("is_required")
    ) and any(
        NIKKI_EXPECTED_TASKS.get(t.name, {}).get("is_required")
        for t in tasks
    )

    if errors:
        LOGGER.info("[nikki] 每日检查部分失败：errors=%s, tasks=%d", errors, len(tasks))
    else:
        LOGGER.info("[nikki] 每日检查成功：%s, tasks=%d", nickname or resolved_uid, len(tasks))

    return GameDailyStatus(
        game_key="nikki",
        game_name="无限暖暖",
        uid=resolved_uid,
        nickname=nickname,
        is_daily_completed=daily_completed,
        tasks=tasks,
        extra=extra,
        raw=gm or None,
        errors=errors,
    )


# ---------------------------------------------------------------------------
# 统一接口
# ---------------------------------------------------------------------------

@dataclass
class DailyCheckResult:
    """所有游戏的每日检查结果汇总。"""
    games: List[GameDailyStatus] = field(default_factory=list)
    checked_at: float = field(default_factory=time.time)

    @property
    def all_completed(self) -> bool:
        """所有已检查游戏是否全部完成每日任务。"""
        if not self.games:
            return False
        return all(g.is_daily_completed for g in self.games)

    @property
    def completed_games(self) -> List[GameDailyStatus]:
        return [g for g in self.games if g.is_daily_completed]

    @property
    def progress_percent(self) -> int:
        if not self.games:
            return 0
        total = sum(g.progress_percent for g in self.games)
        return int(total / len(self.games))


def _load_app_config() -> Dict[str, Any]:
    """加载应用配置（直接读 JSON，避免相对导入问题）。"""
    import json as _json
    from pathlib import Path
    config_path = Path(__file__).resolve().parent.parent / "app_config.json"
    if not config_path.exists():
        return {}
    try:
        with open(config_path, encoding="utf-8") as f:
            return _json.load(f)
    except Exception:
        return {}


def _safe_int(v, default=0):
    if isinstance(v, dict):
        return default
    try:
        return int(v)
    except (ValueError, TypeError):
        return default


def _build_missing_config_status(game_key: str, reason: str) -> GameDailyStatus:
    """构建配置缺失时的状态。"""
    from ..settings.defaults import GAME_KEYS
    return GameDailyStatus(
        game_key=game_key,
        game_name=GAME_KEYS.get(game_key, game_key),
        uid="",
        nickname="",
        is_daily_completed=False,
        errors=[reason],
    )


def _normalize_requested_game_keys(
    game_keys: Optional[List[str]],
) -> tuple[Optional[List[str]], List[str]]:
    """规范化请求的游戏 key 列表，支持别名。"""
    from ..settings import resolve_game_key
    if game_keys is None:
        return None, []
    normalized: List[str] = []
    unknown: List[str] = []
    for key in game_keys:
        gk = resolve_game_key(key)
        from ..settings.defaults import GAME_KEYS
        if gk in GAME_KEYS:
            normalized.append(gk)
        else:
            unknown.append(key)
    return normalized, unknown


def check_games_progress(
    config: Optional[Dict[str, Any]] = None,
    game_keys: Optional[List[str]] = None,
    tracker=None,
) -> DailyCheckResult:
    """按游戏列表查询每日 API 进度。

    config 应包含 mihoyo_sign.cookie、wuwa_kujiequ.profile、
    nikki 的 momo_token/momo_id 等字段。
    game_keys 为 None 时按默认配置检查全部可用游戏；
    传入列表时只查询指定游戏，并支持别名如 genshin/hsr/zzz/wuwa/nikki。
    tracker: DailyProgressTracker 实例，用于持久化记录结果。
    """
    if config is None:
        config = _load_app_config()

    result = DailyCheckResult()
    errors: List[str] = []
    requested_games, unknown_games = _normalize_requested_game_keys(game_keys)
    if unknown_games:
        LOGGER.warning("[每日检查] 忽略未知游戏 key：%s", "、".join(unknown_games))

    def _wants(game_key: str) -> bool:
        return requested_games is None or game_key in requested_games

    # --- 米哈游游戏（原神/星铁/绝区零） ---
    mihoyo_cfg = config.get("mihoyo_sign", {}) if isinstance(config, dict) else {}
    cookie = str(mihoyo_cfg.get("cookie", "") if isinstance(mihoyo_cfg, dict) else "").strip()
    stoken_cookie = str(mihoyo_cfg.get("stoken_cookie", "") if isinstance(mihoyo_cfg, dict) else "").strip()

    hoyo_games = [
        ("genshin", "原神", check_genshin_daily),
        ("honkai_sr", "崩坏：星穹铁道", check_hsr_daily),
        ("zzz", "绝区零", check_zzz_daily),
    ]

    base_cookie = stoken_cookie or cookie
    if base_cookie:
        enriched = enrich_cookie_for_record_api(base_cookie)
        record_cookie = enriched["cookie"]
        record_ready = enriched["record_ready"]

        enabled_games = mihoyo_cfg.get("enabled_games", {}) if isinstance(mihoyo_cfg, dict) else {}
        for game_key, game_name, check_fn in hoyo_games:
            if not _wants(game_key):
                continue
            if requested_games is None and isinstance(enabled_games, dict) and not enabled_games.get(game_key):
                continue
            if not record_ready:
                LOGGER.warning("[每日检查] %s 跳过：缺少 stoken，便笺 API 需密码/短信登录。", game_name)
                status = GameDailyStatus(
                    game_key=game_key, game_name=game_name, uid="", nickname="",
                    is_daily_completed=False,
                    errors=["缺少 stoken，无法查询实时便笺。请使用短信验证码登录米游社获取 stoken。"],
                )
                result.games.append(status)
                if tracker is not None:
                    tracker.record_task(
                        game_key, game_name, "daily_commissions", "每日委托", "skipped",
                        message="缺少 stoken，无法查询", uid="", source="community",
                    )
                continue
            try:
                statuses = check_fn(record_cookie)
                result.games.extend(statuses)
                if tracker is not None:
                    for st in statuses:
                        task_status = "success" if st.is_daily_completed else "pending"
                        for task in st.tasks:
                            if task.is_completed:
                                tracker.record_task(
                                    game_key=st.game_key, game_name=st.game_name,
                                    task_key=task.name, task_name=task.name,
                                    status="success", source="community",
                                    message=f"{task.current}/{task.total}",
                                    uid=st.uid, nickname=st.nickname,
                                    extra={"current": task.current, "total": task.total},
                                )
                            else:
                                tracker.record_task(
                                    game_key=st.game_key, game_name=st.game_name,
                                    task_key=task.name, task_name=task.name,
                                    status="pending", source="community",
                                    message=f"{task.current}/{task.total}",
                                    uid=st.uid, nickname=st.nickname,
                                    extra={"current": task.current, "total": task.total},
                                )
            except Exception as exc:
                LOGGER.warning("[每日检查] %s 检查失败：%s", game_key, exc)
                errors.append(f"{game_key}: {exc}")
                if tracker is not None:
                    tracker.record_error(game_key, str(exc))
    elif requested_games is not None:
        for game_key, _, _ in hoyo_games:
            if _wants(game_key):
                result.games.append(_build_missing_config_status(game_key, "未配置 mihoyo_sign.cookie，无法查询实时便笺 API"))

    # --- 鸣潮 ---
    wuwa_cfg = config.get("wuwa_kujiequ", {}) if isinstance(config, dict) else {}
    profile = profile_from_dict(wuwa_cfg.get("profile") if isinstance(wuwa_cfg, dict) else None)
    if profile is not None and _wants("wuwa"):
        try:
            wuwa_status = check_wuwa_daily(profile)
            result.games.append(wuwa_status)
            if tracker is not None:
                _record_wuwa_to_tracker(tracker, wuwa_status)
        except Exception as exc:
            LOGGER.warning("[每日检查] 鸣潮检查失败：%s", exc)
            errors.append(f"wuwa: {exc}")
            if tracker is not None:
                tracker.record_error("wuwa", str(exc))
    elif requested_games is not None and _wants("wuwa"):
        result.games.append(_build_missing_config_status("wuwa", "未配置库街区 profile，无法查询鸣潮每日 API"))

    # --- 无限暖暖 ---
    nikki_cfg = config.get("nikki", {}) if isinstance(config, dict) else {}
    momo_token = str(nikki_cfg.get("momo_token", "") if isinstance(nikki_cfg, dict) else "").strip()
    momo_id = str(nikki_cfg.get("momo_id", "") if isinstance(nikki_cfg, dict) else "").strip()
    nikki_uid = str(nikki_cfg.get("uid", "") if isinstance(nikki_cfg, dict) else "").strip()

    if _wants("nikki"):
        try:
            nikki_status = check_nikki_daily(momo_token, momo_id, uid=nikki_uid)
            result.games.append(nikki_status)
            if tracker is not None:
                _record_nikki_to_tracker(tracker, nikki_status)
        except Exception as exc:
            LOGGER.warning("[每日检查] 无限暖暖检查失败：%s", exc, exc_info=True)
            errors.append(f"nikki: {exc}")
            if tracker is not None:
                tracker.record_error("nikki", str(exc))

    # --- 终末地 ---
    endfield_cfg = config.get("endfield_protocol", {}) if isinstance(config, dict) else {}
    endfield_api_key = str(endfield_cfg.get("api_key", "") if isinstance(endfield_cfg, dict) else "").strip()
    endfield_framework_token = str(endfield_cfg.get("framework_token", "") if isinstance(endfield_cfg, dict) else "").strip()
    endfield_role_id = str(endfield_cfg.get("role_id", "") if isinstance(endfield_cfg, dict) else "").strip()
    endfield_server_id = str(endfield_cfg.get("server_id", "1") if isinstance(endfield_cfg, dict) else "1").strip()

    if endfield_api_key and endfield_framework_token and endfield_role_id and _wants("endfield"):
        try:
            from ..clients.endfield_protocol import perform_query as _perform_endfield_query
            note_result = _perform_endfield_query(
                endfield_api_key, endfield_framework_token, endfield_role_id, endfield_server_id, "note"
            )
            stamina_result = _perform_endfield_query(
                endfield_api_key, endfield_framework_token, endfield_role_id, endfield_server_id, "stamina"
            )

            tasks: List[DailyTaskItem] = []
            daily_completed = False

            def _ef_get_val(d, *keys):
                for k in keys:
                    v = d.get(k) if isinstance(d, dict) else None
                    if v is not None and v != "":
                        return v
                return None

            def _ef_get_dict(d, *keys):
                for k in keys:
                    v = d.get(k) if isinstance(d, dict) else None
                    if isinstance(v, dict):
                        return v
                return None

            if note_result and isinstance(note_result, dict):
                note_data = note_result.get("data", note_result)
                if isinstance(note_data, dict):
                    daily_mission = _ef_get_dict(note_data, "dailyMission", "daily_mission")
                    dailies = daily_mission or _ef_get_dict(note_data, "dailies", "daily_quest", "daily_quests",
                                                           "daily_task", "daily_tasks", "daily", "quests")
                    if dailies:
                        completed = _safe_int(_ef_get_val(dailies, "activation", "finished", "completed", "done",
                                                          "completed_count", "current", "cur") or 0)
                        total = _safe_int(_ef_get_val(dailies, "maxActivation", "max_activation", "total", "all",
                                                      "total_count", "max") or 0)
                        tasks.append(DailyTaskItem(name="日活跃", current=completed, total=total,
                                                   is_completed=completed >= total if total else False))
                        daily_completed = completed >= total if total else False
                    else:
                        LOGGER.debug("[终末地] note 返回不含 dailies，已有键: %s",
                                      list(note_data.keys())[:15] if isinstance(note_data, dict) else type(note_data))

            if stamina_result and isinstance(stamina_result, dict):
                st_data = stamina_result.get("data", stamina_result)
                if isinstance(st_data, dict):
                    rec = _ef_get_dict(st_data, "recovery", "stamina", "energy", "stamina_info")
                    src = rec if rec else st_data
                    current = _safe_int(_ef_get_val(src, "current", "cur", "value", "stamina") or 0)
                    maximum = _safe_int(_ef_get_val(src, "max", "max_value", "limit", "max_stamina", "maxStamina") or 999, 999)
                    LOGGER.debug("[终末地] 理智状态：%s/%s（不参与每日活跃完成判定）", current, maximum)

            ef_status = GameDailyStatus(
                game_key="endfield",
                game_name="终末地",
                uid=endfield_role_id,
                nickname="",
                is_daily_completed=daily_completed,
                tasks=tasks,
                extra={"note": note_result, "stamina": stamina_result},
            )
            result.games.append(ef_status)

            if tracker is not None:
                for task in ef_status.tasks:
                    tstatus = "success" if task.is_completed else "pending"
                    tracker.record_task(
                        game_key="endfield", game_name="终末地",
                        task_key=task.name, task_name=task.name,
                        status=tstatus, source="community",
                        message=f"{task.current}/{task.total}",
                        uid=endfield_role_id,
                        extra={"current": task.current, "total": task.total},
                    )
        except Exception as exc:
            LOGGER.warning("[每日检查] 终末地检查失败：%s", exc)
            errors.append(f"endfield: {exc}")
            if tracker is not None:
                tracker.record_error("endfield", str(exc))
    elif requested_games is not None and _wants("endfield"):
        missing = []
        if not endfield_api_key:
            missing.append("api_key")
        if not endfield_framework_token:
            missing.append("framework_token")
        if not endfield_role_id:
            missing.append("role_id")
        if missing:
            reason = f"未配置 {'/'.join(missing)}，请在终末地数据中心填写。"
        else:
            reason = "终末地配置不完整，无法查询。"
        result.games.append(_build_missing_config_status("endfield", reason))

    if errors:
        LOGGER.warning("[每日检查] 部分游戏检查出错：%s", "；".join(errors))

    return result


def _record_wuwa_to_tracker(tracker, status: GameDailyStatus):
    """将鸣潮每日状态写入 tracker。"""
    for task in status.tasks:
        tstatus = "success" if task.is_completed else "pending"
        tracker.record_task(
            game_key=status.game_key, game_name=status.game_name,
            task_key=task.name, task_name=task.name,
            status=tstatus, source="community",
            message=f"{task.current}/{task.total}",
            uid=status.uid, nickname=status.nickname,
            extra={"current": task.current, "total": task.total},
        )


def _record_nikki_to_tracker(tracker, status: GameDailyStatus):
    """将无限暖暖每日状态写入 tracker。"""
    for task in status.tasks:
        tstatus = "success" if task.is_completed else "pending"
        tracker.record_task(
            game_key=status.game_key, game_name=status.game_name,
            task_key=task.name, task_name=task.name,
            status=tstatus, source="community",
            message=f"{task.current}/{task.total}",
            uid=status.uid, nickname=status.nickname,
            extra={"current": task.current, "total": task.total},
        )


def check_all_games(config: Optional[Dict[str, Any]] = None) -> DailyCheckResult:
    """兼容旧接口：检查已配置的所有游戏的每日完成状态。"""
    return check_games_progress(config=config, game_keys=None)


def is_script_success(result: DailyCheckResult, required_games: Optional[List[str]] = None) -> bool:
    """基于每日检查结果判定脚本执行是否成功。

    required_games: 要求必须完成的游戏 key 列表。为 None 表示所有检查的游戏都需要完成。
    """
    if required_games:
        for game_key in required_games:
            match = [g for g in result.games if g.game_key == game_key]
            if not match or not match[0].is_daily_completed:
                return False
        return True
    return result.all_completed


# ---------------------------------------------------------------------------
# 格式输出
# ---------------------------------------------------------------------------

def format_daily_status(status: GameDailyStatus) -> str:
    """格式化单个游戏的每日状态为可读字符串。"""
    lines = [f"【{status.game_name}】{status.nickname} ({status.uid})"]
    if status.errors:
        lines.append(f"  错误：{'；'.join(status.errors)}")
        return "\n".join(lines)
    lines.append(f"  每日完成：{'是' if status.is_daily_completed else '否'}")
    for task in status.tasks:
        lines.append(f"  {task.name}：{task.current}/{task.total}")
    return "\n".join(lines)


def format_daily_result(result: DailyCheckResult) -> str:
    """格式化每日检查结果为可读字符串。"""
    lines = [f"每日检查汇总（{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(result.checked_at))}）"]
    for status in result.games:
        lines.append(format_daily_status(status))
    lines.append(f"全部完成：{'是' if result.all_completed else '否'}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------

def _qr_login_and_update_config() -> bool:
    """扫码登录获取 stoken 并更新 app_config.json。"""
    import json as _json
    from pathlib import Path
    from clients.mihoyo_sign import complete_qrcode_login

    print("=" * 50)
    print("米游社扫码登录 - 获取 stoken")
    print("=" * 50)
    print()
    print("即将生成二维码，请用米游社 App 扫码登录。")
    print("扫码后请在手机上确认登录。")
    print()

    try:
        result = complete_qrcode_login()
    except Exception as exc:
        print(f"扫码登录失败：{exc}")
        return False

    cookies = result.get("cookies") or {}
    record_ready = result.get("record_ready", False)
    account_id = result.get("account_id", "")
    stoken = result.get("stoken", "")

    print(f"登录结果：账号 {account_id}")
    print(f"  stoken: {'已获取' if stoken else '未获取'}")
    print(f"  便笺查询: {'已就绪' if record_ready else '未就绪'}")

    if not stoken:
        print("未能获取 stoken，无法查询实时便笺。")
        return False

    # 构建 cookie 字符串
    cookie_parts = []
    for key in ("account_id", "stuid", "stoken", "stoken_v2", "mid",
                "cookie_token", "cookie_token_v2", "ltoken", "ltoken_v2",
                "ltuid", "ltuid_v2", "login_ticket"):
        val = cookies.get(key) or cookies.get(f"{key}_v2") or ""
        if val:
            cookie_parts.append(f"{key}={val}")
    cookie_str = "; ".join(cookie_parts)

    # 更新 app_config.json
    config_path = Path(__file__).resolve().parent.parent / "app_config.json"
    if config_path.exists():
        with open(config_path, encoding="utf-8") as f:
            config = _json.load(f)
    else:
        config = {}

    mihoyo_cfg = config.setdefault("mihoyo_sign", {})
    mihoyo_cfg["cookie"] = cookie_str
    mihoyo_cfg["stoken_cookie"] = cookie_str

    from ..settings.manager import atomic_write_json
    atomic_write_json(config_path, config)

    print(f"\nCookie 已保存到 {config_path}")
    print(f"Cookie 字段: {list(cookies.keys())}")
    return True


def main() -> int:
    """命令行入口。"""
    import argparse
    import logging as _logging

    parser = argparse.ArgumentParser(description="每日任务完成情况检测")
    parser.add_argument("--qr-login", action="store_true",
                        help="扫码登录获取 stoken 并更新配置")
    parser.add_argument("--game", nargs="*", default=None,
                        help="指定游戏 key（genshin/honkai_sr/zzz/wuwa/nikki/endfield）")
    parser.add_argument("--json", action="store_true", help="JSON 格式输出")
    parser.add_argument("--success-check", action="store_true",
                        help="仅输出成功/失败判定（退出码）")
    parser.add_argument("--verbose", "-v", action="store_true", help="详细日志")
    args = parser.parse_args()

    _logging.basicConfig(
        level=_logging.DEBUG if args.verbose else _logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    if args.qr_login:
        ok = _qr_login_and_update_config()
        return 0 if ok else 1

    result = check_games_progress(game_keys=args.game)

    if args.json:
        import json as _json
        import dataclasses

        class _Encoder(_json.JSONEncoder):
            def default(self, o):
                if dataclasses.is_dataclass(o) and not isinstance(o, type):
                    return dataclasses.asdict(o)
                return super().default(o)

        print(_json.dumps(result, cls=_Encoder, ensure_ascii=False, indent=2))
        return 0

    if args.success_check:
        if result.all_completed:
            print("全部完成")
            return 0
        else:
            for g in result.failed_games:
                print(f"未完成: {g.game_name}")
            return 1

    print(format_daily_result(result))
    return 0 if result.all_completed else 1


if __name__ == "__main__":
    raise SystemExit(main())
