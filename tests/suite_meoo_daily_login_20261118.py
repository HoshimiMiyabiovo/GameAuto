# -*- coding: utf-8 -*-
"""Meoo 每日登录送积分（薅羊毛 MEOO 子流程）回归（2026-11-18）。

背景：Meoo 平台每天登录一次可领每日赠送积分（FREE 档 2,000/天）。与窗口型
羊毛任务不同，这是 CLI 型子流程（无窗口无进程）：`meoo whoami` 触发当日登录，
`meoo account --json` 读 points.totalGranted 做到账证据门控（fail-closed）。
触发方式未实锤（注册当天无法判别 whoami 是否算登录——注册即首次登录），
门控会自动暴露：连续多日持平即触发方式不对，按 meoo.com 积分明细校准。

本套件锁：
[1] evaluate_daily_bonus 纯逻辑：首次基线 / 今日持平 fail / 今日增长 success /
    跨日持平 fail / 回落 fail-closed（不静默放行）/ value 只增不减；
[2] parse_account：真实响应形态解析（totalGranted + daily_login.used）、坏 JSON
    抛 MeooCliError；
[3] flow_configs 双份配置：runner=meoo / cli_path / close 全 false；
[4] 接线：WOOL_FLOW_KEYS 含 MEOO、run_wool_flow_background 的 runner 分发、
    两处显示名（结构化导入）、catalog 注记、本机 flow_exe_paths（存在才查）。

跑法：.venv\\Scripts\\python.exe -m pytest tests\\suite_meoo_daily_login_20261118.py -q
"""

import io
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import game_auto.runtime.background_template_runner as btr  # noqa: E402
import game_auto.runtime.meoo_claim as mc  # noqa: E402
import game_auto.services.launch_task_catalog as catalog  # noqa: E402

FAILURES = []


def check(label, ok, detail=None):
    mark = "PASS" if ok else "FAIL"
    print(f"  {mark} {label}" + (f"  ← {detail}" if not ok and detail is not None else ""))
    if not ok:
        FAILURES.append(label)


def test_evaluate_daily_bonus():
    print("[1] evaluate_daily_bonus 每日到账判定")
    ev = mc.evaluate_daily_bonus
    ok, msg, val = ev("2026-11-18", 7000, None)
    check("首次运行记基线且 ok（明示非静默假成功）", ok is True and "基线" in msg, msg)
    check("基线值写入今天", val == {"date": "2026-11-18", "total_granted": 7000}, val)

    ok, msg, val = ev("2026-11-18", 9000, {"date": "2026-11-18", "total_granted": 7000})
    check("今日增长 → 到账成功 +2000", ok is True and "+2000" in msg, msg)

    ok, msg, _ = ev("2026-11-18", 7000, {"date": "2026-11-18", "total_granted": 7000})
    check("今日持平 → fail-closed 未见到账", ok is False and "未见到账" in msg, msg)

    ok, msg, _ = ev("2026-11-19", 7000, {"date": "2026-11-18", "total_granted": 7000})
    check("跨日持平 → fail（昨日已到账今天没到）", ok is False and "未见到账" in msg, msg)

    ok, msg, val = ev("2026-11-19", 6000, {"date": "2026-11-18", "total_granted": 7000})
    check("回落 → 同样 fail-closed 不静默放行", ok is False and val["total_granted"] == 6000,
          (ok, val))

    ok, msg, val = ev("2026-11-19", 9000, {"date": "2026-11-17", "total_granted": 5000})
    check("跨日增长 → 成功（今天早些时候已到账也算）", ok is True and "+4000" in msg, msg)


def test_parse_account():
    print("[2] parse_account 解析真实响应形态")
    payload = {
        "success": True,
        "data": {
            "points": {"availableBalance": 9000, "totalGranted": 9000, "totalConsumed": 0},
            "plan": {"planCode": "free", "benefits": [
                {"typeCode": "model_credits", "quota": 9000, "used": 0},
                {"typeCode": "daily_login", "quota": 2000, "used": 2000},
            ]},
        },
    }
    granted, used = mc.parse_account(json.dumps(payload, ensure_ascii=False))
    check("totalGranted 提取", granted == 9000, granted)
    check("daily_login.used 提取（配额消耗口径，仅日志观察）", used == 2000, used)
    try:
        mc.parse_account("不是JSON")
        check("坏 JSON 抛 MeooCliError", False)
    except mc.MeooCliError:
        check("坏 JSON 抛 MeooCliError", True)
    # 缺 daily_login benefit 不炸（used 兜底 0）
    granted2, used2 = mc.parse_account(json.dumps(
        {"data": {"points": {"totalGranted": 1}, "plan": {"benefits": []}}}))
    check("缺 daily_login 时 used=0 不炸", (granted2, used2) == (1, 0), (granted2, used2))


def test_flow_configs():
    print("[3] flow_configs 双份 MEOO 配置")
    for name, required in (("flow_configs.example.json", True), ("flow_configs.json", False)):
        p = PROJECT_ROOT / name
        if not required and not p.exists():
            check(f"{name} 不存在则跳过（本机文件）", True)
            continue
        cfg = json.loads(p.read_text(encoding="utf-8"))
        flow = cfg.get("MEOO")
        check(f"{name}: flow key 存在", flow is not None)
        if flow is None:
            continue
        check(f"{name}: enabled=true", flow.get("enabled") is True)
        check(f"{name}: runner=meoo（CLI 型子流程分发依据）",
              flow.get("runner") == "meoo", flow.get("runner"))
        check(f"{name}: cli_path 指向 meoo.cmd",
              str(flow.get("cli_path") or "").endswith("meoo.cmd"), flow.get("cli_path"))
        check(f"{name}: close 全 false（无进程可关）",
              flow.get("close_process_on_finish") is False
              and flow.get("close_process_on_fail") is False)


def test_wiring():
    print("[4] 接线锁：WOOL_FLOW_KEYS / runner 分发 / 显示名 / catalog / 路径映射")
    check("WOOL_FLOW_KEYS 含 MEOO", "MEOO" in btr.WOOL_FLOW_KEYS, btr.WOOL_FLOW_KEYS)
    check("MEOO 排在集合末尾", btr.WOOL_FLOW_KEYS[-1] == "MEOO", btr.WOOL_FLOW_KEYS)
    check("meoo_claim.run_wool 可导入", hasattr(mc, "run_wool"))
    check("runner 分发已进 run_wool_flow_background（接线锚点）",
          '== "meoo"' in __import__("inspect").getsource(btr.run_wool_flow_background))
    from game_auto.ui.bridge_views.automation import AutomationView
    check("GUI _WOOL_FLOW_META 含 MEOO",
          AutomationView._WOOL_FLOW_META.get("MEOO") == "秒悟每日登录送积分",
          AutomationView._WOOL_FLOW_META.get("MEOO"))
    webui_src = (PROJECT_ROOT / "src/game_auto/ui/webui/api/wool.py").read_text(encoding="utf-8")
    check("WebUI meta 含 MEOO", '"MEOO"' in webui_src)
    note = str(catalog.STANDALONE_TASK_APPS.get("WoolFlow", {}).get("friendly_note") or "")
    check("WoolFlow friendly_note 提及 秒悟", "秒悟" in note, note)
    for name in ("app_config.json", "data/app_config.json"):
        p = PROJECT_ROOT / name
        if not p.exists():
            continue
        app_cfg = json.loads(p.read_text(encoding="utf-8"))
        exe = str((app_cfg.get("flow_exe_paths") or {}).get("MEOO") or "")
        check(f"flow_exe_paths.MEOO 已配（{name}）", exe.endswith("meoo.cmd"), exe)
        break


def main():
    test_evaluate_daily_bonus()
    test_parse_account()
    test_flow_configs()
    test_wiring()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED {len(FAILURES)}: {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
