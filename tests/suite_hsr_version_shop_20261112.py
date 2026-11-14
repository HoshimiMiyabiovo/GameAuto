# -*- coding: utf-8 -*-
"""崩铁「戏言成真」版本商店回归（2026-11-12）。

背景：常驻商店新增「戏言成真」页签（未熄的虹辉兑换），金品质三件 自塑尘脂×1
+ 遗器残骸×100×2 + 孤星明尘×100×2，42 天一刷与崩铁版本更新同步。调度侧新增
**version_cycle 门控维度**（月度=自然月、每周=ISO 周都不对版本口径：一个月可能
两个版本更新，周门控则天天重跑）。

本套件锁：
[1] version_cycle_due 纯函数：版本号变化到期 / 同版本跳过 / 首跑到期 /
    日历不可用 42 天 CD 兜底 / 旧格式与坏日期容错；
[2] meta 解析容错（"<版本>|<日期>"、旧格式、坏日期）；
[3] 未开服顺延（公告已出 open_at 未到，宁顺延不抢关服窗口）；
[4] runner 端到端（临时库）：到期跑→成功写标记；同版本二次跳过不启动客户端；
    失败不写标记下次重试；
[5] 调度层：enabled=false 短路（恶名狩猎同款，runner 不被调）、维护窗口顺延、
    到期时 runner 被调并产出报告行（patch.dict 注册表，绝不真跑商店）；
[6] flow_configs 双份配置静态断言（example 必查；本地文件存在才查镜像）；
[7] 接线静态锁：调用点/收尾补漏/批次清场都接了版本周期流程。
[8] _fetch_hsr_update_info 传给日历的 key 必须被映射认出（2026-11-14 实锤：
    传公告解析 key "hsr" 恒得 None，上线起没读到过日历；真实日历有数据时
    版本号必须取得到）。

跑法：.venv\\Scripts\\python.exe -m pytest tests\\suite_hsr_version_shop_20261112.py -q
（经 tests/test_suites.py 子进程跑）
"""

import io
import json
import logging
import sys
import tempfile
import threading
import unittest.mock as mock
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import game_auto.runtime.background_template_runner as btr  # noqa: E402
import game_auto.runtime.task_batch_runner as tbr  # noqa: E402

FAILURES = []
TODAY = date(2026, 11, 12)
OPEN_PAST = "2026-11-08T11:05:00+08:00"    # 截图口径：本版本开服已过 4 天
OPEN_FUTURE = "2026-12-20T11:05:00+08:00"  # 新版本公告已出、尚未开服


def check(label, ok, detail=None):
    mark = "PASS" if ok else "FAIL"
    print(f"  {mark} {label}" + (f"  ← {detail}" if not ok and detail is not None else ""))
    if not ok:
        FAILURES.append(label)


def test_version_cycle_due():
    print("[1] version_cycle_due 纯函数四态")
    due, why = btr.version_cycle_due(None, {"version": "3.8"}, today=TODAY)
    check("首跑（无记录+日历有版本）→ 到期", due and "3.8" in why, why)
    due, why = btr.version_cycle_due("3.8|2026-11-12", {"version": "3.8"}, today=TODAY)
    check("同版本已购 → 跳过", not due, why)
    due, why = btr.version_cycle_due("3.7|2026-10-01", {"version": "3.8"}, today=TODAY)
    check("版本更新 → 到期", due and "3.8" in why, why)
    due, _ = btr.version_cycle_due(None, None, today=TODAY)
    check("日历不可用+无记录 → 到期", due)
    due, why = btr.version_cycle_due(
        f"3.8|{(TODAY - timedelta(days=30)).isoformat()}", None, today=TODAY)
    check("日历不可用+30 天 → CD 未满跳过", not due and "42" in why, why)
    due, why = btr.version_cycle_due(
        f"3.8|{(TODAY - timedelta(days=43)).isoformat()}", None, today=TODAY)
    check("日历不可用+43 天 → CD 兜底到期", due and "42" in why, why)
    due, _ = btr.version_cycle_due("3.8", {"version": "3.8"}, today=TODAY)
    check("旧格式（无|）容错", not due)
    due, _ = btr.version_cycle_due("3.8|bad-date", None, today=TODAY)
    check("坏日期容错（按无记录到期）", due)
    due, _ = btr.version_cycle_due("3.8|2026-11-12", {"version": ""}, today=TODAY)
    check("日历无版本字段 → 走 CD 口径", not due)  # 已购日期=今天，42 天未满


def test_parse_meta():
    print("[2] meta 解析容错")
    check("标准格式", btr._parse_version_cycle_meta("3.8|2026-11-12") == ("3.8", date(2026, 11, 12)))
    check("旧格式无|", btr._parse_version_cycle_meta("3.8") == ("3.8", None))
    check("空值", btr._parse_version_cycle_meta("") == ("", None))
    check("None", btr._parse_version_cycle_meta(None) == ("", None))
    ver, day = btr._parse_version_cycle_meta("3.8|not-a-date")
    check("坏日期 → 日期 None 版本保留", ver == "3.8" and day is None)


def test_version_shop_open():
    print("[3] 未开服顺延判定")
    now = datetime.fromisoformat("2026-12-19T10:00:00+08:00")
    check("公告已出未开服 → False", btr._version_shop_open({"open_at": OPEN_FUTURE}, now=now) is False)
    now2 = datetime.fromisoformat("2026-12-20T11:12:00+08:00")
    check("开服 +7min → True", btr._version_shop_open({"open_at": OPEN_FUTURE}, now=now2) is True)
    check("空 open_at → True", btr._version_shop_open({}, now=now) is True)
    check("坏格式 → True（宁可放行）", btr._version_shop_open({"open_at": "garbage"}, now=now) is True)
    check("naive open_at + 真时钟 → 不抛异常", isinstance(
        btr._version_shop_open({"open_at": "2026-01-01T00:00:00"}), bool))


def _run_flow_env(tmp: Path, run_flow_result, update_info, stored=None):
    """_run_version_cycle_flow 的全打桩环境：临时库 + 注入日历/时钟。"""
    import game_auto.settings.paths as paths_mod

    calls = {"run_flow": 0}
    with mock.patch.object(btr, "run_flow",
                           side_effect=lambda *a, **k: (calls.__setitem__("run_flow", calls["run_flow"] + 1),
                                                        tuple(run_flow_result))[1]), \
         mock.patch.object(paths_mod, "WORKFLOW_DB_PATH", tmp / "workflow.sqlite3"):
        ok, msg = btr._run_version_cycle_flow(
            logging.getLogger("suite"), "HSR_VERSION_SHOP", "崩铁戏言成真商店",
            update_info=update_info, now=datetime(2026, 11, 12, 12, 0, 0))
    import game_auto.storage.workflow_db as wdb
    db = wdb.WorkflowDB(str(tmp / "workflow.sqlite3"))
    stored_value = db.get_task_meta("version_cycle:HSR_VERSION_SHOP")
    return ok, msg, stored_value, calls["run_flow"]


def test_runner_end_to_end():
    print("[4] runner 端到端（临时库）：到期跑/同版本跳过/失败不写标记")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        info = {"version": "3.8", "open_at": OPEN_PAST}
        ok, msg, stored, n = _run_flow_env(tmp, (True, "购买完成"), info)
        check("首跑到期执行", ok and n == 1, (ok, n))
        check("成功写标记 版本|日期", stored == f"3.8|{TODAY.isoformat()}", stored)
        ok2, msg2, _, n2 = _run_flow_env(tmp, (True, "购买完成"), info)
        check("同版本二次 → 跳过且不启动流程", ok2 and n2 == 0 and "已购" in msg2, (ok2, n2, msg2))
        # 失败不写标记
        info2 = {"version": "3.9", "open_at": OPEN_PAST}
        ok3, msg3, stored3, n3 = _run_flow_env(tmp, (False, "步骤失败"), info2)
        check("版本更新到期执行", ok3 is False and n3 == 1, (ok3, n3))
        check("失败不写标记（下批重试）", stored3 == f"3.8|{TODAY.isoformat()}", stored3)
        check("失败消息带重试提示", "重试" in msg3, msg3)
        # 未开服顺延
        ok4, msg4, _, n4 = _run_flow_env(
            tmp, (True, "x"), {"version": "4.0", "open_at": OPEN_FUTURE})
        check("新版本未开服 → 顺延且不执行", ok4 and n4 == 0 and "未开服" in msg4, (ok4, n4, msg4))
        # 日历不可用 + 无记录 → 到期执行
        ok5, _, _, n5 = _run_flow_env(tmp / "b", (True, "ok"), None)
        check("日历不可用+无记录 → 到期执行", ok5 and n5 == 1, (ok5, n5))


def _controller():
    return SimpleNamespace(stop_current_task=False, logger=logging.getLogger("suite"))


def test_dispatch_layer():
    print("[5] 调度层：enabled 短路 / 维护顺延 / 到期调用 runner")
    calls = {"runner": 0}

    def fake_runner(logger):
        calls["runner"] += 1
        return True, "✅ 购买完成"

    cfg_disabled = {"enabled": False}
    cfg_enabled = {"enabled": True}
    with tempfile.TemporaryDirectory() as td:
        import game_auto.settings.paths as paths_mod
        db_path = Path(td) / "workflow.sqlite3"
        # 预写标记：让门控判定为"到期"（无记录即到期）
        with mock.patch.object(paths_mod, "WORKFLOW_DB_PATH", db_path), \
             mock.patch.dict(tbr._EXTRA_VERSION_CYCLE_RUNNERS, {"HSR_VERSION_SHOP": fake_runner}):
            # enabled=false 短路：runner 不被调、返回 None
            with mock.patch("game_auto.runtime.foreground_flow_runner._load_flow_config",
                            return_value=cfg_disabled):
                one = tbr._run_pending_version_cycle_flow(
                    _controller(), "HonkaiStarRail", "HSR_VERSION_SHOP", "崩铁戏言成真商店")
            check("enabled=false → None 且 runner 未调", one is None and calls["runner"] == 0)
            # 维护中顺延
            with mock.patch("game_auto.runtime.foreground_flow_runner._load_flow_config",
                            return_value=cfg_enabled), \
                 mock.patch("game_auto.services.game_update_calendar.get_maintenance_state",
                            return_value={"state": "maintenance"}):
                one2 = tbr._run_pending_version_cycle_flow(
                    _controller(), "HonkaiStarRail", "HSR_VERSION_SHOP", "崩铁戏言成真商店")
            check("维护窗口 → None 且 runner 未调", one2 is None and calls["runner"] == 0)
            # 到期正常路径：runner 被调、报告行 ✅
            with mock.patch("game_auto.runtime.foreground_flow_runner._load_flow_config",
                            return_value=cfg_enabled), \
                 mock.patch("game_auto.services.game_update_calendar.get_maintenance_state",
                            return_value=None):
                one3 = tbr._run_pending_version_cycle_flow(
                    _controller(), "HonkaiStarRail", "HSR_VERSION_SHOP", "崩铁戏言成真商店")
            check("到期 → runner 被调 1 次", calls["runner"] == 1, calls)
            check("报告行 ✅ 且成功", one3 is not None and one3[1] is True and "✅" in one3[0], one3)
            # runner 抛异常 → ❌ 行但不拖垮批次
            def boom(logger):
                raise RuntimeError("boom")
            with mock.patch("game_auto.runtime.foreground_flow_runner._load_flow_config",
                            return_value=cfg_enabled), \
                 mock.patch("game_auto.services.game_update_calendar.get_maintenance_state",
                            return_value=None), \
                 mock.patch.dict(tbr._EXTRA_VERSION_CYCLE_RUNNERS, {"HSR_VERSION_SHOP": boom}):
                one4 = tbr._run_pending_version_cycle_flow(
                    _controller(), "HonkaiStarRail", "HSR_VERSION_SHOP", "崩铁戏言成真商店")
            check("runner 异常 → ❌ 行", one4 is not None and one4[1] is False and "❌" in one4[0], one4)


def test_flow_configs():
    print("[6] flow_configs 双份配置静态断言")
    for name, required in (("flow_configs.example.json", True), ("flow_configs.json", False)):
        p = PROJECT_ROOT / name
        if not required and not p.exists():
            check(f"{name} 不存在则跳过（本机文件）", True)
            continue
        cfg = json.loads(p.read_text(encoding="utf-8"))
        flow = cfg.get("HSR_VERSION_SHOP")
        check(f"{name}: flow key 存在", flow is not None)
        if flow is None:
            continue
        check(f"{name}: enabled=true", flow.get("enabled") is True)
        check(f"{name}: exe_from_app", flow.get("exe_from_app") == "HonkaiStarRail")
        check(f"{name}: 进程名 StarRail.exe", "StarRail.exe" in (flow.get("window_process_names") or []))
        check(f"{name}: 跑完关游戏", flow.get("close_process_on_finish") is True)
        steps = flow.get("steps") or []
        check(f"{name}: 两步（进主界面+兑换）",
              [s.get("action") for s in steps] == ["starrail_enter_main", "monthly_exchange"],
              [s.get("action") for s in steps])
        if len(steps) >= 2:
            step = steps[1]
            check(f"{name}: 页签文字", step.get("tab_text") == "戏言成真")
            check(f"{name}: 内容标记含自塑尘脂/孤星明尘",
                  set(step.get("tab_content_markers") or []) >= {"自塑尘脂", "孤星明尘"})
            check(f"{name}: 不配选中态模板（协议约束）",
                  not step.get("tab_template"), step.get("tab_template"))
            check(f"{name}: forbidden 五星光锥 ≥5",
                  len(step.get("forbidden_texts") or []) >= 5)
            check(f"{name}: 确认模板指向存在文件",
                  (PROJECT_ROOT / str(step.get("confirm_template", "x"))).is_file(),
                  step.get("confirm_template"))
            check(f"{name}: 退出标记指向存在文件",
                  (PROJECT_ROOT / str(step.get("exit_marker_template", "x"))).is_file())
            items = {i.get("name"): i.get("count") for i in (step.get("items") or [])}
            check(f"{name}: 金三件清单", items == {"自塑尘脂": 1, "遗器残骸": 2, "孤星明尘": 2}, items)
        hsr_monthly = (cfg.get("HSR_MONTHLY") or {})
        check(f"{name}: HSR_MONTHLY 保持停用不动", hsr_monthly.get("enabled") is False)


def test_wiring_static():
    print("[7] 接线静态锁：调用点/收尾补漏/清场")
    check("注册表：HonkaiStarRail → HSR_VERSION_SHOP",
          tbr._EXTRA_VERSION_CYCLE_FLOWS.get("HonkaiStarRail")
          == (("HSR_VERSION_SHOP", "崩铁戏言成真商店"),))
    check("runner 映射", tbr._EXTRA_VERSION_CYCLE_RUNNERS.get("HSR_VERSION_SHOP")
          is btr.run_hsr_version_shop_background)
    src = (PROJECT_ROOT / "src/game_auto/runtime/task_batch_runner.py").read_text(encoding="utf-8")
    check("日常跑完出口已接", src.count("_run_extra_version_cycle_flows(controller, app_key, report, warnings)") >= 2,
          "正常出口 + 今日已完成出口")
    check("收尾补漏已接", "_sweep_pending_version_cycle_flows(controller, report, warnings)" in src)
    check("批次清场已接", "_VERSION_CYCLE_CHECKED.clear()" in src)
    check("meta 键格式", btr.version_cycle_meta_key("HSR_VERSION_SHOP") == "version_cycle:HSR_VERSION_SHOP")
    check("CD 常量=42", btr.VERSION_CYCLE_CD_DAYS == 42)


def test_fetch_update_info_key():
    print("[8] _fetch_hsr_update_info 的 key 走真实日历映射")
    captured = {}

    def fake_get_update_info(app_key, **kwargs):
        captured["app_key"] = app_key
        return {"version": "9.9", "open_at": "2026-01-01T00:00:00+08:00"}

    with mock.patch("game_auto.services.game_update_calendar.get_update_info",
                    side_effect=fake_get_update_info):
        info = btr._fetch_hsr_update_info()
    check("日历返回值透传", info == {"version": "9.9", "open_at": "2026-01-01T00:00:00+08:00"})
    from game_auto.services.game_update_calendar import APP_KEY_TO_GAME

    key = captured.get("app_key")
    check(f"传入 key {key!r} 在日历映射表里（传 'hsr' 恒 None 教训）",
          key in APP_KEY_TO_GAME, sorted(APP_KEY_TO_GAME))
    # 真实日历探针：缓存/网络有数据时必须取到版本号；离线/无公告时放行
    real = btr._fetch_hsr_update_info()
    if real:
        check(f"真实日历取到版本号 {real.get('version')!r}",
              bool(str((real or {}).get("version") or "").strip()))
    else:
        check("真实日历当前不可用（离线/无公告），版本断言放行", True)


def main():
    test_version_cycle_due()
    test_parse_meta()
    test_version_shop_open()
    test_runner_end_to_end()
    test_dispatch_layer()
    test_flow_configs()
    test_wiring_static()
    test_fetch_update_info_key()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED {len(FAILURES)}: {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
