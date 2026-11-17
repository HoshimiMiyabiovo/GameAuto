# -*- coding: utf-8 -*-
"""AStudio 每日福利领取回归（2026-11-17）。

背景：AStudio（AI 工作台，Electron）左下角有「限时每日福利」卡片（今日可领
100积分 + 黑底 [领取] 按钮）。用户口径「启动后直接领」——全羊毛集合里唯一
零导航流程（对比 QODER/CODEARTS 要点头像/悬停轨迹）。实现镜像 codearts_claim
先例：内建动作 astudio_claim_daily，全 OCR 零模板，OCR 三态 fail-closed 核验。

本套件锁：
[1] flow_configs 双份配置静态断言（example 必查；本地文件存在才查镜像）：
    两步结构 / close 默认 true（TELEAGENT 同款：跑完关，防残留窗口挡下一个
    羊毛任务）/ 第 2 步内建动作 + retry；
[2] 接线锁：WOOL_FLOW_KEYS 含 ASTUDIO 且排末位（后接入的不打断既有顺序）、
    GUI/WebUI 显示名 meta（结构化导入）、WoolFlow friendly_note、本机
    flow_exe_paths 路径映射（存在才查）；
[3] astudio_claim 纯函数：popup_state 三态（已领取含「领取」子串必须先判
    claimed，顺序反了会把已领态再点一次）/ 按钮块选择（精确块优先、并块须带
    卡片锚点词）/ 并块按钮右对齐换算（box 截图局部坐标 + center 屏幕绝对
    坐标的坐标系坑）/ 左下角区域闸（右上块不得混入）；
[4] 区域/核验常量：卡片搜索区锁窗口左下角、claimed 候选词含「已领」覆盖
    已领取/今日已领 变体且不误伤未领态副标题「今日可领100积分」。

跑法：.venv\\Scripts\\python.exe -m pytest tests\\suite_astudio_wool_20261117.py -q
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

import game_auto.runtime.astudio_claim as aclaim  # noqa: E402
import game_auto.runtime.background_template_runner as btr  # noqa: E402
import game_auto.services.launch_task_catalog as catalog  # noqa: E402
from game_auto.runtime.foreground_flow_runner import _BUILTIN_ACTIONS  # noqa: E402

FAILURES = []

STEP_NAMES = ["等待主界面", "点击领取限时每日福利"]
STEP_ACTIONS = ["wait_template", "astudio_claim_daily"]


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
        flow = cfg.get("ASTUDIO")
        check(f"{name}: flow key 存在", flow is not None)
        if flow is None:
            continue
        check(f"{name}: enabled=true", flow.get("enabled") is True)
        check(f"{name}: 窗口标题关键词 AStudio",
              (flow.get("window_title_keywords") or []) == ["AStudio"],
              flow.get("window_title_keywords"))
        check(f"{name}: 进程名 AStudio.exe",
              "AStudio.exe" in (flow.get("window_process_names") or []),
              flow.get("window_process_names"))
        check(f"{name}: close 默认 true（TELEAGENT 同款：跑完关防残留窗口挡下一个羊毛任务）",
              flow.get("close_process_on_finish") is True
              and flow.get("close_process_on_fail") is True)
        steps = flow.get("steps") or []
        check(f"{name}: 两步且顺序锁定", [s.get("name") for s in steps] == STEP_NAMES,
              [s.get("name") for s in steps])
        check(f"{name}: 动作序列 等→内建动作", [s.get("action") for s in steps] == STEP_ACTIONS,
              [s.get("action") for s in steps])
        by_name = {s.get("name"): s for s in steps}
        wait_main = by_name.get("等待主界面") or {}
        check(f"{name}: 等主界面容忍冷启动（timeout≥60）", (wait_main.get("timeout_sec") or 0) >= 60)
        check(f"{name}: 主界面特征含 AStudio / 把任务交给我 双候选",
              "AStudio" in (wait_main.get("target_text") or [])
              and "把任务交给我" in (wait_main.get("target_text") or []),
              wait_main.get("target_text"))
        check(f"{name}: 主界面等不到即失败（on_fail=fail）", wait_main.get("on_fail") == "fail")
        claim_step = by_name.get("点击领取限时每日福利") or {}
        check(f"{name}: 第 2 步内建动作 astudio_claim_daily",
              claim_step.get("action") == "astudio_claim_daily")
        check(f"{name}: 内建动作失败可重试（retry_step）",
              claim_step.get("on_fail") == "retry_step" and (claim_step.get("max_attempts") or 0) >= 2)


def test_wiring():
    print("[2] 接线锁：WOOL_FLOW_KEYS / 显示名 meta / friendly_note / 路径映射")
    check("WOOL_FLOW_KEYS 含 ASTUDIO", "ASTUDIO" in btr.WOOL_FLOW_KEYS, btr.WOOL_FLOW_KEYS)
    check("WOOL_FLOW_KEYS 含 ASTUDIO（2026-11-17 DUMATE 接入后让出末位，末位锁移交 dumate 套件）",
          "ASTUDIO" in btr.WOOL_FLOW_KEYS, btr.WOOL_FLOW_KEYS)
    # 显示名 meta 结构化导入（AutomationView 是纯 mixin，不依赖 Qt 实例化）
    from game_auto.ui.bridge_views.automation import AutomationView
    check("GUI _WOOL_FLOW_META 含 ASTUDIO",
          AutomationView._WOOL_FLOW_META.get("ASTUDIO") == "AStudio 每日福利领取",
          AutomationView._WOOL_FLOW_META.get("ASTUDIO"))
    webui_src = (PROJECT_ROOT / "src/game_auto/ui/webui/api/wool.py").read_text(encoding="utf-8")
    check("WebUI meta 含 ASTUDIO", '"ASTUDIO"' in webui_src)
    note = str(catalog.STANDALONE_TASK_APPS.get("WoolFlow", {}).get("friendly_note") or "")
    check("WoolFlow friendly_note 提及 AStudio", "AStudio" in note, note)
    check("内建动作已登记 _BUILTIN_ACTIONS", "astudio_claim_daily" in _BUILTIN_ACTIONS,
          "astudio_claim_daily")
    check("astudio_claim.claim_daily 可导入", hasattr(aclaim, "claim_daily"))
    # 本机路径映射（app_config.json 不入库，存在才查）
    for app_cfg_path in (PROJECT_ROOT / "data" / "app_config.json", PROJECT_ROOT / "app_config.json"):
        if not app_cfg_path.exists():
            continue
        app_cfg = json.loads(app_cfg_path.read_text(encoding="utf-8"))
        exe = str((app_cfg.get("flow_exe_paths") or {}).get("ASTUDIO") or "")
        check(f"flow_exe_paths.ASTUDIO 已配（{app_cfg_path.name}）", bool(exe), exe)
        if exe:
            check("AStudio.exe 路径实际存在", os.path.exists(exe), exe)
        break


def test_pure_functions():
    print("[3] astudio_claim 纯函数：popup_state / 按钮块选择 / 并块换算 / 区域闸")
    state = aclaim.popup_state
    check("popup_state: 有独立「领取」块 → claimable",
          state([{"text": "限时每日福利"}, {"text": "今日可领100积分"}, {"text": "领取"}]) == "claimable")
    check("popup_state: 无按钮但有已领文字 → claimed",
          state([{"text": "今日已领100积分"}]) == "claimed")
    # 顺序铁律：「已领取」也含「领取」子串，claimed 必须先于 claimable 判
    check("popup_state: 已领取 块判 claimed（不能因含「领取」子串判 claimable）",
          state([{"text": "已领取"}]) == "claimed")
    check("popup_state: 明日再来 → claimed", state([{"text": "明日再来"}]) == "claimed")
    check("popup_state: 未领态副标题「今日可领100积分」不判 claimed（可领≠已领）",
          state([{"text": "今日可领100积分"}]) == "absent")
    check("popup_state: 两者皆无 → absent", state([{"text": "新建任务"}]) == "absent")

    btn = aclaim.claim_button_block
    check("按钮块: 独立「领取」精确块优先",
          btn([{"text": "限时每日福利今日可领100积分领取"}, {"text": "领取"}]) is not None
          and aclaim._norm_text(btn([{"text": "限时每日福利今日可领100积分领取"}, {"text": "领取"}]).get("text")) == "领取")
    check("按钮块: 并块（带卡片锚点词）可选中",
          btn([{"text": "限时每日福利今日可领100积分领取"}]) is not None)
    check("按钮块: 含「领取」但无卡片锚点词的长文本块不选（防误判）",
          btn([{"text": "完成任务领取奖励"}]) is None)
    check("按钮块: 未领态副标题（可领，无「领取」子串）不选",
          btn([{"text": "今日可领100积分"}]) is None)
    check("按钮块: 空结果 → None", btn([]) is None and btn(None) is None)

    pt = aclaim.claim_click_point
    # 精确块：中心即按钮（用户截图实测 1358×843 窗口按钮中心 ≈(211,737)）
    exact = {"text": "领取", "center": (211, 737), "box": (186, 710, 237, 765)}
    check("精确块点击点 = 块中心", pt(exact) == (211, 737), pt(exact))
    # 并块（OCR 把副标题+按钮并成一块的兜底形态）：box 为截图局部坐标、center 为
    # 屏幕绝对坐标（差一个区域原点）——换算必须用相对偏移，结果仍落按钮中心。
    # center 取 box 中点（OCR 自洽数据）：公式退化为 x2 - inset，与区域原点无关。
    merged = {"text": "今日可领100积分领取", "center": (134, 737), "box": (31, 710, 237, 765)}
    check("并块点击点 = 右对齐按钮中心（不能落在副标题与按钮之间）",
          pt(merged) == (237 - aclaim.MERGED_BUTTON_INSET, 737), pt(merged))
    check("缺 center 返回 None", pt({"text": "领取"}) is None)

    region = aclaim.card_region_blocks
    rect = (0, 0, 1358, 843)
    check("区域闸: 左下角卡片块保留",
          region([{"text": "限时每日福利", "center": (88, 717)}], rect)
          == [{"text": "限时每日福利", "center": (88, 717)}])
    check("区域闸: 右上/中央块排除（侧栏 logo AStudio 在 y<0.55h）",
          region([{"text": "AStudio", "center": (679, 300)}], rect) == [])
    check("区域闸: rect 为 None 返回空", region([{"text": "领取", "center": (88, 717)}], None) == [])


def test_constants():
    print("[4] 区域/核验常量")
    check("卡片搜索区 x ≤ 0.4w（卡片钉左下角，实测 1358 窗口卡片区 x≤0.184）",
          0.2 <= aclaim.CARD_REGION_X_MAX_FRAC <= 0.5, aclaim.CARD_REGION_X_MAX_FRAC)
    check("卡片搜索区 y ≥ 0.5h（实测 1358×843 卡片区 y≥0.83，留余量防最大化窗口漏检）",
          0.5 <= aclaim.CARD_REGION_Y_MIN_FRAC <= 0.8, aclaim.CARD_REGION_Y_MIN_FRAC)
    claimed = aclaim.CLAIMED_TEXTS
    check("claimed 候选含「已领」（覆盖 已领取/今日已领 变体）", "已领" in claimed, claimed)
    check("claimed 候选含「明日再来」（常见已领态文案）", "明日再来" in claimed, claimed)
    check("未领态副标题不被 claimed 候选误伤（今日可领100积分 不含任何候选词）",
          not any(k in "今日可领100积分" for k in claimed), claimed)
    check("卡片锚点词含「限时每日福利」（标题全串）与「今日可领」（漏字/并块容忍）",
          "限时每日福利" in aclaim.CARD_ANCHOR_TEXTS and "今日可领" in aclaim.CARD_ANCHOR_TEXTS,
          aclaim.CARD_ANCHOR_TEXTS)
    check(f"并块右缘内缩 >0 且 < 按钮半宽量级（按钮宽约50px，实测 {aclaim.MERGED_BUTTON_INSET}）",
          0 < aclaim.MERGED_BUTTON_INSET < 40, aclaim.MERGED_BUTTON_INSET)
    check("幂等等待预算 15-60s（太短误判已领、太长拖垮薅羊毛整批）",
          15 <= aclaim.POPUP_WAIT_SEC <= 60, aclaim.POPUP_WAIT_SEC)


def main():
    test_flow_configs()
    test_wiring()
    test_pure_functions()
    test_constants()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED {len(FAILURES)}: {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
