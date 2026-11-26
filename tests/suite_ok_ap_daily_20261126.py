"""蓝色星原：旅谣 ok-ap 接入回归（2026-11-26）。

只使用合成汇总与临时日志，不启动游戏、不运行 ok-ap、不访问网络。
跑法：PYTHONIOENCODING=utf-8 .venv\\Scripts\\python.exe tests/suite_ok_ap_daily_20261126.py
"""

from __future__ import annotations

import json
import logging
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sandbox_temp import install as install_sandbox_temp

install_sandbox_temp()
TEMP_ROOT = PROJECT_ROOT / "data" / "cache"
TEMP_ROOT.mkdir(parents=True, exist_ok=True)

from game_auto.games import get_profile
from game_auto.runtime.automation_controller import AutomationController
from game_auto.runtime.script_error_log_monitor import build_script_error_watchers
from game_auto.services import ok_ap_daily_parser as parser
from game_auto.services.script_version_check import detect_local_version


def _write_report(path: Path, *, status: str, success: str = "无", failed: str = "无",
                  skipped: str = "无", details: str = "", exception: str = "") -> None:
    lines = [
        f"一键日常执行情况汇总 - {datetime.now():%Y-%m-%d %H:%M:%S}",
        "=" * 50,
        f"执行状态: {status}",
        "",
    ]
    if exception:
        lines.extend(["异常信息:", f"  {exception}", ""])
    if details:
        lines.extend(["失败消息:", details, ""])
    lines.extend([
        "成功任务:", f"  {success}", "",
        "失败任务:", f"  {failed}", "",
        "跳过任务:", f"  {skipped}", "",
    ])
    path.write_text("\n".join(lines), encoding="utf-8")


def test_complete_report_and_latest_selection():
    with tempfile.TemporaryDirectory(dir=str(TEMP_ROOT)) as tmp:
        report_dir = Path(tmp) / "ok-ap" / "一键日常"
        report_dir.mkdir(parents=True)
        today = parser._today_key().replace("-", "")
        older = report_dir / f"一键日常_{today}_010000.txt"
        newer = report_dir / f"一键日常_{today}_020000.txt"
        _write_report(older, status="完成", success="家园每日")
        _write_report(newer, status="完成", success="家园每日, 每日收菜", skipped="委托每日")

        assert parser.find_today_ok_ap_report(tmp) == newer
        data = parser.parse_ok_ap_report(newer)
        assert data["is_all_success"] is True
        assert data["has_failed"] is False
        assert data["success_count"] == 2
        assert data["skipped_count"] == 1
        assert data["progress_percent"] == 66


def test_partial_failure_and_duplicate_task_precedence():
    with tempfile.TemporaryDirectory(dir=str(TEMP_ROOT)) as tmp:
        report = Path(tmp) / "report.txt"
        _write_report(
            report,
            status="部分失败",
            success="家园每日, 每日收菜",
            failed="每日收菜",
            skipped="委托每日",
            details="  - 每日收菜 : 领取失败\n  - 每日收菜 : 奖励已领取",
        )
        data = parser.parse_ok_ap_report(report)
        assert data["is_all_success"] is False
        assert data["has_failed"] is True
        assert data["failed_count"] == 1
        task = next(item for item in data["tasks"] if item["task_name"] == "每日收菜")
        assert task["status"] == "failed"
        assert task["message"] == "领取失败；奖励已领取"
        assert any("每日收菜" in error for error in data["errors"])


def test_exception_and_not_started_are_not_success():
    with tempfile.TemporaryDirectory(dir=str(TEMP_ROOT)) as tmp:
        report = Path(tmp) / "report.txt"
        _write_report(report, status="异常结束", success="家园每日", exception="Failed to ensure main.")
        data = parser.parse_ok_ap_report(report)
        assert data["is_all_success"] is False
        assert data["has_failed"] is True
        assert "Failed to ensure main." in data["errors"]

        _write_report(report, status="未开始")
        data = parser.parse_ok_ap_report(report)
        assert data["is_all_success"] is False
        assert data["has_failed"] is False
        assert data["errors"] == ["执行状态: 未开始"]


def test_profile_and_pyappify_version_source():
    profile = get_profile("AzurPromilia")
    assert profile is not None
    assert profile.daily_tool == "ok-ap"
    assert profile.daily_parser.endswith("ok_ap_daily_parser:parse_latest_ok_ap_report")
    assert profile.default_app["launcher_params"] == ["-t", "DailyTask", "-h", "-e"]
    assert profile.default_app["script_process_name"] == "ok-ap.exe"
    assert profile.default_app["cleanup_cmd_patterns"] == [
        {"name": "pythonw.exe", "cmdline": "ok-ap"}
    ]
    monitor = profile.default_app["ok_script_log_monitor"]
    assert monitor["abort_on_error"] is True
    assert monitor["require_date_token"] is False
    assert "以下任务未完成或失败" in monitor["error_markers"]
    assert "日常完成!" in monitor["completion_markers"]

    spec = profile.script_tools[0]
    assert spec.repo == "longer-sausage/ok-ap"
    assert spec.pyappify is True
    assert spec.tool_key == "ok-ap"
    assert spec.asset_patterns == ()

    with tempfile.TemporaryDirectory(dir=str(TEMP_ROOT)) as tmp:
        root = Path(tmp)
        exe = root / "ok-ap.exe"
        exe.write_bytes(b"")
        manifest = root / "data" / "apps" / "ok-ap" / "app.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"current_version": "v0.3.9"}), encoding="utf-8")
        assert detect_local_version(str(exe), spec) == "0.3.9"


def test_error_watcher_only_reads_new_lines():
    profile = get_profile("AzurPromilia")
    monitor = dict(profile.default_app["ok_script_log_monitor"])
    with tempfile.TemporaryDirectory(dir=str(TEMP_ROOT)) as tmp:
        log_path = Path(tmp) / "ok-script.log"
        log_path.write_text("旧日志：以下任务未完成或失败\n", encoding="utf-8")
        monitor["log_dir"] = tmp
        config = {
            "ok_script_log_monitor": monitor,
            "_discovery": {"monitors": {"ok_script_log_monitor": dict(monitor)}},
        }
        watchers = build_script_error_watchers(config, logging.getLogger("ok-ap-test"))
        assert len(watchers) == 1
        assert watchers[0].poll_new_errors() is None
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write("2026-11-26 20:00:00 | 以下任务未完成或失败: 每日收菜\n")
        error = watchers[0].poll_new_errors()
        assert error and "每日收菜" in error


def test_short_run_requires_fresh_success_report():
    class FakeProfile:
        daily_parser = "fake:parser"

        def latest_daily_log(self):
            return self.data

    controller = object.__new__(AutomationController)
    controller.logger = logging.getLogger("ok-ap-test")
    profile = FakeProfile()
    now = time.time()
    with patch("game_auto.games.get_profile", return_value=profile):
        profile.data = {
            "is_all_success": True,
            "report_time": datetime.fromtimestamp(now - 5).strftime("%Y-%m-%d %H:%M:%S"),
        }
        assert controller._script_daily_completed_since("AzurPromilia", now - 60) is True

        profile.data = {
            "is_all_success": True,
            "report_time": datetime.fromtimestamp(now - 120).strftime("%Y-%m-%d %H:%M:%S"),
        }
        assert controller._script_daily_completed_since("AzurPromilia", now - 60) is False

        profile.data = {"is_all_success": True, "report_time": ""}
        assert controller._script_daily_completed_since("AzurPromilia", now - 60) is False


def main():
    tests = [value for name, value in globals().items() if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"ALL PASS ({len(tests)} 组)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
