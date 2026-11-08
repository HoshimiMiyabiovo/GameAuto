"""2026-11-06 暖暖共鸣商店两页签的**双向商品指纹**回归。

用户提出：两个页签各有独占商品，应该像绝区零/崩铁/原神那样用
`tab_content_markers` + `forbidden_texts` 双向判定，而不是只看同名商品。

背景（22:2x 事故）：一次性探针停在「澎湃浪花兑换」页就调了购买函数。
两页都卖「启示水晶 / 共鸣水晶 / 无敌碎星闪粉」，只是货币不同
（澎湃 20 粉色浪花 / 静谧 40 蓝色水滴），**这三个不能当指纹**。
正式代码原本只有正向 `SERENE_MARKERS`，没有反向排除；本套件锁死补齐后的行为。

跑法：.venv\\Scripts\\python.exe tests\\suite_nikki_tab_fingerprint_20261106.py
"""

from __future__ import annotations

import ast
import io
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import game_auto.runtime.nikki_calendar as nc  # noqa: E402

FAILURES = []
SHARED_ITEMS = ("启示水晶", "共鸣水晶", "无敌碎星闪粉")


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} :: {detail}")
        FAILURES.append(name)


def row(text, conf=0.85):
    return {"text": text, "confidence": conf, "center": (100, 100),
            "box": (60, 88, 140, 112)}


def page(*names):
    return [row(n) for n in names]


def test_disjoint_fingerprints():
    print("[1] 两组指纹互斥，且不含两页共有的同名商品")
    check("SERENE / SURGE 无交集",
          not (set(nc.SERENE_MARKERS) & set(nc.SURGE_MARKERS)),
          (nc.SERENE_MARKERS, nc.SURGE_MARKERS))
    for shared in SHARED_ITEMS:
        check(f"共有商品「{shared}」不在任何指纹里",
              shared not in nc.SERENE_MARKERS and shared not in nc.SURGE_MARKERS)
    check("静谧页含万象星实/棱镜", "万象星实" in nc.SERENE_MARKERS
          and "虹彩棱镜" in nc.SERENE_MARKERS)
    check("澎湃页含日光果/花露", "日光果" in nc.SURGE_MARKERS
          and "缓速花露" in nc.SURGE_MARKERS)


def test_guard():
    print("[2] 双向闸：静谧独占必须出现、澎湃独占必须一个都不出现")
    surge_page = page("商城", "购物车", "澎湃浪花兑换", "静谧水滴兑换",
                      "启示水晶", "共鸣水晶", "无敌碎星闪粉",
                      "慢慢恢复花露", "日光果", "星光果")
    serene_page = page("商城", "购物车", "澎湃浪花兑换", "静谧水滴兑换",
                       "启示水晶", "共鸣水晶", "无敌碎星闪粉",
                       "万象星实×1250", "璨光棱镜", "噗灵x2000", "本月限购 5/5")
    mixed = serene_page + surge_page[-3:]

    check("澎湃页 → guard False", nc._serene_page_guard(surge_page) is False)
    check("澎湃页指纹命中", bool(nc._page_fingerprint(surge_page)[1]),
          nc._page_fingerprint(surge_page))
    check("静谧页 → guard True", nc._serene_page_guard(serene_page) is True)
    check("静谧页指纹命中", bool(nc._page_fingerprint(serene_page)[0]),
          nc._page_fingerprint(serene_page))
    check("切页中间帧（两页商品都在）→ 保守判 False",
          nc._serene_page_guard(mixed) is False)
    check("空 OCR → False", nc._serene_page_guard([]) is False)
    check("只有同名水晶 → False（同名不能当指纹）",
          nc._serene_page_guard(page(*SHARED_ITEMS)) is False)
    check("低置信度商品不算命中",
          nc._serene_page_guard([row("万象星实", conf=0.2)]) is False)


def test_wiring():
    print("[3] 正式流程接线：切页校验 + 购买前二次复核")
    src = Path(nc.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)

    def body(name):
        fn = next((n for n in tree.body
                   if isinstance(n, ast.FunctionDef) and n.name == name), None)
        return ast.get_source_segment(src, fn) or ""

    select = body("_select_serene_tab")
    run = body("_run_resonance_shop")
    check("_select_serene_tab 用双向闸而非仅正向",
          "_serene_page_guard" in select and "_has_serene_markers" not in select)
    check("_select_serene_tab 命中澎湃独占时明确告警禁止购买",
          "禁止购买" in select and "sorted(surge)" in select)
    check("购买前有二次 OCR 复核",
          "recheck = _ocr_full(logger, hwnd)" in run
          and "_serene_page_guard(recheck)" in run)
    check("复核不过直接 return，不调用 _buy_plan_items",
          run.index("购买前货币复核未通过") < run.index("return _buy_plan_items"))
    check("复核不过会落失败记录", '"共鸣商店-货币复核"' in run)


def test_probe_reuses_constants():
    print("[4] 一次性探针复用正式常量，不另写一份清单")
    probe = PROJECT_ROOT / "data" / "artifacts" / "nikki_stage6.py"
    if not probe.exists():
        check("探针存在", False, str(probe))
        return
    p = probe.read_text(encoding="utf-8")
    check("探针导入 nikki_calendar 并取 SERENE/SURGE_MARKERS",
          "SERENE_MARKERS = nc.SERENE_MARKERS" in p
          and "SURGE_MARKERS = nc.SURGE_MARKERS" in p)
    check("ensure_serene_tab 走双向判定",
          "serene, surge = fingerprint(after)" in p and "if surge:" in p)
    check("默认不购买（BUY_ENABLED=False）", "BUY_ENABLED = False" in p)
    check("确认词表去掉「兑换」（正式代码 docstring 早有此坑）",
          '("确认", "确定", "购买")' in p and "兑换" not in
          p.split("cands = [x for x in items")[1].split("]")[0])
    for ref in ("ref_nikki_resonance_serene_20261101.png",
                "ref_nikki_resonance_surge_20261101.png",
                "ref_nikki_shop_menu_scrolled_20261101.png"):
        check(f"参考图留存 {ref}", (PROJECT_ROOT / "data" / "artifacts" / ref).exists())


def test_entry_wiring():
    print("[5] 正式流程入口：上拖 + 点文字上方图标（11-06 实机）")
    src = Path(nc.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)

    def body(name):
        fn = next((n for n in tree.body
                   if isinstance(n, ast.FunctionDef) and n.name == name), None)
        return ast.get_source_segment(src, fn) or ""

    find = body("_find_resonance_entry")
    run = body("_run_resonance_shop")
    drag = body("_drag_menu_up")
    check("_find_resonance_entry 改用上拖、不再用滚轮",
          "_drag_menu_up(logger, hwnd, rect)" in find and "_scroll_at(" not in find)
    check("上拖常量是窗口比例三元组",
          len(nc.SHOP_MENU_DRAG) == 3
          and all(0 < v < 1 for v in nc.SHOP_MENU_DRAG), nc.SHOP_MENU_DRAG)
    check("上拖只发一个大位移 moveTo（不分小步）",
          "pyautogui.moveTo(x, y1)" in drag
          and "range(30" not in drag and "steps" not in drag)
    check("上拖有 finally 保证松开左键", "pyautogui.mouseUp()" in drag
          and "finally:" in drag)
    check("入口点击统一走 _resonance_entry_point（不裸点 center）",
          "_click(entry[\"center\"])" not in run
          and "_resonance_entry_point(entry, rect)" in run)

    # 2026-11-07 #211 实机：共鸣商店与晶石兑换**不同构**——点文字中心即可命中，
    # 上移到图标圆区反而点不进去；晶石兑换保留图标偏移（图标在上、文字在下）。
    entry = {"text": "共鸣商店", "center": (202, 915),
             "box": (160, 903, 244, 927)}
    p1080 = nc._resonance_entry_point(entry, (0, 0, 1920, 1080))
    p4k = nc._resonance_entry_point(
        {"text": "共鸣商店", "center": (404, 1830), "box": (320, 1806, 488, 1854)},
        (0, 0, 3840, 2160))
    check("1080p 共鸣商店入口 = 文字中心 (202,915)", p1080 == (202, 915), p1080)
    check("4K 共鸣商店入口 = 文字中心 (404,1830)", p4k == (404, 1830), p4k)
    check("共鸣商店不再用晶石兑换的图标偏移",
          p1080 != nc._exchange_entry_point(entry, (0, 0, 1920, 1080)))
    check("晶石兑换保留图标偏移（框高 24 → 上移约 26px）",
          abs(nc._exchange_entry_point(entry, (0, 0, 1920, 1080))[1] - 889) <= 3,
          nc._exchange_entry_point(entry, (0, 0, 1920, 1080)))


def main():
    test_disjoint_fingerprints()
    test_guard()
    test_wiring()
    test_probe_reuses_constants()
    test_entry_wiring()


if __name__ == "__main__":
    main()
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILED: {FAILURES}")
        sys.exit(1)
    print("\nALL PASS")
