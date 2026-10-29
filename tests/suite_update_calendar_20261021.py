"""游戏更新公告日历 + 动态补跑队列回归测试（2026-10-21）。

不连游戏。覆盖：
  1. 原神「〓更新时间〓」、崩铁「■更新时间」、绝区零「【更新开始时间】」/
     「【版本更新时间】」四种公告写法解析
  2. 多版本公告选择最新版本
  3. 维护跳过 → 创建补跑条目 → 到点触发（force_rerun=False）→ 成功移除
  4. 有补跑条目时 _has_unfired_scheduled_today 为 True（关机顺延）

跑法：.venv\\Scripts\\python.exe artifacts\\test_update_calendar_20261021.py
"""

import json
import shutil
import sys
import tempfile
import threading
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, r"E:\GithubProject")

import game_auto.services.game_update_calendar as cal  # noqa: E402
import game_auto.ui.bridge as bridge_mod  # noqa: E402
from game_auto.ui.bridge import UiBridge  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def parse(title, content):
    return cal._parse_update_item({"title": title, "content": content, "ann_id": 1})


def test_genshin_parser():
    print("[1] 原神 〓更新时间〓")
    info = parse(
        "「往冥府的安魂歌」7.1版本更新说明",
        "亲爱的旅行者：…… 〓更新时间〓 2026/09/23 06:00 开始，预计5个小时完成。 〓客户端更新方式〓",
    )
    check("解析出 7.1", bool(info) and info["version"] == "7.1", info)
    check("开服 11:00", bool(info) and info["open_at"].startswith("2026-09-23T11:00"), info)


def test_hsr_parser():
    print("[2] 崩铁 ■更新时间")
    info = parse(
        "4.6版本「月升之前，与兽共舞」版本更新说明",
        "■更新时间 2026/09/28 06:00:00 开始，预计5个小时完成。 ■补偿说明",
    )
    check("解析出 4.6", bool(info) and info["version"] == "4.6", info)
    check("开服 11:00", bool(info) and info["open_at"].startswith("2026-09-28T11:00"), info)


def test_zzz_32_parser():
    print("[3] 绝区零 3.2【更新开始时间】+ 全角括号")
    info = parse(
        "3.2版本「她与她的隐秘往事」更新公告",
        "【更新开始时间】 2026/09/09 06:00（UTC+8） 预计5个小时完成。 【补偿说明】",
    )
    check("解析出 3.2", bool(info) and info["version"] == "3.2", info)
    check("开服 11:00", bool(info) and info["open_at"].startswith("2026-09-09T11:00"), info)


def test_zzz_33_parser():
    print("[4] 绝区零 3.3【版本更新时间】+ ASCII 括号")
    info = parse(
        "3.3版本「重返天空的旅程」预下载开启&更新通知",
        "【版本更新时间】 2026/10/21 06:00 (UTC+8) 开始，预计需要5个小时。",
    )
    check("解析出 3.3", bool(info) and info["version"] == "3.3", info)
    check("开服 11:00", bool(info) and info["open_at"].startswith("2026-10-21T11:00"), info)


def test_select_latest():
    print("[5] 多版本公告选最新")
    old = parse(
        "3.2版本「她与她的隐秘往事」更新公告",
        "【更新开始时间】 2026/09/09 06:00（UTC+8） 预计5个小时完成。",
    )
    new = parse(
        "3.3版本「重返天空的旅程」预下载开启&更新通知",
        "【版本更新时间】 2026/10/21 06:00 (UTC+8) 开始，预计需要5个小时。",
    )
    latest = cal._select_latest([old, new])
    check("选到 3.3", bool(latest) and latest["version"] == "3.3", latest)


def test_maintenance_window():
    print("[6] 维护窗口边界：11:00 开服 + 5 分钟 buffer")
    start = datetime(2026, 10, 21, 6, 0, tzinfo=cal.CN_TZ)
    open_at = datetime(2026, 10, 21, 11, 0, tzinfo=cal.CN_TZ)
    info = {
        "version": "3.3",
        "title": "3.3版本更新通知",
        "maintenance_start_dt": start,
        "open_at_dt": open_at,
        "maintenance_start": start.isoformat(timespec="seconds"),
        "open_at": open_at.isoformat(timespec="seconds"),
    }
    old = cal.get_update_info
    cal.get_update_info = lambda app_key, now=None, refresh_if_stale=True: dict(info)
    try:
        def state_at(h, m):
            return cal.is_under_maintenance(
                "ZenlessZoneZero", now=datetime(2026, 10, 21, h, m, tzinfo=cal.CN_TZ)
            )

        check("07:30 维护中", state_at(7, 30) is True)
        check("11:04 维护中", state_at(11, 4) is True)
        check("11:05 已开服", state_at(11, 5) is False)
        check("11:06 已开服", state_at(11, 6) is False)
    finally:
        cal.get_update_info = old


def test_wuwa_nte_regex():
    print("[7] 鸣潮/异环维护时间正则")
    m = cal._RE_WUWA_TIME.search(
        "更新维护时间： 2026年9月30日04:00 ~ 2026年9月30日11:00（UTC+8）"
    )
    check(
        "鸣潮匹配 2026/9/30 04:00~11:00",
        bool(m) and m.group(1) == "2026" and m.group(9) == "11",
        m.groups() if m else None,
    )
    m2 = cal._RE_NTE_TIME.search("将于10月15日06:00 - 11:00进行停服维护")
    check(
        "异环匹配 10/15 06:00~11:00",
        bool(m2) and m2.group(1) == "10" and m2.group(5) == "11",
        m2.groups() if m2 else None,
    )
    m3 = cal._RE_NIKKI_TIME.search(
        "我们计划将于2026年10月12日10:00-11:00（UTC+8）进行停服维护更新"
    )
    check(
        "暖暖匹配 2026/10/12 10:00~11:00",
        bool(m3) and m3.group(1) == "2026" and m3.group(2) == "10" and m3.group(6) == "11",
        m3.groups() if m3 else None,
    )


def test_qoder_gate():
    print("[8] Qoder 10:00 门槛 + 独立任务")
    from game_auto.runtime.background_template_runner import _flow_time_allowed
    from game_auto.services.launch_task_catalog import (
        get_standalone_task_config,
        is_standalone_task,
    )

    cfg = {"not_before": "10:00"}
    check("09:59 不跑", _flow_time_allowed(cfg, "09:59") is False)
    check("10:00 可跑", _flow_time_allowed(cfg, "10:00") is True)
    check("10:01 可跑", _flow_time_allowed(cfg, "10:01") is True)
    check("QoderFlow 已注册", is_standalone_task("QoderFlow") is True)
    check(
        "QoderFlow 显示名",
        "Qoder" in str(get_standalone_task_config("QoderFlow").get("display_name", "")),
    )


def test_prelaunch_guard():
    print("[9] 启动前维护拦截：直接 skipped，不启动脚本")
    import logging
    import game_auto.runtime.task_batch_runner as tbr

    saved = {
        "get_saved_script_choice": tbr.get_saved_script_choice,
        "is_standalone_task": tbr.is_standalone_task,
        "get_app_display_name": tbr.get_app_display_name,
        "get_script_display_name": tbr.get_script_display_name,
    }
    old_maint = cal.get_maintenance_state
    try:
        cal.get_maintenance_state = lambda app_key: {
            "version": "3.3",
            "open_at_dt": datetime(2026, 10, 21, 11, 0, tzinfo=cal.CN_TZ),
        }
        tbr.get_saved_script_choice = lambda app_key: "default"
        tbr.is_standalone_task = lambda app_key: False
        tbr.get_app_display_name = lambda app_key: "绝区零"
        tbr.get_script_display_name = lambda app_key, choice: "默认脚本"
        ctl = SimpleNamespace(logger=logging.getLogger("test"))
        result = tbr._run_single_task(ctl, "ZenlessZoneZero", None, cj_should_run=False)
    finally:
        cal.get_maintenance_state = old_maint
        for key, value in saved.items():
            setattr(tbr, key, value)
    check("返回 skipped", getattr(result, "state", "") == "skipped", result)
    check(
        "消息含维护和 11:00",
        "维护" in getattr(result, "message", "") and "11:00" in getattr(result, "message", ""),
        getattr(result, "message", ""),
    )


def make_bridge(retry_file: Path):
    b = UiBridge.__new__(UiBridge)
    b._update_retry_lock = threading.RLock()
    b._is_running = False
    b._pending_scheduled = []
    b._request_toast = lambda *a, **k: None
    b.loadPostRunPrefs = lambda: json.dumps({"email": False, "shutdown": False})
    b.calls = []
    b.startAutomation = lambda *a, **k: b.calls.append((a, k))
    bridge_mod.UPDATE_RETRY_FILE = retry_file
    open_dt = datetime.now(cal.CN_TZ) + timedelta(minutes=10)
    bridge_mod.get_update_info = lambda app_key, now=None: {
        "version": "3.3",
        "title": "3.3版本更新通知",
        "maintenance_start_dt": open_dt - timedelta(hours=5),
        "open_at_dt": open_dt,
        "open_at": open_dt.isoformat(timespec="seconds"),
    }
    return b


def test_retry_create_fire_success(tmp_dir: Path):
    print("[10] 维护跳过 → 排队 → 到点触发 → 成功移除")
    retry_file = tmp_dir / "update_retries.json"
    retry_file.write_text("[]", encoding="utf-8")
    b = make_bridge(retry_file)
    skip_result = SimpleNamespace(
        app_key="ZenlessZoneZero", script_choice=None,
        state="skipped", message="游戏更新中，跳过", success=False,
    )
    b._handle_update_retry_result(skip_result)
    items = json.loads(retry_file.read_text(encoding="utf-8"))
    check("创建了补跑条目", len(items) == 1, items)
    check("条目是绝区零", bool(items) and items[0].get("app_key") == "ZenlessZoneZero", items)
    check("尝试次数从 0 开始", bool(items) and items[0].get("attempt") == 0, items)

    # 把 due_at 改到过去，触发到点补跑
    items[0]["due_at"] = (datetime.now(cal.CN_TZ) - timedelta(minutes=1)).isoformat(timespec="seconds")
    retry_file.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    b._check_update_retries()
    check("触发了补跑批", len(b.calls) == 1, b.calls)
    if b.calls:
        args, kwargs = b.calls[0]
        check("force_rerun=False", kwargs.get("force_rerun") is False, kwargs)
        check("补跑包含绝区零", "ZenlessZoneZero" in str(args[0]), args)
    items = json.loads(retry_file.read_text(encoding="utf-8"))
    check("触发后 attempt=1", bool(items) and items[0].get("attempt") == 1, items)

    success_result = SimpleNamespace(
        app_key="ZenlessZoneZero", script_choice=None,
        state="success", message="成功", success=True,
    )
    b._handle_update_retry_result(success_result)
    items = json.loads(retry_file.read_text(encoding="utf-8"))
    check("成功后移除条目", items == [], items)


def test_fallback_window(tmp_dir: Path):
    print("[12] 无公告时按维护窗口兜底（暖暖 10:00-11:00）")
    retry_file = tmp_dir / "fallback_retries.json"
    retry_file.write_text("[]", encoding="utf-8")
    b = make_bridge(retry_file)
    bridge_mod.get_update_info = lambda *a, **k: None
    result = SimpleNamespace(
        app_key="InfinityNikki", script_choice=None,
        state="failed", message="启动失败", success=False,
    )
    b._handle_update_retry_result(
        result, now=datetime(2026, 10, 21, 10, 30, tzinfo=cal.CN_TZ)
    )
    items = json.loads(retry_file.read_text(encoding="utf-8"))
    check("创建暖暖补跑", len(items) == 1, items)
    check(
        "补跑时间 11:05",
        bool(items) and str(items[0].get("due_at", "")).startswith("2026-10-21T11:05"),
        items,
    )


def test_unfired_includes_retry(tmp_dir: Path):
    print("[11] 有补跑条目时关机顺延判断为 True")
    retry_file = tmp_dir / "update_retries.json"
    retry_file.write_text("[]", encoding="utf-8")
    b = make_bridge(retry_file)
    b._unfired_scheduled_today = lambda: []
    skip_result = SimpleNamespace(
        app_key="ZenlessZoneZero", script_choice=None,
        state="skipped", message="游戏更新中，跳过", success=False,
    )
    b._handle_update_retry_result(skip_result)
    check("有未完成补跑", b._has_unfired_scheduled_today() is True)
    check("pending 能查到绝区零", b._has_pending_update_retry("ZenlessZoneZero") is True)


def main():
    test_genshin_parser()
    test_hsr_parser()
    test_zzz_32_parser()
    test_zzz_33_parser()
    test_select_latest()
    test_maintenance_window()
    test_wuwa_nte_regex()
    test_qoder_gate()
    test_prelaunch_guard()
    tmp_dir = Path(tempfile.mkdtemp(prefix="game_auto_update_test_"))
    old_file = bridge_mod.UPDATE_RETRY_FILE
    old_get = bridge_mod.get_update_info
    try:
        test_retry_create_fire_success(tmp_dir)
        test_fallback_window(tmp_dir)
        test_unfired_includes_retry(tmp_dir)
    finally:
        bridge_mod.UPDATE_RETRY_FILE = old_file
        bridge_mod.get_update_info = old_get
        shutil.rmtree(tmp_dir, ignore_errors=True)
    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
