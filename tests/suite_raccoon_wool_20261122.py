# -*- coding: utf-8 -*-
"""商汤小浣熊「每日打开即关闭」薅羊毛回归（2026-11-22）。

背景：用户要求把开始菜单快捷方式
`C:\\ProgramData\\Microsoft\\Windows\\Start Menu\\Programs\\商汤小浣熊.lnk`
加进薅羊毛集合，「每天打开一次再关闭就行了」。奖励口径是**打开过**，
不需要点任何按钮——和 DUMATE（启动五秒后退出）同族，但风险完全不同：
小浣熊桌面端可能正被用户使用，甚至**本自动化本身就跑在它里面**（当前会话的
祖先进程链：box-agent-acp → 商汤小浣熊.exe）。按进程名批量关（羊毛集合默认
收尾策略）会把宿主一起杀掉，所以这条流程走内建动作 `wool_open_close_app`，
按 pid 精确关闭 + 三道安全闸。

本套件锁：
[1] 配置契约：双份 flow_configs 的 RACCOON 存在/启用/窗口关键词/进程名/
    单步内建动作/停留与年龄参数；**close_process_on_finish/fail 必须为 false**
    （否则收尾按进程名开杀）。
[2] 安全闸行为（真函数 + 桩）：
    - 复用既有实例 → 不关闭、直接成功（今天已打开过）；
    - 目标 PID 是自身祖先进程 → 不关闭；
    - PID 存活超过 max_process_age_sec → 不关闭；
    - 正常路径 → 先 WM_CLOSE，超时后按 pid 强制结束；
    - 停留期间目标自行退出 → 成功且不再关闭；
    - 没有 pid → 失败（fail-closed，不假装成功）。
[3] 接线锁：WOOL_FLOW_KEYS 含 RACCOON 且排末位、GUI/WebUI 显示名 meta、
    friendly_note、.lnk 解析、内建动作已登记。
[4] 本机路径映射（app_config.json 存在才查）：RACCOON 指向的 .lnk/exe 存在。

跑法：.venv\\Scripts\\python.exe -m pytest tests\\suite_raccoon_wool_20261122.py -q
"""

import io
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import game_auto.runtime.background_template_runner as btr  # noqa: E402
import game_auto.runtime.foreground_flow_runner as ffr  # noqa: E402
import game_auto.runtime.wool_app_session as was  # noqa: E402
import game_auto.services.launch_task_catalog as catalog  # noqa: E402

FAILURES = []

STEP_NAMES = ["打开即关闭"]


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
        flow = cfg.get("RACCOON")
        check(f"{name}: flow key 存在", flow is not None)
        if flow is None:
            continue
        check(f"{name}: enabled=true", flow.get("enabled") is True)
        check(f"{name}: 窗口标题关键词 商汤小浣熊",
              (flow.get("window_title_keywords") or []) == ["商汤小浣熊"],
              flow.get("window_title_keywords"))
        check(f"{name}: 进程名 商汤小浣熊.exe",
              "商汤小浣熊.exe" in (flow.get("window_process_names") or []),
              flow.get("window_process_names"))
        check(f"{name}: reuse_existing_window=true（已在跑=今天已打开过，不重拉）",
              flow.get("reuse_existing_window") is True)
        # 核心安全约束：集合收尾是按进程名批量清理的，置 true 会连宿主一起杀。
        check(f"{name}: close_process_on_finish=false（关闭由内建动作按 pid 精确做）",
              flow.get("close_process_on_finish") is False,
              flow.get("close_process_on_finish"))
        check(f"{name}: close_process_on_fail=false（失败也不按名清理宿主）",
              flow.get("close_process_on_fail") is False,
              flow.get("close_process_on_fail"))
        check(f"{name}: 单步且名称锁定",
              [s.get("name") for s in (flow.get("steps") or [])] == STEP_NAMES,
              [s.get("name") for s in (flow.get("steps") or [])])
        step = (flow.get("steps") or [{}])[0]
        check(f"{name}: 步骤动作=wool_open_close_app",
              step.get("action") == "wool_open_close_app", step.get("action"))
        check(f"{name}: hold_sec 为正（窗口起来后还要停留一会儿才关）",
              float(step.get("hold_sec") or 0) > 0, step.get("hold_sec"))
        check(f"{name}: max_process_age_sec 已配（既有实例不关的年龄闸）",
              float(step.get("max_process_age_sec") or 0) > 0, step.get("max_process_age_sec"))
        check(f"{name}: 步骤没有硬编码模板/OCR（零点击流程）",
              not step.get("template") and not step.get("target_text"), step)


def _run_once(**kwargs):
    """用桩替换真实关闭动作，只观察 open_close_app_once 的决策。"""
    calls = {"graceful": 0, "force": 0}
    # 默认桩：新进程、非宿主、存活但 WM_CLOSE 后仍未退出（走到强制结束兜底）。
    # 各闸用例通过 kwargs 覆盖这些开关，避免"外层 patch 被内层默认桩覆盖"。
    ancestor = kwargs.pop("ancestor", False)
    age = kwargs.pop("age", 30.0)
    alive_after_close = kwargs.pop("alive_after_close", True)
    held_exit = kwargs.pop("held_exit", True)
    defaults = dict(
        logger=_logger(),
        flow_key="RACCOON",
        hwnd=1234,
        pid=4321,
        reused_window=False,
        step={"hold_sec": 0, "close_timeout_sec": 0, "max_process_age_sec": 600},
    )
    defaults.update(kwargs)
    with patch.object(was, "_request_graceful_close", lambda *a, **k: calls.__setitem__("graceful", calls["graceful"] + 1) or True), \
            patch.object(was, "_force_close_tree", lambda *a, **k: calls.__setitem__("force", calls["force"] + 1) or True), \
            patch.object(was, "_sleep_watch", lambda *a, **k: held_exit), \
            patch.object(was, "_process_alive", lambda pid: alive_after_close), \
            patch.object(was, "_process_age_sec", lambda pid: age), \
            patch.object(was, "_pid_is_self_ancestor", lambda pid: ancestor):
        ok, msg = was.open_close_app_once(**defaults)
    return ok, msg, calls


def _logger():
    import logging
    log = logging.getLogger("GameAutomation.test_raccoon")
    log.addHandler(logging.NullHandler())
    log.propagate = False
    return log


def test_safety_gates():
    print("[2] 三道安全闸 + 正常路径（真函数 + 桩）")

    # 闸1：执行前已有实例 → 不碰进程
    ok, msg, calls = _run_once(reused_window=True)
    check("复用既有实例 → 成功且不关闭", ok is True and calls == {"graceful": 0, "force": 0}, (msg, calls))
    check("复用实例的说明写明无需打开", "无需打开" in msg, msg)

    # 跨天未关的旧会话：只提示，不动进程
    with patch.object(was, "_created_before_today", lambda pid: True):
        ok, msg, calls = _run_once(reused_window=True)
    check("跨天旧实例 → 仍不关闭（只提示手动重启）",
          ok is True and calls == {"graceful": 0, "force": 0}, (msg, calls))

    # 闸2：目标是宿主祖先进程（本自动化跑在它里面）
    ok, msg, calls = _run_once(ancestor=True)
    check("目标是宿主机进程 → 成功且不关闭", ok is True and calls == {"graceful": 0, "force": 0}, (msg, calls))
    check("宿主拦截留了可读说明", "宿主" in msg, msg)

    # 闸3：进程本来就活着（超过年龄闸）
    ok, msg, calls = _run_once(age=3600.0)
    check("既有老进程 → 成功且不关闭", ok is True and calls == {"graceful": 0, "force": 0}, (msg, calls))
    check("年龄闸说明写明既有实例", "既有实例" in msg, msg)

    # 正常路径：WM_CLOSE → 等不到退出 → 强制结束
    ok, msg, calls = _run_once()
    check("本轮新进程 → 先发 WM_CLOSE 再兜底强杀",
          ok is True and calls["graceful"] == 1 and calls["force"] == 1, (msg, calls))
    check("成功说明写明已打开并关闭", "已打开" in msg, msg)

    # 优雅退出成功则不再强杀
    ok, msg, calls = _run_once(alive_after_close=False)
    check("WM_CLOSE 后进程已退出 → 不做强制结束",
          ok is True and calls["graceful"] == 1 and calls["force"] == 0, (msg, calls))

    # 停留期间自行退出（Electron 单实例交接）
    ok, msg, calls = _run_once(held_exit=False, pid=4321,
                              step={"hold_sec": 1, "close_timeout_sec": 0, "max_process_age_sec": 600})
    check("停留期间自行退出 → 成功且不再发关闭",
          ok is True and calls == {"graceful": 0, "force": 0}, (msg, calls))
    check("自行退出有说明", "自行退出" in msg, msg)

    # fail-closed：没有 pid
    ok, msg, calls = _run_once(pid=None)
    check("没有进程句柄 → 失败（不假装成功）", ok is False, msg)


def test_wiring():
    print("[3] 接线锁：WOOL_FLOW_KEYS / 显示名 meta / friendly_note / .lnk 解析")
    check("WOOL_FLOW_KEYS 含 RACCOON", "RACCOON" in btr.WOOL_FLOW_KEYS, btr.WOOL_FLOW_KEYS)
    check("RACCOON 排末位（后接入的不打断既有顺序）",
          btr.WOOL_FLOW_KEYS[-1] == "RACCOON", btr.WOOL_FLOW_KEYS)
    from game_auto.ui.bridge_views.automation import AutomationView
    check("GUI _WOOL_FLOW_META 含 RACCOON",
          AutomationView._WOOL_FLOW_META.get("RACCOON") == "商汤小浣熊 每日打开",
          AutomationView._WOOL_FLOW_META.get("RACCOON"))
    webui_src = (PROJECT_ROOT / "src/game_auto/ui/webui/api/wool.py").read_text(encoding="utf-8")
    check("WebUI meta 含 RACCOON", '"RACCOON"' in webui_src)
    note = str(catalog.STANDALONE_TASK_APPS.get("WoolFlow", {}).get("friendly_note") or "")
    check("WoolFlow friendly_note 提及商汤小浣熊", "商汤小浣熊" in note, note)
    check("内建动作已登记", "wool_open_close_app" in ffr._BUILTIN_ACTIONS)
    check("GUI 路径配置页有 RACCOON 行",
          "flow_exe.RACCOON" in (PROJECT_ROOT / "src/game_auto/ui/bridge_views/setup.py").read_text(encoding="utf-8"))

    # .lnk 解析：Popen 不认快捷方式，必须落到真实 exe
    lnk = r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs\商汤小浣熊.lnk"
    if os.path.exists(lnk):
        target = ffr._resolve_shortcut_target(lnk)
        check("本机 .lnk 能解析成 exe", target.lower().endswith(".exe") and os.path.exists(target), target)
    else:
        check("本机无该 .lnk，跳过解析验证（真机专用）", True)
    check("非 .lnk 路径原样返回", ffr._resolve_shortcut_target(r"C:\a\b.exe") == r"C:\a\b.exe")


def test_local_path():
    print("[4] 本机路径映射（app_config.json 不入库，存在才查）")
    for app_cfg_path in (PROJECT_ROOT / "app_config.json", PROJECT_ROOT / "data" / "app_config.json"):
        if not app_cfg_path.exists():
            continue
        app_cfg = json.loads(app_cfg_path.read_text(encoding="utf-8"))
        exe = str((app_cfg.get("flow_exe_paths") or {}).get("RACCOON") or "")
        check(f"flow_exe_paths.RACCOON 已配（{app_cfg_path.name}）", bool(exe), exe)
        if exe:
            check("RACCOON 入口路径实际存在", os.path.exists(exe), exe)
        break


def main():
    test_flow_configs()
    test_safety_gates()
    test_wiring()
    test_local_path()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED {len(FAILURES)}: {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
