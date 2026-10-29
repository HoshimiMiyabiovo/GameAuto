# -*- coding: utf-8 -*-
"""WorkBuddy 签到流程修复回归（2026-10-29）。

当天两次 WoolFlow 都报 WORKBUDDY ❌，实际是：
1. 点完「立即领取」后加油站页关闭，重新导航时模板把聊天侧栏的
   「做任务赢积分好礼」促销卡误认成菜单项，点了没反应；
2. 验证步找不到「今日已领取」→ 假失败；
3. 收尾逻辑只认 close_process_on_finish，WorkBuddy fail=false 仍被强杀
   （它是用户日常聊天软件，AGENTS 明确禁止）。

本套件锁定：
- 步骤2/6 改为纯 OCR 定位「Buddy加油站」（无模板、OCR 开启、失败回退上一步）；
- WORKBUDDY / QODER 的 close_process_on_finish/fail 均为 false；
- _close_wool_flow 成功看 finish、失败看 fail，尊重配置。
"""

import json
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT.parent))

import game_auto.runtime.background_template_runner as btr  # noqa: E402
import game_auto.runtime.foreground_flow_runner as ffr  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def _load(name):
    return json.loads((PROJECT_ROOT / name).read_text(encoding="utf-8"))


def test_config():
    print("[1] 流程配置：纯 OCR 导航 + 不关闭软件")
    for name in ("flow_configs.example.json", "flow_configs.json"):
        path = PROJECT_ROOT / name
        if not path.is_file():
            continue
        cfg = _load(name)
        wb = cfg.get("WORKBUDDY") or {}
        steps = wb.get("steps") or []
        check(f"{name} WORKBUDDY 7 步", len(steps) == 7, len(steps))
        for idx, label in ((1, "步骤2"), (5, "步骤6")):
            s = steps[idx]
            check(f"{name} {label} 不再用模板", not s.get("template"), s.get("template"))
            check(f"{name} {label} OCR 定位 Buddy加油站", s.get("target_text") == "Buddy加油站")
            check(f"{name} {label} 启用 OCR", s.get("enable_ocr_fallback") is True)
            check(f"{name} {label} 失败回退上一步", s.get("on_fail") == "retry_prev")
        check(f"{name} WORKBUDDY 成功后不关程序", wb.get("close_process_on_finish") is False)
        check(f"{name} WORKBUDDY 失败后不关程序", wb.get("close_process_on_fail") is False)
        qoder = cfg.get("QODER") or {}
        check(f"{name} QODER 成功后不关程序", qoder.get("close_process_on_finish") is False)
        check(f"{name} QODER 失败后不关程序", qoder.get("close_process_on_fail") is False)


def test_close_logic():
    print("[2] 收尾逻辑：成功看 finish、失败看 fail")
    calls = []
    logger = logging.getLogger("test_wool")
    orig = ffr.close_flow_processes
    ffr.close_flow_processes = lambda logger, key, process_names=None: calls.append(key)
    try:
        cfg_wb = {"window_process_names": ["WorkBuddy.exe"], "close_process_on_finish": False, "close_process_on_fail": False}
        btr._close_wool_flow(logger, "WORKBUDDY", cfg_wb, True)
        btr._close_wool_flow(logger, "WORKBUDDY", cfg_wb, False)
        check("WorkBuddy 成功/失败都保留", calls == [], calls)

        cfg_mm = {"window_process_names": ["MiniMax.exe"], "close_process_on_finish": True, "close_process_on_fail": True}
        btr._close_wool_flow(logger, "MINIMAX", cfg_mm, True)
        btr._close_wool_flow(logger, "MINIMAX", cfg_mm, False)
        check("MiniMax 成功/失败都关闭", calls == ["MINIMAX", "MINIMAX"], calls)

        cfg_q = {"window_process_names": ["Qoder.exe"], "close_process_on_finish": False, "close_process_on_fail": False}
        btr._close_wool_flow(logger, "QODER", cfg_q, True)
        check("Qoder 成功保留", calls == ["MINIMAX", "MINIMAX"], calls)

        cfg_only_fail = {"window_process_names": ["X.exe"], "close_process_on_finish": True, "close_process_on_fail": False}
        btr._close_wool_flow(logger, "X", cfg_only_fail, False)
        check("失败且 fail=false 不关闭", calls == ["MINIMAX", "MINIMAX"], calls)
        btr._close_wool_flow(logger, "X", cfg_only_fail, True)
        check("成功且 finish=true 关闭", calls[-1] == "X", calls)
    finally:
        ffr.close_flow_processes = orig


def main():
    test_config()
    test_close_logic()
    src = (PROJECT_ROOT / "runtime" / "background_template_runner.py").read_text(encoding="utf-8")
    check("调用方把 ok 传给收尾", "_close_wool_flow(logger, flow_key, cfg, ok)" in src)
    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
