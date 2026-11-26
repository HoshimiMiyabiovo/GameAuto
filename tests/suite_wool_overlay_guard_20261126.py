# -*- coding: utf-8 -*-
"""WorkBuddy「成长计划」整屏遮罩兜底 + 立即领取页面特征门控（2026-11-26）。

事故（2026-11-26 21:03:56→21:05:14，失败记录 #514）：
1. 点完「立即领取」后 WorkBuddy 弹「WorkBuddy 成长计划」整屏遮罩，左侧头像菜单
   被压住；步骤6「重新点击菜单里的Buddy加油站」纯 OCR 找字连续 4 轮 0 命中，
   on_fail=retry_prev 回退的步骤5（头像模板）同样被遮罩盖住 → 死循环后失败。
2. 遮罩上的 ✕ 不带文字，步骤级 dismiss_texts（按按钮文字点）覆盖不到。
3. 步骤3/4 的 hy4-立即领取.png 只按"长得像"命中，没有页面上下文校验，
   商品/促销卡上的同款按钮也可能被闭眼点掉。

本套件锁定：
- 流程级 overlay_guard 的识别（归一化后包含匹配）+ 动作序列 + 预算语义；
- 模板命中必须同时看到页面特征（template_require_texts），否则丢弃；
- 两份 flow_configs 的 WORKBUDDY 接线与关闭模板存在；
- 失败当帧的 22 条 OCR 文字能命中遮罩特征（实锤回归）。
"""

import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import game_auto.runtime.foreground_flow_runner as ffr  # noqa: E402

FAILURES = []

# 失败截图（data/artifacts/flow_failures/20261126_210514_WORKBUDDY_重新点击菜单里的Buddy加_ocr.jpg）
# 当帧 OCR 的 22 条文字（标注图下方的清单，顺序/置信度原样）。
FAILURE_FRAME_TEXTS = [
    "编辑(E) 窗口(W) 帮助(H) 关于(A)",
    "奖品",
    "自12月1日起，成长计划积分有效期调整为7天（自然日），请及时使用积分",
    "WorkBuddy成长计划",
    "做任务攒能量，开盲盒解锁你的专属 Buddy ①查看活动规则",
    "我的 Buddy",
    "查看 Buddy图鉴>",
    "PC展示",
    "完成新手任务获取你的第一只专属",
    "Buddy，每次消耗10能量值可开启盲",
    "8",
    "开启盲盒 0/10",
    "未解锁",
    "未解锁",
    "未解锁",
    "限时连登抽奖距活动结束28天",
    "抽奖(0)",
    "本1已 45能量·+1张补登卡·+1次抽奖",
    "本月已连登4天",
    "查看",
    "任务",
    "任务徽章0",
]

# 正常聊天页（同一次运行 21:03:31 步骤2 的帧，含聊天侧栏促销卡宣传语）。
CHAT_PAGE_TEXTS = [
    "编辑(E) 窗口(W) 帮助(H) 关于(A)",
    "新建任务 BETA",
    "助理 BETA",
    "专家 技能连接器",
    "定时任务",
    "资料库",
    "发现",
    "做任务赢积分好礼",
    "Buddy加油站",
    "去邀约",
    "成长计划",
    "设置",
    "退出登录",
    "WorkBuddy,我帮你",
    "今天帮你做些什么？",
]


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def test_signature_matching():
    print("[1] 遮罩特征识别：实锤帧命中、正常帧不误报")
    patterns = ["WorkBuddy成长计划", "我的Buddy", "做任务攒能量"]
    hits = ffr._match_overlay_signatures(FAILURE_FRAME_TEXTS, patterns)
    check("实锤帧命中 3 条特征", len(hits) == 3, hits)
    check("空格拆字仍命中（我的 Buddy）", "我的Buddy" in hits, hits)
    check("聊天页促销卡（做任务赢积分好礼）不误报",
          ffr._match_overlay_signatures(CHAT_PAGE_TEXTS, patterns) == [],
          ffr._match_overlay_signatures(CHAT_PAGE_TEXTS, patterns))
    check("空模式不命中", ffr._match_overlay_signatures(FAILURE_FRAME_TEXTS, []) == [])
    check("空文字不命中", ffr._match_overlay_signatures([""], patterns) == [])


def test_guard_actions():
    print("[2] overlay_guard：动作顺序 / 预算 / 关闭判定")
    logger = logging.getLogger("test_overlay_guard")
    orig_hits = ffr._overlay_signature_hits
    orig_action = ffr._run_overlay_action
    orig_sleep = ffr._sleep_interruptible
    guard = {
        "texts": ["WorkBuddy成长计划", "我的Buddy", "做任务攒能量"],
        "require": 2,
        "actions": [{"type": "key", "key": "escape"}, {"type": "template", "template": "x.png"}],
        "settle_sec": 0.0,
        "max_dismiss": 2,
    }
    try:
        ffr._sleep_interruptible = lambda seconds, stop_check=None: True

        # 2.1 命中 → 第一个动作后消失
        seq = ["hits", "none"]
        calls = []
        ffr._overlay_signature_hits = lambda logger, hwnd, texts: (
            ["WorkBuddy成长计划", "我的Buddy"] if seq.pop(0) == "hits" else []
        )
        ffr._run_overlay_action = lambda *a, **k: calls.append(a[3].get("type")) or True
        state = {"count": 0}
        ok = ffr._dismiss_flow_overlay(logger, "WORKBUDDY", 1, guard, [1.0], [0.9],
                                       state=state)
        check("命中后按 ESC 关闭成功", ok is True, ok)
        check("只试了第一个动作", calls == ["key"], calls)
        check("预算计数 +1", state["count"] == 1, state)

        # 2.2 动作无效 → 继续试下一个 → 仍失败
        seq = ["hits", "hits", "hits"]
        calls = []
        ffr._overlay_signature_hits = lambda logger, hwnd, texts: (
            ["WorkBuddy成长计划", "我的Buddy"] if seq.pop(0) == "hits" else []
        )
        ffr._run_overlay_action = lambda *a, **k: calls.append(a[3].get("type")) or True
        state = {"count": 0}
        ok = ffr._dismiss_flow_overlay(logger, "WORKBUDDY", 1, guard, [1.0], [0.9], state=state)
        check("两个动作都试过", calls == ["key", "template"], calls)
        check("关不掉返回 False", ok is False, ok)
        check("关不掉也计一次预算", state["count"] == 1, state)

        # 2.3 特征条数不足 require → 不动作
        ffr._overlay_signature_hits = lambda logger, hwnd, texts: ["我的Buddy"]
        calls = []
        ffr._run_overlay_action = lambda *a, **k: calls.append(1) or True
        state = {"count": 0}
        ok = ffr._dismiss_flow_overlay(logger, "WORKBUDDY", 1, guard, [1.0], [0.9], state=state)
        check("命中条数 < require 不动作", ok is False and calls == [], (ok, calls))
        check("未命中不消耗预算", state["count"] == 0, state)

        # 2.4 预算耗尽 → 连扫描都不做
        scanned = []
        ffr._overlay_signature_hits = lambda logger, hwnd, texts: scanned.append(1) or [
            "WorkBuddy成长计划", "我的Buddy"
        ]
        state = {"count": 2}
        ok = ffr._dismiss_flow_overlay(logger, "WORKBUDDY", 1, guard, [1.0], [0.9], state=state)
        check("预算耗尽后不再扫描", ok is False and scanned == [], (ok, scanned))

        # 2.5 非 dict / 无特征配置 → 零开销
        check("guard=None 直接返回", ffr._dismiss_flow_overlay(logger, "X", 1, None, [1.0], [0.9]) is False)
        check("guard 空 texts 直接返回",
              ffr._dismiss_flow_overlay(logger, "X", 1, {"texts": []}, [1.0], [0.9]) is False)

        # 2.6 settle 期间被停止 → 不再判定
        seq = ["hits"]
        ffr._sleep_interruptible = lambda seconds, stop_check=None: False
        ffr._overlay_signature_hits = lambda logger, hwnd, texts: (
            ["WorkBuddy成长计划", "我的Buddy"] if seq else []
        )
        ffr._run_overlay_action = lambda *a, **k: True
        state = {"count": 0}
        ok = ffr._dismiss_flow_overlay(logger, "WORKBUDDY", 1, guard, [1.0], [0.9],
                                       stop_check=lambda: True, state=state)
        check("settle 被停止时返回 False", ok is False, ok)
    finally:
        ffr._overlay_signature_hits = orig_hits
        ffr._run_overlay_action = orig_action
        ffr._sleep_interruptible = orig_sleep


def test_template_gate():
    print("[3] 模板命中必须看到页面特征（立即领取不再闭眼点）")
    logger = logging.getLogger("test_template_gate")
    orig_tpl = ffr._locate_template_in_window
    orig_ocr = ffr._ocr_locate_labeled_in_window
    try:
        ffr._locate_template_in_window = lambda *a, **k: (229, 958)

        ffr._ocr_locate_labeled_in_window = lambda *a, **k: None
        center, method = ffr._locate_with_ocr_fallback(
            logger=logger, hwnd=1, template_path="x.png", target_text="立即领取",
            scales=[1.0], confidences=[0.9], enable_ocr=False,
            require_template_texts=["Buddy加油站", "每日可领"],
        )
        check("特征缺失时丢弃模板命中", center is None and method is None, (center, method))

        ffr._ocr_locate_labeled_in_window = lambda *a, **k: (50, 60)
        center, method = ffr._locate_with_ocr_fallback(
            logger=logger, hwnd=1, template_path="x.png", target_text="立即领取",
            scales=[1.0], confidences=[0.9], enable_ocr=False,
            require_template_texts=["Buddy加油站"],
        )
        check("特征在场时模板命中生效", center == (229, 958) and method == "template",
              (center, method))

        center, method = ffr._locate_with_ocr_fallback(
            logger=logger, hwnd=1, template_path="x.png", target_text="立即领取",
            scales=[1.0], confidences=[0.9], enable_ocr=False,
        )
        check("未配置特征时保持旧行为", center == (229, 958) and method == "template",
              (center, method))
    finally:
        ffr._locate_template_in_window = orig_tpl
        ffr._ocr_locate_labeled_in_window = orig_ocr


def test_config():
    print("[4] 两份 flow_configs 接线")
    import json

    tpl = PROJECT_ROOT / "assets" / "templates" / "hy4-成长计划关闭.png"
    check("关闭按钮模板存在", tpl.is_file(), tpl)
    for name in ("flow_configs.json", "flow_configs.example.json"):
        cfg = json.loads((PROJECT_ROOT / name).read_text(encoding="utf-8"))
        wb = cfg.get("WORKBUDDY") or {}
        guard = wb.get("overlay_guard") or {}
        check(f"{name} 配了 overlay_guard", isinstance(guard, dict) and bool(guard), guard)
        check(f"{name} 遮罩特征含成长计划", "WorkBuddy成长计划" in (guard.get("texts") or []),
              guard.get("texts"))
        check(f"{name} require >= 2", int(guard.get("require", 0)) >= 2, guard.get("require"))
        actions = guard.get("actions") or []
        kinds = [str(a.get("type")) for a in actions]
        check(f"{name} 先 ESC 再点 ✕ 模板", kinds[:2] == ["key", "template"], kinds)
        check(f"{name} ESC 动作键是 escape", actions[0].get("key") == "escape", actions[0])
        close_tpl = (actions[1].get("template") or "").replace("\\", "/")
        check(f"{name} ✕ 模板指向新裁的关闭键", close_tpl.endswith("hy4-成长计划关闭.png"), close_tpl)
        check(f"{name} ✕ 只用 1.0 尺度", actions[1].get("scales") == [1.0], actions[1].get("scales"))
        check(f"{name} 有预算上限", int(guard.get("max_dismiss", 0)) >= 1, guard.get("max_dismiss"))
        steps = wb.get("steps") or []
        check(f"{name} 仍是 7 步", len(steps) == 7, len(steps))
        for idx, label in ((2, "步骤3"), (3, "步骤4")):
            step = steps[idx]
            req = step.get("template_require_texts") or []
            anchor = step.get("ocr_anchor") or {}
            check(f"{name} {label} 模板要求加油站页面特征",
                  "Buddy加油站" in req, req)
            check(f"{name} {label} OCR 锚点 required",
                  anchor.get("required") is True and "Buddy加油站" in (anchor.get("texts") or []),
                  anchor)
            check(f"{name} {label} 开标注图取证",
                  step.get("ocr_debug_snapshot") is True, step.get("ocr_debug_snapshot"))

    src = (PROJECT_ROOT / "src" / "game_auto" / "runtime" / "foreground_flow_runner.py").read_text(encoding="utf-8")
    check("run_flow 步骤循环调用遮罩兜底", "_dismiss_flow_overlay(" in src)
    check("流程级 overlay_guard 生效", 'step.get("overlay_guard", cfg.get("overlay_guard"))' in src)
    check("模板门控参数已透传", "require_template_texts=step_template_require_texts or None" in src)


def main():
    test_signature_matching()
    test_guard_actions()
    test_template_gate()
    test_config()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
