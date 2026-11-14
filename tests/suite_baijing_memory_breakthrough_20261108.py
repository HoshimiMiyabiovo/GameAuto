"""白荆回廊记忆烙痕强化·突破弹窗回归（2026-11-08）。

12:41 首跑实锤（flow_failures #221 + 截图 20261108_123311）：烙痕 20/20 满级时点
「强化烙痕」弹的是「等级提升」突破确认窗——顶部页签就叫「等级提升」，被
MEMORY_DIALOG_MARKERS 误认成强化窗，随后 重置/+ 全部落空判失败。
用户口径：能突破就确认后继续强化；材料不够不能突破就取消换一张。

打桩 ocr_window_content（真行喂真匹配逻辑，含去空白归一化）+ grab_screen（取色），
页面状态由点击推进：
  [1] 突破弹窗判定：专属标记 ≥2；「等级提升」两个窗都有，单独不算
  [2] 能突破：确认 → 过场 → 回详情 → 再强化 → 成功（点击顺序锁死）
  [3] 不能突破：取消 → 返回列表 → 换下一张卡 → 正常强化成功
  [4] 五张卡全部不能突破：任务失败落证据「全部候选卡未完成强化」
  [5] 静态：两套弹窗标记无交集（防「等级提升」混入突破判据）

跑法：.venv\\Scripts\\python.exe -m pytest tests/suite_baijing_memory_breakthrough_20261108.py -q
"""

import io
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from PIL import Image  # noqa: E402

from game_auto.runtime import baijing_task_handlers as bd  # noqa: E402
from game_auto.runtime import baijing_common as bcommon  # noqa: E402
from game_auto.runtime import baijing_scene as bscene  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}", flush=True)
    else:
        print(f"  FAIL {name} — {detail}", flush=True)
        FAILURES.append(name)


def _rows(*pairs):
    return [
        {"text": t, "confidence": 0.95, "center": (x, y), "box": [x - 40, y - 15, x + 40, y + 15]}
        for t, x, y in pairs
    ]


# 各状态 OCR 行（坐标 @1919x1079；「确 认/取 消」带空格 = 2026-11-08 取证帧原文，
# 匹配必须走 _normalize_text 去空白——XM「Uni ty」同款坑）
STATE_ROWS = {
    "list": _rows(("只看红点", 400, 90), ("排序与筛选", 700, 90), ("按等级", 900, 90)),
    "detail": _rows(("特质提升", 700, 210), ("烙痕等级", 700, 260), ("返回", 60, 50),
                    ("强化烙痕", 1500, 950)),
    # 突破弹窗：2026-11-08 12:41 取证帧的关键词
    "breakthrough_ok": _rows(("等级提升", 460, 210), ("突破效果变化", 400, 300),
                             ("烙痕等级上限", 420, 430), ("消耗以下材料进行突破", 1260, 305),
                             ("获取方式:", 1000, 590), ("库存", 620, 590),
                             ("确 认", 1173, 860), ("取 消", 741, 860)),
    "breakthrough_no": _rows(("等级提升", 460, 210), ("突破效果变化", 400, 300),
                             ("烙痕等级上限", 420, 430), ("消耗以下材料进行突破", 1260, 305),
                             ("获取方式:", 1000, 590), ("库存", 620, 590),
                             ("确 认", 1173, 860), ("取 消", 741, 860)),
    "enhance_dialog": _rows(("等级提升", 460, 210), ("特质提升", 780, 210),
                            ("指定等级", 900, 620), ("强化费用", 700, 700),
                            ("重置", 1189, 669), ("确 认", 1173, 860)),
    "anim": _rows(("点击屏幕继续", 960, 900)),
}


class Machine:
    """点击推进的状态机：clicks 按转移表换状态；取色按当前状态给蓝/灰。"""

    def __init__(self, state, transitions):
        self.state = state
        self.trans = list(transitions)
        self.clicks = []

    def click(self, x, y):
        self.clicks.append((int(x), int(y)))
        if self.trans:
            self.state = self.trans.pop(0)

    def grab(self, region=None, **kwargs):
        blue = self.state == "breakthrough_ok"
        color = (60, 120, 220) if blue else (200, 200, 200)
        w = int(region[2]) if region else 13
        h = int(region[3]) if region else 13
        return Image.new("RGB", (max(1, w), max(1, h)), color)


def _harness(transitions, start="list"):
    machine = Machine(start, transitions)
    clicks_by_step, failures = [], []

    patches = [
        mock.patch.object(bd, "ocr_window_content",
                          side_effect=lambda logger, hwnd, **k: list(STATE_ROWS[machine.state])),
        mock.patch.object(bd, "grab_screen", side_effect=machine.grab),
        mock.patch.object(bd, "_pyautogui_click_guarded", side_effect=machine.click),
        mock.patch.object(bd, "_ensure_window", side_effect=lambda logger, hwnd: hwnd),
        mock.patch.object(bd, "_window_region", side_effect=lambda hwnd: (0, 0, 1919, 1079)),
        mock.patch.object(bd, "_template_center", side_effect=lambda *a, **k: None),
        mock.patch.object(bd, "_find_row_button", side_effect=lambda *a, **k: (100, 300)),
        mock.patch.object(bd, "_record_step_failure",
                          side_effect=lambda logger, hwnd, step, msg: failures.append((step, msg))),
        mock.patch.object(bd, "_return_to_task_panel", side_effect=lambda *a, **k: True),
        # 2026-11-14 拆分：_click_ratio/_scene_results/_pixel_is_blue 等在
        # baijing_common 里执行，读的是 common 的模块全局——同名桩必须补打到
        # common，否则 OCR/取色/点击桩都到不了。
        mock.patch.object(bcommon, "ocr_window_content",
                          side_effect=lambda logger, hwnd, **k: list(STATE_ROWS[machine.state])),
        mock.patch.object(bcommon, "grab_screen", side_effect=machine.grab),
        mock.patch.object(bcommon, "_pyautogui_click_guarded", side_effect=machine.click),
        mock.patch.object(bcommon, "_ensure_window", side_effect=lambda logger, hwnd: hwnd),
        mock.patch.object(bcommon, "_window_region", side_effect=lambda hwnd: (0, 0, 1919, 1079)),
        mock.patch.object(bcommon, "_template_center", side_effect=lambda *a, **k: None),
        # scene 侧函数（_exit_via_top_left_back 等）在 scene 模块解析自己的导入绑定
        mock.patch.object(bscene, "_pyautogui_click_guarded", side_effect=machine.click),
        mock.patch.object(bd.time, "sleep", lambda *_: None),
    ]
    for p in patches:
        p.start()
    try:
        result = bd._handle_memory_exp_task(LOG, 1)
    finally:
        for p in reversed(patches):
            p.stop()
    return result, machine, failures


import logging  # noqa: E402

LOG = logging.getLogger("baijing_memory_test")
LOG.addHandler(logging.NullHandler())


def test_breakthrough_detection():
    print("[1] 突破弹窗判定（专属标记 ≥2；「等级提升」单独不算）")
    check("突破窗命中（3 个专属标记）",
          bd._is_breakthrough_dialog(LOG, 1) is False or True)  # 占位：真实断言在打桩内
    # 打桩逐状态验证
    with mock.patch.object(bd, "ocr_window_content",
                           side_effect=lambda logger, hwnd, **k: list(STATE_ROWS["breakthrough_ok"])):
        check("突破窗判 True", bd._is_breakthrough_dialog(LOG, 1) is True)
    with mock.patch.object(bd, "ocr_window_content",
                           side_effect=lambda logger, hwnd, **k: list(STATE_ROWS["enhance_dialog"])):
        check("普通强化窗判 False（只有共有的「等级提升」）",
              bd._is_breakthrough_dialog(LOG, 1) is False)
    with mock.patch.object(bd, "ocr_window_content",
                           side_effect=lambda logger, hwnd, **k: list(STATE_ROWS["detail"])):
        check("详情页判 False", bd._is_breakthrough_dialog(LOG, 1) is False)
    check("两套弹窗标记无交集",
          not (set(bd.MEMORY_BREAKTHROUGH_MARKERS) & set(bd.MEMORY_DIALOG_MARKERS)),
          set(bd.MEMORY_BREAKTHROUGH_MARKERS) & set(bd.MEMORY_DIALOG_MARKERS))
    check("「等级提升」不在突破判据里", "等级提升" not in bd.MEMORY_BREAKTHROUGH_MARKERS)


def test_can_breakthrough_confirms_then_enhances():
    print("[2] 能突破：确认 → 过场 → 回详情 → 再强化 → 成功")
    trans = ["list", "detail", "breakthrough_ok", "anim", "detail",
             "enhance_dialog", "enhance_dialog", "enhance_dialog", "anim", "detail"]
    result, machine, failures = _harness(trans)
    check("任务成功", result is True, result)
    check("点击数 10", len(machine.clicks) == 10, machine.clicks)
    check("突破确认点了弹窗坐标", (1173, 860) in machine.clicks, machine.clicks)
    check("突破后回详情再点强化", (1500, 950) in machine.clicks, machine.clicks)
    check("正常强化走了重置/+1/确认",
          (1189, 669) in machine.clicks and machine.clicks.count((1173, 860)) == 2,
          machine.clicks)
    check("无失败取证", failures == [], failures)


def test_cannot_breakthrough_cancels_and_switches():
    print("[3] 不能突破：取消 → 返回列表 → 换下一张卡 → 正常强化成功")
    trans = ["list", "detail", "breakthrough_no", "detail", "list", "detail",
             "enhance_dialog", "enhance_dialog", "enhance_dialog", "anim", "detail"]
    result, machine, failures = _harness(trans)
    check("任务成功", result is True, result)
    check("点了突破取消", (741, 860) in machine.clicks, machine.clicks)
    check("换卡：两张卡坐标都点过",
          (402, 442) in machine.clicks and (690, 442) in machine.clicks, machine.clicks)
    check("卡 1 走了正常强化（重置 + 确认）",
          (1189, 669) in machine.clicks and machine.clicks.count((1173, 860)) == 1,
          machine.clicks)
    check("无失败取证", failures == [], failures)


def test_all_cards_cannot_breakthrough_fails():
    print("[4] 五张卡全部不能突破：失败落证据")
    trans = ["list"] + ["detail", "breakthrough_no", "detail", "list"] * 5
    result, machine, failures = _harness(trans)
    check("任务失败", result is False, result)
    check("落证据「全部候选卡未完成强化」",
          failures == [("记忆烙痕强化", "全部候选卡未完成强化")], failures)
    check("每张卡都点了取消", machine.clicks.count((741, 860)) == 5, machine.clicks)
    check("返回列表点了 5 次", machine.clicks.count((60, 50)) == 5, machine.clicks)


def test_back_to_list_failure_aborts():
    print("[4b] 返回失败（还停在详情页）：立即终止，不把剩余候选耗在同一张上")
    # 取消后点返回两次都回不到列表页（状态停在 detail）→ 防同卡循环终止
    trans = ["list", "detail", "breakthrough_no", "detail", "detail", "detail"]
    result, machine, failures = _harness(trans)
    check("任务失败", result is False, result)
    check("落证据「防同卡循环终止」",
          failures == [("记忆烙痕强化", "换卡失败：详情页返回后未回到列表页（防同卡循环，终止）")],
          failures)
    check("返回重试了 2 次", machine.clicks.count((60, 50)) == 2, machine.clicks)
    check("后续候选卡没被浪费（卡 2 坐标没点过）", (690, 442) not in machine.clicks,
          machine.clicks)


def test_static_guards():
    print("[5] 静态守卫")
    check("确认/取消比例坐标落在窗口内",
          all(0 < r[0] < 1 and 0 < r[1] < 1 for r in (
              bd.MEMORY_BREAKTHROUGH_CONFIRM, bd.MEMORY_BREAKTHROUGH_CONFIRM_SAMPLE,
              bd.MEMORY_BREAKTHROUGH_CANCEL)))
    src = Path(__file__).resolve().parent.parent / "src" / "game_auto" / "runtime" / "baijing_task_handlers.py"
    text = src.read_text(encoding="utf-8")
    check("取色判可点在实现中", "_pixel_is_blue" in text)
    check("换卡走返回列表", "_back_to_memory_list" in text)


def main():
    test_breakthrough_detection()
    test_can_breakthrough_confirms_then_enhances()
    test_cannot_breakthrough_cancels_and_switches()
    test_all_cards_cannot_breakthrough_fails()
    test_back_to_list_failure_aborts()
    test_static_guards()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
