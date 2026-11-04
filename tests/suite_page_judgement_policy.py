"""页面判定强制规范（2026-11-04 用户要求：后续不要再踩同名词误判的坑）。

规则：
  1. 每个「页面/状态判定函数」必须登记，并写明证据来源（专属特征、最少证据数、
     区域限定或模板优先）。新函数不登记 → 本测试失败。
  2. 禁止「页面上出现某词就判定是该页」：同名词（晶石兑换/本月限购/背包/商城）
     必须用 ≥2 个专属标记、标题+内容双证据，或标题+区域限定。
  3. 每个游戏的实际截图（data/artifacts/ref_<game>_*.png）必须进入该游戏的
     页面判定矩阵测试，逐张断言互斥（负样本也要有）。
  4. 新增截图必须存到 data/artifacts/ref_<game>_<page>_<YYYYMMDD>.png。

跑法：.venv\\Scripts\\python.exe tests/suite_page_judgement_policy.py
"""

import ast
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

RUNTIME = PROJECT_ROOT / "src/game_auto/runtime"
ART = PROJECT_ROOT / "data" / "artifacts"
NIKKI_MATRIX = PROJECT_ROOT / "tests/suite_nikki_page_matrix_20261104.py"

NAME_RE = re.compile(r"(_is_.*page|_is_main_world|.*_visible|_on_.*page)$")

# 登记表：函数 → 判定证据（必须能独立解释"为什么不会误判"）
REGISTRY = {
    ("nikki_calendar.py", "_is_calendar_page"): "主世界 HUD ≥2 直接排除；标题兜底只作辅助；矩阵覆盖",
    ("nikki_calendar.py", "_is_main_world"): "主世界 HUD 专属标记 ≥2",
    ("nikki_calendar.py", "_is_stage_page"): "巅峰赛速报（专属弹窗）或搭配赛专属标记 ≥2",
    ("nikki_calendar.py", "_is_exchange_page"): "「晶石兑换」标题 AND 「本月限购」双证据",
    ("nikki_calendar.py", "_is_shop_page"): "商城 AND 购物车两个标记",
    ("nikki_calendar.py", "_is_resonance_page"): "两个共鸣页签标记",
    ("hsr_resin_craft.py", "_bag_visible"): "模板优先；OCR 只认左上角标题区（避开 ESC 菜单的背包按钮）",
    ("genshin_launcher.py", "_agreement_visible"): "协议专属长标题，或「同意」+「拒绝」双短按钮",
    ("baijing_daily.py", "_on_memory_detail_page"): "强化烙痕模板优先；OCR 限定按钮区域",
    ("qoder_claim.py", "_claim_button_visible"): "模板匹配（非 OCR 文本）",
    # 以下不是页面判定：登记是为了让「新增函数必须声明」的守卫能区分动作函数
    ("baijing_daily.py", "_do_friend_medal_on_page"): "动作函数（非页面判定）",
    ("qoder_claim.py", "_ensure_visible"): "动作函数（聚焦/置前，非页面判定）",
    ("hsr_resin_craft.py", "detail_visible"): "双证据：商品名 + 「获得途径/消耗品」",
    ("hsr_resin_craft.py", "synth_visible"): "合成页专属词「本月还可合成 / 所需材料」",
}

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def _discover():
    found = set()
    for path in sorted(RUNTIME.glob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and NAME_RE.search(node.name):
                found.add((path.name, node.name))
    return found


def test_registry():
    print("[1] 页面判定函数登记")
    found = _discover()
    registered = set(REGISTRY)
    missing = sorted(found - registered)
    stale = sorted(registered - found)
    check("所有判定函数都已登记（新增判定必须声明证据）", not missing, missing)
    check("登记表没有失效函数（防改名遗漏）", not stale, stale)


def test_nikki_screenshots_in_matrix():
    print("[2] 暖暖实际截图全部进矩阵")
    text = NIKKI_MATRIX.read_text(encoding="utf-8")
    shots = sorted(p.name for p in ART.glob("ref_nikki_*.png"))
    missing = [name for name in shots if name not in text]
    check(f"{len(shots)} 张 ref_nikki_* 截图都在矩阵里", not missing, missing)
    check("矩阵有期望表（正/负样本都断言）", "EXPECTED = {" in text)


def test_no_single_keyword_without_guard():
    print("[3] 单关键词判定必须带区域/双证据")
    # 允许的模式：_find_text 带 region_frac/hwnd；双关键词 AND；专属弹窗标题 +
    # 其他证据。这里只拦「单个普通词直接 return」的写法，专属词走登记表说明。
    allowed = {
        ("nikki_calendar.py", "_is_stage_page"),      # 巅峰赛速报是专属弹窗标题
        ("nikki_calendar.py", "_is_exchange_page"),   # 两个单关键词 AND
        ("nikki_calendar.py", "_is_calendar_page"),   # 单标题 + 主世界排除 + 正文兜底
        ("hsr_resin_craft.py", "detail_visible"),     # 商品名 + 获得途径/消耗品双证据
    }
    offenders = []
    for path in sorted(RUNTIME.glob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef) or not NAME_RE.search(node.name):
                continue
            key = (path.name, node.name)
            if key in allowed:
                continue
            for sub in ast.walk(node):
                if not isinstance(sub, ast.Call):
                    continue
                fn = sub.func
                fname = fn.id if isinstance(fn, ast.Name) else (fn.attr if isinstance(fn, ast.Attribute) else "")
                if fname not in ("_pick", "_find_text", "_markers_present"):
                    continue
                if len(sub.args) < 2:
                    continue
                seq = sub.args[1]
                if not isinstance(seq, (ast.Tuple, ast.List)) or len(seq.elts) != 1:
                    continue
                kw = {k.arg for k in sub.keywords}
                # 模板/区域限定允许：_find_text(..., hwnd=, region_frac=) 或 min_conf 以外的守卫
                if fname == "_find_text" and {"hwnd", "region_frac"} <= kw:
                    continue
                offenders.append(f"{path.name}:{sub.lineno} {node.name} -> {ast.unparse(sub)[:90]}")
    check("没有裸单关键词判定（除登记例外）", not offenders, offenders)


def main():
    test_registry()
    test_nikki_screenshots_in_matrix()
    test_no_single_keyword_without_guard()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())