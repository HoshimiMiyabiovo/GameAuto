"""ZZZ 场景登记表（zzz_enter_main）回归（2026-11-08）。

背景：#219/#220——OneDragon 异常收尾后画面停在功能菜单，后勤商店/恶名狩猎的
「点击进入游戏→等待主界面」进入段假设冷启动登录态全面失效；月商店失败杀游戏
69ms 后周常又冷启动。用户拍板：两个任务都从大世界启动、中间不关游戏、像白荆
那样加场景登记表智能调度。

覆盖：
  [1] 场景判定矩阵：登录页/月卡弹窗/功能菜单/大世界/商店页 各状态识别正确，
      月卡词与功能菜单词互不误判
  [2] 状态机全链：登录→点进入→月卡→点掉→功能菜单→ESC→大世界 终态
  [3] 大世界直接终态（已在主世界秒过，零动作）
  [4] 商店页残留：ESC 退出后到大世界
  [5] unknown 连续 2 轮：落 flow_failures 取证并失败（绝不盲点）
  [6] 配置与接线：两份 flow_configs 的 close/reuse/步骤改造、_BUILTIN_ACTIONS
      注册、dispatch 分支、月卡词与菜单词无交集

跑法：.venv\\Scripts\\python.exe -m pytest tests/suite_zzz_scene_gate_20261108.py -q
"""

import io
import json
import sys
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from game_auto.runtime import flow_diagnostics as fd  # noqa: E402
from game_auto.runtime import foreground_flow_runner as ffr  # noqa: E402
from game_auto.runtime import zzz_scene as zs  # noqa: E402

FAILURES = []
LOG = __import__("logging").getLogger("zzz_scene_test")
LOG.addHandler(__import__("logging").NullHandler())


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


# 各状态 OCR 行（坐标 @1919x1079；「确 认」式空格由 _normalize_text 处理）
STATE_ROWS = {
    "login": _rows(("点击进入游戏", 960, 886), ("登录其他账号", 960, 700)),
    "month_popup": _rows(("今日到账", 960, 540), ("月卡剩余31天", 960, 610), ("×90", 1100, 540)),
    "menu": _rows(("丽都城募", 1270, 940), ("调频", 1550, 940), ("代理人", 1220, 940),
                  ("更多", 320, 940), ("NEW!", 120, 800)),
    "world": _rows(("星期四", 1800, 80), ("前往任务目标", 200, 178)),
    "shop": _rows(("哨站后勤", 300, 200), ("音擎残芯", 500, 400)),
    "unknown": _rows(("诡屋惊魂记活动开启", 960, 300)),
}
# 电台模板命中状态：world；商店页签命中状态：shop


class Machine:
    def __init__(self, state):
        self.state = state
        self.clicks = []
        self.escapes = []

    def rows(self, logger, hwnd=None, **kwargs):
        return list(STATE_ROWS[self.state])

    def template(self, hwnd, template_path, scales, confidences, use_color=False):
        if "主界面-电台" in template_path:
            return (960, 540) if self.state == "world" else None
        if "哨站后勤页签" in template_path:
            return (300, 200) if self.state == "shop" else None
        return None

    def click(self, x, y):
        self.clicks.append((int(x), int(y)))
        # 点击推进状态（与真实流程等价：进入游戏→月卡弹窗；点掉弹窗→功能菜单）
        if self.state == "login":
            self.state = "month_popup"
        elif self.state == "month_popup":
            self.state = "menu"

    def escape(self, key, **kwargs):
        self.escapes.append(key)
        if self.state in ("menu", "shop"):
            self.state = "world"


def _harness(start, flow_key="ZZZ_TEST"):
    machine = Machine(start)
    failures = []

    patches = [
        mock.patch.object(zs, "ocr_window_content",
                          side_effect=lambda logger, hwnd=None, **k: machine.rows(logger)),
        mock.patch.object(ffr, "_locate_template_in_window", side_effect=machine.template),
        mock.patch.object(ffr, "_pyautogui_click_guarded", side_effect=machine.click),
        mock.patch.object(ffr, "press_key_scancode",
                          side_effect=lambda key, **k: machine.escape(key)),
        mock.patch.object(fd, "record_failure",
                          side_effect=lambda logger, fk, step, err, **k: failures.append((fk, step, err))),
        mock.patch.object(zs.time, "sleep", lambda *_: None),
    ]
    for p in patches:
        p.start()
    try:
        ok, msg = zs.ensure_main_world(LOG, 1, scales=[1.0], confidences=[0.88],
                                       flow_key=flow_key, context="测试校正")
    finally:
        for p in reversed(patches):
            p.stop()
    return ok, msg, machine, failures


def test_scene_detection():
    print("[1] 场景判定矩阵")
    with mock.patch.object(zs, "ocr_window_content",
                           side_effect=lambda logger, hwnd=None, **k: list(STATE_ROWS["menu"])):
        texts = zs._texts(STATE_ROWS["menu"])
        check("功能菜单词 ≥2 判定", zs._count_markers(texts, zs.MENU_MARKERS) >= 2)
        check("月卡词在功能菜单页不误判", zs._first_hit(STATE_ROWS["menu"], zs.POPUP_MARKERS) is None)
    with mock.patch.object(zs, "ocr_window_content",
                           side_effect=lambda logger, hwnd=None, **k: list(STATE_ROWS["world"])):
        texts = zs._texts(STATE_ROWS["world"])
        check("大世界「星期」OCR 判定", zs._has_word(texts, zs.MAIN_WORLD_OCR))
        check("功能菜单词在大世界不误判", zs._count_markers(texts, zs.MENU_MARKERS) < 2)
        check("月卡词在大世界不误判", zs._first_hit(STATE_ROWS["world"], zs.POPUP_MARKERS) is None)
    with mock.patch.object(zs, "ocr_window_content",
                           side_effect=lambda logger, hwnd=None, **k: list(STATE_ROWS["month_popup"])):
        hit = zs._first_hit(STATE_ROWS["month_popup"], zs.POPUP_MARKERS)
        check("月卡弹窗命中", hit is not None and "今日到账" in str(hit.get("text")))
    check("月卡词与功能菜单词无交集",
          not (set(zs.POPUP_MARKERS) & set(zs.MENU_MARKERS)))


def test_full_chain():
    print("[2] 全链：登录→点进入→月卡→点掉→功能菜单→ESC→大世界")
    ok, msg, machine, failures = _harness("login")
    check("到达大世界", ok is True, msg)
    check("点了「点击进入游戏」", (960, 886) in machine.clicks, machine.clicks)
    check("点掉了月卡弹窗", (960, 540) in machine.clicks, machine.clicks)
    check("功能菜单按了 ESC", machine.escapes == ["escape"], machine.escapes)
    check("无失败取证", failures == [], failures)


def test_world_direct():
    print("[3] 已在大世界：零动作秒过")
    ok, msg, machine, failures = _harness("world")
    check("直接成功", ok is True, msg)
    check("没有点击/按键", machine.clicks == [] and machine.escapes == [],
          (machine.clicks, machine.escapes))


def test_shop_residual():
    print("[4] 商店页残留：ESC 退出后到大世界")
    ok, msg, machine, failures = _harness("shop")
    check("到达大世界", ok is True, msg)
    check("按了 ESC", machine.escapes == ["escape"], machine.escapes)
    check("没有盲点", machine.clicks == [], machine.clicks)


def test_unknown_fails_with_evidence():
    print("[5] unknown 连续 2 轮：落取证并失败")
    ok, msg, machine, failures = _harness("unknown")
    check("校正失败", ok is False, msg)
    check("落了 flow_failures 取证", len(failures) == 1 and failures[0][0] == "ZZZ_TEST"
          and "未知场景" in failures[0][2], failures)
    check("没有点击/按键（绝不盲点）",
          machine.clicks == [] and machine.escapes == [], machine.clicks)


def test_configs_and_wiring():
    print("[6] 配置与接线")
    runner_src = (PROJECT_ROOT / "src" / "game_auto" / "runtime" / "foreground_flow_runner.py").read_text(
        encoding="utf-8")
    check("action 已注册", '"zzz_enter_main"' in runner_src)
    check("dispatch 分支存在", "from .zzz_scene import enter_main_interface" in runner_src)
    check("月卡词不在菜单判据", "今日到账" not in repr(zs.MENU_MARKERS))

    for path in ("flow_configs.json", "flow_configs.example.json"):
        with open(PROJECT_ROOT / path, encoding="utf-8") as f:
            cfg = json.load(f)
        lg = cfg["ZZZ_LOGISTICS_MONTHLY"]
        nt = cfg["ZZZ_NOTORIOUS_HUNT"]
        check(f"[{path}] LOGISTICS 游戏留给周常（finish/fail 都不关）",
              lg.get("close_process_on_finish") is False and lg.get("close_process_on_fail") is False)
        check(f"[{path}] LOGISTICS 复用运行中窗口", lg.get("reuse_existing_window") is True)
        check(f"[{path}] LOGISTICS 第一步是场景校正",
              lg["steps"][0]["action"] == "zzz_enter_main", lg["steps"][0]["name"])
        check(f"[{path}] LOGISTICS 补了丽都城募中间层",
              any(s.get("target_text") == "丽都城募" for s in lg["steps"]),
              [s.get("name") for s in lg["steps"]])
        check(f"[{path}] LOGISTICS 删了 start_from_main", "start_from_main" not in lg)
        check(f"[{path}] NOTORIOUS 跑完关游戏",
              nt.get("close_process_on_finish") is True and nt.get("close_process_on_fail") is True)
        check(f"[{path}] NOTORIOUS 复用窗口 + 场景校正第一步",
              nt.get("reuse_existing_window") is True
              and nt["steps"][0]["action"] == "zzz_enter_main")
        check(f"[{path}] NOTORIOUS 删了 start_from_main", "start_from_main" not in nt)


def main():
    test_scene_detection()
    test_full_chain()
    test_world_direct()
    test_shop_residual()
    test_unknown_fails_with_evidence()
    test_configs_and_wiring()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
