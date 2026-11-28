"""2026-11-28：whimbox 异常结束（无汇总块）被记成「监控超时」回归。

背景：14:25 一条龙起跑，14:26~14:29 三次 `goto_page(page_bigmap)` 失败，
14:29:35 main.py 只打一行「一条龙任务完成: 前往页面 「地图」 失败」就退出——
没有「任务结果如下：」汇总块，也没有「✅ 一条龙结束」。旧 completion_markers
只认汇总行，监控永不触发：游戏不关、控制器空等 60 分钟监控超时，失败记录写成
「超时」（实际脚本 4 分钟就挂了）。本套件锁住通用前缀兜底 + 异常结束日志。
"""

import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

# 控制台默认 GBK，标记里有 ✅/「」 会 UnicodeEncodeError（本机实测）
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from game_auto.games.nikki import PROFILE  # noqa: E402
from game_auto.runtime.whimbox_log_monitor import FileCompletionMonitor  # noqa: E402
from game_auto.runtime import task_batch_runner as tbr  # noqa: E402

LOG = logging.getLogger("whimbox_failure_marker")
FAILS = []

FAIL_LINE = ("2026-11-28 14:29:35.529 | INFO     | __main__:run_one_dragon:80 - "
             "一条龙任务完成: 前往页面 「地图」 失败")
OK_LINE = ("2026-11-28 14:29:35.529 | INFO     | __main__:run_one_dragon:80 - "
           "一条龙任务完成: 任务结果如下：")
GOTO_FAIL_LINE = ("2026-11-28 14:26:55.198 | ERROR    | whimbox.ui.ui:goto_page:213 - "
                  "goto_page failed: 前往页面 「地图」 失败")


def check(name, cond, extra=""):
    if cond:
        print(f"  PASS {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL {name} {extra}")


def _monitor(markers, on_complete=None):
    return FileCompletionMonitor(
        LOG,
        log_dir=str(PROJECT_ROOT / ".temp" / "whimbox_marker_probe"),
        file_pattern="whimbox-*.log",
        completion_markers=markers,
        poll_interval=0.5,
        on_complete=on_complete,
    )


def test_profile_markers():
    print("[1] nikki 配置含通用前缀兜底")
    markers = PROFILE.default_app["whimbox_log_monitor"]["completion_markers"]
    check("含「一条龙任务完成: 」通用前缀", "一条龙任务完成: " in markers, markers)
    check("保留汇总行专用标记", "一条龙任务完成: 任务结果如下：" in markers, markers)
    check("保留「✅ 一条龙结束」", "✅ 一条龙结束" in markers, markers)


def test_failure_line_triggers():
    print("[2] 异常结束行能触发完成（旧标记不触发）")
    calls = []
    mon = _monitor(["✅ 一条龙结束", "一条龙任务完成: 任务结果如下："],
                   on_complete=lambda m: calls.append(m))
    mon._process_line(Path("whimbox-2026-11-28.log"), FAIL_LINE)
    check("旧标记：异常结束行不触发", not mon.completion_event.is_set())
    check("旧标记：回调未调用", not calls)

    mon2 = _monitor(["✅ 一条龙结束", "一条龙任务完成: "],
                    on_complete=lambda m: calls.append(m))
    mon2._process_line(Path("whimbox-2026-11-28.log"), FAIL_LINE)
    check("新标记：异常结束行触发", mon2.completion_event.is_set())
    check("新标记：回调调用一次", len(calls) == 1, calls)
    check("matched_line 记的是失败行", "失败" in mon2.matched_line, mon2.matched_line)


def test_success_line_still_triggers():
    print("[3] 正常汇总行不受影响")
    mon = _monitor(["✅ 一条龙结束", "一条龙任务完成: "])
    mon._process_line(Path("whimbox-2026-11-28.log"), OK_LINE)
    check("汇总行触发", mon.completion_event.is_set())
    check("汇总行 matched_marker 正确", mon.matched_marker == "一条龙任务完成: ", mon.matched_marker)


def test_intermediate_failure_not_trigger():
    print("[4] 中途 goto_page 失败行不提前触发")
    mon = _monitor(["✅ 一条龙结束", "一条龙任务完成: "])
    mon._process_line(Path("whimbox-2026-11-28.log"), GOTO_FAIL_LINE)
    check("中途失败行不触发", not mon.completion_event.is_set())


def test_watch_once_reads_appended_line():
    print("[5] 单轮轮询：追加失败行后触发")
    probe_dir = PROJECT_ROOT / ".temp" / "whimbox_marker_probe"
    probe_dir.mkdir(parents=True, exist_ok=True)
    log_file = probe_dir / "whimbox-2026-11-28.log"
    log_file.write_text("2026-11-28 14:25:10.522 | INFO | __main__:_prepare:32 - 开始\n",
                        encoding="utf-8")
    try:
        mon = _monitor(["✅ 一条龙结束", "一条龙任务完成: "])
        mon._offsets = mon._snapshot_offsets()
        with log_file.open("a", encoding="utf-8") as fh:
            fh.write(FAIL_LINE + "\n")
        mon._watch_once(False)
        check("追加后单轮即触发", mon.completion_event.is_set())
    finally:
        log_file.unlink(missing_ok=True)
        probe_dir.rmdir()


def test_verify_judges_failure():
    print("[6] 复核层：异常结束不会被当成成功")
    log_path = Path(r"E:\GithubProject\autoGame\whimbox\whimbox_app\logs\whimbox-2026-11-28.log")
    if not log_path.is_file():
        print("  SKIP 本机今日 whimbox 日志不存在")
        return
    from game_auto.services.whimbox_daily_parser import parse_whimbox_log

    data = parse_whimbox_log(log_path)
    check("parser 能读到数据", isinstance(data, dict) and bool(data.get("tasks")))
    profile = SimpleNamespace(
        game_key="nikki",
        app_key="InfinityNikki",
        daily_parser="x:y",
        verify_scripts=(),
        latest_daily_log=lambda: data,
        augment_verify=lambda *a, **k: False,
        daily_tool="whimbox",
    )
    with patch("game_auto.games.get_profile", return_value=profile):
        got = tbr._verify_script_log_result("InfinityNikki", "whimbox", "无限暖暖", LOG)
    check("改判失败（返回非空）", isinstance(got, str) and bool(got), got)
    check("失败原因点名朝夕心愿", isinstance(got, str) and "朝夕心愿未完成" in got, got)


def main():
    test_profile_markers()
    test_failure_line_triggers()
    test_success_line_still_triggers()
    test_intermediate_failure_not_trigger()
    test_watch_once_reads_appended_line()
    test_verify_judges_failure()
    print(f"\n{'FAILED: ' + ', '.join(FAILS) if FAILS else 'ALL PASS'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
