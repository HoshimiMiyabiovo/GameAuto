"""每日进度持久化追踪器。

每天 4:00 为分界点，记录：
- 社区便笺查询结果（米游社、库街区、Pearpal 的每日任务完成状态）
- 每日一条龙执行记录（启动前签到、自动化脚本执行结果）

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
    """单个任务的每日记录。"""
    task_key: str
    task_name: str
    source: str  # 'community' | 'script'
    status: str  # 'pending' | 'success' | 'failed' | 'skipped' | 'already'
    message: str = ""
    updated_at: float = field(default_factory=time.time)
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class GameDailyRecord:
    """单个游戏的每日记录汇总。"""
    game_key: str
    game_name: str
    uid: str = ""
    nickname: str = ""
    tasks: List[DailyTaskRecord] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    checked_at: float = 0.0

    @property
    def is_all_success(self) -> bool:
        if not self.tasks:
            return False
        return all(t.status in ("success", "already") for t in self.tasks)

    @property
    def has_failed(self) -> bool:
        return any(t.status == "failed" for t in self.tasks) or bool(self.errors)

    @property
    def progress_percent(self) -> int:
        if not self.tasks:
            return 0
        done = sum(1 for t in self.tasks if t.status in ("success", "already", "failed"))
        return int(done / len(self.tasks) * 100)


@dataclass
class DailyProgressSnapshot:
    """今日进度快照。"""
    date_key: str
    games: List[GameDailyRecord] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    @property
    def all_completed(self) -> bool:
        if not self.games:
            return False
        return all(g.is_all_success for g in self.games)

    @property
    def completed_games(self) -> List[GameDailyRecord]:
        return [g for g in self.games if g.is_all_success]

    @property
    def failed_games(self) -> List[GameDailyRecord]:
        return [g for g in self.games if g.has_failed]


class DailyProgressTracker:
    """每日进度追踪器，基于 SQLite 持久化。"""

    def __init__(self, db: WorkflowDB):
        self.db = db
        self._ensure_table()

    def _ensure_table(self):
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
        """记录单个任务状态。"""
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
        date_key = _today_key()
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
    """获取追踪器实例。"""
    db = WorkflowDB(db_path)
    return DailyProgressTracker(db)
