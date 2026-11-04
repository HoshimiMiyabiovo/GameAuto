"""2026-11-04 批次中断与崩铁误杀回归。

两件事：
  1. 09:20 薅羊毛步骤 `wool_dismiss_update` 把 bool 当 (bool, str) 解包 →
     TypeError → 整批中断，异环等后面的任务全部没跑。
  2. 07:45 崩铁 March7th 的 "Failed to set window foreground"（激活窗口失败）
     命中 "发生错误" 关键字，日志监控直接把任务杀掉（35 秒判失败）。

跑法：.venv\\Scripts\\python.exe tests\\suite_script_monitor_20261104.py
"""

import io
import logging
import sys
import tempfile
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def _logger():
    logger = logging.getLogger("test_script_monitor")
    logger.setLevel(logging.CRITICAL)
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


def test_minimax_dismiss_no_unpack():
    print("[1] wool_dismiss_update 不再按 tuple 解包")
    src = (PROJECT_ROOT / "src/game_auto/runtime/foreground_flow_runner.py").read_text(
        encoding="utf-8"
    )
    check("没有 `dismissed, dismiss_msg = dismiss_minimax_update_popup`",
          "dismissed, dismiss_msg = dismiss_minimax_update_popup" not in src)
    check("改成单值接收", "dismissed = dismiss_minimax_update_popup(logger, hwnd)" in src)

    from game_auto.runtime.wool_claim_logs import dismiss_minimax_update_popup

    check("函数本身返回 bool（无 hwnd 时 False）",
          dismiss_minimax_update_popup(_logger(), None) is False)


def test_ignore_keywords_skip_transient_error():
    print("[2] 崩铁瞬时错误被忽略、真错误仍然止损")
    from game_auto.runtime.script_error_log_monitor import ScriptErrorLogWatcher

    with tempfile.TemporaryDirectory() as tmp:
        log_path = Path(tmp) / f"app-{datetime.now():%Y-%m-%d}.log"
        log_path.write_text("旧日志：发生错误（启动前，不能被读到）\n", encoding="utf-8")
        watcher = ScriptErrorLogWatcher(
            _logger(),
            "starrail_daily_log_monitor",
            tmp,
            error_keywords=("发生错误",),
            ignore_keywords=("Failed to set window foreground", "激活窗口时发生错误"),
        )
        with log_path.open("a", encoding="utf-8") as fp:
            fp.write(
                "2026-11-04 07:44:54,865 | ERROR | 激活窗口时发生错误："
                "Failed to set window foreground\n"
            )
        check("激活窗口失败不当致命错误", watcher.poll_new_errors() is None)

        with log_path.open("a", encoding="utf-8") as fp:
            fp.write("2026-11-04 07:46:00,000 | ERROR | 发生错误 任务执行失败\n")
        line = watcher.poll_new_errors()
        check("真正的执行错误仍然返回", bool(line and "任务执行失败" in line), line)


def test_honkai_profile_config():
    print("[3] 崩铁 profile 带 ignore_keywords")
    from game_auto.games.honkai_sr import PROFILE

    cfg = (PROFILE.default_app or {}).get("starrail_daily_log_monitor") or {}
    ignored = cfg.get("ignore_keywords") or []
    check("配置了忽略词", bool(ignored), cfg)
    check("包含 Failed to set window foreground",
          any("Failed to set window foreground" in k for k in ignored), ignored)


def main():
    test_minimax_dismiss_no_unpack()
    test_ignore_keywords_skip_transient_error()
    test_honkai_profile_config()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())