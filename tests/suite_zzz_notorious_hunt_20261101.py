# -*- coding: utf-8 -*-
"""绝区零恶名狩猎（每周三次）回归（2026-11-01）。

锁定：
1. 周门控按「周一 04:00 刷新」算：周一 01:00 仍属上一周（否则会把新一周跳过）；
2. 「本周核心材料掉落次数 N/3」能解析，已打满时一次都不打；
3. 战斗识别在真实截图上分得开「大招可用 0.82 / 灰 0.60」；
4. 闪光分类器能加载并推理（模型缺失时降级不崩）；
5. 流程各步失败都判失败、不留菜单；
6. dispatch / 内建动作 / 批次调度都已接线；
7. 配置里的流程模板文件真实存在。

跑法：.venv\\Scripts\\python.exe tests\\suite_zzz_notorious_hunt_20261101.py
"""

import logging
import sys
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import game_auto.runtime.background_template_runner as btr  # noqa: E402
import game_auto.runtime.foreground_flow_runner as ffr  # noqa: E402
import game_auto.runtime.task_batch_runner as tbr  # noqa: E402
import game_auto.runtime.zzz_battle_assist as zb  # noqa: E402
import game_auto.runtime.zzz_notorious_hunt as nh  # noqa: E402

FAILURES = []
LOG = logging.getLogger("suite")

SHOT_DIR = Path(r"C:\Users\admin\.workbuddy\clipboard-images")
SHOT_7 = SHOT_DIR / "clipboard-2026-11-01T13-28-22-118Z-28d13b89.jpg"   # 00:02 大招亮
SHOT_8 = SHOT_DIR / "clipboard-2026-11-01T13-28-22-120Z-e74701ce.jpg"   # 00:24 大招灰


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name}  {detail}")
        FAILURES.append(name)


def load_shot(path):
    if not path.is_file():
        return None
    try:
        import numpy as np
        from PIL import Image
    except ImportError:
        return None
    return np.array(Image.open(path).convert("RGB"))


class FakeFlow:
    """假流程：按「当前页面」返回 OCR 文本，点击推进状态。"""

    def __init__(self):
        self.page = "world"     # world / menu / training / list / detail / team / battle
        self.clicks = []
        self.keys = []
        self.holds = []
        self.ocr_texts = []
        self.times = (0, 3)
        self.enter_ok = True

    def texts(self, logger, hwnd):
        mapping = {
            "world": [],
            "menu": [{"text": "训练", "confidence": 0.9, "center": (1000, 170)},
                     {"text": "作战", "confidence": 0.9, "center": (1200, 170)}],
            "training": [{"text": "恶名狩猎", "confidence": 0.9, "center": (200, 700)},
                         {"text": "实战模拟室", "confidence": 0.9, "center": (200, 560)}],
            "list": [{"text": "库萨里库", "confidence": 0.9, "center": (600, 600)},
                     {"text": f"本周核心材料掉落次数：{self.times[0]}/{self.times[1]}",
                      "confidence": 0.9, "center": (700, 290)}],
            "detail": [{"text": "库萨里库", "confidence": 0.9, "center": (600, 300)},
                       {"text": "下一步", "confidence": 0.9, "center": (1700, 1000)}],
            "team": [{"text": "出战", "confidence": 0.9, "center": (1750, 1020)}],
            "battle": [{"text": "00:02.54", "confidence": 0.9, "center": (1750, 80)}],
            "result": [{"text": "完成", "confidence": 0.9, "center": (1700, 1030)}],
            "reward": [{"text": "周期挑战", "confidence": 0.9, "center": (500, 200)},
                       {"text": "全部领取", "confidence": 0.9, "center": (1300, 360)}],
            "claimed": [{"text": "已领取", "confidence": 0.9, "center": (1300, 360)}],
        }
        return list(mapping.get(self.page, []))

    def click(self, hwnd, frac, logger, note=""):
        self.clicks.append(tuple(frac))
        if frac == nh.P_TRAINING_TAB:
            self.page = "training"
        elif frac == nh.P_HUNT_ENTRY:
            self.page = "list"
        elif frac == nh.P_GO:
            self.page = "detail"
        elif frac == nh.P_NEXT:
            self.page = "team"
        elif frac == nh.P_START:
            self.page = "battle"
        elif frac == nh.P_REWARD_ENTRY:
            self.page = "reward"
        elif frac == nh.P_CLAIM_ALL:
            self.page = "claimed"

    def press(self, hwnd, key):
        self.keys.append(key)
        if key == "f2":
            self.page = "menu"

    def hold(self, hwnd, key, seconds):
        self.holds.append((key, seconds))


def patch_flow(stack, flow, enter_ok=True):
    def fake_wait_text(logger, hwnd, keywords, timeout_sec, stop_check=None, min_conf=0.5, region_frac=None):
        items = flow.texts(logger, hwnd)
        return nh._find_text(items, keywords, min_conf=min_conf, region_frac=region_frac, hwnd=hwnd)

    stack.enter_context(patch.object(nh, "_texts", lambda logger, hwnd: flow.texts(logger, hwnd)))
    stack.enter_context(patch.object(nh, "_click_frac", flow.click))
    stack.enter_context(patch.object(nh, "_press_key", flow.press))
    stack.enter_context(patch.object(nh, "_hold_key", flow.hold))
    stack.enter_context(patch.object(nh, "_wait_text", fake_wait_text))
    stack.enter_context(patch.object(nh, "_sleep", lambda *a, **k: None))
    stack.enter_context(patch.object(ffr, "_window_rect", lambda hwnd: (0, 0, 1920, 1080)))
    stack.enter_context(patch.object(ffr, "_click_window_local", lambda *a, **k: None))
    # 进战斗：直接切到 battle 页
    if enter_ok:
        stack.enter_context(
            patch.object(nh, "_enter_battle", lambda *a, **k: (True, "已进入战斗"))
        )
    else:
        stack.enter_context(
            patch.object(nh, "_enter_battle", lambda *a, **k: (False, "没看到库萨里库详情页"))
        )


class FakeAssist:
    instances = []

    def __init__(self, hwnd, logger=None, **kw):
        self.hwnd = hwnd
        FakeAssist.instances.append(self)

    def run(self, stop_check=None, timeout_sec=300):
        return zb.END_WIN, "普攻按钮消失 7 帧，战斗画面结束（帧 100 普攻 50）"


def case_week_key():
    print("[1] 周门控：周一 04:00 才算新的一周")
    check("周日 23:00 属本周", btr.weekly_flow_week_key(datetime(2026, 11, 1, 23, 0)) == "2026-W44",
          btr.weekly_flow_week_key(datetime(2026, 11, 1, 23, 0)))
    check("周一 01:00 仍属上一周", btr.weekly_flow_week_key(datetime(2026, 11, 2, 1, 0)) == "2026-W44",
          btr.weekly_flow_week_key(datetime(2026, 11, 2, 1, 0)))
    check("周一 05:00 进新周", btr.weekly_flow_week_key(datetime(2026, 11, 2, 5, 0)) == "2026-W45",
          btr.weekly_flow_week_key(datetime(2026, 11, 2, 5, 0)))
    check("meta key 格式", btr.weekly_flow_meta_key("ZZZ_NOTORIOUS_HUNT") == "weekly_flow:ZZZ_NOTORIOUS_HUNT")


def case_parse_times():
    print("[2] 「本周核心材料掉落次数 N/3」解析")
    flow = FakeFlow()
    with ExitStack() as stack:
        stack.enter_context(patch.object(nh, "_texts", lambda logger, hwnd: flow.texts(logger, hwnd)))
        stack.enter_context(patch.object(ffr, "_window_rect", lambda hwnd: (0, 0, 1920, 1080)))
        flow.page = "list"
        flow.times = (0, 3)
        check("0/3 -> (0,3)", nh.read_remaining_times(LOG, 1) == (0, 3), nh.read_remaining_times(LOG, 1))
        flow.times = (2, 3)
        check("2/3 -> (2,3)", nh.read_remaining_times(LOG, 1) == (2, 3))
        flow.texts = lambda logger, hwnd: [{"text": "本周核心材料掉落次数：3/3", "confidence": 0.9,
                                            "center": (700, 290)}]
        check("3/3 -> (3,3)", nh.read_remaining_times(LOG, 1) == (3, 3))
        flow.texts = lambda logger, hwnd: []
        check("读不到 -> None", nh.read_remaining_times(LOG, 1) is None)


def case_battle_recognition():
    print("[3] 战斗识别（真实截图两帧对比）")
    img7, img8 = load_shot(SHOT_7), load_shot(SHOT_8)
    if img7 is None or img8 is None:
        print("  SKIP 缺少战斗截图")
        return
    ba = zb.ZzzBattleAssist(hwnd=0)
    check("图7 在战斗画面", ba.in_battle(img7) is True)
    check("图7 终结技可用（Q 亮）", ba.ultimate_ready(img7) is True)
    check("图8 终结技不可用（Q 灰）", ba.ultimate_ready(img8) is False)
    check("特殊技两帧都可用", ba.special_ready(img7) and ba.special_ready(img8))
    check("连携默认关闭", zb.ZzzBattleAssist(hwnd=0).enable_chain is False)


def case_flash_model():
    print("[4] 闪光分类器")
    model = PROJECT_ROOT / zb.FLASH_MODEL_DIR / zb.FLASH_MODEL_NAME
    check("模型文件存在", model.is_file(), str(model))
    if not model.is_file():
        return
    fc = zb.FlashClassifier(str(model))
    check("加载成功", fc.load() is True)
    img = load_shot(SHOT_7)
    if img is not None:
        idx = fc.run(img)
        check("推理返回合法类别", idx in (-1, 0, 1, 2), idx)
    missing = zb.FlashClassifier(str(PROJECT_ROOT / "data/models/not_exist.onnx"))
    check("模型缺失时降级返回 -1", missing.run(img) == -1)


def case_full_skip():
    print("[5] 本周已打满 3/3 -> 一次都不打")
    flow = FakeFlow()
    flow.page = "list"
    flow.times = (3, 3)
    with ExitStack() as stack:
        patch_flow(stack, flow)
        ok, msg = nh.run_notorious_hunt(LOG, 1, {}, scales=[1.0], confidences=[0.8])
    check("成功返回", ok is True, msg)
    check("摘要写明已打完", "已打完" in msg, msg)
    check("没点前往", nh.P_GO not in flow.clicks, flow.clicks)


def case_full_three_times():
    print("[6] 读不到次数 -> 按每周 3 次执行")
    flow = FakeFlow()
    flow.page = "list"
    flow.texts_original = flow.texts

    def texts_no_times(logger, hwnd):
        return [i for i in flow.texts_original(logger, hwnd) if "核心材料" not in i["text"]]

    flow.texts = texts_no_times
    FakeAssist.instances.clear()
    with ExitStack() as stack:
        patch_flow(stack, flow)
        stack.enter_context(patch.object(zb, "ZzzBattleAssist", FakeAssist))
        ok, msg = nh.run_notorious_hunt(LOG, 1, {"weekly_times": 3}, scales=[1.0], confidences=[0.8])
    check("成功返回", ok is True, msg)
    check("打了 3 场", len(FakeAssist.instances) == 3, len(FakeAssist.instances))
    check("每场都 W 走 2 秒", all(h[0] == "w" and abs(h[1] - 2.0) < 1e-6 for h in flow.holds), flow.holds)
    check("走完了训练→恶名狩猎导航",
          nh.P_TRAINING_TAB in flow.clicks and nh.P_HUNT_ENTRY in flow.clicks, flow.clicks)


def case_enter_fail():
    print("[7] 进战斗失败 -> 判失败并退回大世界")
    flow = FakeFlow()
    flow.page = "list"
    flow.times = (0, 3)
    with ExitStack() as stack:
        patch_flow(stack, flow, enter_ok=False)
        ok, msg = nh.run_notorious_hunt(LOG, 1, {}, scales=[1.0], confidences=[0.8])
    check("判失败", ok is False, msg)
    check("原因写明进战斗失败", "进战斗失败" in msg, msg)
    check("退出了菜单", "escape" in flow.keys, flow.keys)


def case_dispatch():
    print("[8] 接线：内建动作 + dispatch + 批次调度")
    check("登记为内建动作", "zzz_notorious_hunt" in ffr._BUILTIN_ACTIONS)
    check("批次有每周流程表", tbr._WEEKLY_FLOW_BY_APP.get("ZenlessZoneZero", ("", ""))[0] == "ZZZ_NOTORIOUS_HUNT")
    check("有执行入口", "ZZZ_NOTORIOUS_HUNT" in tbr._WEEKLY_RUNNERS)

    kwargs = dict(
        logger=LOG, flow_key="ZZZ_NOTORIOUS_HUNT", step={"name": "恶名狩猎每周挑战"},
        name="恶名狩猎每周挑战", step_no=3, action="zzz_notorious_hunt",
        hwnd=1, pid=2, scales=[1.0], confidences=[0.8], stop_check=None,
    )
    with patch.object(nh, "run_notorious_hunt", lambda *a, **k: (True, "完成 3/3 次，已全部领取")):
        ok, msg, _h, _p = ffr._dispatch_step_action(**kwargs)
    check("成功放行", ok is True, msg)
    with patch.object(nh, "run_notorious_hunt", lambda *a, **k: (False, "没看到库萨里库详情页")):
        ok, msg, _h, _p = ffr._dispatch_step_action(**kwargs)
    check("失败冒泡带原因", ok is False and "步骤失败" in msg, msg)


def case_weekly_gate():
    print("[9] 批次每周门控：本周跑过就不再跑")
    calls = []

    class Ctl:
        stop_current_task = False
        logger = LOG

    with ExitStack() as stack:
        stack.enter_context(patch.object(tbr, "is_weekly_flow_done", lambda key: True))
        stack.enter_context(
            patch.dict(tbr._WEEKLY_RUNNERS, {"ZZZ_NOTORIOUS_HUNT": lambda *a, **k: calls.append(1) or (True, "x")})
        )
        result = tbr._run_pending_weekly_flow(Ctl(), "ZenlessZoneZero")
    check("本周已跑 -> 返回 None", result is None, result)
    check("没有真的执行", calls == [], calls)

    with ExitStack() as stack:
        stack.enter_context(patch.object(tbr, "is_weekly_flow_done", lambda key: False))
        stack.enter_context(patch.object(tbr, "_run_pending_weekly_flow", tbr._run_pending_weekly_flow))
        stack.enter_context(
            patch.dict(tbr._WEEKLY_RUNNERS, {"ZZZ_NOTORIOUS_HUNT": lambda *a, **k: (False, "❌ 超时")})
        )
        stack.enter_context(
            patch("game_auto.services.game_update_calendar.get_maintenance_state", lambda app: None)
        )
        result = tbr._run_pending_weekly_flow(Ctl(), "ZenlessZoneZero")
    check("本周没跑 -> 返回报告行", result is not None and result[1] is False, result)


def case_config():
    print("[10] 配置与资源")
    import json

    data = json.loads((PROJECT_ROOT / "flow_configs.example.json").read_text(encoding="utf-8"))
    flow = data.get("ZZZ_NOTORIOUS_HUNT")
    check("流程存在", isinstance(flow, dict), list(data)[:6])
    if not isinstance(flow, dict):
        return
    steps = flow.get("steps", [])
    actions = [s.get("action") for s in steps]
    check("步骤含 zzz_notorious_hunt", "zzz_notorious_hunt" in actions, actions)
    check("开局复用场景校正（zzz_enter_main）", actions[0] == "zzz_enter_main", actions[:3])
    tpl = PROJECT_ROOT / "assets/templates/绝区零-主界面-电台.png"
    check("主界面模板存在", tpl.is_file(), tpl)
    for name in ("绝区零-战斗-普通攻击.png", "绝区零-战斗-特殊技1.png", "绝区零-战斗-大招1.png"):
        check(f"战斗模板 {name} 存在", (PROJECT_ROOT / "assets" / "templates" / name).is_file())


def case_rotation():
    print("[11] 异常队轮转（蕾米埃尔/菲欧妮/维琳娜 三属性轮流站场）")
    ba = zb.ZzzBattleAssist(hwnd=0)
    check("闪避键 = shift（ZZZ 默认）", ba.dodge_key == "shift", ba.dodge_key)
    check("切人键 = space（ZZZ 默认）", ba.switch_key == "space", ba.switch_key)
    check("默认开启轮转", ba.enable_rotation is True)
    check("默认 6 秒一换", abs(ba.dwell_sec - 6.0) < 1e-6, ba.dwell_sec)

    pressed = []
    ba._press = lambda key, hold=0.03: pressed.append(key)
    ba.do_switch()
    ba.do_switch()
    check("切人按空格（不是 1/2/3）", pressed == ["space", "space"], pressed)
    ba.do_dodge()
    check("闪避按 shift（不是空格）", pressed[-1] == "shift", pressed)
    check("切人计数 2 次", ba.stats["switches"] == 2, ba.stats["switches"])
    check("切人后重置站场计时", ba._dwell_start > 0)

    custom = zb.ZzzBattleAssist(hwnd=0, dodge_key="r", switch_key="c")
    check("键位可覆盖（改过键位的用户）", custom.dodge_key == "r" and custom.switch_key == "c")

    off = zb.ZzzBattleAssist(hwnd=0, enable_rotation=False)
    check("可关闭轮转（单角色站场）", off.enable_rotation is False)


def case_config_team():
    print("[12] 配置里的配队参数")
    import json

    data = json.loads((PROJECT_ROOT / "flow_configs.example.json").read_text(encoding="utf-8"))
    step = next(s for s in data["ZZZ_NOTORIOUS_HUNT"]["steps"] if s.get("action") == "zzz_notorious_hunt")
    check("闪避键配置 = shift", step.get("dodge_key") == "shift", step.get("dodge_key"))
    check("切人键配置 = space", step.get("switch_key") == "space", step.get("switch_key"))
    check("轮转开启", step.get("enable_rotation") is True)
    check("dwell_sec 合法", isinstance(step.get("dwell_sec"), (int, float)) and 1 <= step["dwell_sec"] <= 20,
          step.get("dwell_sec"))


def case_state_bars():
    print("[13] 角色状态条识别（燔焰/风华/浮晖）")
    import numpy as np

    import game_auto.runtime.zzz_agent_state as zas

    rect = (584, 97, 631, 99)   # 菲欧妮 2 号位
    empty = np.zeros((1080, 1920, 3), dtype=np.uint8)
    check("全黑 -> 0", zas.read_bar(empty, rect, (4, 128, 255), (9, 128, 55), 120) == 0)

    full = np.zeros((1080, 1920, 3), dtype=np.uint8)
    full[:, :] = (255, 60, 30)   # 近似燔焰的橙红
    check("整条命中 -> 满值 120", zas.read_bar(full, rect, (4, 128, 255), (9, 128, 55), 120) == 120)

    half = np.zeros((1080, 1920, 3), dtype=np.uint8)
    half[:, 584:607] = (255, 60, 30)   # 只填左半条
    got = zas.read_bar(half, rect, (4, 128, 255), (9, 128, 55), 120)
    check("半条 -> 约 60", got is not None and 50 <= got <= 70, got)

    check("位置 1/2/3 映射到正确角色",
          zas.agent_at_position(1) == "remielle"
          and zas.agent_at_position(2) == "phoenix"
          and zas.agent_at_position(3) == "velina")
    check("切下一位循环", zas.next_position(1) == 2 and zas.next_position(3) == 1)
    check("三人阈值照抄 OneDragon",
          (zas.PHOENIX_BURST_AT, zas.VELINA_HIGH_AT, zas.REMIELLE_FULL_AT) == (90, 85, 100))


def case_state_decide():
    print("[14] 状态驱动出招")
    import time as _t

    import numpy as np

    import game_auto.runtime.zzz_agent_state as zas

    screen = np.zeros((1080, 1920, 3), dtype=np.uint8)
    ba = zb.ZzzBattleAssist(hwnd=0)
    ba.special_ready = lambda s: False
    ba._dwell_start = _t.time()      # 刚切过，兜底分支不会立刻触发

    def decide(pos, value):
        ba._position = pos
        with patch.object(zas, "read_agent_state", lambda s, a, p: value):
            return ba._decide(screen)

    check("菲欧妮 燔焰 95 -> 焚罪", decide(2, 95) == "phoenix_burst", decide(2, 95))
    check("菲欧妮 燔焰 10 -> 切人", decide(2, 10) == "switch", decide(2, 10))
    check("菲欧妮 燔焰 50 -> 普攻攒", decide(2, 50) in ("attack", "special", "wait"), decide(2, 50))

    check("维琳娜 风华 90 -> 长按普攻", decide(3, 90) == "velina_hold", decide(3, 90))
    check("维琳娜 风华 10 -> 切人", decide(3, 10) == "switch", decide(3, 10))

    check("蕾米埃尔 浮晖 100 -> 长按强化特殊技", decide(1, 100) == "remielle_hold", decide(1, 100))
    check("蕾米埃尔 浮晖 5 -> 切人", decide(1, 5) == "switch", decide(1, 5))

    ba._position = 1
    ba._dwell_start = _t.time() - 100     # 站太久了
    with patch.object(zas, "read_agent_state", lambda s, a, p: None):
        check("读不到状态 -> 兜底轮转切人", ba._decide(screen) == "switch", ba._decide(screen))


def main():
    logging.basicConfig(level=logging.CRITICAL)
    print("=" * 60)
    case_week_key()
    case_parse_times()
    case_battle_recognition()
    case_flash_model()
    case_full_skip()
    case_full_three_times()
    case_enter_fail()
    case_dispatch()
    case_weekly_gate()
    case_config()
    case_rotation()
    case_config_team()
    case_state_bars()
    case_state_decide()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} 项 → {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
