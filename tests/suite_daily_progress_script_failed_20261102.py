"""ok-ww 脚本失败时的日进度口径回归（2026-11-02）。

背景：2026-11-02 16:31 ok-ww 启动后 80 秒退出（窗口关闭），日志解析器把
**上一次 run** 的子任务开始行当成功，算出 100%；bridge 在 API 活跃度为 0/stale
时又回退到这个 100%，卡片显示「脚本 100% · 失败 2 项」，而本次脚本其实没跑完。
修复口径：
  1. 子任务只在 `DailyTask:Task completed` / already 时记成功，开始行只记 pending；
  2. DB 里 `script_ok-ww=failed` 时，bridge 用 `script_failed` 覆盖脚本百分比，
     子任务标「上次运行」，今日活跃说明改为「脚本未完成，活跃度未采集」；
  3. completion_state 记为 failed，不再因为看板 0/100 显示「未刷新」而掩盖失败。

跑法：.venv\\Scripts\\python.exe tests\\suite_daily_progress_script_failed_20261102.py
"""

import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import game_auto.services.bettergi_daily_parser as bettergi_parser  # noqa: E402
import game_auto.services.ok_ef_daily_parser as ok_ef_parser  # noqa: E402
import game_auto.services.ok_nte_daily_parser as ok_nte_parser  # noqa: E402
import game_auto.services.ok_ww_daily_parser as ok_ww_parser  # noqa: E402
import game_auto.services.starrail_daily_parser as starrail_parser  # noqa: E402
import game_auto.services.whimbox_daily_parser as whimbox_parser  # noqa: E402
import game_auto.services.zzz_daily_parser as zzz_parser  # noqa: E402
import game_auto.ui.bridge_backends.daily_progress as dp  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def _log(*bodies):
    lines = []
    for idx, body in enumerate(bodies):
        lines.append(f"2026-11-02 16:31:{idx:02d},000 INFO TaskExecutor {body}")
    return "\n".join(lines) + "\n"


def _parse(text):
    with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False, encoding="utf-8") as fh:
        fh.write(text)
        path = fh.name
    try:
        return ok_ww_parser.parse_ok_ww_log(path)
    finally:
        Path(path).unlink(missing_ok=True)


def test_parser():
    print("[1] ok-ww 日志解析：开始 ≠ 成功")
    data = _parse(_log(
        "DailyTask:open_daily",
        "DailyTask:Task completed",
        "DailyTask:claim daily",
    ))
    statuses = {t["task_key"]: t["status"] for t in data["tasks"]}
    check("open_daily 有完成行 → success", statuses.get("open_daily") == "success", statuses)
    check("claim daily 只有开始行 → pending（不能算成功）", statuses.get("claim daily") == "pending", statuses)
    check("未跑完时 progress_percent < 100", data["progress_percent"] < 100, data["progress_percent"])
    check("未跑完时 is_all_success=False", data["is_all_success"] is False, data["is_all_success"])

    # 一个完成行都没有时，才补「未检测到任务完成标志」
    data = _parse(_log("DailyTask:open_daily"))
    check("只有开始行时错误写明未检测到完成标志",
          any("未检测到任务完成标志" in e for e in data["errors"]), data["errors"])

    data = _parse(_log(
        "DailyTask:open_daily",
        "DailyTask:Task completed",
        "DailyTask:claim daily",
        "DailyTask:Task completed",
        "TaskExecutor:Successfully Executed Task, Exiting Game and App!",
    ))
    statuses = {t["task_key"]: t["status"] for t in data["tasks"]}
    check("全部完成 → 两个任务 success", statuses.get("open_daily") == "success"
          and statuses.get("claim daily") == "success", statuses)
    check("全部完成 → progress_percent=100", data["progress_percent"] == 100, data["progress_percent"])
    check("全部完成 → is_all_success=True", data["is_all_success"] is True, data["is_all_success"])


def test_bridge():
    print("[2] bridge：最终脚本失败覆盖上一次 run 的 100%")
    parser_data = {
        "game_key": "wuwa",
        "game_name": "鸣潮",
        "uid": "",
        "nickname": "ok-ww",
        "is_all_success": False,
        "has_failed": False,
        "progress_percent": 100,
        "tasks": [
            {"task_key": "open_daily", "task_name": "打开每日任务",
             "status": "success", "message": ""},
            {"task_key": "claim daily", "task_name": "领取每日奖励",
             "status": "success", "message": ""},
        ],
        "errors": ["未检测到任务完成标志"],
        "report_time": "2026-11-02 16:31:02",
    }
    games = [{
        "game_key": "wuwa",
        "game_name": "鸣潮",
        "uid": "105658482",
        "nickname": "飘飖",
        "is_all_success": False,
        "has_failed": True,
        "progress_percent": 100,
        "progress_source": "script",
        "completion_state": "unknown",
        "tasks": [
            {"task_key": "今日活跃", "task_name": "今日活跃", "status": "pending",
             "message": "0/100",
             "extra": {"current": 0, "total": 100, "stale": True,
                       "stale_reason": "库街区活跃度需进入游戏后才会刷新，当前为未刷新值"}},
            {"task_key": "script_ok-ww", "task_name": "ok-ww", "status": "failed",
             "message": "运行时间过短(小于5分钟)"},
        ],
    }]

    class FakeBridge:
        def _real_completion_percent(self, game_key, tasks):
            return None, None

    with patch.object(ok_ef_parser, "parse_latest_ok_ef_report", return_value=None), \
            patch.object(ok_nte_parser, "parse_latest_ok_nte_log", return_value=None), \
            patch.object(ok_ww_parser, "parse_latest_ok_ww_log", return_value=parser_data), \
            patch.object(whimbox_parser, "parse_latest_whimbox_log", return_value=None), \
            patch.object(bettergi_parser, "parse_latest_bettergi_log", return_value=None), \
            patch.object(zzz_parser, "parse_latest_zzz_log", return_value=None), \
            patch.object(starrail_parser, "parse_latest_starrail_log", return_value=None):
        dp._inject_external_log_progress(FakeBridge(), games)

    game = games[0]
    check("脚本失败 → progress_source=script_failed", game.get("progress_source") == "script_failed",
          game.get("progress_source"))
    check("脚本失败 → progress_percent=0（不显示上一次的 100%）",
          game.get("progress_percent") == 0, game.get("progress_percent"))
    check("脚本失败 → completion_state=failed（不是未刷新）",
          game.get("completion_state") == "failed", game.get("completion_state"))
    log_tasks = [t for t in game["tasks"] if t.get("task_key") == "open_daily"]
    check("旧 run 的子任务标「上次运行」",
          bool(log_tasks) and log_tasks[0].get("message") == "上次运行",
          log_tasks)
    active = next(t for t in game["tasks"] if t.get("task_key") == "今日活跃")
    check("今日活跃说明改成脚本未完成",
          active.get("extra", {}).get("stale_reason") == "脚本未完成，活跃度未采集",
          active.get("extra"))


def main():
    test_parser()
    test_bridge()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
