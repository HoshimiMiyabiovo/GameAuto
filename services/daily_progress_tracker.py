"""每日进度持久化追踪器。

每天 4:00 为分界点（RESET_HOUR，凌晨 4 点前算前一天），记录：
- 社区便笺查询结果（米游社、库街区、塔吉多的每日任务完成状态）
- 每日一条龙执行记录（启动前签到、自动化脚本执行结果）
- 各脚本日志解析出的关键任务明细（whimbox 朝夕心愿计数、zzz 活跃度奖励等）

**数据流**（谁写谁读）：
- 写入方：
  * clients/daily_checker.check_games_progress —— 便笺/社区 API 查询后按
    DailyTaskItem 逐条 record_task（source=community / pearpal_notebook）；
  * runtime/task_batch_runner._run_single_task —— 每个任务跑完后
    record_script_run（task_key=script_ 前缀，"脚本跑了没"的证据）；
  * daily_checker._record_zzz_script_tasks / _record_whimbox_script_tasks ——
    脚本日志解析出的关键判定任务落库（不落库统计/复核永远拿不到，见 AGENTS.md）；
  * startup_sign_runner —— 签到结果（record_sign_result，task_key=sign_ 前缀）。
- 读取方：
  * ui/bridge.DailyView —— 今日卡片与历史回看（load_today / load_date）；
  * task_batch_runner._is_game_script_done_today —— "今日已完成跳过"的判定
    （脚本行全成功之外还要求 completion_state ∈ {completed, unknown}）；
  * task_batch_runner._verify_script_log_result / _load_api_key_progress_tasks ——
    事后复核时合并 API 侧关键计数（如终末地日活跃、异环活跃度）；
  * stats_recent_days —— 历史完成率统计（分母排除 unknown 天，见下）。

**判定契约**：某游戏当天是否"完成"由 GameDailyRecord.completion_state() 统一裁决，
优先级：KEY_STATUS_TASKS（纯状态任务，如 nte 日常领取）> KEY_PROGRESS_TASKS
（计数任务，如鸣潮今日活跃/异环 dayvalue）> is_all_success > has_failed。
四态语义与消费方见 completion_state 的 docstring——**任何新游戏接入判定，
必须先在这里登记关键任务名，且名字要与数据源产出的字符串逐字一致**。

**表结构与并发**：daily_progress 以 (date_key, game_key, task_key) 为唯一键，
record_task 走 UPSERT（后写覆盖先写）。库由 storage/workflow_db.py 打开并统一
WAL 模式（AGENTS.md：勿改回 DELETE，多视图后台线程并发读是常态）。
daily_progress_errors 是补充表：游戏级查询错误（一条一行，只追加不覆盖）。

下次打开 GUI 时加载今日记录，醒目展示已完成/失败的项目。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..storage.workflow_db import WorkflowDB

LOGGER = logging.getLogger("GameAutomation")

# 每日重置时间点（小时）
RESET_HOUR = 4

# 各游戏"真实游玩进度"对应的关键任务名（按优先级匹配）。
# 用于 progress_percent：优先用这些任务的 cur/total 计算，
# 而不是"任务条数比例"（后者跟玩家实际进度无关，且会把 failed 误算成完成）。
#
# 脚本型游戏没有计数（只有 success/failed 状态），用 KEY_STATUS_TASKS
# 按状态判定，不走计数口径。
KEY_STATUS_TASKS: Dict[str, tuple] = {
    "nte": ("日常领取",),
    # 无限暖暖：重试/复核的关键判定任务（whimbox 日志口径，用户拍板 2026-10-06）：
    # 朝夕心愿成功即当日完成，美鸭梨挖掘/星海拾光等边角失败不触发整轮重试
    "nikki": ("朝夕心愿",),
    # 绝区零：OneDragon 日志口径的「活跃度奖励」是**纯状态型**任务
    # （日志只写"活跃度已满"/"活跃度未满"，天然没有 X/Y 计数）。
    # 放进 KEY_STATUS_TASKS 按状态判定；否则走计数口径会因拿不到计数
    # 被误判成 unknown（2026-10-08 实例：明明"活跃度已满"却显示数据未刷新）
    "zzz": ("活跃度奖励",),
}

KEY_PROGRESS_TASKS: Dict[str, tuple] = {
    "wuwa": ("今日活跃", "活跃度"),
    "endfield": ("日活跃", "每日活跃"),
    "genshin": ("每日委托", "委托"),
    "honkai_sr": ("每日实训", "活跃度"),
    # 白荆回廊：视觉版日常流程（baijing_daily）扫描任务列表后按静态目录核算
    # 活跃度（15 个日常任务，2026-10-18 实测提取），记入「活跃度」任务
    "baijing": ("活跃度",),
    # 绝区零：OneDragon 日志实际产出的任务名是「活跃度奖励」（2026-10-07 实测确认），
    # 写"活跃度"精确匹配不上，会退回 is_all_success 口径
    "zzz": ("活跃度奖励", "活跃度"),
    # 无限暖暖：whimbox 日志里的心愿任务叫"朝夕心愿"（用户拍板 2026-10-06，
    # 优先取 whimbox 计数）；Pearpal API 侧叫"每日心愿"（满值 500，常年 0/500 不刷新），
    # Pearpal 落库已于 2026-10-24 停用（假进度任务删除），名字保留仅为兼容历史记录
    "nikki": ("朝夕心愿", "每日心愿"),
    # 异环：塔吉多 roleHome 的 dayvalue（每日活跃度，满 100 领全部奖励），
    # 2026-10-24 接入；判定口径 dayvalue >= 100 → 当日完成（复核兜底优先于 ok-nte 边角失败）
    "nte": ("活跃度",),
}


def _today_key() -> str:
    """获取今天的日期键（按 4:00 分界）。"""
    tm = time.localtime()
    hour = tm.tm_hour
    if hour < RESET_HOUR:
        # 4 点前算昨天
        tm_yesterday = time.localtime(time.time() - 86400)
        return time.strftime("%Y-%m-%d", tm_yesterday)
    return time.strftime("%Y-%m-%d", tm)


def _is_same_day(ts: float) -> bool:
    """判断时间戳是否属于当前日期周期（按 4:00 分界）。"""
    return _day_key(ts) == _today_key()


def _day_key(ts: float) -> str:
    """获取时间戳对应的日期键（按 4:00 分界）。"""
    tm = time.localtime(ts)
    hour = tm.tm_hour
    if hour < RESET_HOUR:
        tm_yesterday = time.localtime(ts - 86400)
        return time.strftime("%Y-%m-%d", tm_yesterday)
    return time.strftime("%Y-%m-%d", tm)


@dataclass
class DailyTaskRecord:
    """单个任务的每日记录。

    extra 约定：计数型任务必须带 {"current": X, "total": Y}——
    KEY_PROGRESS_TASKS 的真实进度百分比从这里取；缺 extra 会让
    "朝夕心愿 500/500" 这类真实进度被当成 0/未刷新（AGENTS.md 铁律）。
    status 用五种：pending/success/failed/skipped/already
    （already 专指"今天已经签到过"，等价成功）。
    """
    task_key: str
    task_name: str
    source: str  # 'community' | 'script'
    status: str  # 'pending' | 'success' | 'failed' | 'skipped' | 'already'
    message: str = ""
    updated_at: float = field(default_factory=time.time)
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class GameDailyRecord:
    """单个游戏的每日记录汇总。

    由 load_today/load_date 从 daily_progress 表按 game_key 聚合而成，
    也被 task_batch_runner 的事后复核用脚本任务临时拼出来走同一套判定。
    is_all_success / has_failed / completion_state 三个判定的口径差异
    见 completion_state 的 docstring——历史统计与复核必须走 completion_state，
    禁止直接用 is_all_success（鸣潮/暖暖混入资源周本后永远判不满）。
    """
    game_key: str
    game_name: str
    uid: str = ""
    nickname: str = ""
    tasks: List[DailyTaskRecord] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    checked_at: float = 0.0

    @property
    def is_all_success(self) -> bool:
        """所有任务行全部成功/已签到才为 True。

        警告：只适合"任务列表干净"的游戏。鸣潮/暖暖的列表混有资源与周本
        （结晶波片、千道门扉），永远凑不齐全部 success——整体判定必须走
        completion_state()，此属性仅作兜底与展示。
        """
        if not self.tasks:
            return False
        return all(t.status in ("success", "already") for t in self.tasks)

    @property
    def has_failed(self) -> bool:
        """任一任务失败或存在游戏级错误（与 is_all_success 互补，作兜底判定）。"""
        return any(t.status == "failed" for t in self.tasks) or bool(self.errors)

    @property
    def progress_percent(self) -> int:
        """完成度百分比。

        口径优先级：
          1. 关键任务的真实计数（如鸣潮"今日活跃" 90/100 → 90）
          2. 任务条数比例（此时 **failed 不算完成**）
        """
        real = self.real_progress_percent()
        if real is not None:
            return real
        if not self.tasks:
            return 0
        done = sum(1 for t in self.tasks if t.status in ("success", "already"))
        return int(done / len(self.tasks) * 100)

    def completion_state(self) -> str:
        """判定该游戏当天是否算"每日完成"。

        返回：
          "completed" — 明确完成
          "unknown"   — 关键计数未刷新（如库街区活跃度需进游戏才更新，cur=0），无法判定
          "failed"    — 有失败项
          "pending"   — 未完成但也没失败

        为什么不能直接用 is_all_success：
          鸣潮/暖暖这类游戏的任务列表里混有资源与周本（结晶波片 132/240、
          千道门扉 0/6000），这些本来就不要求满，用"全部任务完成"判定会导致
          完成率永远为 0。必须以关键任务（活跃度）为准。
        """
        # 脚本型关键任务（异环"日常领取"）：按状态直接判定，优先级最高
        status_names = KEY_STATUS_TASKS.get(self.game_key) or ()
        for want in status_names:
            for t in self.tasks:
                if (t.task_name or t.task_key) != want:
                    continue
                if t.status in ("success", "already"):
                    return "completed"
                if t.status == "failed":
                    return "failed"
                break
        real = self.real_progress_percent()
        if real is not None:
            return "completed" if real >= 100 else "pending"
        # 有关键任务名但拿不到有效计数（cur=0 / 缺字段）→ 官方未刷新
        if KEY_PROGRESS_TASKS.get(self.game_key) and any(
            (t.task_name or t.task_key) in KEY_PROGRESS_TASKS[self.game_key]
            for t in self.tasks
        ):
            return "unknown"
        if self.is_all_success:
            return "completed"
        if self.has_failed:
            return "failed"
        return "pending"

    def real_progress_percent(self) -> Optional[int]:
        """取关键任务的真实游玩进度百分比；无法判定返回 None。

        官方 API 未刷新时（如库街区活跃度需进游戏才更新，cur 为 0）返回 None，
        交由调用方降级，避免把"未刷新"显示成 0%。
        """
        names = KEY_PROGRESS_TASKS.get(self.game_key)
        if not names:
            return None
        for want in names:
            for t in self.tasks:
                if (t.task_name or t.task_key) != want:
                    continue
                extra = t.extra if isinstance(t.extra, dict) else {}
                try:
                    cur_i = int(extra.get("current"))
                    total_i = int(extra.get("total"))
                except (TypeError, ValueError):
                    continue
                if total_i <= 0 or cur_i <= 0:
                    continue
                return min(int(cur_i / total_i * 100), 100)
        return None


@dataclass
class DailyProgressSnapshot:
    """今日进度快照。"""
    date_key: str
    games: List[GameDailyRecord] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    @property
    def all_completed(self) -> bool:
        """快照粗口径"全部完成"（基于 is_all_success，展示用；严谨判定走逐游戏 completion_state）。"""
        if not self.games:
            return False
        return all(g.is_all_success for g in self.games)

    @property
    def completed_games(self) -> List[GameDailyRecord]:
        """全部成功（含 already）的游戏列表。"""
        return [g for g in self.games if g.is_all_success]

    @property
    def failed_games(self) -> List[GameDailyRecord]:
        """有失败项或有游戏级错误的游戏列表。"""
        return [g for g in self.games if g.has_failed]


class DailyProgressTracker:
    """每日进度追踪器，基于 SQLite 持久化。

    轻量无状态包装：所有方法即开即用，连接由 WorkflowDB._connect() 的
    contextmanager 管理（自动提交+关闭，禁止裸持有 Connection——历史泄漏 bug）。
    实例可多线程共用（WAL 读者不阻塞），但同一 (date_key, game_key, task_key)
    的写入是"后写覆盖先写"，调用方需自行保证顺序（如复核兜底后再查API）。
    """

    def __init__(self, db: WorkflowDB):
        self.db = db
        self._ensure_table()

    def _ensure_table(self):
        """建表（幂等，IF NOT EXISTS）。老库自动迁移，无版本号字段。

        注意 UNIQUE(date_key, game_key, task_key) 是整个 UPSERT 语义的根：
        record_script_run 的 task_key 形如 script_BetterGI、签到为 sign_xxx，
        同一天重复执行会被覆盖成最新状态——"按最终结果落库"依赖这条约束。
        """
        with self.db._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_progress (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date_key TEXT NOT NULL,
                    game_key TEXT NOT NULL,
                    game_name TEXT NOT NULL DEFAULT '',
                    uid TEXT NOT NULL DEFAULT '',
                    nickname TEXT NOT NULL DEFAULT '',
                    task_key TEXT NOT NULL,
                    task_name TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT 'community',
                    status TEXT NOT NULL DEFAULT 'pending',
                    message TEXT NOT NULL DEFAULT '',
                    extra_json TEXT NOT NULL DEFAULT '{}',
                    updated_at REAL NOT NULL DEFAULT 0,
                    UNIQUE(date_key, game_key, task_key)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_daily_progress_date ON daily_progress(date_key)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_daily_progress_game ON daily_progress(date_key, game_key)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_progress_errors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date_key TEXT NOT NULL,
                    game_key TEXT NOT NULL,
                    error_message TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_daily_errors_date ON daily_progress_errors(date_key)"
            )

    def record_task(
        self,
        game_key: str,
        game_name: str,
        task_key: str,
        task_name: str,
        status: str,
        source: str = "community",
        message: str = "",
        uid: str = "",
        nickname: str = "",
        extra: Optional[Dict[str, Any]] = None,
    ):
        """记录单个任务状态（UPSERT，后写覆盖先写）。

        同一天同一 (game_key, task_key) 重复写入会整体覆盖旧行（含 status/message/
        extra）——"按最终结果落库"依赖此语义：脚本首轮失败、统一重试成功后，
        最终库里是 success。task_key 命名约定：script_<脚本名> / sign_<类型> /
        其余为数据源自定义（如 活跃度/朝夕心愿）。
        """
        date_key = _today_key()
        now = time.time()
        extra_json = json.dumps(extra or {}, ensure_ascii=False)
        with self.db._connect() as conn:
            conn.execute(
                """
                INSERT INTO daily_progress(
                    date_key, game_key, game_name, uid, nickname,
                    task_key, task_name, source, status, message, extra_json, updated_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(date_key, game_key, task_key) DO UPDATE SET
                    game_name = excluded.game_name,
                    uid = excluded.uid,
                    nickname = excluded.nickname,
                    task_name = excluded.task_name,
                    source = excluded.source,
                    status = excluded.status,
                    message = excluded.message,
                    extra_json = excluded.extra_json,
                    updated_at = excluded.updated_at
                """,
                (
                    date_key, game_key, game_name, uid, nickname,
                    task_key, task_name, source, status, message, extra_json, now,
                ),
            )

    def record_error(self, game_key: str, error_message: str):
        """记录游戏级别的错误。"""
        date_key = _today_key()
        with self.db._connect() as conn:
            conn.execute(
                """
                INSERT INTO daily_progress_errors(date_key, game_key, error_message, created_at)
                VALUES(?, ?, ?, ?)
                """,
                (date_key, game_key, error_message, time.time()),
            )

    def record_sign_result(
        self,
        game_key: str,
        game_name: str,
        sign_type: str,
        status: str,
        message: str = "",
        uid: str = "",
    ):
        """记录签到结果（source='script'）。"""
        self.record_task(
            game_key=game_key,
            game_name=game_name,
            task_key=f"sign_{sign_type}",
            task_name=f"{sign_type}签到",
            status=status,
            source="script",
            message=message,
            uid=uid,
        )

    def record_script_run(
        self,
        game_key: str,
        game_name: str,
        script_name: str,
        status: str,
        message: str = "",
    ):
        """记录自动化脚本执行结果。"""
        self.record_task(
            game_key=game_key,
            game_name=game_name,
            task_key=f"script_{script_name}",
            task_name=script_name,
            status=status,
            source="script",
            message=message,
        )

    def load_today(self) -> DailyProgressSnapshot:
        """加载今日进度。"""
        return self.load_date(_today_key())

    def load_date(self, date_key: str) -> DailyProgressSnapshot:
        """加载指定日期的进度（历史回看）。

        date_key 形如 "2026-09-27"。该日无记录时返回空 games 的快照。
        """
        games: Dict[str, GameDailyRecord] = {}

        with self.db._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM daily_progress
                WHERE date_key = ?
                ORDER BY game_key, task_key
                """,
                (date_key,),
            ).fetchall()

            for row in rows:
                gk = row["game_key"]
                if gk not in games:
                    games[gk] = GameDailyRecord(
                        game_key=gk,
                        game_name=row["game_name"] or gk,
                        uid=row["uid"] or "",
                        nickname=row["nickname"] or "",
                        checked_at=row["updated_at"] or 0,
                    )
                games[gk].tasks.append(DailyTaskRecord(
                    task_key=row["task_key"],
                    task_name=row["task_name"] or row["task_key"],
                    source=row["source"] or "community",
                    status=row["status"] or "pending",
                    message=row["message"] or "",
                    updated_at=row["updated_at"] or 0,
                    extra=json.loads(row["extra_json"] or "{}"),
                ))
                if row["updated_at"] and row["updated_at"] > games[gk].checked_at:
                    games[gk].checked_at = row["updated_at"]

            error_rows = conn.execute(
                "SELECT * FROM daily_progress_errors WHERE date_key = ?",
                (date_key,),
            ).fetchall()
            for row in error_rows:
                gk = row["game_key"]
                if gk not in games:
                    games[gk] = GameDailyRecord(
                        game_key=gk,
                        game_name=gk,
                        checked_at=row["created_at"] or 0,
                    )
                games[gk].errors.append(row["error_message"])

        return DailyProgressSnapshot(
            date_key=date_key,
            games=list(games.values()),
            created_at=time.time(),
        )

    def list_available_dates(self, limit: int = 60) -> List[str]:
        """列出有记录的日期，倒序（最新在前）。"""
        with self.db._connect() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT date_key FROM daily_progress
                ORDER BY date_key DESC LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        return [r["date_key"] for r in rows]

    def stats_recent_days(self, days: int = 30) -> List[Dict[str, Any]]:
        """统计最近 N 天各游戏的完成情况。

        返回按完成率降序的列表，每项：
          game_key / game_name / days_recorded / days_completed /
          completion_rate(0-100) / last_percent / last_date

        口径：某天"完成" = 该游戏当日 GameDailyRecord.completion_state()
        （关键指标口径，禁止 is_all_success——鸣潮/暖暖任务列表混有
        资源与周本，全部 success 会把完成率压到失真）。
        只统计有记录的日期，未记录（没跑/没查）的天数不计入分母。
        """
        date_keys = self.list_available_dates(limit=int(days))
        if not date_keys:
            return []

        # game_key 别名（大驼峰 app_key 与小写 game_key）需归一，
        # 否则同一款游戏会被拆成两行统计
        try:
            from ..settings import resolve_game_key
        except Exception:  # pragma: no cover - 设置模块异常时退化为原样
            def resolve_game_key(k: str) -> str:
                # app_key -> 小写 game_key 别名归一（这里仅兜底，正常走 settings 版）
                return k

        agg: Dict[str, Dict[str, Any]] = {}
        for dk in date_keys:
            snap = self.load_date(dk)
            for game in snap.games:
                gk = resolve_game_key(game.game_key)
                slot = agg.setdefault(gk, {
                    "game_key": gk,
                    "game_name": game.game_name or gk,
                    "days_recorded": 0,
                    "days_completed": 0,
                    "days_unknown": 0,
                    "days_failed": 0,
                    "last_percent": 0,
                    "last_date": "",
                    "last_state": "",
                })
                slot["days_recorded"] += 1
                state = game.completion_state()
                if state == "completed":
                    slot["days_completed"] += 1
                elif state == "unknown":
                    slot["days_unknown"] += 1
                elif state == "failed":
                    slot["days_failed"] += 1
                # list_available_dates 是倒序，第一次遇到即为最近一天
                if not slot["last_date"]:
                    slot["last_date"] = dk
                    slot["last_percent"] = game.progress_percent
                    slot["last_state"] = state
                # 展示名取最近一次见到的中文名
                if game.game_name:
                    slot["game_name"] = game.game_name

        result = list(agg.values())
        for item in result:
            # 分母排除"数据未刷新"的天数：那些天并非真的没做，
            # 只是官方 API 没给数，计入分母会把完成率压到失真
            effective = item["days_recorded"] - item["days_unknown"]
            item["completion_rate"] = (
                int(item["days_completed"] / effective * 100) if effective > 0 else 0
            )
        result.sort(key=lambda x: (-x["completion_rate"], -x["days_recorded"]))
        return result

    def clear_old_records(self, keep_days: int = 7):
        """清理旧记录，默认保留 7 天。"""
        cutoff = time.time() - keep_days * 86400
        cutoff_key = _day_key(cutoff)
        with self.db._connect() as conn:
            conn.execute(
                "DELETE FROM daily_progress WHERE date_key < ?",
                (cutoff_key,),
            )
            conn.execute(
                "DELETE FROM daily_progress_errors WHERE date_key < ?",
                (cutoff_key,),
            )
            LOGGER.info("[每日进度] 已清理 %s 之前的旧记录", cutoff_key)


def get_tracker(db_path: str) -> DailyProgressTracker:
    """获取追踪器实例（每次新建，不缓存）。

    调用方遍布 bridge/task_batch_runner/daily_checker——每实例化一次就开一个
    WorkflowDB 连接。SQLite 句柄在 _connect 的 finally 里关闭，不留泄漏；
    WAL 模式下多实例并发读写安全，无需全局单例。
    """
    db = WorkflowDB(db_path)
    return DailyProgressTracker(db)
