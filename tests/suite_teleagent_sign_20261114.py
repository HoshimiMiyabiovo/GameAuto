# -*- coding: utf-8 -*-
"""TeleAgent 每日签到回归（2026-11-14）。

背景：TeleAgent（电信星辰超级智能体，Electron，F:\\Program Files\\teleagent）
每日可领 100 积分。用户拍板：并入薅羊毛集合调度（WOOL_FLOW_KEYS 追加）、
跑完关进程、入口「点击左下角头像/用户名」进用户面板（面板含积分福利社块）。
用户两张截图实锤两态：未领 = 面板内「立即领取 >」；已领 = 「今日已领 ✓ +
已领 N 天/累计 100积分」。TeleAgent 无本地领取日志 → 成败核验 OCR
「今日已领」fail-closed（MINIMAX 弃假阳性模板同款思路）。

本套件锁：
[1] flow_configs 双份配置静态断言（example 必查；本地文件存在才查镜像）：
    五步结构 / OCR-only 无模板 / 第 4 步 optional_if_missing（幂等）/
    第 5 步 fail-closed 核验 / 锚与区域防误点主界面悬浮福利社卡片；
[2] 接线锁：WOOL_FLOW_KEYS 含 TELEAGENT（GUI 薅羊毛页 / WebUI /flows /
    WoolFlow 集合全自动获得）、两处显示名 meta、WoolFlow friendly_note、
    本机 flow_exe_paths 路径映射（存在才查）。

跑法：.venv\\Scripts\\python.exe -m pytest tests\\suite_teleagent_sign_20261114.py -q
（经 tests/test_suites.py 子进程跑；直接文件调用有 stdout 包装 capture 老毛病）
"""

import io
import json
import os
import sys
import glob as _glob
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import game_auto.runtime.background_template_runner as btr  # noqa: E402
import game_auto.services.launch_task_catalog as catalog  # noqa: E402

FAILURES = []

STEP_NAMES = ["等待主界面", "点击左下角用户名", "等待用户面板", "点击立即领取", "核验今日已领"]
STEP_ACTIONS = ["wait_template", "click_template", "wait_template", "click_template", "wait_template"]


def check(label, ok, detail=None):
    mark = "PASS" if ok else "FAIL"
    print(f"  {mark} {label}" + (f"  ← {detail}" if not ok and detail is not None else ""))
    if not ok:
        FAILURES.append(label)


def _steps_by_name(flow):
    return {s.get("name"): s for s in (flow.get("steps") or [])}


def test_flow_configs():
    print("[1] flow_configs 双份配置静态断言")
    for name, required in (("flow_configs.example.json", True), ("flow_configs.json", False)):
        p = PROJECT_ROOT / name
        if not required and not p.exists():
            check(f"{name} 不存在则跳过（本机文件）", True)
            continue
        cfg = json.loads(p.read_text(encoding="utf-8"))
        flow = cfg.get("TELEAGENT")
        check(f"{name}: flow key 存在", flow is not None)
        if flow is None:
            continue
        check(f"{name}: enabled=true", flow.get("enabled") is True)
        check(f"{name}: 进程名 TeleAgent.exe", "TeleAgent.exe" in (flow.get("window_process_names") or []))
        check(f"{name}: 窗口标题含 TeleAgent（防桌面宠物同进程悬浮窗）",
              "TeleAgent" in (flow.get("window_title_keywords") or []))
        check(f"{name}: 跑完关游戏（含失败）", flow.get("close_process_on_finish") is True
              and flow.get("close_process_on_fail") is True)
        steps = flow.get("steps") or []
        check(f"{name}: 五步且顺序锁定", [s.get("name") for s in steps] == STEP_NAMES,
              [s.get("name") for s in steps])
        check(f"{name}: 动作序列 等→点→等→点→核验", [s.get("action") for s in steps] == STEP_ACTIONS,
              [s.get("action") for s in steps])
        check(f"{name}: 全 OCR 路线（不裁模板）",
              all(not (s.get("template") or "").strip() for s in steps),
              [s.get("template") for s in steps])
        by_name = _steps_by_name(flow)
        wait_main = by_name.get("等待主界面") or {}
        check(f"{name}: 等主界面容忍冷启动（timeout≥60）", (wait_main.get("timeout_sec") or 0) >= 60)
        click_user = by_name.get("点击左下角用户名") or {}
        check(f"{name}: 用户名 prefix 可容忍改名", "user_" in (click_user.get("target_text") or []))
        region = click_user.get("ocr_region_frac") or []
        check(f"{name}: 用户名区域锁左下角（y1≥0.8）", len(region) == 4 and region[1] >= 0.8, region)
        wait_panel = by_name.get("等待用户面板") or {}
        check(f"{name}: 面板闸用专属词积分余额", "积分余额" in (wait_panel.get("target_text") or []))
        check(f"{name}: 面板没开即失败（on_fail=fail）", wait_panel.get("on_fail") == "fail")
        click_claim = by_name.get("点击立即领取") or {}
        check(f"{name}: 已领态幂等跳过（optional_if_missing）",
              click_claim.get("optional_if_missing") is True)
        check(f"{name}: 目标文字 立即领取", "立即领取" in (click_claim.get("target_text") or []))
        check(f"{name}: 锚「积分余额」防误点悬浮卡片",
              "积分余额" in (click_claim.get("ocr_anchor") or []))
        region = click_claim.get("ocr_region_frac") or []
        check(f"{name}: 领取区域排除悬浮卡片（y2≤0.85）", len(region) == 4 and region[3] <= 0.85, region)
        verify = by_name.get("核验今日已领") or {}
        check(f"{name}: 核验含 今日已领", "今日已领" in (verify.get("target_text") or []))
        check(f"{name}: 核验 fail-closed（on_fail=fail）", verify.get("on_fail") == "fail")


def test_wiring():
    print("[2] 接线锁：WOOL_FLOW_KEYS / 显示名 meta / friendly_note / 路径映射")
    check("WOOL_FLOW_KEYS 含 TELEAGENT", "TELEAGENT" in btr.WOOL_FLOW_KEYS, btr.WOOL_FLOW_KEYS)
    # 显示名 meta 两处（稳定配置键接线锚，01-workflow 例外条款允许）
    gui_src = (PROJECT_ROOT / "src/game_auto/ui/bridge_views/automation.py").read_text(encoding="utf-8")
    check("GUI _WOOL_FLOW_META 含 TELEAGENT",
          '"TELEAGENT": "TeleAgent 每日签到"' in gui_src)
    webui_src = (PROJECT_ROOT / "src/game_auto/ui/webui/api/wool.py").read_text(encoding="utf-8")
    check("WebUI meta 含 TELEAGENT",
          '"TELEAGENT":"TeleAgent 每日签到"' in webui_src)
    note = str(catalog.STANDALONE_TASK_APPS.get("WoolFlow", {}).get("friendly_note") or "")
    check("WoolFlow friendly_note 提及 TeleAgent", "TeleAgent" in note, note)
    # 本机路径映射（app_config.json 不入库，存在才查）
    app_cfg_path = PROJECT_ROOT / "app_config.json"
    if app_cfg_path.exists():
        app_cfg = json.loads(app_cfg_path.read_text(encoding="utf-8"))
        exe = str((app_cfg.get("flow_exe_paths") or {}).get("TELEAGENT") or "")
        check("flow_exe_paths.TELEAGENT 已配", bool(exe), exe)
        if exe:
            ready = bool(os.path.exists(exe) or _glob.glob(exe))
            check("TeleAgent.exe 路径实际存在", ready, exe)


def main():
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
