"""崩铁（March7th）活跃度数值接入判定回归测试（2026-10-24）。

用户拍板：解析 March7th 日志的「当前累计分数：X/500」为活跃度计数任务，
复核走真实进度口径；超时但分数已达标 → 改判成功、不触发统一重试。

断言：
  1. 解析器：分数行 → 活跃度任务（current/total），多次出现取最后一次
  2. 分数未达标 → pending；无分数行但有「每日实训已完成」→ 500/500 兜底
  3. 两者皆无 → 不产出活跃度任务（旧行为）
  4. 既有布尔标记任务不受影响，错误/overall 口径不变
  5. completion_state：活跃度 600/500 → completed；300/500 → pending
  6. 事后复核：有计数时关键任务说了算；无计数时保持旧的 has_failed 改判
  7. 超时兜底：分数达标 + 失败 → 成功；未达标/无数据/停止/成功 → 原判
  8. 调用点接线：仅 script=March7th 时启用

跑法：.venv\\Scripts\\python.exe artifacts\\test_starrail_activity_20261024.py
"""

import logging
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, r"E:\GithubProject")

from game_auto.runtime import task_batch_runner as tbr  # noqa: E402
from game_auto.services import starrail_daily_parser as sp  # noqa: E402
from game_auto.services.daily_progress_tracker import (  # noqa: E402
    DailyTaskRecord, GameDailyRecord,
)

FAILURES = []
LOGGER = logging.getLogger("hsr_test")


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}", flush=True)
    else:
        print(f"  FAIL {name} {detail}", flush=True)
        FAILURES.append(name)


def _write_log(lines):
    import os
    fd, path = tempfile.mkstemp(suffix=".log")
    os.close(fd)
    f = Path(path)
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return f


def _line(body, ts="2026-10-24 09:04:06,105", level="INFO"):
    return f"{ts[:10]} {ts[11:]} {level} | {body}" if False else f"2026-10-24 {ts.split(' ')[1] if ' ' in ts else ts} | {level} | {body}"


def _activity(data):
    for t in data.get("tasks") or []:
        if t.get("task_name") == "活跃度":
            return t
    return None


def test_parse_score():
    print("[1] 分数行解析")
    f = _write_log([
        _line("INFO | 副本任务完成"),
        _line("INFO | 每日实训已完成"),
        _line("INFO | 邮件奖励已领取"),
    ])
    d = sp.parse_starrail_log(f)
    a = _activity(d)
    check("无分数行但有每日实训已完成 → 500/500 兜底",
          a and a["extra"] == {"current": 500, "total": 500}, a)
    check("兜底任务状态 success", a and a["status"] == "success", a)
    f.unlink()

    f = _write_log([
        _line("INFO | 当前累计分数：300/500"),
        _line("INFO | 副本任务完成"),
        _line("INFO | 当前累计分数：600/500"),
        _line("INFO | 实训分数已达标"),
    ])
    d = sp.parse_starrail_log(f)
    a = _activity(d)
    check("多次分数取最后一次(600/500)", a and a["extra"] == {"current": 600, "total": 500}, a)
    check("达标任务状态 success", a and a["status"] == "success", a)
    check("布尔标记任务仍在(副本战斗)", any(t["task_name"] == "副本战斗（开拓力）" and t["status"] == "success" for t in d["tasks"]))
    f.unlink()

    f = _write_log([
        _line("INFO | 副本任务完成"),
        _line("INFO | 当前累计分数：300/500"),
    ])
    d = sp.parse_starrail_log(f)
    a = _activity(d)
    check("未达标 → pending", a and a["status"] == "pending" and a["extra"]["current"] == 300, a)
    check("未达标不计入 errors/overall",
          d["is_all_success"] is False and all("活跃度" not in e for e in d["errors"]), d["errors"])
    f.unlink()

    f = _write_log([_line("INFO | 副本任务完成")])
    d = sp.parse_starrail_log(f)
    check("无分数无实训完成 → 无活跃度任务", _activity(d) is None)
    f.unlink()


def test_completion_state():
    print("[2] completion_state 走真实进度")
    def mk(cur, total, extra_tasks=()):
        tasks = [DailyTaskRecord(task_key="daily_activity", task_name="活跃度",
                                 source="script", status="pending",
                                 extra={"current": cur, "total": total})]
        tasks.extend(extra_tasks)
        return GameDailyRecord(game_key="honkai_sr", game_name="崩坏：星穹铁道", tasks=tasks)

    check("600/500 → completed", mk(600, 500).completion_state() == "completed")
    check("300/500 → pending", mk(300, 500).completion_state() == "pending")
    failed_mail = DailyTaskRecord(task_key="邮件", task_name="邮件", source="script", status="failed")
    check("600/500+邮件失败 仍 completed", mk(600, 500, (failed_mail,)).completion_state() == "completed")


def _fake_data(activity, mail_failed=False):
    tasks = [
        {"task_key": "副本战斗", "task_name": "副本战斗（开拓力）", "status": "success", "message": "副本任务完成"},
        {"task_key": "每日实训", "task_name": "每日实训", "status": "success", "message": "每日实训已完成"},
        {"task_key": "邮件", "task_name": "邮件", "status": "failed" if mail_failed else "success",
         "message": "邮件奖励已领取"},
    ]
    if activity is not None:
        tasks.append({"task_key": "daily_activity", "task_name": "活跃度", "status": "pending",
                      "message": f"{activity[0]}/{activity[1]}",
                      "extra": {"current": activity[0], "total": activity[1]}})
    return {"game_key": "honkai_sr", "game_name": "崩坏：星穹铁道",
            "is_all_success": not mail_failed, "has_failed": mail_failed,
            "progress_percent": 66, "tasks": tasks, "errors": [], "report_time": ""}


def test_recheck():
    print("[3] 事后日志复核口径")
    with patch.object(sp, "parse_latest_starrail_log", lambda: _fake_data((600, 500), mail_failed=True)):
        r = tbr._verify_script_log_result("HonkaiStarRail", "March7th", "崩坏：星穹铁道", LOGGER)
    check("活跃度达标+邮件失败 → 不改判", r is None, r)

    with patch.object(sp, "parse_latest_starrail_log", lambda: _fake_data((300, 500))):
        r = tbr._verify_script_log_result("HonkaiStarRail", "March7th", "崩坏：星穹铁道", LOGGER)
    check("活跃度300 → pending 不改判", r is None, r)

    with patch.object(sp, "parse_latest_starrail_log", lambda: _fake_data(None, mail_failed=True)):
        r = tbr._verify_script_log_result("HonkaiStarRail", "March7th", "崩坏：星穹铁道", LOGGER)
    # 既有行为：「每日实训」布尔任务名命中 KEY_PROGRESS_TASKS → completion_state=unknown
    # → 复核从不改判（改判失败侧由 _apply_starrail_score_verdict 的超时兜底覆盖，见 [4]）
    check("无计数 → unknown 不改判（旧行为一致）", r is None, r)

    with patch.object(sp, "parse_latest_starrail_log", lambda: _fake_data((600, 500))):
        r = tbr._verify_script_log_result("HonkaiStarRail", "March7th", "崩坏：星穹铁道", LOGGER)
    check("达标+全部成功 → None", r is None, r)

    with patch.object(sp, "parse_latest_starrail_log", lambda: None):
        r = tbr._verify_script_log_result("HonkaiStarRail", "March7th", "崩坏：星穹铁道", LOGGER)
    check("当天无日志 → None", r is None, r)


def test_timeout_verdict():
    print("[4] 超时兜底判定")
    with patch.object(sp, "parse_latest_starrail_log", lambda: _fake_data((600, 500))):
        s, m = tbr._apply_starrail_score_verdict(False, "超时", LOGGER, False)
    check("超时+达标 → 成功", s is True and "实训分数" in m, (s, m))

    with patch.object(sp, "parse_latest_starrail_log", lambda: _fake_data((300, 500))):
        s, m = tbr._apply_starrail_score_verdict(False, "超时", LOGGER, False)
    check("超时+未达标 → 原判", (s, m) == (False, "超时"), (s, m))

    with patch.object(sp, "parse_latest_starrail_log", lambda: None):
        s, m = tbr._apply_starrail_score_verdict(False, "超时", LOGGER, False)
    check("无数据 → 原判", (s, m) == (False, "超时"), (s, m))

    s, m = tbr._apply_starrail_score_verdict(False, "超时", LOGGER, True)
    check("用户停止 → 原判", (s, m) == (False, "超时"), (s, m))

    s, m = tbr._apply_starrail_score_verdict(True, "成功", LOGGER, False)
    check("本就成功 → 原判", (s, m) == (True, "成功"), (s, m))


def test_wiring():
    print("[5] 调用点接线")
    import inspect
    src = inspect.getsource(tbr._run_single_task)
    check("March7th 分支调用兜底判定",
          "app_key == \"HonkaiStarRail\"" in src and "_apply_starrail_score_verdict" in src)
    check("限制 script=March7th", '(effective_script_choice or "") == "March7th"' in src)


def main():
    test_parse_score()
    test_completion_state()
    test_recheck()
    test_timeout_verdict()
    test_wiring()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
