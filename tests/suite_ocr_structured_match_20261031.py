# -*- coding: utf-8 -*-
"""OCR 结构化匹配回归（2026-10-31）。

背景：MiniMax「签到得」按旧逻辑子串命中卡片宣传语「连续签到得更多积分」，
点在文案中间，真正的按钮从未被点击，接口日志里没有 signin/claim。

本套件锁定：
1. legacy 保留旧行为（可复现撞词），smart 按 精确>前缀>子串 排序；
2. anchor + direction/max_distance 几何过滤，锚点缺失时 required 语义；
3. exact / prefix / substring / reverse 各模式边界；
4. 排除词仍作最后兜底；
5. 歧义检测、ambiguous_action=fail、候选标注图（含节流）；
6. 流程 runner 透传 ocr_match_mode/ocr_anchor/ocr_near/ocr_debug_snapshot；
7. MiniMax / WorkBuddy 配置迁移。

跑法：.venv\\Scripts\\python.exe tests\\suite_ocr_structured_match_20261031.py
"""

import json
import logging
import shutil
import sys
import tempfile
from pathlib import Path

from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT.parent))

from game_auto.runtime import ocr_click_tester as oct  # noqa: E402
from game_auto.runtime import foreground_flow_runner as ffr  # noqa: E402

FAILURES = []
LOGGER = logging.getLogger("test.ocr.structured")
LOGGER.setLevel(logging.DEBUG)
_MESSAGES = []


class _Capture(logging.Handler):
    def emit(self, record):
        _MESSAGES.append(record.getMessage())


LOGGER.addHandler(_Capture())
LOGGER.propagate = False

_ORIG_SCAN = oct._ocr_scan
_ORIG_DEBUG_DIR = oct.PROJECT_OCR_DEBUG_DIR


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def _patch_scan(results, size=(1920, 1080)):
    image = Image.new("RGB", size, (30, 30, 30))
    oct._ocr_scan = lambda *_a, **_k: ([dict(r) for r in results], image, None)


def _scene():
    """用户 2026-10-31 日志里的真实布局：宣传语在上，按钮在下。"""
    return [
        {
            "text": "连续签到得更多积分",
            "confidence": 0.85,
            "center": (582.0, 748.0),
            "box": (500, 730, 660, 766),
            "source": "rapidocr",
        },
        {
            "text": "签到得 ⊙ 400",
            "confidence": 0.90,
            "center": (330.0, 900.0),
            "box": (280, 880, 380, 920),
            "source": "rapidocr",
        },
    ]


PROMO = "连续签到得更多积分"
BUTTON = "签到得 ⊙ 400"


def _smart_anchor_args():
    return {
        "match_mode": "smart",
        "anchor": ["连续签到"],
        "near": {"direction": "below", "max_distance": 500},
    }


def test_legacy_vs_smart():
    print("[1] legacy 复现撞词 / smart 优先按钮")
    try:
        _patch_scan(_scene())
        hit = oct.ocr_find_any_text(["签到得"], LOGGER, match_mode="legacy")
        check("legacy 仍先撞宣传语（原始 bug）", hit is not None and hit["text"] == PROMO,
              hit and hit["text"])

        hit = oct.ocr_find_any_text(["签到得"], LOGGER, match_mode="smart")
        check("smart 无锚点也优先按钮（前缀>子串）",
              hit is not None and hit["text"] == BUTTON, hit and hit["text"])
        check("smart 返回匹配等级", hit is not None and hit.get("match_tier") == "prefix",
              hit and hit.get("match_tier"))

        hit = oct.ocr_find_any_text(["签到得⊙400", "签到得 400", "签到得"], LOGGER,
                                    match_mode="smart")
        check("多候选按钮文案精确命中", hit is not None and hit["text"] == BUTTON,
              hit and hit["text"])
        check("精确命中优先于前缀", hit is not None and hit.get("match_tier") == "exact",
              hit and hit.get("match_tier"))
    finally:
        oct._ocr_scan = _ORIG_SCAN


def test_anchor_geometry():
    print("[2] 锚点 / 方向 / 距离")
    try:
        _patch_scan(_scene())
        hit = oct.ocr_find_any_text(["签到得"], LOGGER, **_smart_anchor_args())
        check("锚点下方按钮被选中", hit is not None and hit["text"] == BUTTON,
              hit and hit["text"])
        check("返回锚点坐标", hit is not None and hit.get("anchor_center") == (582.0, 748.0),
              hit and hit.get("anchor_center"))

        # 远处还有一个「签到得」精确候选：锚点 max_distance 必须把它挡掉
        far = _scene() + [{
            "text": "签到得",
            "confidence": 0.99,
            "center": (1500.0, 200.0),
            "box": (1470, 180, 1530, 220),
            "source": "rapidocr",
        }]
        _patch_scan(far)
        hit = oct.ocr_find_any_text(["签到得"], LOGGER, **_smart_anchor_args())
        check("锚点距离过滤远处精确候选",
              hit is not None and hit["text"] == BUTTON, hit and hit["text"])

        # 宣传语在锚点同一点，direction=below 必须排除它
        _patch_scan(_scene())
        hit = oct.ocr_find_any_text(
            ["签到得"], LOGGER, match_mode="smart",
            anchor=["连续签到"], near={"direction": "below", "max_distance": 500},
            save_debug=False,
        )
        check("锚点自身不当作下方按钮", hit is not None and hit["text"] == BUTTON,
              hit and hit["text"])

        hit = oct.ocr_find_any_text(
            ["签到得"], LOGGER, match_mode="smart",
            anchor=["连续签到"], near={"direction": "above", "max_distance": 500},
        )
        check("方向不符时判定未匹配", hit is None, hit and hit["text"])

        # 锚点找不到：required=true 直接失败，不让全局候选乱点
        _patch_scan([_scene()[1]])
        hit = oct.ocr_find_any_text(
            ["签到得"], LOGGER, match_mode="smart",
            anchor={"texts": ["连续签到"], "required": True},
            near={"direction": "below", "max_distance": 500},
        )
        check("锚点 required 且缺失时返回 None", hit is None, hit and hit["text"])

        hit = oct.ocr_find_any_text(
            ["签到得"], LOGGER, match_mode="smart",
            anchor=["连续签到"],
            near={"direction": "below", "max_distance": 500},
        )
        check("锚点非 required 时回退全量", hit is not None and hit["text"] == BUTTON,
              hit and hit["text"])
    finally:
        oct._ocr_scan = _ORIG_SCAN


def test_modes():
    print("[3] exact / prefix / substring / reverse")
    try:
        _patch_scan(_scene())
        hit = oct.ocr_find_any_text(["签到得"], LOGGER, match_mode="exact")
        check("exact 不接受前缀/子串", hit is None, hit and hit["text"])

        hit = oct.ocr_find_any_text(["签到得⊙400"], LOGGER, match_mode="exact")
        check("exact 命中归一化后的按钮", hit is not None and hit["text"] == BUTTON,
              hit and hit["text"])

        hit = oct.ocr_find_any_text(["签到得"], LOGGER, match_mode="prefix")
        check("prefix 接受按钮前缀", hit is not None and hit["text"] == BUTTON,
              hit and hit["text"])

        _patch_scan([_scene()[0]])
        hit = oct.ocr_find_any_text(["签到得"], LOGGER, match_mode="substring")
        check("substring 接受宣传语子串", hit is not None and hit["text"] == PROMO,
              hit and hit["text"])

        _patch_scan([{
            "text": "脱机",
            "confidence": 0.80,
            "center": (100.0, 100.0),
            "box": (80, 90, 120, 110),
            "source": "rapidocr",
        }])
        hit = oct.ocr_find_any_text(["进入脱机"], LOGGER, match_mode="smart")
        check("smart 保留 OCR 漏字的反向匹配",
              hit is not None and hit["text"] == "脱机", hit and hit["text"])
        check("反向匹配等级最低", hit is not None and hit.get("match_tier") == "reverse",
              hit and hit.get("match_tier"))

        hit = oct.ocr_find_any_text(["进入脱机"], LOGGER, match_mode="substring")
        check("substring 模式不允许反向匹配", hit is None, hit and hit["text"])
    finally:
        oct._ocr_scan = _ORIG_SCAN


def test_exclusions():
    print("[4] 排除词仍是最后兜底")
    try:
        _patch_scan(_scene())
        hit = oct.ocr_find_any_text(
            ["签到得"], LOGGER, match_mode="smart", exclude_texts=["更多积分"])
        check("排除宣传语后选中按钮", hit is not None and hit["text"] == BUTTON,
              hit and hit["text"])

        hit = oct.ocr_find_any_text(
            ["签到得"], LOGGER, match_mode="smart", exclude_texts=["签到"])
        check("全部被排除时返回 None", hit is None, hit and hit["text"])

        chosen, meta = oct._rank_ocr_candidates(
            _scene(), ["签到得"], "smart", 0.5, exclude_texts=["更多积分"])
        promo_item = next(i for i in meta["items"] if i["text"] == PROMO)
        check("被排除项标记为 excluded", promo_item["status"] == "excluded",
              promo_item["status"])
        check("排除后最优为按钮", chosen is not None and chosen["text"] == BUTTON,
              chosen and chosen["text"])

        # 锚点文字和被排除的宣传语是同一句：排除不能把锚点一起挡掉
        chosen, meta = oct._rank_ocr_candidates(
            _scene(), ["签到得"], "smart", 0.5,
            exclude_texts=["更多积分"],
            anchor_texts=["连续签到"],
            near={"direction": "below", "max_distance": 500},
        )
        check("被排除的宣传语仍可当锚点",
              meta["anchor_item"] is not None and meta["anchor_item"]["text"] == PROMO,
              meta["anchor_item"] and meta["anchor_item"]["text"])
        check("排除+锚点仍选中按钮", chosen is not None and chosen["text"] == BUTTON,
              chosen and chosen["text"])
    finally:
        oct._ocr_scan = _ORIG_SCAN


def test_ambiguity_and_debug():
    print("[5] 歧义检测与候选标注图")
    debug_dir = tempfile.mkdtemp(prefix="ocr_match_debug_")
    oct.PROJECT_OCR_DEBUG_DIR = debug_dir
    oct._last_debug_save.clear()
    try:
        ambiguous_results = [
            {"text": "领取", "confidence": 0.90, "center": (100.0, 100.0),
             "box": (80, 90, 120, 110), "source": "rapidocr"},
            {"text": "领取", "confidence": 0.90, "center": (500.0, 100.0),
             "box": (480, 90, 520, 110), "source": "rapidocr"},
        ]
        _patch_scan(ambiguous_results)
        _MESSAGES.clear()
        hit = oct.ocr_find_any_text(
            ["领取"], LOGGER, match_mode="smart", save_debug=True, debug_tag="ambig")
        check("歧义仍选出最高分候选", hit is not None and hit["text"] == "领取",
              hit and hit["text"])
        check("返回歧义标记", hit is not None and hit.get("ambiguous") is True,
              hit and hit.get("ambiguous"))
        check("记录歧义告警", any("歧义" in m for m in _MESSAGES), _MESSAGES[-3:])
        check("歧义自动保存标注图",
              hit is not None and hit.get("debug_image")
              and Path(hit["debug_image"]).is_file(), hit and hit.get("debug_image"))

        _MESSAGES.clear()
        hit = oct.ocr_find_any_text(
            ["领取"], LOGGER, match_mode="smart",
            ambiguous_action="fail", save_debug=True, debug_tag="ambig_fail")
        check("ambiguous_action=fail 时不瞎点", hit is None, hit and hit["text"])
        check("fail 模式仍记录歧义", any("歧义" in m for m in _MESSAGES), _MESSAGES[-3:])

        oct._last_debug_save.clear()
        _patch_scan(_scene())
        hit = oct.ocr_find_any_text(
            ["签到得"], LOGGER, match_mode="smart", save_debug=True, debug_tag="one")
        first_image = hit and hit.get("debug_image")
        check("save_debug 保存标注图", bool(first_image) and Path(first_image).is_file(),
              first_image)
        hit = oct.ocr_find_any_text(
            ["签到得"], LOGGER, match_mode="smart", save_debug=True, debug_tag="one")
        check("同一步骤 5 秒内不重复保存",
              hit is not None and not hit.get("debug_image"), hit and hit.get("debug_image"))
        hit = oct.ocr_find_any_text(
            ["签到得"], LOGGER, match_mode="smart", save_debug=True, debug_tag="two")
        check("不同步骤各自保存",
              hit is not None and bool(hit.get("debug_image")), hit and hit.get("debug_image"))

        hit = oct.ocr_find_any_text(
            ["签到得"], LOGGER, match_mode="legacy", save_debug=True, debug_tag="legacy")
        check("legacy 也返回标注图",
              hit is not None and bool(hit.get("debug_image"))
              and Path(hit["debug_image"]).is_file(), hit and hit.get("debug_image"))
        check("legacy 标注图仍按旧逻辑选宣传语", hit is not None and hit["text"] == PROMO,
              hit and hit["text"])

        oct._last_debug_save.clear()
        before = len(list(Path(debug_dir).glob("*.jpg")))
        missing = [{"text": "完全无关", "confidence": 0.9, "center": (10.0, 10.0),
                    "box": (0, 0, 20, 20), "source": "rapidocr"}]
        _patch_scan(missing)
        hit = oct.ocr_find_any_text(
            ["签到得"], LOGGER, match_mode="smart", save_debug=True, debug_tag="miss")
        after = len(list(Path(debug_dir).glob("*.jpg")))
        check("未命中时也保存标注图（看得到扫到了什么）",
              hit is None and after > before, (before, after, hit))
    finally:
        oct._ocr_scan = _ORIG_SCAN
        oct.PROJECT_OCR_DEBUG_DIR = _ORIG_DEBUG_DIR
        oct._last_debug_save.clear()
        shutil.rmtree(debug_dir, ignore_errors=True)


def test_runner_wiring():
    print("[6] 流程 runner 透传")
    captured = {}
    original = oct.ocr_find_any_text

    def fake_find(**kwargs):
        captured.update(kwargs)
        return {"center": (10, 20)}

    oct.ocr_find_any_text = fake_find
    try:
        point = ffr._ocr_locate_in_window(
            LOGGER,
            None,
            ["签到得"],
            match_mode="smart",
            anchor=["连续签到"],
            near={"direction": "below", "max_distance": 500},
            ambiguous_action="fail",
            debug_tag="flow_s4",
            save_debug=True,
        )
    finally:
        oct.ocr_find_any_text = original
    check("_ocr_locate_in_window 返回坐标", point == (10, 20), point)
    check("透传 match_mode", captured.get("match_mode") == "smart", captured.get("match_mode"))
    check("透传 anchor", captured.get("anchor") == ["连续签到"], captured.get("anchor"))
    check("透传 near", captured.get("near") == {"direction": "below", "max_distance": 500},
          captured.get("near"))
    check("透传 ambiguous_action", captured.get("ambiguous_action") == "fail",
          captured.get("ambiguous_action"))
    check("透传 debug_tag/save_debug",
          captured.get("debug_tag") == "flow_s4" and captured.get("save_debug") is True,
          (captured.get("debug_tag"), captured.get("save_debug")))

    runner_src = Path(ffr.__file__).read_text(encoding="utf-8")
    check("runner 读取步骤 match_mode",
          'step.get("ocr_match_mode", step.get("match_mode"))' in runner_src)
    check("runner 读取步骤 anchor/near",
          'step.get("ocr_anchor", step.get("anchor"))' in runner_src
          and 'step.get("ocr_near", step.get("near"))' in runner_src)
    check("runner 读取 ocr_ambiguous", 'step.get("ocr_ambiguous", "warn")' in runner_src)
    check("runner 读取 ocr_debug_snapshot",
          'step.get("ocr_debug_snapshot", flow_ocr_debug)' in runner_src)
    check("runner 把配置传给 OCR 定位",
          "save_debug=step_ocr_debug" in runner_src and "anchor=step_ocr_anchor" in runner_src)

    ocr_src = Path(oct.__file__).read_text(encoding="utf-8")
    check("OCR 入口支持 match_mode", 'match_mode: str = "legacy"' in ocr_src)
    check("OCR 有分层常量", "_MATCH_TIER_EXACT = 4" in ocr_src and "_MATCH_TIER_REVERSE = 1" in ocr_src)
    check("OCR 单次扫描返回截图", "def _ocr_scan(" in ocr_src
          and "return _check_log_contamination(logger, results), screenshot, current_region" in ocr_src)


def test_configs():
    print("[7] MiniMax / WorkBuddy 配置迁移")
    for name in ("flow_configs.json", "flow_configs.example.json"):
        path = PROJECT_ROOT / name
        if not path.is_file():
            continue
        cfg = json.loads(path.read_text(encoding="utf-8"))

        minimax = cfg.get("MINIMAX") or {}
        check(f"{name} MINIMAX 流程 smart", minimax.get("ocr_match_mode") == "smart",
              minimax.get("ocr_match_mode"))
        step = next((s for s in minimax.get("steps") or []
                     if s.get("name") == "点击签到得按钮"), None)
        check(f"{name} MiniMax 步骤4存在", step is not None)
        if step:
            check(f"{name} MiniMax 步骤4 smart", step.get("ocr_match_mode") == "smart",
                  step.get("ocr_match_mode"))
            check(f"{name} MiniMax 锚点=连续签到",
                  "连续签到" in (step.get("ocr_anchor") or []), step.get("ocr_anchor"))
            near = step.get("ocr_near") or {}
            check(f"{name} MiniMax 只取下方按钮",
                  near.get("direction") == "below" and near.get("max_distance", 0) > 0, near)
            check(f"{name} MiniMax 保留兜底排除词",
                  "更多积分" in (step.get("ocr_exclude_texts") or []),
                  step.get("ocr_exclude_texts"))
            check(f"{name} MiniMax 步骤4保存标注图",
                  step.get("ocr_debug_snapshot") is True, step.get("ocr_debug_snapshot"))

        workbuddy = cfg.get("WORKBUDDY") or {}
        check(f"{name} WORKBUDDY 流程 smart",
              workbuddy.get("ocr_match_mode") == "smart", workbuddy.get("ocr_match_mode"))
        steps = workbuddy.get("steps") or []
        for idx, label in ((1, "步骤2"), (5, "步骤6")):
            if idx >= len(steps):
                check(f"{name} WORKBUDDY {label} 存在", False, len(steps))
                continue
            s = steps[idx]
            check(f"{name} WORKBUDDY {label} OCR 定位 Buddy加油站",
                  s.get("target_text") == "Buddy加油站", s.get("target_text"))
            check(f"{name} WORKBUDDY {label} 保存标注图",
                  s.get("ocr_debug_snapshot") is True, s.get("ocr_debug_snapshot"))
        if len(steps) >= 7:
            check(f"{name} WorkBuddy 验证步保存标注图",
                  steps[6].get("ocr_debug_snapshot") is True,
                  steps[6].get("ocr_debug_snapshot"))


def main():
    test_legacy_vs_smart()
    test_anchor_geometry()
    test_modes()
    test_exclusions()
    test_ambiguity_and_debug()
    test_runner_wiring()
    test_configs()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
