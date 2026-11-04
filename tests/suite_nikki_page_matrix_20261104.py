"""无限暖暖页面判定矩阵（2026-11-04 用户要求：用判定法测其他页面）。

背景：用户批评「有奇想日历就在奇想日历界面？」——只靠页面里出现同名字样
判定会误判。本测试把 data/artifacts 下所有实际截图（参考图 + 失败现场）
逐张跑 OCR，再对每个页面判定函数求笛卡尔积，断言：
  - 本页面判定必须命中；
  - 其他页面判定不得误命中（只允许真实 UI 层级包含关系，如共鸣商店在商城内）。

跑法：.venv\\Scripts\\python.exe tests/suite_nikki_page_matrix_20261104.py
"""

import io
import sys
from pathlib import Path

import numpy as np
from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ART = PROJECT_ROOT / "data" / "artifacts"
FF = ART / "flow_failures"

PAGES = {
    "calendar": ART / "ref_nikki_calendar_20261101.png",
    "hub": ART / "ref_nikki_crown_hub_20261101.png",
    "claim": ART / "ref_nikki_crown_claim_20261101.png",
    "reward": ART / "ref_nikki_reward_continue_20261101.png",
    "stage": ART / "ref_nikki_crown_stage_20261101.png",
    "exchange": ART / "ref_nikki_crystal_exchange_20261101.png",
    "dialog": ART / "ref_nikki_exchange_dialog_20261101.png",
    "shop": ART / "ref_nikki_shop_20261101.png",
    "shop_menu": ART / "ref_nikki_shop_menu_scrolled_20261101.png",
    "surge": ART / "ref_nikki_resonance_surge_20261101.png",
    "serene": ART / "ref_nikki_resonance_serene_20261101.png",
    "main": ART / "ref_nikki_main_20261101.png",
    # 2026-11-04 08:52 实际失败现场（游戏在主世界）
    "fw_main_085220": FF / "20261104_085220_NikkiLauncher_奇迹之冠-入口.jpg",
    "fw_main_085227": FF / "20261104_085227_NikkiLauncher_晶石兑换-入口.jpg",
    "fw_main_085255": FF / "20261104_085255_NikkiLauncher_共鸣商店-商城.jpg",
    # 2026-11-02 主世界（OCR 识别不到 HUD，但也不能误判成任何子页面）
    "fw_main_173139": FF / "20261102_173139_NikkiLauncher_奇想日历-打开.jpg",
    # 2026-11-01 搭配赛点不进去，停在 hub
    "fw_hub_122626": FF / "20261101_122626_NikkiLauncher_奇迹之冠搭配赛-页面.jpg",
    # 更早的启动器/主界面特征参考图（负样本：不能命中任何子页面判定）
    "launcher_button_1022": ART / "ref_nikki_launcher_button_20261022.png",
    "main_feature_1022": ART / "ref_nikki_main_feature_20261022.png",
}

# 每个页面允许命中的判定集合（真实 UI 包含关系之外一律不允许）
EXPECTED = {
    "calendar": {"calendar"},
    "hub": {"hub"},
    "claim": {"stage", "claim_modal", "claim"},
    "reward": {"continue"},
    "stage": {"stage"},
    "exchange": {"exchange"},
    "dialog": {"exchange"},
    "shop": {"shop"},
    "shop_menu": {"shop"},
    "surge": {"shop", "resonance"},
    "serene": {"shop", "resonance"},
    "main": {"main_world"},
    "fw_main_085220": {"main_world"},
    "fw_main_085227": {"main_world"},
    "fw_main_085255": {"main_world"},
    "fw_main_173139": set(),
    "fw_hub_122626": {"hub"},
    "launcher_button_1022": set(),
    "main_feature_1022": set(),
}

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def _rows(engine, path):
    img = Image.open(path).convert("RGB")
    result, _ = engine(np.asarray(img))
    rows = []
    for box, text, conf in result or []:
        x1 = min(p[0] for p in box)
        y1 = min(p[1] for p in box)
        x2 = max(p[0] for p in box)
        y2 = max(p[1] for p in box)
        rows.append({
            "text": str(text),
            "confidence": float(conf),
            "box": (int(x1), int(y1), int(x2), int(y2)),
            "center": (int((x1 + x2) // 2), int((y1 + y2) // 2)),
        })
    return rows


def main():
    from game_auto.runtime import nikki_calendar as nc
    from game_auto.runtime.ocr_click_tester import _get_rapidocr_engine

    engine = _get_rapidocr_engine()
    if not engine:
        print("SKIP RapidOCR 不可用")
        return 0

    checks = {
        "calendar": lambda rows: nc._is_calendar_page(rows),
        "main_world": lambda rows: nc._is_main_world(rows),
        "stage": lambda rows: nc._is_stage_page(rows),
        "exchange": lambda rows: nc._is_exchange_page(rows),
        "shop": lambda rows: nc._is_shop_page(rows),
        "resonance": lambda rows: nc._is_resonance_page(rows),
        "hub": lambda rows: bool(nc._pick(rows, nc.CROWN_HUB_MARKERS, min_conf=0.5)),
        "claim_modal": lambda rows: bool(nc._pick(rows, (nc.STAGE_CLAIM_MODAL,), 0.5)),
        "claim": lambda rows: bool(nc._pick(rows, nc.CLAIM_TEXTS, 0.5)),
        "continue": lambda rows: bool(nc._pick(rows, nc.CONTINUE_TEXTS, 0.5)),
    }

    for tag, path in PAGES.items():
        if not path.exists():
            check(f"{tag} 截图存在", False, path)
            continue
        rows = _rows(engine, path)
        hit = {name for name, fn in checks.items() if fn(rows)}
        expected = EXPECTED[tag]
        check(f"{tag}: 命中 {sorted(hit)}（期望 {sorted(expected)}）", hit == expected, hit)

    # 关键互斥对单独断言，失败时比整集合更直观
    print("[互斥对]")
    rows_main = _rows(engine, PAGES["fw_main_085220"])
    check("主世界不判成奇想日历", not nc._is_calendar_page(rows_main))
    rows_ex = _rows(engine, PAGES["exchange"])
    check("晶石兑换页不判成搭配赛页", not nc._is_stage_page(rows_ex))
    check("晶石兑换页不判成共鸣商店", not nc._is_resonance_page(rows_ex))
    rows_serene = _rows(engine, PAGES["serene"])
    check("共鸣商店不判成晶石兑换页", not nc._is_exchange_page(rows_serene))
    rows_cal = _rows(engine, PAGES["calendar"])
    check("奇想日历页不判成主世界", not nc._is_main_world(rows_cal))

    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())