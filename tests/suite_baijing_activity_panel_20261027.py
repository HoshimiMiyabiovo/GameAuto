# -*- coding: utf-8 -*-
"""回归：白荆活跃度必须以游戏面板大数字为准（2026-10-27 实锤）。

事故：用户看到验证截图里是 105（五档里程碑全部已领取），daily_progress 却记
90/100。根因是活跃度用静态目录 DAILY_TASK_CATALOG 的已完成任务分值相加，
而目录已和游戏版本脱节（当天游戏是「完成2次战斗」，目录写「完成3次战斗」），
同样 10 个已完成任务目录算出 90、游戏面板实际 105。10-25 也出现过 +15
（目录 70 / 面板 85）。

修复：process_task_list 结束时优先 OCR 每日任务面板顶部的「累计活跃度」
大数字，读不到才退回目录核算；activity_source / activity_catalog 落库留痕。
同时任务扫描每屏落快照（daily_scan_1_日期.jpg），便于下次核对目录漂移。

跑法：.venv\\Scripts\\python.exe artifacts\\test_baijing_activity_panel_20261027.py
"""

import logging
import sys
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT.parent))

import game_auto.runtime.baijing_daily as bd  # noqa: E402

FAILURES = []
LOG = logging.getLogger("baijing-activity-panel-test")

# 模拟窗口区域：_abs_region 直接返回这个矩形，数字中心落在 (1000~1400, 100~300)
FAKE_REGION = (1000, 100, 400, 200)


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def _hit(text, cx, cy, conf=0.9):
    return {"text": text, "center": (cx, cy), "confidence": conf}


def _read_with(results_per_call, with_marker=True):
    """按调用次数喂 OCR 结果，返回 _read_activity_number 的结果。"""
    calls = {"n": 0}

    def fake_ocr(*args, **kwargs):
        idx = min(calls["n"], len(results_per_call) - 1)
        calls["n"] += 1
        results = list(results_per_call[idx])
        if with_marker:
            # 面板特征放在数字区域外，只用于确认当前屏是每日任务面板
            results.append(_hit("累计活跃度", 900, 500, conf=0.8))
        return results

    with patch.object(bd, "_ensure_window", lambda logger, hwnd: hwnd), \
         patch.object(bd, "_abs_region", lambda hwnd, frac: FAKE_REGION), \
         patch.object(bd, "ocr_window_content", fake_ocr), \
         patch.object(bd.time, "sleep", lambda _s: None):
        return bd._read_activity_number(LOG, 12345)


def test_reader():
    # 1) 完整三位数：直接采用
    got = _read_with([[_hit("105", 1200, 200)]])
    check("完整数字 105 可读", got == 105, f"got={got}")

    # 2) 数字被拆成 1/0/5 三块：按 x 拼接
    got = _read_with([[
        _hit("5", 1250, 200), _hit("1", 1150, 200), _hit("0", 1200, 200),
    ]])
    check("拆分数字按 x 拼接为 105", got == 105, f"got={got}")

    # 3) 区域外的数字（例如别处 UI 的 105）不得采用
    got = _read_with([[_hit("105", 800, 200)]])
    check("区域外数字被忽略", got is None, f"got={got}")

    # 4) 只读到 0（入场动画）→ 返回 None，让调用方退回目录核算
    got = _read_with([[_hit("0", 1200, 200, conf=0.8)]])
    check("只读到 0 不采用", got is None, f"got={got}")

    # 5) 多次读取 0 → 90 → 105：取非零最大值，避免被入场动画骗
    got = _read_with([
        [_hit("0", 1200, 200)],
        [_hit("90", 1200, 200)],
        [_hit("105", 1200, 200)],
    ])
    check("动画过程取最大值 105", got == 105, f"got={got}")

    # 6) 超出合理上限的误读（如 1000）不采用
    got = _read_with([[_hit("1000", 1200, 200)]])
    check("超上限数字被忽略", got is None, f"got={got}")

    # 6.1) 同时出现截断 token（10）和完整 105：优先取字符数更多的 105
    got = _read_with([[
        _hit("10", 1150, 200, conf=0.95),
        _hit("105", 1200, 200, conf=0.6),
    ]])
    check("优先完整 105 而非截断的 10", got == 105, f"got={got}")

    # 7) 没有面板特征（面板已关/别的界面）→ 不采用，避免误读其它数字
    got = _read_with([[_hit("105", 1200, 200)]], with_marker=False)
    check("无面板特征时不采用数字", got is None, f"got={got}")


def test_real_evidence_image():
    """真实 2026-10-27 验证图上，105 必须落在面板数字区域内。"""
    from rapidocr_onnxruntime import RapidOCR

    img = PROJECT_ROOT / "data" / "artifacts" / "baijing_verify" / "2026-10-27.jpg"
    im_w, im_h = 480, 270
    res, _ = RapidOCR()(str(img))
    hits = []
    for box, text, conf in (res or []):
        if str(text).strip() != "105":
            continue
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        cx, cy = sum(xs) / 4, sum(ys) / 4
        x0, y0 = 0.22 * im_w, 0.15 * im_h
        x1, y1 = 0.42 * im_w, 0.34 * im_h
        if x0 <= cx <= x1 and y0 <= cy <= y1:
            hits.append((cx, cy, float(conf)))
    check(
        "真实验证图 105 落在面板数字区域",
        bool(hits),
        f"hits={hits}",
    )


def test_scan_snapshot_wiring():
    """scan_daily_task_states 传 capture_prefix 时每屏必须落快照。"""
    captured = []

    def fake_capture(logger, hwnd, prefix="daily_list"):
        captured.append(prefix)
        return None

    row = {
        "text": "登录游戏 (1/1)",
        "center": (1200, 400),
        "confidence": 0.9,
        "box": (1100, 380, 1300, 420),
    }
    with patch.object(bd, "_ensure_window", lambda logger, hwnd: hwnd), \
         patch.object(bd, "_abs_region", lambda hwnd, frac: None), \
         patch.object(bd, "ocr_window_content", lambda *a, **k: [row]), \
         patch.object(bd, "_scroll_list_with_verify", lambda *a, **k: False), \
         patch.object(bd, "_capture_daily_list_snapshot", fake_capture), \
         patch.object(bd, "_stopped", lambda stop: False):
        states = bd.scan_daily_task_states(
            LOG, 12345, capture_prefix="daily_scan"
        )
    check("扫描后能拿到登录任务", "login" in states, f"states={list(states)}")
    check(
        "首屏快照前缀为 daily_scan_1",
        captured == ["daily_scan_1"],
        f"captured={captured}",
    )


def test_source_wiring():
    src = (PROJECT_ROOT / "runtime" / "baijing_daily.py").read_text(encoding="utf-8")
    check(
        "process_task_list 调用面板活跃度读取",
        "game_activity = _read_activity_number(logger, hwnd)" in src,
    )
    check("面板优先口径写库", 'summary["activity_source"] = "game_panel"' in src)
    check("目录核算值留档", 'summary["activity_catalog"] = catalog_earned' in src)
    check("初始扫描逐屏快照已接线", 'capture_prefix="daily_scan"' in src)
    check("重扫逐屏快照已接线", 'capture_prefix="daily_scan_final"' in src)


def test_battle_variant_match():
    """战斗项每天可能是 2/3/4 次中的一种，必须都能匹配到同一个 10 分槽位。

    2026-10-27 用户指出：战斗 2/3/4 是分别的任务、不会同时显示；当天列表是
    「完成2次战斗」，旧目录只认「完成3次战斗」→ 扫描靠 OCR 误读成 3/3。
    """
    for n in (2, 3, 4):
        text = f"完成{n}次战斗（{n}/{n}）"
        rows = [{
            "y": 100,
            "text": text,
            "items": [{"text": text, "center": (1200, 100)}],
        }]
        states = bd._match_rows_to_catalog(LOG, rows)
        check(f"完成{n}次战斗匹配 battle_3", "battle_3" in states, f"states={states}")
        if "battle_3" in states:
            check(
                f"完成{n}次战斗进度 {n}/{n}",
                states["battle_3"]["progress"] == f"{n}/{n}",
                states["battle_3"],
            )
            check(
                f"完成{n}次战斗记 10 分",
                states["battle_3"]["activity"] == 10,
                states["battle_3"],
            )
    # 5 次战斗仍是独立任务（20 分），不能被 2/3/4 槽位吞掉
    text = "完成5次战斗（5/5）"
    rows = [{"y": 100, "text": text, "items": [{"text": text, "center": (1200, 100)}]}]
    states = bd._match_rows_to_catalog(LOG, rows)
    check("完成5次战斗匹配 battle_5", "battle_5" in states, f"states={states}")
    if "battle_5" in states:
        check(
            "完成5次战斗记 20 分",
            states["battle_5"]["activity"] == 20,
            states["battle_5"],
        )


def main():
    print("== 面板活跃度读取 ==")
    test_reader()
    print("== 真实截图取证 ==")
    test_real_evidence_image()
    print("== 战斗 2/3/4 变体 ==")
    test_battle_variant_match()
    print("== 扫描快照接线 ==")
    test_scan_snapshot_wiring()
    print("== 源码接线 ==")
    test_source_wiring()
    if FAILURES:
        print(f"\nFAILED ({len(FAILURES)}): {FAILURES}")
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
