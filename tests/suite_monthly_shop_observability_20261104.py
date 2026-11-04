"""月商店「失败不再静默」观测性回归（2026-11-04）。

背景（11-03 实锤）：
- 崩铁自塑尘脂步骤失败时，craft 返回、dispatch 分支（run_flow 的
  `if not dispatch_ok: return`）、run_flow 三层都不打日志，flow_failures
  也无记录，失败只剩邮件日报一行 ❌——从"点格子"到"下一个游戏启动"
  中间日志一片空白，无法定位点歪在哪。
- 原神 1/5 假成功已把 `monthly_flow:GENSHIN_MONTHLY=2026-11` 写进库，
  月度标记一旦误写本月不再补买；结果行以前只进邮件日报。
- 「剩余数量」判据（NOT_FULL）没有"自家商品名"守卫：崩铁商品到月上限
  后整卡从货架消失，点中的是邻卡，邻卡的「可兑换: 80/80」会把真买完
  误判成"未买满"，从此每月报失败（08:47 证据图：命运的足迹 2/2 后消失）。

覆盖：
  1. _collect_failure_evidence 的 error 参数落到 record_failure
  2. run_flow 的 dispatch 失败 → logger.error + 统一取证（error=分支消息）
  3. run_flow 的 dispatch「用户停止」→ 只留 info 日志，不写 flow_failures
  4. _read_item_remaining 邻卡守卫：无自家商品名 → None；有 → 数值
  5. _buy_once 行为：邻卡剩余 80 → NO_POPUP；自家卡剩余 4 → NOT_FULL
  6. _run_monthly_flow 成功/失败都打日志行
  7. _append_monthly_report 把结果行同步进 runner 日志
  8. hsr_resin_craft 的 fail() 先打日志再退主界面

跑法：.venv\\Scripts\\python.exe tests\\suite_monthly_shop_observability_20261104.py
"""

import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import game_auto.runtime.foreground_flow_runner as ff  # noqa: E402
import game_auto.runtime.background_template_runner as btr  # noqa: E402
import game_auto.runtime.task_batch_runner as tbr  # noqa: E402
import game_auto.runtime.zzz_monthly_shop as shop  # noqa: E402
import game_auto.runtime.hsr_resin_craft as hr  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def _capture_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    records = []

    class _H(logging.Handler):
        def emit(self, record):
            records.append(record)

    logger.handlers = [_H()]
    logger.records = records
    return logger


def _ocr_entries(entries):
    def _fake(logger, hwnd=None, **kwargs):
        return entries
    return _fake


def test_collect_failure_evidence_error_param():
    print("[1] _collect_failure_evidence 的 error 参数落到 record_failure")
    recorded = []
    with mock.patch("game_auto.runtime.ocr_click_tester.ocr_window_content",
                    _ocr_entries([])), \
            mock.patch("game_auto.runtime.flow_diagnostics.record_failure",
                       lambda *a, **k: recorded.append(k)):
        ff._collect_failure_evidence(
            logger=_capture_logger("obs1"),
            flow_key="HSR_MONTHLY",
            step={"name": "自塑尘脂每月合成"},
            name="自塑尘脂每月合成",
            step_no=3,
            action="hsr_resin_craft",
            target_texts=[],
            template="",
            hwnd=None,
            retry_count=0,
            max_attempts=1,
            step_enable_ocr=False,
            error="点开后详情区没显示「自塑尘脂」",
        )
    check("record_failure 被调用一次", len(recorded) == 1, recorded)
    check("error 保留分支原因",
          recorded and recorded[0].get("error") == "点开后详情区没显示「自塑尘脂」",
          recorded)
    check("默认 error 文案保持兼容",
          "未匹配模板/OCR" in str((ff._collect_failure_evidence.__kwdefaults__ or {}).get("error")),
          ff._collect_failure_evidence.__kwdefaults__)


def _run_fake_flow(dispatch_result, obs_logger):
    """驱动 run_flow 走到 dispatch 失败分支（单步 builtin 动作流程）。"""
    cfg = {
        "enabled": True,
        "close_process_on_finish": False,
        "close_process_on_fail": False,
        "startup_wait_sec": 0,
        "steps": [{"name": "自塑尘脂每月合成", "action": "hsr_resin_craft",
                   "post_delay_sec": 0}],
    }
    evidence = []
    with mock.patch.object(ff, "_load_flow_config", lambda key: dict(cfg)), \
            mock.patch.object(ff, "_resolve_exe_path", lambda *a, **k: "C:/fake/game.exe"), \
            mock.patch.object(ff, "_launch_maybe_elevated", lambda *a, **k: 4321), \
            mock.patch.object(ff, "_sleep_interruptible", lambda sec, sc=None: True), \
            mock.patch.object(ff, "_wait_window_for_flow", lambda *a, **k: (1234, 4321)), \
            mock.patch.object(ff, "_refresh_flow_window", lambda *a, **k: None), \
            mock.patch.object(ff, "_dispatch_step_action",
                              lambda **k: (dispatch_result + (1234, 4321))), \
            mock.patch.object(ff, "_collect_failure_evidence",
                              lambda **k: evidence.append(k)), \
            mock.patch.object(ff, "close_flow_processes", lambda *a, **k: None):
        ok, msg = ff.run_flow(obs_logger, "HSR_MONTHLY")
    return ok, msg, evidence


def test_run_flow_dispatch_failure_recorded():
    print("[2] dispatch 失败 → error 日志 + 统一取证（error=分支消息）")
    lg = _capture_logger("obs2")
    ok, msg, evidence = _run_fake_flow(
        (False, "[HSR_MONTHLY] 步骤失败: 自塑尘脂每月合成 (点开后详情区没显示「自塑尘脂」)"), lg
    )
    check("流程失败返回", ok is False, msg)
    check("取证被调用一次", len(evidence) == 1, evidence)
    check("取证 error 带分支原因",
          evidence and "详情区没显示" in str(evidence[0].get("error")), evidence)
    check("日志有执行失败行",
          any("执行失败" in r.getMessage() for r in lg.records),
          [r.getMessage() for r in lg.records])
    check("日志级别是 ERROR",
          any(r.levelno == logging.ERROR and "执行失败" in r.getMessage() for r in lg.records))


def test_run_flow_user_stop_not_recorded():
    print("[3] dispatch「用户停止」→ 只留 info 日志，不写 flow_failures")
    lg = _capture_logger("obs3")
    ok, msg, evidence = _run_fake_flow(
        (False, "[HSR_MONTHLY] 步骤失败: 自塑尘脂每月合成 (用户停止)"), lg
    )
    check("流程失败返回", ok is False, msg)
    check("用户停止不落取证", not evidence, evidence)
    check("日志有用户停止行",
          any("由用户停止" in r.getMessage() for r in lg.records),
          [r.getMessage() for r in lg.records])
    check("用户停止不打 ERROR",
          not any(r.levelno == logging.ERROR for r in lg.records),
          [r.levelname for r in lg.records])


def test_read_item_remaining_neighbor_guard():
    print("[4] _read_item_remaining：邻卡的剩余数量不可信")
    own_center = (300, 400)
    # 4a. 坐标附近只有邻卡的「可兑换: 80/80」→ None（卡片已消失的场景）
    with mock.patch("game_auto.runtime.ocr_click_tester.ocr_window_content",
                    _ocr_entries([
                        {"text": "可兑换：80/80", "confidence": 0.86, "center": (340, 420)},
                    ])):
        val = shop._read_item_remaining(
            _capture_logger("obs4"), 1234, own_center, item_text="星轨专票"
        )
    check("邻卡剩余数量返回 None", val is None, val)

    # 4b. 自家商品名 + 剩余 4 → 4
    with mock.patch("game_auto.runtime.ocr_click_tester.ocr_window_content",
                    _ocr_entries([
                        {"text": "星轨专票", "confidence": 0.80, "center": (300, 430)},
                        {"text": "可兑换：4/5", "confidence": 0.85, "center": (300, 380)},
                    ])):
        val = shop._read_item_remaining(
            _capture_logger("obs4"), 1234, own_center, item_text="星轨专票"
        )
    check("自家卡剩余读出 4", val == 4, val)

    # 4c. 自家卡剩余 0 → 0（走已达上限口径）
    with mock.patch("game_auto.runtime.ocr_click_tester.ocr_window_content",
                    _ocr_entries([
                        {"text": "星轨专票", "confidence": 0.80, "center": (300, 430)},
                        {"text": "可兑换：0/5", "confidence": 0.85, "center": (300, 380)},
                    ])):
        val = shop._read_item_remaining(
            _capture_logger("obs4"), 1234, own_center, item_text="星轨专票"
        )
    check("自家卡剩余 0 读出 0", val == 0, val)

    # 4d. 不给 item_text 时保持旧行为（后勤商店等旧调用方不受影响）
    with mock.patch("game_auto.runtime.ocr_click_tester.ocr_window_content",
                    _ocr_entries([
                        {"text": "可兑换：80/80", "confidence": 0.86, "center": (340, 420)},
                    ])):
        val = shop._read_item_remaining(_capture_logger("obs4"), 1234, own_center)
    check("旧调用无守卫仍读出 80", val == 80, val)


def test_buy_once_neighbor_badge_not_not_full():
    print("[5] _buy_once：邻卡剩余 80 → NO_POPUP；自家剩余 4 → NOT_FULL")
    ITEM = {"name": "星轨专票", "ocr_text": "星轨专票"}

    def _run(entries):
        lg = _capture_logger("obs5")
        with mock.patch.object(shop, "_locate_item", lambda *a, **k: (300, 400)), \
                mock.patch.object(shop, "_locate", lambda *a, **k: None), \
                mock.patch("game_auto.runtime.foreground_flow_runner._click_window_local",
                           lambda *a, **k: None), \
                mock.patch("game_auto.runtime.ocr_click_tester.ocr_window_content",
                           _ocr_entries(entries)), \
                mock.patch("game_auto.runtime.zzz_monthly_shop.time.sleep", lambda s: None):
            return shop._buy_once(
                lg, 1234, dict(ITEM), "confirm.png", [1.0], [0.8], None, label="余烬兑换"
            )

    check("邻卡剩余 80 不判未买满（NO_POPUP）",
          _run([{"text": "可兑换：80/80", "confidence": 0.86, "center": (340, 420)}])
          == shop.NO_POPUP)
    check("自家剩余 4 判未买满（NOT_FULL）",
          _run([
              {"text": "星轨专票", "confidence": 0.80, "center": (300, 430)},
              {"text": "可兑换：4/5", "confidence": 0.85, "center": (300, 380)},
          ]) == shop.NOT_FULL)


def test_run_monthly_flow_logs_result():
    print("[6] _run_monthly_flow 成功/失败都打日志行")
    lg = _capture_logger("obs6")

    class _FakeDB:
        def __init__(self, *a, **k):
            pass

        def get_task_meta(self, key):
            return None

        def set_task_meta(self, key, value):
            pass

    with mock.patch("game_auto.storage.workflow_db.WorkflowDB", _FakeDB), \
            mock.patch.object(btr, "run_flow", lambda *a, **k: (True, "流程执行成功")):
        ok, msg = btr._run_monthly_flow(lg, "GENSHIN_MONTHLY", "原神每月商店")
    check("成功返回 True", ok is True, msg)
    check("成功打 info 日志",
          any(r.levelno == logging.INFO and "✅" in r.getMessage() for r in lg.records),
          [r.getMessage() for r in lg.records])

    lg2 = _capture_logger("obs6b")
    with mock.patch("game_auto.storage.workflow_db.WorkflowDB", _FakeDB), \
            mock.patch.object(btr, "run_flow",
                              lambda *a, **k: (False, "[GENSHIN_MONTHLY] 步骤失败: x (y)")):
        ok2, msg2 = btr._run_monthly_flow(lg2, "GENSHIN_MONTHLY", "原神每月商店")
    check("失败返回 False", ok2 is False, msg2)
    check("失败打 warning 日志",
          any(r.levelno == logging.WARNING and "❌" in r.getMessage() for r in lg2.records),
          [r.getMessage() for r in lg2.records])


def test_append_monthly_report_logs_line():
    print("[7] _append_monthly_report 把结果行同步进 runner 日志")
    lg = _capture_logger("obs7")
    controller = SimpleNamespace(logger=lg)
    report, warnings = [], []

    with mock.patch.object(tbr, "_run_pending_monthly_flow",
                           lambda ctrl, app: ("余烬兑换 ❌ 星轨专票: 0/5", False)):
        tbr._append_monthly_report(controller, "HonkaiStarRail", report, warnings)
    check("失败行进 report", report == ["余烬兑换 ❌ 星轨专票: 0/5"], report)
    check("失败行进 warnings", warnings == report, warnings)
    check("失败行进日志（WARNING）",
          any(r.levelno == logging.WARNING and "月商店" in r.getMessage()
              and "星轨专票" in r.getMessage() for r in lg.records),
          [r.getMessage() for r in lg.records])

    lg2 = _capture_logger("obs7b")
    controller2 = SimpleNamespace(logger=lg2)
    report2, warnings2 = [], []
    with mock.patch.object(tbr, "_run_pending_monthly_flow",
                           lambda ctrl, app: ("星尘兑换 ✅ 纠缠之缘: 5/5", True)):
        tbr._append_monthly_report(controller2, "GenshinImpact", report2, warnings2)
    check("成功行进日志（INFO）",
          any(r.levelno == logging.INFO and "纠缠之缘" in r.getMessage() for r in lg2.records),
          [r.getMessage() for r in lg2.records])
    check("成功不进 warnings", warnings2 == [], warnings2)


def test_craft_fail_logs_reason():
    print("[8] hsr_resin_craft 的 fail() 先打日志再退主界面")
    lg = _capture_logger("obs8")
    with mock.patch.object(hr, "_open_bag", lambda *a, **k: (False, "背包打不开")), \
            mock.patch.object(hr, "_leave_to_main", lambda *a, **k: True):
        ok, msg = hr.craft_self_modeling_resin(
            lg, 1234, {}, scales=[1.0], confidences=[0.8]
        )
    check("返回失败与原因", ok is False and msg == "背包打不开", (ok, msg))
    check("失败原因进了日志",
          any("失败: 背包打不开" in r.getMessage() and r.levelno == logging.WARNING
              for r in lg.records),
          [r.getMessage() for r in lg.records])


def main():
    test_collect_failure_evidence_error_param()
    test_run_flow_dispatch_failure_recorded()
    test_run_flow_user_stop_not_recorded()
    test_read_item_remaining_neighbor_guard()
    test_buy_once_neighbor_badge_not_not_full()
    test_run_monthly_flow_logs_result()
    test_append_monthly_report_logs_line()
    test_craft_fail_logs_reason()

    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
