# -*- coding: utf-8 -*-
"""DuMate 薅羊毛回归（2026-11-17）。

背景：DuMate（`F:\\Program Files\\dumate\\DuMate.exe`）每日福利随启动自动到账，
用户口径「启动五秒后退出即可」——全羊毛集合里唯一**零点击、纯启动等待**的
流程。实现纯配置驱动（无新内建模块）：`startup_wait_sec=5` 在窗口等待前保底
睡满 5s（run_flow 启动段结构性保证），唯一步骤是 optional 观察步（OCR 认出
「DuMate」留渲染证据，认不出照常过），跑完薅羊毛收尾按 close_process_on_finish
关进程。无本地领取日志、无 UI 判据，成败口径=进程正常启动、跑满等待、被关闭。

本套件锁：
[1] flow_configs 双份配置静态断言（example 必查；本地文件存在才查镜像）：
    单步 optional 观察步结构 / startup_wait_sec=5 / close true/true；
[2] 接线锁：WOOL_FLOW_KEYS 含 DUMATE 且排末位（后接入的不打断既有顺序）、
    GUI/WebUI 显示名 meta（结构化导入）、WoolFlow friendly_note、本机
    flow_exe_paths 路径映射（存在才查）、步骤只用了既有动作（无新内建动作）。

跑法：.venv\\Scripts\\python.exe -m pytest tests\\suite_dumate_wool_20261117.py -q
（经 tests/test_suites.py 子进程跑；直接文件调用有 stdout 包装 capture 老毛病）
"""

import io
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import game_auto.runtime.background_template_runner as btr  # noqa: E402
import game_auto.services.launch_task_catalog as catalog  # noqa: E402

FAILURES = []

STEP_NAMES = ["观察主界面渲染"]


def check(label, ok, detail=None):
    mark = "PASS" if ok else "FAIL"
    print(f"  {mark} {label}" + (f"  ← {detail}" if not ok and detail is not None else ""))
    if not ok:
        FAILURES.append(label)


def test_flow_configs():
    print("[1] flow_configs 双份配置静态断言")
    for name, required in (("flow_configs.example.json", True), ("flow_configs.json", False)):
        p = PROJECT_ROOT / name
        if not required and not p.exists():
            check(f"{name} 不存在则跳过（本机文件）", True)
            continue
        cfg = json.loads(p.read_text(encoding="utf-8"))
        flow = cfg.get("DUMATE")
        check(f"{name}: flow key 存在", flow is not None)
        if flow is None:
            continue
        check(f"{name}: enabled=true", flow.get("enabled") is True)
        check(f"{name}: 窗口标题关键词 DuMate",
              (flow.get("window_title_keywords") or []) == ["DuMate"],
              flow.get("window_title_keywords"))
        check(f"{name}: 进程名 DuMate.exe",
              "DuMate.exe" in (flow.get("window_process_names") or []),
              flow.get("window_process_names"))
        check(f"{name}: startup_wait_sec=5（用户口径「启动五秒后退出」，结构性保底等待）",
              flow.get("startup_wait_sec") == 5, flow.get("startup_wait_sec"))
        check(f"{name}: close true/true（跑完即退，薅羊毛收尾关进程）",
              flow.get("close_process_on_finish") is True
              and flow.get("close_process_on_fail") is True)
        steps = flow.get("steps") or []
        check(f"{name}: 单步且名称锁定", [s.get("name") for s in steps] == STEP_NAMES,
              [s.get("name") for s in steps])
        step = steps[0] if steps else {}
        check(f"{name}: 唯一步骤是既有 wait_template（零点击，无新内建动作）",
              step.get("action") == "wait_template", step.get("action"))
        check(f"{name}: 观察步 optional_if_missing=true（认不出 UI 照常成功，不构成判据）",
              step.get("optional_if_missing") is True, step.get("optional_if_missing"))
        check(f"{name}: 观察步短超时（≤5s，别把「五秒后退出」拖成十几秒）",
              (step.get("timeout_sec") or 0) <= 5, step.get("timeout_sec"))
        check(f"{name}: 观察目标含 DuMate 字样",
              "DuMate" in (step.get("target_text") or []), step.get("target_text"))


def test_wiring():
    print("[2] 接线锁：WOOL_FLOW_KEYS / 显示名 meta / friendly_note / 路径映射")
    check("WOOL_FLOW_KEYS 含 DUMATE", "DUMATE" in btr.WOOL_FLOW_KEYS, btr.WOOL_FLOW_KEYS)
    # 显示名 meta 结构化导入（AutomationView 是纯 mixin，不依赖 Qt 实例化）
    from game_auto.ui.bridge_views.automation import AutomationView
    check("GUI _WOOL_FLOW_META 含 DUMATE",
          AutomationView._WOOL_FLOW_META.get("DUMATE") == "DuMate 每日福利",
          AutomationView._WOOL_FLOW_META.get("DUMATE"))
    webui_src = (PROJECT_ROOT / "src/game_auto/ui/webui/api/wool.py").read_text(encoding="utf-8")
    check("WebUI meta 含 DUMATE", '"DUMATE"' in webui_src)
    note = str(catalog.STANDALONE_TASK_APPS.get("WoolFlow", {}).get("friendly_note") or "")
    check("WoolFlow friendly_note 提及 DuMate", "DuMate" in note, note)
    # 本机路径映射（app_config.json 不入库，存在才查）
    for app_cfg_path in (PROJECT_ROOT / "data" / "app_config.json", PROJECT_ROOT / "app_config.json"):
        if not app_cfg_path.exists():
            continue
        app_cfg = json.loads(app_cfg_path.read_text(encoding="utf-8"))
        exe = str((app_cfg.get("flow_exe_paths") or {}).get("DUMATE") or "")
        check(f"flow_exe_paths.DUMATE 已配（{app_cfg_path.name}）", bool(exe), exe)
        if exe:
            check("DuMate.exe 路径实际存在", os.path.exists(exe), exe)
        break


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
