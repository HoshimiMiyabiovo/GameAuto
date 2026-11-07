"""2026-11-07 正式购买链路回填「兑换确认」实机教训的回归。

事故：11-06 提权探针（data/artifacts/zzz_stage1.py）实机买通绝区零信号残响
（加密母带×3 + 原装母带×5，邦布币 10665→9945），靠的是两条正式代码没有的规矩：
  1. 兑换确认弹窗的**标题**就叫「兑换确认」@y≈316，真按钮在**底部** @y≈763。
     OCR 按「确认」找会先命中标题，点上去库存一点没扣，却像"点了确认"。
  2. 买完必须核对库存/货币变化，只看「点击成功」会假成功。
而正式 `_buy_once` 只走 confirm_template：模板命中不校验 y、失配没有 OCR 兜底、
点完用同一模板 `_wait_gone`（OCR 兜底时恒真）、也没有任何购买证据核对。
11-06 上午 #201「加密母带 0/5（确认后弹窗未关）」就是这个通道。

本套件锁：底部 y 闸按比例缩放 / 标题区命中被拒 / OCR 只扫底部 /
模板与 OCR 两条路径 / 按钮真的消失 / 数字签名防假成功 / 配置与调用接线。

跑法：.venv\\Scripts\\python.exe tests\\suite_confirm_button_guard_20261107.py
"""

from __future__ import annotations

import io
import json
import logging
import sys
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import game_auto.runtime.foreground_flow_runner as ffr  # noqa: E402
import game_auto.runtime.monthly_exchange as mx  # noqa: E402
import game_auto.runtime.ocr_click_tester as ocr_mod  # noqa: E402
import game_auto.runtime.zzz_monthly_shop as shop  # noqa: E402

FAILURES = []
LOG = logging.getLogger("confirm_button_guard_test")
LOG.addHandler(logging.NullHandler())


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} — {detail}")
        FAILURES.append(name)


def _item(text, x, y, conf=0.9):
    return {"text": text, "center": (x, y), "confidence": conf, "box": (x - 20, y - 10, x + 20, y + 10)}


def _patch_rect(rect):
    return mock.patch.object(ffr, "_window_rect", return_value=rect)


# ── [1] 底部 y 闸：分辨率无关、可覆盖、取不到窗口就放行 ────────────────────
def test_min_local_y():
    print("[1] 底部确认按钮下界按窗高比例换算")
    with _patch_rect((0, 0, 1920, 1080)):
        check("1080p 下界 = 700", shop._confirm_min_local_y(1) == 700, shop._confirm_min_local_y(1))
    with _patch_rect((100, 50, 1280, 720)):
        check("720p 下界 = 466（局部坐标，不叠 top）", shop._confirm_min_local_y(1) == 466)
    with _patch_rect((0, 0, 3840, 2160)):
        check("4K 下界 = 1400（不写死 700）", shop._confirm_min_local_y(1) == 1400)
    with _patch_rect((0, 0, 1920, 1080)):
        check("自定义 ratio=0.8 → 864", shop._confirm_min_local_y(1, 0.8) == 864)
        check("非法 ratio 退默认 700", shop._confirm_min_local_y(1, 9) == 700)
    with _patch_rect(None):
        check("窗口 rect 取不到 → None（放行不猜测）", shop._confirm_min_local_y(1) is None)
    with _patch_rect((0, 0, 1920, 1080)):
        region = shop._confirm_bottom_region(1, 700)
        check("OCR 区域从 y=700 起，标题进不来", region == (0, 700, 1920, 380), region)
    with _patch_rect((100, 50, 1920, 1080)):
        region = shop._confirm_bottom_region(1, 700)
        check("副屏区域叠加 left/top", region == (100, 750, 1920, 380), region)


# ── [2] OCR 兜底：只认底部按钮词，标题被排除 ──────────────────────────────
def test_ocr_bottom_confirm():
    print("[2] OCR 兜底只认底部确认按钮")
    items = [
        _item("兑换确认", 960, 316, 0.95),      # 标题：conf 更高，但 y 在上部
        _item("取消", 800, 763),                # 底部但不是按钮词
        _item("确定", 900, 800, 0.2),           # 底部但置信度太低
        _item("确认", 1122, 763, 0.88),         # 真按钮
    ]
    seen = {}

    def fake_ocr(logger, hwnd=None, region=None):
        seen["region"] = region
        return items

    with _patch_rect((0, 0, 1920, 1080)), \
            mock.patch.object(ocr_mod, "ocr_window_content", side_effect=fake_ocr), \
            mock.patch.object(shop.time, "sleep", lambda *_: None):
        point, text = shop._ocr_bottom_confirm(LOG, 1, 700, 1, None)
        check("命中底部「确认」而不是标题", point == (1122, 763) and text == "确认", (point, text))
        check("扫描区域限定底部", seen.get("region") == (0, 700, 1920, 380), seen.get("region"))

    with _patch_rect((0, 0, 1920, 1080)), \
            mock.patch.object(ocr_mod, "ocr_window_content", return_value=[_item("兑换确认", 960, 316, 0.95)]), \
            mock.patch.object(shop.time, "sleep", lambda *_: None):
        point, text = shop._ocr_bottom_confirm(LOG, 1, 700, 1, None)
        check("只有标题时找不到按钮（不点标题）", point is None and text is None, (point, text))

    # 副屏：OCR center 是屏幕绝对坐标，要减窗口原点换算回局部
    with _patch_rect((100, 50, 1920, 1080)), \
            mock.patch.object(ocr_mod, "ocr_window_content", return_value=[_item("购买", 1222, 813)]), \
            mock.patch.object(shop.time, "sleep", lambda *_: None):
        point, _text = shop._ocr_bottom_confirm(LOG, 1, 700, 1, None)
        check("副屏 center 减原点 → 局部坐标", point == (1122, 763), point)


# ── [3] 模板命中也要过底部闸；失配/标题区一律转 OCR ──────────────────────
def test_locate_confirm_button():
    print("[3] 模板 / OCR 两条定位路径")
    with _patch_rect((0, 0, 1920, 1080)), \
            mock.patch.object(shop, "_locate", return_value=(960, 316)), \
            mock.patch.object(shop, "_ocr_bottom_confirm", return_value=((1122, 763), "确认")) as ocr_fallback:
        point, method = shop._locate_confirm_button(LOG, 1, "tpl", [1.0], [0.8], None)
        check("模板命中标题区 → 不采用，转 OCR 底部", point == (1122, 763) and method == "ocr", (point, method))
        check("确实调用了 OCR 兜底", ocr_fallback.called)

    with _patch_rect((0, 0, 1920, 1080)), \
            mock.patch.object(shop, "_locate", return_value=(1122, 763)), \
            mock.patch.object(shop, "_ocr_bottom_confirm") as ocr_fallback:
        point, method = shop._locate_confirm_button(LOG, 1, "tpl", [1.0], [0.8], None)
        check("模板命中底部按钮 → 直接采用", point == (1122, 763) and method == "template", (point, method))
        check("底部命中不多跑一次 OCR", not ocr_fallback.called)

    with _patch_rect((0, 0, 1920, 1080)), \
            mock.patch.object(shop, "_locate", return_value=None), \
            mock.patch.object(shop, "_ocr_bottom_confirm", return_value=((1122, 763), "确认")):
        point, method = shop._locate_confirm_button(LOG, 1, "tpl", [1.0], [0.8], None)
        check("模板失配 → OCR 兜底成功", point == (1122, 763) and method == "ocr", (point, method))

    with _patch_rect((0, 0, 1920, 1080)), \
            mock.patch.object(shop, "_locate", return_value=None), \
            mock.patch.object(shop, "_ocr_bottom_confirm", return_value=(None, None)):
        point, method = shop._locate_confirm_button(LOG, 1, "tpl", [1.0], [0.8], None)
        check("两条路径都没有 → None（按已达上限处理）", point is None and method is None)


# ── [4] 点完确认必须真的看到按钮消失（OCR 兜底路径专用）────────────────────
def test_confirm_gone():
    print("[4] 点完确认复核按钮真的消失")
    with _patch_rect((0, 0, 1920, 1080)), \
            mock.patch.object(ocr_mod, "ocr_window_content", return_value=[_item("确认", 1122, 763)]):
        check("按钮还在原位 → 判没消失", shop._confirm_button_still_at(LOG, 1, (1122, 763), 700))
    with _patch_rect((0, 0, 1920, 1080)), \
            mock.patch.object(ocr_mod, "ocr_window_content", return_value=[_item("确认", 300, 900)]):
        check("别处的同名文字不算按钮还在", not shop._confirm_button_still_at(LOG, 1, (1122, 763), 700))
    with _patch_rect((0, 0, 1920, 1080)), \
            mock.patch.object(ocr_mod, "ocr_window_content", return_value=[]), \
            mock.patch.object(shop.time, "sleep", lambda *_: None):
        check("按钮消失 → 等待返回 True", shop._wait_confirm_button_gone(LOG, 1, (1122, 763), 700, None, 1))
    with _patch_rect((0, 0, 1920, 1080)), \
            mock.patch.object(ocr_mod, "ocr_window_content", return_value=[_item("确认", 1122, 763)]), \
            mock.patch.object(shop.time, "sleep", lambda *_: None):
        check("按钮一直在 → 等待返回 False", not shop._wait_confirm_button_gone(LOG, 1, (1122, 763), 700, None, 1))


# ── [5] 购买证据签名：只收命中标签的数字，读不到保守放行 ──────────────────
def test_verify_signature():
    print("[5] 限购/库存数字签名")
    items = [
        _item("月剩余×3", 500, 300),
        _item("月剩余 × 5", 700, 300),
        _item("本月限购 0/3", 900, 600),
        _item("UID:21054491", 1800, 1050),
    ]
    with mock.patch.object(ocr_mod, "ocr_window_content", return_value=items):
        check("只收「月剩余」的数字且排序", shop._read_verify_signature(LOG, 1, {"labels": ["月剩余"]}) == (3, 5))
        check("换标签读到不同数字", shop._read_verify_signature(LOG, 1, {"labels": ["本月限购"]}) == (0, 3))
    with mock.patch.object(ocr_mod, "ocr_window_content", return_value=[_item("无关文字", 1, 1)]):
        check("没有标签 → None（放行）", shop._read_verify_signature(LOG, 1, {"labels": ["月剩余"]}) is None)
    with mock.patch.object(ocr_mod, "ocr_window_content", side_effect=RuntimeError("ocr down")):
        check("OCR 异常 → None（放行）", shop._read_verify_signature(LOG, 1, {"labels": ["月剩余"]}) is None)
    check("未配置 / 空标签 → None", shop._read_verify_signature(LOG, 1, None) is None
          and shop._read_verify_signature(LOG, 1, {"labels": []}) is None)


# ── [6] _buy_once 端到端打桩 ──────────────────────────────────────────────
def _buy_once_harness(*, confirm=((1122, 763), "ocr"), gone=True, signatures=None, step=None):
    recorded = []

    def fake_click(hwnd, x, y, clicks=1, **kwargs):  # noqa: A002 - 对齐生产函数签名
        recorded.append((x, y))

    patches = [
        mock.patch.object(shop, "_locate_item", return_value=(571, 276)),
        mock.patch.object(ffr, "_click_window_local", side_effect=fake_click),
        mock.patch.object(shop, "_locate_confirm_button", return_value=confirm),
        mock.patch.object(shop, "_wait_gone", return_value=gone),
        mock.patch.object(shop, "_wait_confirm_button_gone", return_value=gone),
        mock.patch.object(shop, "_dismiss_reward_popup", return_value=True),
        mock.patch.object(shop, "SETTLE_SEC", 0),
        mock.patch.object(shop, "POST_BUY_SETTLE_SEC", 0),
        mock.patch.object(shop, "RETRY_SETTLE_SEC", 0),
        mock.patch.object(shop.time, "sleep", lambda *_: None),
    ]
    if signatures is not None:
        patches.append(mock.patch.object(shop, "_read_verify_signature", side_effect=list(signatures)))
    else:
        patches.append(mock.patch.object(shop, "_read_verify_signature", return_value=None))
    with _patch_rect((0, 0, 1920, 1080)):
        for p in patches:
            p.start()
        try:
            result = shop._buy_once(
                LOG, 1, {"name": "加密母带"}, "tpl", [1.0], [0.8], None,
                label="信源兑换", step=step,
            )
        finally:
            for p in reversed(patches):
                p.stop()
    return result, recorded


def test_buy_once():
    print("[6] _buy_once 端到端")
    result, clicks = _buy_once_harness()
    check("OCR 兜底路径买成功 → BOUGHT", result == shop.BOUGHT, result)
    check("确认点落在底部按钮 (1122,763)", (1122, 763) in clicks, clicks)

    result, _ = _buy_once_harness(gone=False)
    check("OCR 路径按钮没消失 → CONFIRM_GONE_FAIL", result == shop.CONFIRM_GONE_FAIL, result)

    result, _ = _buy_once_harness(confirm=((960, 316), "template"))
    check("模板路径仍走 _wait_gone（旧行为不变）", result == shop.BOUGHT, result)

    step = {"purchase_verify": {"labels": ["月剩余"]}}
    result, _ = _buy_once_harness(signatures=[(3, 5), (3, 5)], step=step)
    check("点完确认但数字没变 → PURCHASE_UNVERIFIED", result == shop.PURCHASE_UNVERIFIED, result)

    result, _ = _buy_once_harness(signatures=[(3, 5), (5,)], step=step)
    check("数字变了 → BOUGHT", result == shop.BOUGHT, result)

    result, _ = _buy_once_harness(signatures=[None, None], step=step)
    check("两次都读不到 → 保守放行 BOUGHT", result == shop.BOUGHT, result)

    result, _ = _buy_once_harness(step=None)
    check("未配置 purchase_verify → 不做数字核对", result == shop.BOUGHT, result)


# ── [7] 接线：配置、调用、失败码、静态口径 ────────────────────────────────
def test_wiring():
    print("[7] 接线与静态口径")
    cfgs = {}
    for name in ("flow_configs.json", "flow_configs.example.json"):
        cfgs[name] = json.loads((PROJECT_ROOT / name).read_text(encoding="utf-8"))
    for key, label in (("ZZZ_MONTHLY", "月剩余"),
                       ("ZZZ_LOGISTICS_MONTHLY", "月剩余"),
                       ("NTE_MONTHLY", "本月库存")):
        for name, data in cfgs.items():
            verify = data[key]["steps"][-1].get("purchase_verify")
            check(f"{name} {key} 配了 purchase_verify", bool(verify) and label in verify["labels"], verify)
    check("两份配置三条流程完全一致",
          all(cfgs["flow_configs.json"][k] == cfgs["flow_configs.example.json"][k]
              for k in ("ZZZ_MONTHLY", "ZZZ_LOGISTICS_MONTHLY", "NTE_MONTHLY")))

    shop_src = (PROJECT_ROOT / "src/game_auto/runtime/zzz_monthly_shop.py").read_text(encoding="utf-8")
    mx_src = (PROJECT_ROOT / "src/game_auto/runtime/monthly_exchange.py").read_text(encoding="utf-8")
    check("_buy_once 走 _locate_confirm_button", "_locate_confirm_button(" in shop_src)
    check("确认定位不再裸调 _locate", "_locate(hwnd, confirm_template, scales, confidences, CONFIRM_TIMEOUT_SEC" not in shop_src)
    check("OCR 兜底路径单独复核按钮消失", "_wait_confirm_button_gone(" in shop_src)
    check("模板命中过底部 y 闸", "center[1] >= min_y" in shop_src)
    check("没有写死 y>700", "y > 700" not in shop_src and "y>700" not in shop_src)
    check("共享比例来自 ui_coords", "from .ui_coords import CONFIRM_MIN_Y_RATIO" in shop_src)
    check("购买证据核对接进 _buy_once", "after == before" in shop_src)
    check("monthly_exchange 透传 step", "step=step" in mx_src and "PURCHASE_UNVERIFIED" in mx_src)
    check("后勤商店透传 step", "step=step" in shop_src)
    check("两处失败摘要都有假成功说明",
          shop_src.count("疑似假成功") >= 1 and mx_src.count("疑似假成功") >= 1)


def main():
    test_min_local_y()
    test_ocr_bottom_confirm()
    test_locate_confirm_button()
    test_confirm_gone()
    test_verify_signature()
    test_buy_once()
    test_wiring()


if __name__ == "__main__":
    main()
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILED: {FAILURES}")
        sys.exit(1)
    print("\nALL PASS")
