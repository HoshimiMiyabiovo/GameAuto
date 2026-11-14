# -*- coding: utf-8 -*-
"""绝区零进入阶段「月卡弹窗」处理回归（2026-11-07）。

事故：11-07 08:37 / 08:50 两轮 ZZZ_LOGISTICS_MONTHLY 卡死在「点击进入游戏」，
失败截图当帧就是「互联会员卡」月卡弹窗（OCR: 今日到账/月卡剩余31天/×90）。
链路：点「点击进入游戏」→ 加载 → 月卡弹窗把背景压暗 → 步骤2 等不到
电台模板/「星期」OCR → retry_prev 回步骤1 → 弹窗挡着标题文字 → 重试耗尽。
OneDragon enter_game（issue #893/#1996）同场景的做法：识别循环里 OCR 见
「今日到账/惊喜补给」直接点击点掉。本套件锁定同款处理：

  [1] _locate_with_ocr_fallback 的 dismiss_texts 语义（弹窗词优先、共用一次扫描）
  [2] _ocr_locate_labeled_in_window 返回命中词 + 局部坐标（旧函数向后兼容）
  [3] run_flow 状态机：等待主界面途中点掉弹窗继续等 / 弹窗点不掉按时限失败
  [4] run_flow 状态机：接管已开游戏（弹窗在场）→ 步骤1 点掉弹窗后 optional 跳过
  [5] 两份 flow_configs：三条 ZZZ 流程接线 + NOTORIOUS 补 start_from_main
  （原 [6] runner 静态接线已删：锁 .get()/分支写法的断言对重构零容忍，
  dismiss 链路行为由 [1]-[4] 覆盖）

跑法：.venv\\Scripts\\python.exe tests\\suite_zzz_enter_popup_dismiss_20261107.py
"""

import io
import json
import logging
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import game_auto.runtime.foreground_flow_runner as ff  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} :: {detail}")
        FAILURES.append(name)


def _logger():
    logger = logging.getLogger("test_zzz_popup_dismiss")
    logger.setLevel(logging.CRITICAL)
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


DISMISS = ["今日到账", "惊喜补给", "月卡剩余"]


def test_locate_fallback_dismiss():
    print("[1] _locate_with_ocr_fallback 的 dismiss 语义")
    cases = []

    def _capture(*args, **kwargs):
        cases.append(list(args[2]))
        return _capture.result

    _locate = mock.patch.object(ff, "_locate_template_in_window", lambda *a, **k: None)

    # 1a. OCR 命中弹窗词 → ocr_dismiss，候选 = 弹窗词在前 + 目标词
    _capture.result = ("今日到账", (500, 400))
    with _locate, mock.patch.object(ff, "_ocr_locate_labeled_in_window", _capture):
        center, method = ff._locate_with_ocr_fallback(
            _logger(), 7, "assets/templates/绝区零-主界面-电台.png", ["星期"],
            [1.0], [0.8], enable_ocr=True, dismiss_texts=DISMISS,
        )
    check("命中弹窗词 → ocr_dismiss", (center, method) == ((500, 400), "ocr_dismiss"), (center, method))
    check("候选 = 弹窗词 + 目标词（弹窗词在前）",
          cases and cases[-1] == DISMISS + ["星期"], cases and cases[-1])

    # 1b. OCR 命中目标词 → ocr（弹窗不在场时普通命中）
    _capture.result = ("星期", (253, 63))
    with _locate, mock.patch.object(ff, "_ocr_locate_labeled_in_window", _capture):
        center, method = ff._locate_with_ocr_fallback(
            _logger(), 7, "assets/templates/绝区零-主界面-电台.png", ["星期"],
            [1.0], [0.8], enable_ocr=True, dismiss_texts=DISMISS,
        )
    check("命中目标词 → ocr", (center, method) == ((253, 63), "ocr"), (center, method))

    # 1c. 未配 dismiss_texts → 候选只有目标词（向后兼容）
    _capture.result = ("星期", (253, 63))
    with _locate, mock.patch.object(ff, "_ocr_locate_labeled_in_window", _capture):
        ff._locate_with_ocr_fallback(
            _logger(), 7, "assets/templates/绝区零-主界面-电台.png", ["星期"],
            [1.0], [0.8], enable_ocr=True,
        )
    check("未配 dismiss 候选不变", cases and cases[-1] == ["星期"], cases and cases[-1])

    # 1d. 模板命中优先，不走 OCR
    cases.clear()
    _capture.result = ("今日到账", (1, 1))
    with mock.patch.object(ff, "_locate_template_in_window", lambda *a, **k: (9, 9)), \
            mock.patch.object(ff, "_ocr_locate_labeled_in_window", _capture):
        center, method = ff._locate_with_ocr_fallback(
            _logger(), 7, "assets/templates/绝区零-主界面-电台.png", ["星期"],
            [1.0], [0.8], enable_ocr=True, dismiss_texts=DISMISS,
        )
    check("模板命中 → template，OCR 未被调用", (center, method) == ((9, 9), "template") and not cases,
          (center, method, cases))

    # 1e. 都没命中 → (None, None)
    _capture.result = None
    with _locate, mock.patch.object(ff, "_ocr_locate_labeled_in_window", _capture):
        center, method = ff._locate_with_ocr_fallback(
            _logger(), 7, "", ["星期"], [1.0], [0.8], enable_ocr=True, dismiss_texts=DISMISS,
        )
    check("全部未命中 → (None, None)", (center, method) == (None, None), (center, method))


def test_labeled_ocr():
    print("[2] _ocr_locate_labeled_in_window 带标签返回 + 向后兼容")
    fake_result = {"center": (350, 400), "matched_target": "今日到账", "confidence": 0.73}
    with mock.patch.object(ff, "_window_rect", lambda hwnd: (100, 50, 2020, 1130)), \
            mock.patch("game_auto.runtime.ocr_click_tester.ocr_find_any_text",
                       lambda *a, **k: dict(fake_result)):
        hit = ff._ocr_locate_labeled_in_window(_logger(), 7, ["今日到账", "星期"])
        plain = ff._ocr_locate_in_window(_logger(), 7, ["今日到账", "星期"])
    check("返回 (命中词, 窗口局部坐标)", hit == ("今日到账", (250, 350)), hit)
    check("旧函数仍只返回坐标", plain == (250, 350), plain)

    with mock.patch("game_auto.runtime.ocr_click_tester.ocr_find_any_text",
                    lambda *a, **k: dict(fake_result)):
        hit = ff._ocr_locate_labeled_in_window(_logger(), None, ["今日到账"])
    check("hwnd=None 原样返回屏幕坐标", hit == ("今日到账", (350, 400)), hit)

    with mock.patch("game_auto.runtime.ocr_click_tester.ocr_find_any_text",
                    lambda *a, **k: None):
        hit = ff._ocr_locate_labeled_in_window(_logger(), 7, ["星期"])
    check("无命中 → None", hit is None, hit)


class _FakeClock:
    """sleep 推进假时钟：等待循环不用真实热旋，超时窗口按 sleep 步进收敛。"""

    def __init__(self):
        self.now = 1000.0

    def time(self):
        return self.now

    def sleep(self, sec):
        self.now += float(sec)


def _drive_flow(cfg, ocr_results, clicks, log_records=None):
    """驱动 run_flow：模板恒不命中，OCR 按脚本序列返回 (matched, center)。"""
    seq = list(ocr_results)

    def _fake_labeled(logger, hwnd, candidates, **kwargs):
        return seq.pop(0) if seq else (None, None)

    clock = _FakeClock()
    logger = _logger()
    if log_records is not None:
        handler = logging.Handler()
        handler.emit = lambda record: log_records.append(record)
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)

    patches = (
        mock.patch.object(ff, "_load_flow_config", lambda key: json.loads(json.dumps(cfg))),
        mock.patch.object(ff, "_resolve_exe_path", lambda *a, **k: "C:/fake/ZenlessZoneZero.exe"),
        mock.patch.object(ff, "_launch_maybe_elevated", lambda *a, **k: 4321),
        mock.patch.object(ff, "_sleep_interruptible", lambda sec, sc=None: True),
        mock.patch.object(ff, "_wait_window_for_flow", lambda *a, **k: (1234, 4321)),
        mock.patch.object(ff, "_refresh_flow_window", lambda *a, **k: None),
        mock.patch.object(ff, "_recover_flow_window", lambda *a, **k: (None, None)),
        mock.patch.object(ff, "_uipi_block_reason", lambda pid: None),
        mock.patch.object(ff, "_window_available_for_ocr", lambda hwnd: True),
        mock.patch.object(ff, "_focus_window", lambda *a, **k: None),
        mock.patch.object(ff, "_locate_template_in_window", lambda *a, **k: None),
        mock.patch.object(ff, "_ocr_locate_labeled_in_window", _fake_labeled),
        mock.patch.object(ff, "_click_window_local",
                          lambda hwnd, x, y, **k: clicks.append((x, y, k))),
        mock.patch.object(ff, "_collect_failure_evidence", lambda **k: None),
        mock.patch.object(ff, "close_flow_processes", lambda *a, **k: None),
        mock.patch.object(ff.time, "time", clock.time),
        mock.patch.object(ff.time, "sleep", clock.sleep),
    )
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        return ff.run_flow(logger, "ZZZ_TEST")


def test_wait_step_dismisses_popup():
    print("[3] 等待主界面途中点掉月卡弹窗")
    cfg = {
        "enabled": True,
        "close_process_on_finish": False,
        "close_process_on_fail": False,
        "startup_wait_sec": 0,
        "steps": [{
            "name": "等待进入主界面",
            "action": "wait_template",
            "template": "assets/templates/绝区零-主界面-电台.png",
            "target_text": "星期",
            "enable_ocr_fallback": True,
            "dismiss_texts": DISMISS,
            "timeout_sec": 5,
            "search_interval_sec": 0.01,
            "post_delay_sec": 0,
            "on_fail": "fail",
            "max_attempts": 1,
        }],
    }
    # 第 1 轮 OCR 见弹窗 → 点击 → 第 2 轮见主界面特征 → 成功
    clicks = []
    ok, msg = _drive_flow(cfg, [("今日到账", (500, 400)), ("星期", (253, 63))], clicks)
    check("弹窗点掉后等到了主界面，流程成功", ok is True, msg)
    check("弹窗被点击一次，坐标=弹窗词位置",
          clicks == [(500, 400, {"clicks": 1, "background": False, "hold_sec": None})], clicks)

    # 点击无效（弹窗一直在）→ 超时失败
    clicks.clear()
    ok, msg = _drive_flow(cfg, [("今日到账", (500, 400))] * 50, clicks)
    check("弹窗点不掉 → 按时限失败", ok is False, msg)
    check("超时窗口内反复尝试点击（不止一次）", len(clicks) > 1, len(clicks))


def test_click_step_dismiss_then_optional_skip():
    print("[4] 接管已开游戏：步骤1点掉弹窗后 optional 跳过，步骤2确认主界面")
    cfg = {
        "enabled": True,
        "close_process_on_finish": False,
        "close_process_on_fail": False,
        "startup_wait_sec": 0,
        "steps": [
            {
                "name": "点击进入游戏",
                "action": "click_template",
                "template": "",
                "target_text": "点击进入游戏",
                "enable_ocr_fallback": True,
                "dismiss_texts": DISMISS,
                "optional_if_missing": True,
                "timeout_sec": 1,
                "search_interval_sec": 0.01,
                "post_delay_sec": 0,
                "confirm_type": "none",
            },
            {
                "name": "等待进入主界面",
                "action": "wait_template",
                "template": "assets/templates/绝区零-主界面-电台.png",
                "target_text": "星期",
                "enable_ocr_fallback": True,
                "dismiss_texts": DISMISS,
                "timeout_sec": 5,
                "search_interval_sec": 0.01,
                "post_delay_sec": 0,
                "on_fail": "fail",
                "max_attempts": 1,
            },
        ],
    }
    # 步骤1：第1轮见弹窗（点掉）→ 之后标题文字不再出现（已在主世界）→ optional 跳过
    # 步骤2：主界面特征命中 → 成功
    clicks = []
    ok, msg = _drive_flow(
        cfg, [("今日到账", (500, 400)), (None, None), ("星期", (253, 63))], clicks)
    check("流程成功", ok is True, msg)
    check("步骤1把残留弹窗点掉", clicks[:1] == [(500, 400, mock.ANY)], clicks)
    check("步骤1没有误点主世界", len(clicks) == 1, clicks)


def test_flow_configs():
    print("[5] 两份配置：三条 ZZZ 流程接线")
    from game_auto.runtime import zzz_scene as zs

    for name in ("flow_configs.json", "flow_configs.example.json"):
        cfg = json.loads((PROJECT_ROOT / name).read_text(encoding="utf-8"))
        flows = cfg["ZZZ_MONTHLY"], cfg["ZZZ_LOGISTICS_MONTHLY"], cfg["ZZZ_NOTORIOUS_HUNT"]
        for flow in flows:
            tag = f"{name}/{flow['steps'][0] and flow.get('exe_from_app')}"
            s1, s2 = flow["steps"][0], flow["steps"][1]
            # 2026-11-08 #219/#220：后台月/周流程开局改场景校正（月卡 dismiss
            # 移进 zzz_scene.POPUP_MARKERS），日常流程保留原步骤 1/2
            if s1.get("action") == "zzz_enter_main":
                check(f"{tag} 开局=场景校正（月卡 dismiss 在场景表内）",
                      s1.get("action") == "zzz_enter_main"
                      and set(zs.POPUP_MARKERS) == {"今日到账", "惊喜补给", "月卡剩余"},
                      s1.get("name"))
                continue
            check(f"{tag} 步骤1/2 配了 dismiss_texts",
                  s1.get("dismiss_texts") == DISMISS and s2.get("dismiss_texts") == DISMISS,
                  (s1.get("dismiss_texts"), s2.get("dismiss_texts")))
            check(f"{tag} 步骤1 可选跳过（接管已开游戏）",
                  s1.get("optional_if_missing") is True, s1.get("optional_if_missing"))
            check(f"{tag} 步骤2 仍是主界面等待",
                  s2.get("action") == "wait_template" and "电台" in str(s2.get("template")), s2)
        check(f"{name} NOTORIOUS 无 start_from_main（场景表接管，不再跳步）",
              "start_from_main" not in cfg["ZZZ_NOTORIOUS_HUNT"])
    tpl = PROJECT_ROOT / "assets/templates/绝区零-主界面-电台.png"
    check("电台模板存在", tpl.is_file(), tpl)


def main():
    test_locate_fallback_dismiss()
    test_labeled_ocr()
    test_wait_step_dismisses_popup()
    test_click_step_dismiss_then_optional_skip()
    test_flow_configs()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
