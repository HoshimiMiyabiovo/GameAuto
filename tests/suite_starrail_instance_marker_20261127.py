"""崩铁「副本战斗（开拓力）」标志行修正回归（2026-11-27）。

2026-11-27 排查实锤（用户报障"明明全都完成了为什么刷本失败"）：
March7thAssistant v2.2.0.3 的日志从不打「副本任务完成」（最近 8 天 0 次命中），
正常完成时打结算行「已结算 N 次」（11-21~11-26 每天都有），失败时打
「清体力未完成 {error}」（config.yaml 消息模板 InstanceNotCompleted）。
旧标志导致每天副本战斗被补判「未执行 → failed」，崩铁卡片单项红叉误导；
is_all_success=False 还让 automation_controller._script_daily_completed_since
的短时长兜底对崩铁永久失效。

断言：
  1. v2.2.0.3 真实日志形态（已结算 N 次）→ 副本战斗 success，is_all_success=True
  2. 旧标志「副本任务完成」仍 success（兼容旧版文案）
  3. 「清体力未完成 ...」→ failed + message 带原因，has_failed=True、is_all_success=False
  4. 完全无副本行 → 补「未执行」（原口径不变）
  5. failed 行在前、结算行在后 → 最终 success（行序覆盖、最后一次命中说了算）
  6. 结算行在前、failed 行在后 → 最终 failed
  7. 本机真实日志（11-25/11-26）副本战斗 success（日志文件存在才断言，CI 无此盘自动跳过）

跑法：.venv\\Scripts\\python.exe tests\\suite_starrail_instance_marker_20261127.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, r"E:\GithubProject\game_auto\src")

# 沙箱兼容：tempfile 目录创建改默认权限（0700 受保护 DACL 在沙箱里写不进）
from sandbox_temp import install as _install_sandbox_temp  # noqa: E402

_install_sandbox_temp()

from game_auto.services import starrail_daily_parser as sp  # noqa: E402

FAILURES = []


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


def _line(body, ts="2026-11-26 08:37:07,525", level="INFO"):
    # March7th 日志行格式（LOG_LINE_RE 要求毫秒）：YYYY-MM-DD HH:MM:SS,mmm | LEVEL | body
    return f"{ts} | {level} | {body}"


def _task(data, name):
    for t in data.get("tasks") or []:
        if t.get("task_name") == name:
            return t
    return None


# ── v2.2.0.3 真实日志形态（11-26 实拍节选） ──────────────────────
REAL_HSR_LINES = [
    _line("当前界面：活动", "2026-11-26 08:33:38,363"),
    _line("切换到：主界面", "2026-11-26 08:33:41,450"),
    _line("开拓力：251/300", "2026-11-26 08:33:45,353"),
    _line("已确认副本选中队伍 06", "2026-11-26 08:34:03,723"),
    _line("进入战斗", "2026-11-26 08:34:19,087"),
    _line("已结算 6 次", "2026-11-26 08:37:07,525"),
    _line("开拓力不足，结束本副本项", "2026-11-26 08:37:07,525"),
    _line("货币战争奖励已领取", "2026-11-26 08:37:35,854"),
    _line("已领取邮件奖励", "2026-11-26 08:37:51,527"),
    _line("邮件奖励已领取", "2026-11-26 08:37:51,569"),
    _line("每日实训活跃度 500/500", "2026-11-26 08:38:21,269"),
    _line("已领取实训档位奖励", "2026-11-26 08:38:23,028"),
    _line("每日实训已完成", "2026-11-26 08:38:23,922"),
]


def test_real_session_shape():
    print("[1] v2.2.0.3 真实日志形态")
    f = _write_log(REAL_HSR_LINES)
    d = sp.parse_starrail_log(f)
    ins = _task(d, "副本战斗（开拓力）")
    check("已结算行 → 副本战斗 success", ins and ins["status"] == "success", ins)
    check("message=结算标志", ins and ins["message"] == "已结算", ins)
    check("不再补「未执行」", d["errors"] == [], d["errors"])
    check("is_all_success=True", d["is_all_success"] is True, d)
    check("has_failed=False", d["has_failed"] is False, d)
    act = _task(d, "活跃度")
    check("活跃度 500/500 兜底仍在", act and act["extra"] == {"current": 500, "total": 500}, act)
    check("报告时间取最后一行", d["report_time"] == "2026-11-26 08:38:23", d["report_time"])
    f.unlink()

    # 结算行有多种次数格式（1 次 / 12 次），都应命中
    f = _write_log([
        _line("已结算 1 次", "2026-11-22 08:41:10,111"),
        _line("开拓力不足，结束本副本项", "2026-11-22 08:41:12,222"),
    ])
    d = sp.parse_starrail_log(f)
    ins = _task(d, "副本战斗（开拓力）")
    check("已结算 1 次 → success", ins and ins["status"] == "success", ins)
    f.unlink()


def test_legacy_marker():
    print("[2] 旧标志兼容")
    f = _write_log([_line("副本任务完成", "2026-10-22 09:04:06,105")])
    d = sp.parse_starrail_log(f)
    ins = _task(d, "副本战斗（开拓力）")
    check("「副本任务完成」仍 success", ins and ins["status"] == "success", ins)
    f.unlink()


def test_failed_marker():
    print("[3] 失败标志（清体力未完成）")
    f = _write_log([
        _line("开始清体力", "2026-11-27 08:30:00,000"),
        _line("进入战斗", "2026-11-27 08:31:00,000"),
        _line("清体力未完成 连接中断", "2026-11-27 08:32:00,000", "ERROR"),
        _line("每日实训已完成", "2026-11-27 08:40:00,000"),
    ])
    d = sp.parse_starrail_log(f)
    ins = _task(d, "副本战斗（开拓力）")
    check("清体力未完成 → failed", ins and ins["status"] == "failed", ins)
    check("message 带脚本原因", ins and "连接中断" in str(ins["message"]), ins)
    check("has_failed=True", d["has_failed"] is True, d)
    check("is_all_success=False", d["is_all_success"] is False, d)
    check("不重复补「未执行」", all("副本战斗" not in e for e in d["errors"]), d["errors"])
    f.unlink()


def test_missing_marker():
    print("[4] 无副本行补「未执行」")
    f = _write_log([
        _line("每日实训已完成", "2026-11-27 08:40:00,000"),
        _line("邮件奖励已领取", "2026-11-27 08:41:00,000"),
    ])
    d = sp.parse_starrail_log(f)
    ins = _task(d, "副本战斗（开拓力）")
    check("无任何标志 → 未执行/failed", ins and ins["status"] == "failed"
          and ins["message"] == "未执行", ins)
    check("errors 报副本战斗未执行", any("副本战斗" in e for e in d["errors"]), d["errors"])
    check("is_all_success=False", d["is_all_success"] is False, d)
    f.unlink()


def test_order_override():
    print("[5] 行序覆盖：最后一次命中说了算")
    f = _write_log([
        _line("清体力未完成 战斗超时", "2026-11-27 08:31:00,000"),
        _line("已结算 1 次", "2026-11-27 08:33:00,000"),
    ])
    d = sp.parse_starrail_log(f)
    ins = _task(d, "副本战斗（开拓力）")
    check("failed 在前 → 结算在后 → success", ins and ins["status"] == "success", ins)
    f.unlink()

    f = _write_log([
        _line("已结算 1 次", "2026-11-27 08:33:00,000"),
        _line("清体力未完成 战斗超时", "2026-11-27 08:34:00,000"),
    ])
    d = sp.parse_starrail_log(f)
    ins = _task(d, "副本战斗（开拓力）")
    check("结算在前 → failed 在后 → failed", ins and ins["status"] == "failed", ins)
    f.unlink()


def test_real_logs_on_disk():
    print("[6] 本机真实日志回放（无此盘则跳过）")
    log_dir = Path(r"D:\down\March7thAssistant_v2.2.0.3_full\logs")
    if not log_dir.is_dir():
        print("  SKIP 本机无 March7th 日志目录（CI 环境）")
        return
    for day in ("2026-11-25", "2026-11-26"):
        p = log_dir / f"{day}.log"
        if not p.is_file():
            continue
        d = sp.parse_starrail_log(p)
        ins = _task(d, "副本战斗（开拓力）")
        check(f"{day} 真实日志 → 副本战斗 success",
              ins and ins["status"] == "success", ins)
        check(f"{day} is_all_success=True", d and d["is_all_success"] is True,
              (d or {}).get("errors"))


def main():
    test_real_session_shape()
    test_legacy_marker()
    test_failed_marker()
    test_missing_marker()
    test_order_override()
    test_real_logs_on_disk()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
