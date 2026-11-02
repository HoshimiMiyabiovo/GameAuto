from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import threading
from pathlib import Path
from typing import Iterable, List, Optional


@dataclass
class BetterGIDailyRewardSummary:
    log_found: bool
    source_files: List[str]
    commission_completed: bool
    expedition_completed: bool
    commission_incomplete_seen: bool
    expedition_incomplete_seen: bool
    last_commission_line: str = ""
    last_expedition_line: str = ""
    # BetterGI 自身报错（[ERR] 行）计数 + 样本：2026-10-26 用户指出"今天是 BetterGI
    # 自己出问题"，日志里确实有 40+ 条 ERR（未识别到地脉花/未能返回主界面/打开大地图
    # 失败/传送失败）。不带进判定文案，失败原因就只能被读成"委托没做"。
    error_count: int = 0
    error_samples: List[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.error_samples is None:
            self.error_samples = []

    @property
    def all_completed(self) -> bool:
        return self.commission_completed and self.expedition_completed

    def missing_items(self) -> List[str]:
        missing: List[str] = []
        if not self.commission_completed:
            missing.append("委托奖励")
        if not self.expedition_completed:
            missing.append("探索派遣")
        return missing

    def failure_message(self) -> str:
        """失败原因要能直接看出"是谁没做"，不能只说"未确认"。

        2026-10-26 实例：用户当天没做每日委托，BetterGI 打出
        `▶ "领取『每日委托』奖励" 未完成或者已领取` 直接跳过领取，我们的旧文案
        只写"未确认今日委托奖励完成"，看起来像监控误报，实际是委托根本没做完
        （这套一条龙配置里只有"领取委托奖励"，没有"做每日委托"的任务）。
        """
        if not self.log_found:
            return "BetterGI 日志缺失，无法确认今日委托奖励与探索派遣状态"
        missing = self.missing_items()
        if not missing:
            return "成功"
        reasons: List[str] = []
        for item in missing:
            if item == "委托奖励" and self.commission_incomplete_seen:
                reasons.append(
                    "委托奖励：BetterGI 判定「未完成」并跳过领取"
                    "（一条龙 6 个任务里只有「领取每日奖励」，没有做每日委托的任务）"
                )
            elif item == "探索派遣" and self.expedition_incomplete_seen:
                reasons.append("探索派遣：BetterGI 判定「未探索完成或已领取」并跳过")
            else:
                reasons.append(f"{item}：日志里没有完成记录")
        msg = "BetterGI 日志未确认今日" + "、".join(missing) + "完成 —— " + "；".join(reasons)
        if self.error_count:
            samples = "；".join(self.error_samples[:3]) if self.error_samples else ""
            msg += f"。本次 BetterGI 自身报错 {self.error_count} 条"
            if samples:
                msg += f"（示例：{samples}）"
        return msg


class BetterGILogMonitor:
    def __init__(
        self,
        logger,
        log_dir: str,
        file_pattern: str,
        *,
        date_format: str = "%Y%m%d",
        poll_interval: float = 2.0,
        encoding: str = "utf-8",
    ):
        self.logger = logger
        self.log_dir = Path(log_dir)
        self.file_pattern = file_pattern
        self.date_format = date_format or "%Y%m%d"
        self.poll_interval = max(0.5, float(poll_interval))
        self.encoding = encoding or "utf-8"

        self.stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._offsets: dict[str, int] = {}

        self.log_found = False
        self.source_files: List[str] = []
        self.commission_completed = False
        self.expedition_completed = False
        self.commission_incomplete_seen = False
        self.expedition_incomplete_seen = False
        self.last_commission_line = ""
        self.last_expedition_line = ""
        self.error_count = 0
        self.error_samples: List[str] = []
        self._pending_error = False

    def start(self) -> "BetterGILogMonitor":
        if self._thread and self._thread.is_alive():
            return self
        self._initialize_from_existing_logs()
        self.logger.info(
            f"[GI监控] 开始监控 BetterGI 每日奖励日志: {self.log_dir}\\{self.file_pattern}"
        )
        self._thread = threading.Thread(target=self._watch_loop, name="BetterGILogMonitor", daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout: float = 2.0):
        self.stop_event.set()
        if self._thread and self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout=timeout)

    def build_summary(self) -> BetterGIDailyRewardSummary:
        return BetterGIDailyRewardSummary(
            log_found=self.log_found,
            source_files=list(self.source_files),
            commission_completed=self.commission_completed,
            expedition_completed=self.expedition_completed,
            commission_incomplete_seen=self.commission_incomplete_seen,
            expedition_incomplete_seen=self.expedition_incomplete_seen,
            last_commission_line=self.last_commission_line,
            last_expedition_line=self.last_expedition_line,
            error_count=self.error_count,
            error_samples=list(self.error_samples),
        )

    def _today_token(self) -> str:
        return datetime.now().strftime(self.date_format)

    def _matching_files(self) -> List[Path]:
        if not self.log_dir.exists():
            return []
        all_files = [path for path in self.log_dir.glob(self.file_pattern) if path.is_file()]
        if not all_files:
            return []
        today_token = self._today_token()
        todays = [path for path in all_files if today_token in path.name]
        return sorted(todays, key=lambda item: (item.stat().st_mtime, item.name))

    def _initialize_from_existing_logs(self):
        files = self._matching_files()
        self.source_files = [str(path) for path in files]
        self.log_found = bool(files)
        for path in files:
            self._parse_file_from_offset(path, 0)
            try:
                self._offsets[str(path)] = path.stat().st_size
            except OSError:
                continue

    def _process_line(self, line: str):
        if not line:
            return

        # BetterGI 自身报错：日志格式是「[HH:mm:ss.f] [ERR] [...] 类名」+ 下一行正文，
        # 计数 + 留 3 条样本，失败文案里带上，避免把"BetterGI 跑坏了"说成"委托没做"。
        if "[ERR]" in line:
            self.error_count += 1
            self._pending_error = True
            return
        if self._pending_error:
            self._pending_error = False
            if line.startswith("["):
                return  # 又是一行日志头，说明这条错误没有独立正文行
            if len(self.error_samples) < 3:
                self.error_samples.append(line[:80])
            return

        if "▶" in line and "每日委托" in line and ("未完成" in line or "已领取" in line):
            self.commission_incomplete_seen = True
            self.last_commission_line = line
            return

        if "▶" in line and "每日委托" in line and "！" in line:
            self.commission_completed = True
            self.last_commission_line = line
            return

        if "▶" in line and "探索派遣" in line and ("未探索完成" in line or "已领取" in line):
            self.expedition_incomplete_seen = True
            self.last_expedition_line = line
            return

        if "探索派遣" in line and "完成" in line and "未探索完成" not in line and "再次派遣" not in line:
            self.expedition_completed = True
            self.last_expedition_line = line

    def _parse_file_from_offset(self, path: Path, offset: int):
        try:
            with open(path, "r", encoding=self.encoding, errors="ignore") as handle:
                handle.seek(offset)
                chunk = handle.read()
        except OSError as exc:
            self.logger.debug(f"[GI监控] 读取 BetterGI 日志失败 {path}: {str(exc)}")
            return

        if not chunk:
            return

        for raw_line in chunk.splitlines():
            self._process_line(raw_line.strip())

    def _watch_loop(self):
        missing_dir_warned = False
        while not self.stop_event.is_set():
            if not self.log_dir.exists():
                if not missing_dir_warned:
                    self.logger.warning(f"[GI监控] BetterGI 日志目录不存在，继续等待: {self.log_dir}")
                    missing_dir_warned = True
                self.stop_event.wait(self.poll_interval)
                continue

            files = self._matching_files()
            self.source_files = [str(path) for path in files]
            self.log_found = bool(files)

            for path in files:
                file_key = str(path)
                previous_offset = self._offsets.get(file_key, 0)
                try:
                    current_size = path.stat().st_size
                except OSError:
                    continue

                if current_size < previous_offset:
                    previous_offset = 0

                if current_size == previous_offset:
                    self._offsets[file_key] = previous_offset
                    continue

                self._parse_file_from_offset(path, previous_offset)
                self._offsets[file_key] = current_size

            self.stop_event.wait(self.poll_interval)


def start_bettergi_log_monitor(config: dict, logger) -> Optional[BetterGILogMonitor]:
    monitor_config = config.get("bettergi_log_monitor") or {}
    log_dir = str(monitor_config.get("log_dir", "") or "").strip()
    if not log_dir:
        logger.warning("[GI监控] 未配置 BetterGI 日志目录，跳过每日奖励监控")
        return None

    return BetterGILogMonitor(
        logger,
        log_dir=log_dir,
        file_pattern=str(monitor_config.get("file_pattern", "better-genshin-impact*.log") or "better-genshin-impact*.log"),
        date_format=str(monitor_config.get("date_format", "%Y%m%d") or "%Y%m%d"),
        poll_interval=float(monitor_config.get("poll_interval", 2.0) or 2.0),
        encoding=str(monitor_config.get("encoding", "utf-8") or "utf-8"),
    ).start()


def format_bettergi_daily_reward_summary(summary: Optional[BetterGIDailyRewardSummary]) -> List[str]:
    if summary is None:
        return []

    if not summary.log_found:
        return ["  BetterGI 每日奖励日志：未找到今日日志，无法确认委托奖励与探索派遣状态"]

    source_hint = "、".join(Path(path).name for path in summary.source_files) if summary.source_files else "无"
    if summary.all_completed:
        return [
            f"  BetterGI 每日奖励日志：今日委托奖励与探索派遣均已至少完成一次（来源：{source_hint}）",
        ]

    lines = [
        f"  BetterGI 每日奖励日志：来源 {source_hint}",
        f"  委托奖励：{'已完成' if summary.commission_completed else '未确认完成'}；探索派遣：{'已完成' if summary.expedition_completed else '未确认完成'}",
        f"  重点提示：今日仍缺少 {'、'.join(summary.missing_items())} 完成记录",
    ]
    if summary.commission_incomplete_seen and not summary.commission_completed:
        lines.append("  原因：BetterGI 判定「每日委托未完成」，直接跳过了领取（需要先做完 4 个委托）")
    if summary.expedition_incomplete_seen and not summary.expedition_completed:
        lines.append("  原因：BetterGI 判定「探索派遣未探索完成或已领取」，跳过了再次派遣")
    if summary.error_count:
        sample = "；".join(summary.error_samples[:3]) if summary.error_samples else "（无正文样本）"
        lines.append(f"  BetterGI 自身报错 {summary.error_count} 条，示例：{sample}")
    return lines