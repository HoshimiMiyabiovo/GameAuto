# -*- coding: utf-8 -*-
"""码道Agent Space（CodeArts Agent）每日领取回归（2026-11-16）。

背景：码道Agent Space（华为 CodeArts Agent，Electron）每日可领积分。用户三张
截图实锤页面模型：右上角标题栏「关闭/最大化/最小化」之后第 4 个个人图标 →
下拉面板**悬停**「活动福利」（不点击）→ 签到浮层（每日签到领100积分 +
[每日领取]）。**轨迹铁律**：浮层与面板是共享一条边的两个四边形（实测
面板左缘 x≈1538 = 浮层右缘 x≈1537，重叠段 y 143-430），鼠标必须从连接处
进浮层，走其他位置浮层整体消失——轨迹控制在 codearts_claim_daily 内建动作里。

本套件锁：
[1] flow_configs 双份配置静态断言（example 必查；本地文件存在才查镜像）：
    两步结构 / 复用窗口 / close 默认 false（薅羊毛收尾不感知复用窗口，开了
    会误杀用户会话，同 QODER）/ 第 2 步内建动作 + retry；
[2] 接线锁：WOOL_FLOW_KEYS 含 CODEARTS、GUI/WebUI 显示名 meta（结构化导入）、
    WoolFlow friendly_note、本机 flow_exe_paths 路径映射（存在才查）；
[3] 个人图标模板：尺寸锁（17x14，裁剪时避开右上角红点——红点领取后消失，
    模板含红点会导致已领态失配）+ 参考条带命中/无误命中（参考图在本机
    data/artifacts，CI 隐藏 data/ 后自动跳过）；
[4] codearts_claim 纯函数：popup_state 三态 / 并块按钮右对齐换算（用户截图
    实测值回归：标签+按钮并块时点击点落在按钮中心而非两块之间）；
[5] 轨迹常量：JUNCTION_ENTRY_DX（水平左移进层=从连接处过）、头像搜索区锁
    窗口顶部条带（设置面板「账号」行同款人形图标 0.997 误命中）。

跑法：.venv\\Scripts\\python.exe -m pytest tests\\suite_codearts_wool_20261116.py -q
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
import game_auto.runtime.codearts_claim as cclaim  # noqa: E402
import game_auto.services.launch_task_catalog as catalog  # noqa: E402
from game_auto.runtime.foreground_flow_runner import _BUILTIN_ACTIONS  # noqa: E402

FAILURES = []

STEP_NAMES = ["等待主界面", "点头像悬停活动福利并每日领取"]
STEP_ACTIONS = ["wait_template", "codearts_claim_daily"]

# 参考条带内个人图标模板期望命中位（条带从 (1550,0) 起，模板裁自 (1785,12)）
REF_EXPECTED_LOC = (235, 12)
REF_PATH = PROJECT_ROOT / "data" / "artifacts" / "ref_codearts_topbar_20261116.png"


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
        flow = cfg.get("CODEARTS")
        check(f"{name}: flow key 存在", flow is not None)
        if flow is None:
            continue
        check(f"{name}: enabled=true", flow.get("enabled") is True)
        check(f"{name}: 窗口标题含 CodeArts Agent（工作区名会变，标题尾缀最稳）",
              "CodeArts Agent" in (flow.get("window_title_keywords") or []))
        check(f"{name}: 进程名 codearts-agent.exe",
              "codearts-agent.exe" in (flow.get("window_process_names") or []))
        check(f"{name}: 已在跑就复用窗口（用户日常在用，不重拉进程）",
              flow.get("reuse_existing_window") is True)
        if required:
            # example 锁默认值：close 默认 false（收尾不感知复用窗口，误杀用户
            # 会话；同 QODER）。本地 flow_configs.json 是 GUI 可改的用户偏好
            # （2026-11-18 用户已勾选「完成后关闭」），不锁。
            check(f"{name}: close 默认 false（example；收尾不感知复用窗口，同 QODER）",
                  flow.get("close_process_on_finish") is False
                  and flow.get("close_process_on_fail") is False)
        else:
            check(f"{name}: close 标志存在（本地为用户 GUI 偏好，不锁值）",
                  "close_process_on_finish" in flow)
        steps = flow.get("steps") or []
        check(f"{name}: 两步且顺序锁定", [s.get("name") for s in steps] == STEP_NAMES,
              [s.get("name") for s in steps])
        check(f"{name}: 动作序列 等→内建动作", [s.get("action") for s in steps] == STEP_ACTIONS,
              [s.get("action") for s in steps])
        by_name = {s.get("name"): s for s in steps}
        wait_main = by_name.get("等待主界面") or {}
        check(f"{name}: 等主界面容忍冷启动（timeout≥60）", (wait_main.get("timeout_sec") or 0) >= 60)
        check(f"{name}: 主界面特征含 码道Agent Space",
              "码道Agent Space" in (wait_main.get("target_text") or []))
        check(f"{name}: 主界面等不到即失败（on_fail=fail）", wait_main.get("on_fail") == "fail")
        claim_step = by_name.get("点头像悬停活动福利并每日领取") or {}
        check(f"{name}: 第 2 步内建动作 codearts_claim_daily",
              claim_step.get("action") == "codearts_claim_daily")
        check(f"{name}: 内建动作失败可重试（retry_step）",
              claim_step.get("on_fail") == "retry_step" and (claim_step.get("max_attempts") or 0) >= 2)


def test_wiring():
    print("[2] 接线锁：WOOL_FLOW_KEYS / 显示名 meta / friendly_note / 路径映射")
    check("WOOL_FLOW_KEYS 含 CODEARTS", "CODEARTS" in btr.WOOL_FLOW_KEYS, btr.WOOL_FLOW_KEYS)
    check("WOOL_FLOW_KEYS 含 CODEARTS（2026-11-17 ASTUDIO 接入后让出末位，末位锁移交 astudio 套件）",
          "CODEARTS" in btr.WOOL_FLOW_KEYS, btr.WOOL_FLOW_KEYS)
    # 显示名 meta 结构化导入（AutomationView 是纯 mixin，不依赖 Qt 实例化）
    from game_auto.ui.bridge_views.automation import AutomationView
    check("GUI _WOOL_FLOW_META 含 CODEARTS",
          AutomationView._WOOL_FLOW_META.get("CODEARTS") == "码道Agent Space 每日领取",
          AutomationView._WOOL_FLOW_META.get("CODEARTS"))
    webui_src = (PROJECT_ROOT / "src/game_auto/ui/webui/api/wool.py").read_text(encoding="utf-8")
    check("WebUI meta 含 CODEARTS", '"CODEARTS"' in webui_src)
    note = str(catalog.STANDALONE_TASK_APPS.get("WoolFlow", {}).get("friendly_note") or "")
    check("WoolFlow friendly_note 提及 码道Agent Space", "码道Agent Space" in note, note)
    check("内建动作已登记 _BUILTIN_ACTIONS", "codearts_claim_daily" in _BUILTIN_ACTIONS,
          "codearts_claim_daily")
    check("codearts_claim.claim_daily 可导入", hasattr(cclaim, "claim_daily"))
    # 本机路径映射（app_config.json 不入库，存在才查）
    for app_cfg_path in (PROJECT_ROOT / "data" / "app_config.json", PROJECT_ROOT / "app_config.json"):
        if not app_cfg_path.exists():
            continue
        app_cfg = json.loads(app_cfg_path.read_text(encoding="utf-8"))
        exe = str((app_cfg.get("flow_exe_paths") or {}).get("CODEARTS") or "")
        check(f"flow_exe_paths.CODEARTS 已配（{app_cfg_path.name}）", bool(exe), exe)
        if exe:
            check("codearts-agent.exe 路径实际存在", os.path.exists(exe), exe)
        break


def test_template():
    print("[3] 个人图标模板：尺寸 / 参考条带命中与无误命中")
    tpl_path = Path(cclaim.AVATAR_TEMPLATE)
    check("模板文件存在", tpl_path.exists(), str(tpl_path))
    if not tpl_path.exists():
        return
    try:
        import cv2
        import numpy as np
    except ImportError:
        check("cv2/numpy 可用（跳过图像断言）", True)
        return
    import PIL.Image as pil

    tpl = np.array(pil.open(tpl_path).convert("L"))
    check("模板 17x14（裁剪避开右上角红点：红点领取后消失，含红点会已领态失配）",
          tpl.shape == (14, 17), tpl.shape)
    if not REF_PATH.exists():
        # CI 仿真探针会隐藏 data/：参考图缺失时跳过命中断言（模板本身已入库）
        check("参考条带缺失则跳过命中断言（CI 无 data/）", True)
        return
    strip = np.array(pil.open(REF_PATH).convert("L"))
    res = cv2.matchTemplate(strip, tpl, cv2.TM_CCOEFF_NORMED)
    _min, mx, _minl, loc = cv2.minMaxLoc(res)
    check(f"参考条带命中位置 = {REF_EXPECTED_LOC}", loc == REF_EXPECTED_LOC, loc)
    check(f"参考条带命中分数 ≥0.95（实测 {mx:.4f}）", mx >= 0.95, round(mx, 4))
    # 排除真命中邻域后的第二高分：同款人形图标（设置面板「账号」行）不在条带内，
    # 表情/齿轮/刷新图标不得 ≥0.9（顶条带误命中会点到别的控件）
    res_masked = res.copy()
    x0, y0 = REF_EXPECTED_LOC
    res_masked[max(0, y0 - 8):y0 + 8, max(0, x0 - 8):x0 + 8] = -1.0
    _m2, mx2, _l, _c = cv2.minMaxLoc(res_masked)
    check(f"条带内排除真命中后最高分 <0.9（实测 {mx2:.4f}）", mx2 < 0.9, round(mx2, 4))


def test_pure_functions():
    print("[4] codearts_claim 纯函数：popup_state / 并块按钮换算")
    find = cclaim.find_block
    check("find_block 命中含关键词的块",
          find([{"text": "每日签到领100积分"}, {"text": "每日领取"}], "每日领取") is not None)
    check("find_block 空结果/无命中 → None",
          find([], "每日领取") is None and find([{"text": "已完成与已隐藏活动"}], "每日领取") is None)

    state = cclaim.popup_state
    check("popup_state: 有每日领取 → claimable",
          state([{"text": "每日签到领100积分"}, {"text": "每日领取"}]) == "claimable")
    check("popup_state: 无按钮但有已领文字 → claimed",
          state([{"text": "今日已领"}]) == "claimed")
    check("popup_state: 两者皆无 → absent", state([{"text": "码道控制台"}]) == "absent")

    check("pending_subtitle_visible: 有待领取 → True",
          cclaim.pending_subtitle_visible([{"text": "您有权益待领取"}]) is True)
    check("pending_subtitle_visible: 无 → False",
          cclaim.pending_subtitle_visible([{"text": "码道控制台"}]) is False)

    pt = cclaim.claim_click_point
    # 精确块：中心即按钮（用户截图实测 每日领取 按钮文本中心 ≈(1480,315)）
    exact = {"text": "每日领取", "center": (1480, 315), "box": (1444, 304, 1516, 327)}
    check("精确块点击点 = 块中心", pt(exact) == (1480, 315), pt(exact))
    # 并块（OCR 把标签+按钮并成一块的实测形态）：box 为截图局部坐标、center 为
    # 屏幕绝对坐标（差一个区域原点）——换算必须用相对偏移，结果仍落按钮中心。
    merged = {"text": "每日签到领100积分每日领取", "center": (1384, 315), "box": (1252, 304, 1516, 327)}
    check("并块点击点 = 右对齐按钮中心 (1480,315)（不能落在标签与按钮之间）",
          pt(merged) == (1480, 315), pt(merged))
    check("缺 center 返回 None", pt({"text": "每日领取"}) is None)

    dx = cclaim.JUNCTION_ENTRY_DX
    check(f"JUNCTION_ENTRY_DX < 0（从「活动福利」水平左移进浮层=从连接处过，实测 {dx}）",
          dx < 0 and abs(dx) >= 60, dx)
    check("头像搜索区起点 ≥0.75（锁顶条带：设置面板「账号」行同款图标 0.997 误命中）",
          cclaim.AVATAR_REGION_X0_FRAC >= 0.75, cclaim.AVATAR_REGION_X0_FRAC)
    check("头像搜索区高度 ≤0.08（只盖标题栏，不伸进面板区）",
          cclaim.AVATAR_REGION_H_FRAC <= 0.08, cclaim.AVATAR_REGION_H_FRAC)


def main():
    test_flow_configs()
    test_wiring()
    test_template()
    test_pure_functions()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED {len(FAILURES)}: {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
