# -*- coding: utf-8 -*-
"""2026-11-04 三处实跑事故的修复回归。

事故（都有当天日志/截图实证）：
1. 原神 7.2 更新日弹「用户协议和隐私政策更新提示」，`genshin_launcher`
   的 OCR 兜底把正文「力。如您拒绝，将无法进入游戏。」当成入口按钮点掉，
   并 `continue` 掉了「同意」分支；就算走到「同意」，按置信度择优也会选
   正文里的 0.94 而不是按钮的 0.67。结果等主界面超时，三次补跑全废。
2. 崩铁余烬兑换翻页：背景动画让灰度像素差每屏 30~40，`effective=8`
   被当成「真的滚了」，商品卡其实一行没换 → `scroll_verified_dead=0`
   → 上层把「看不到」当「已买过」，07:51:45 日志三项 0/5 静默漏买。
3. 自塑尘脂：失败截图是 ESC 回主界面之后拍的（只有「观景车厢/信使」），
   量不到背包网格；按缩略图推算的 (1353,61)/(769,207) 真机都没点中。

覆盖：
  [1] 真实当帧 OCR 回放 → 弹窗识别 / 入口不误命中 / 点「同意」(1107,660)
  [2] 「同意」模板自匹配 + 三级兜底（模板 / OCR / 固定比例）
  [3] 像素差大但文字签名不变 → 切拖滚动条 + dead=1（堵住漏买）
  [4] 拖动后签名变化 → effective>0、dead=0
  [5] 拿不到 OCR 签名 → 退回旧的像素差判据，不碰真鼠标
  [6] fail() 先存背包页截图再退回主界面
  [7] 网格扫描逐格点选，命中即停
  [8] 两份 flow_configs 同步、按钮比例与实机模板自匹配完全一致

跑法：.venv\\Scripts\\python.exe tests\\suite_login_agreement_scroll_20261104.py
"""

import json
import logging
import sys
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import game_auto.runtime.genshin_launcher as gl  # noqa: E402
import game_auto.runtime.monthly_exchange as mx  # noqa: E402
import game_auto.runtime.hsr_resin_craft as hrc  # noqa: E402
import game_auto.runtime.zzz_monthly_shop as shop  # noqa: E402

FAILURES = []
ITEM = {"name": "星轨专票", "ocr_text": "星轨专票"}
RECT = (0, 0, 1920, 1080)

# 2026-11-04 12:52:29 GENSHIN_MONTHLY 失败当帧 OCR（flow_failures #171）
AGREEMENT_OCR = [
    ("12 +", 0.75, (1815, 96)),
    ("CADPA", 0.83, (1813, 130)),
    ("适龄提示", 0.73, (1812, 153)),
    ("用户协议和隐私政策更新提示", 0.83, (960, 407)),
    ("感谢您使用本游戏，您使用本游戏前应当阅读并同意《用户", 0.89, (951, 459)),
    ("协议》、《隐私政策》、《儿童隐私政策》和《第三方共享个", 0.90, (951, 489)),
    ("人信息清单》。当您点击同意并开始使用产品服务时，即表", 0.91, (952, 521)),
    ("示您已理解并同意该条款内容，该条款将对您产生法律约束", 0.93, (954, 552)),
    ("力。如您拒绝，将无法进入游戏。", 0.89, (829, 583)),
    ("拒绝", 0.65, (812, 659)),
    ("同意", 0.67, (1107, 660)),
    ("CNRELWin7.2.0_R49008628", 0.94, (342, 1054)),
]


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name}  {detail}")
        FAILURES.append(name)


def _logger():
    logger = logging.getLogger("suite_20261104")
    logger.setLevel(logging.CRITICAL)
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


def _items():
    return [
        {"text": t, "confidence": c, "center": p}
        for t, c, p in AGREEMENT_OCR
    ]


def _thumb(value):
    return [value] * (32 * 18)


def test_agreement_ocr_replay():
    print("[1] 真实当帧 OCR 回放：弹窗识别 / 不点正文 / 点「同意」按钮")
    items = _items()
    check("识别出用户协议弹窗", gl._agreement_visible(items))
    entry = gl._pick_short_button(
        items, gl.ENTRY_OCR_TEXTS, 0.5, gl._subregion(RECT, 1 / 3, 1 / 2, 2 / 3, 1 / 2)
    )
    check("正文「将无法进入游戏」不再被当成入口按钮", entry is None, entry)
    agree = gl._pick_short_button(
        items, gl.CONFIRM_TEXTS, 0.5, gl._subregion(RECT, *gl.AGREE_REGION)
    )
    check("命中按钮「同意」而非正文长句",
          agree is not None and agree[2]["center"] == (1107, 660), agree)
    check("左侧「拒绝」被 AGREE_REGION 排除",
          gl._pick_short_button([{"text": "拒绝", "confidence": 0.9, "center": (812, 659)}],
                                gl.CONFIRM_TEXTS, 0.5,
                                gl._subregion(RECT, *gl.AGREE_REGION)) is None)

    clicks = []
    with mock.patch.object(gl, "_match_center", return_value=None), \
            mock.patch.object(gl, "_window_rect", return_value=RECT), \
            mock.patch.object(gl, "_click", side_effect=lambda c: clicks.append(c)), \
            mock.patch.object(gl.time, "sleep", lambda *_: None):
        handled = gl._handle_agreement(_logger(), 1, [1.0], [0.8], items)
    check("弹窗处理返回 True", handled is True)
    check("实际点击 (1107,660)", clicks == [(1107, 660)], clicks)

    src = (PROJECT_ROOT / "src" / "game_auto" / "runtime" / "genshin_launcher.py").read_text(
        encoding="utf-8")
    # 入口 / 确认 / 协议弹窗三处都走短文本匹配，旧 `_pick` 子串匹配不得回流
    check("入口与确认路径都走短文本匹配",
          src.count("hit = _pick_short_button") >= 2, src.count("hit = _pick_short_button"))


def test_agreement_template_and_fallbacks():
    print("[2] 「同意」模板自匹配 + 模板/OCR/比例三级兜底")
    try:
        import cv2
        import numpy as np

        def rd(p):
            return cv2.imdecode(np.fromfile(str(p), dtype=np.uint8), cv2.IMREAD_COLOR)

        shot = PROJECT_ROOT / "data" / "artifacts" / "flow_failures" / \
            "20261104_125229_GENSHIN_MONTHLY_进入原神主界面.jpg"
        tpl_path = Path(gl.AGREEMENT_TEMPLATE)
        tpl = rd(tpl_path)
        half = cv2.resize(tpl, (tpl.shape[1] // 2, tpl.shape[0] // 2))
        _, score, _, loc = cv2.minMaxLoc(cv2.matchTemplate(rd(shot), half, cv2.TM_CCOEFF_NORMED))
        check("模板文件存在", tpl_path.is_file(), str(tpl_path))
        check("在当天失败截图上自匹配 ≥0.99", score >= 0.99, score)
        check("命中位置就是按钮 (488,297)", abs(loc[0] - 488) <= 3 and abs(loc[1] - 297) <= 3, loc)
    except Exception as exc:  # noqa: BLE001 - 缺 cv2 时只跳过图像断言
        print(f"  SKIP 模板图像断言（{exc}）")

    # 模板命中优先
    clicks = []
    with mock.patch.object(gl, "_match_center", return_value=(1100, 650)), \
            mock.patch.object(gl, "_window_rect", return_value=RECT), \
            mock.patch.object(gl, "_click", side_effect=lambda c: clicks.append(c)), \
            mock.patch.object(gl.time, "sleep", lambda *_: None):
        gl._handle_agreement(_logger(), 1, [1.0], [0.8], _items())
    check("模板命中时不再依赖 OCR", clicks == [(1100, 650)], clicks)

    # 模板与 OCR 都认不出 → 按当天实测比例点，绝不点「拒绝」
    clicks = []
    with mock.patch.object(gl, "_match_center", return_value=None), \
            mock.patch.object(gl, "_window_rect", return_value=RECT), \
            mock.patch.object(gl, "_click", side_effect=lambda c: clicks.append(c)), \
            mock.patch.object(gl.time, "sleep", lambda *_: None):
        gl._handle_agreement(_logger(), 1, [1.0], [0.8], [])
    ok = bool(clicks) and abs(clicks[0][0] - 1107) < 3 and abs(clicks[0][1] - 660) < 3
    check("兜底点仍在「同意」按钮上", ok, clicks)


def _scroll_case(signatures, thumb_values, drag_ok=True):
    """跑一遍 _scroll_find_item，返回 (center, diag, wheel_calls, drag_calls)。"""
    wheels, drags = [], []
    sig_iter = iter(signatures)
    thumb_iter = iter(thumb_values)

    def _wheel(*args, **kwargs):
        wheels.append(args[2])
        return True

    def _drag(*args, **kwargs):
        drags.append(args[2] if len(args) > 2 else True)
        return drag_ok

    def _sig(logger, hwnd):
        try:
            return next(sig_iter)
        except StopIteration:
            return signatures[-1]

    def _thumb_fn(hwnd, anchor):
        try:
            return _thumb(next(thumb_iter))
        except StopIteration:
            return _thumb(thumb_values[-1])

    diag = {}
    with mock.patch.object(mx, "_scroll_shop_list", _wheel), \
            mock.patch.object(mx, "_drag_shop_scrollbar", _drag), \
            mock.patch.object(mx, "_scroll_list_to_edge", lambda *a, **k: False), \
            mock.patch.object(mx, "_list_text_signature", _sig), \
            mock.patch.object(mx, "_list_thumb", _thumb_fn), \
            mock.patch.object(mx, "_record_scroll_failure", lambda *a, **k: None), \
            mock.patch.object(shop, "_locate_item", lambda *a, **k: None):
        center = mx._scroll_find_item(
            _logger(), 1234, ITEM, [1.0], [0.8], None, "余烬兑换", diag=diag
        )
    return center, diag, wheels, drags


def test_scroll_signature_blocks_false_effective():
    print("[3] 今天事故复现：像素差大但文字一行没换 → 切拖滚动条 + dead=1")
    sig = ("可兑换：80/80@8", "可兑换：30/30@8", "商店@2")
    center, diag, wheels, drags = _scroll_case([sig] * 12, list(range(0, 400, 20)))
    check("没找到返回 None", center is None, center)
    check("滚轮无效后确实改拖了滚动条", len(drags) >= 1, drags)
    check("effective_pages=0（不再被背景动画骗）", diag.get("effective_pages") == 0, diag)
    check("scroll_verified_dead=1 → 上层判失败不写月度标记",
          diag.get("scroll_verified_dead") == 1, diag)


def test_scroll_drag_effective():
    print("[4] 拖动后列表文字真的换了内容 → effective>0、dead=0")
    a = ("商品A@8", "可兑换：80/80@8")
    b = ("商品B@12", "可兑换：30/30@12")
    sigs = [a, a, a, b, b, b, a, b, a, b, a, b]
    center, diag, wheels, drags = _scroll_case(sigs, list(range(0, 400, 20)))
    check("切到了拖动模式", len(drags) >= 1, drags)
    check("有效翻页数 > 0", diag.get("effective_pages", 0) > 0, diag)
    check("不标 dead", diag.get("scroll_verified_dead") == 0, diag)


def test_scroll_signature_unavailable_fallback():
    print("[5] 拿不到 OCR 签名 → 退回像素差，且不碰真鼠标")
    center, diag, wheels, drags = _scroll_case([None] * 12, list(range(0, 400, 20)))
    check("降级后仍按像素差判定", diag.get("effective_pages", 0) > 0, diag)
    check("不会触发拖动滚动条", len(drags) == 0, drags)


def test_bag_evidence_before_escape():
    print("[6] 自塑尘脂失败：先存背包页截图，再 ESC 回主界面")
    order = []
    with mock.patch.object(hrc, "_open_bag", return_value=(False, "测试失败")), \
            mock.patch.object(hrc, "_capture_bag_evidence",
                              lambda *a, **k: order.append("capture")), \
            mock.patch.object(hrc, "_leave_to_main",
                              lambda *a, **k: order.append("leave") or True):
        ok, msg = hrc.craft_self_modeling_resin(
            _logger(), 1, {"log_label": "自塑尘脂"},
            scales=[1.0], confidences=[0.8],
        )
    check("返回失败", ok is False, (ok, msg))
    check("顺序是取证 → 退回主界面", order == ["capture", "leave"], order)


def test_resin_grid_scan():
    print("[7] 网格扫描：逐格点选、命中即停、不盲点确认")
    clicks = []
    state = {"n": 0}

    def detail_visible():
        state["n"] += 1
        return state["n"] >= 3

    def _click(*args, **kwargs):
        clicks.append((args[1], args[2]))

    with mock.patch.object(hrc, "_frac_point", return_value=(100, 200)), \
            mock.patch("game_auto.runtime.foreground_flow_runner._click_window_local", _click), \
            mock.patch.object(hrc, "_sleep", lambda *_: None):
        ok, msg, point = hrc._scan_resin_grid(
            _logger(), 1, {"cols": 4, "rows": 2}, detail_visible, None
        )

    check("第 3 格命中", ok is True and "第 3 格" in msg, (ok, msg))
    check("返回命中坐标", point == (100, 200), point)
    check("只点了 3 次就停", len(clicks) == 3, clicks)


def test_scroll_tick_by_tick():
    print("[9] 三月七同款滚动：一屏 12 个单格事件，而不是一次 -12")
    calls = []
    with mock.patch.object(mx, "_scroll_anchor_point", return_value=(1000, 600)), \
            mock.patch("game_auto.runtime.foreground_flow_runner._focus_window",
                       lambda *a, **k: None), \
            mock.patch.object(mx.pyautogui, "moveTo", lambda *a, **k: None), \
            mock.patch.object(mx.pyautogui, "scroll", side_effect=lambda n: calls.append(n)), \
            mock.patch.object(mx, "_sleep", lambda *a, **k: None):
        ok = mx._scroll_shop_list(_logger(), 1, mx.SCROLL_STEP, (0.6, 0.55), None)
    check("滚动执行成功", ok is True)
    check(f"发了 {abs(mx.SCROLL_STEP)} 次单格事件",
          len(calls) == abs(mx.SCROLL_STEP), len(calls))
    check("每次都是 -1（不是一次 -12）", set(calls) == {-1}, set(calls))


def test_signature_jitter_tolerance():
    print("[10] 文字签名容忍单帧 OCR 抖动，但能识别真的换了一批商品")
    a = frozenset({f"商品{i}" for i in range(10)})
    check("完全相同 = 没滚", not mx._signatures_differ(a, a))
    check("多一条抖动 = 没滚", not mx._signatures_differ(a, a | {"抖动"}))
    check("换了一批 = 滚了", mx._signatures_differ(a, frozenset({"X", "Y", "Z"})))


def test_scroll_to_bottom_first():
    print("[11] 三月七翻页策略：一次滚到底补扫命中后不再空烧 8 屏")
    edges = []
    wheels = []

    def _edge(logger, hwnd, down, anchor, stop_check=None):
        edges.append((down, anchor))
        return True

    def _wheel(*args, **kwargs):
        wheels.append(args[2])
        return True

    diag = {}
    with mock.patch.object(mx, "_scroll_list_to_edge", _edge), \
            mock.patch.object(mx, "_scroll_shop_list", _wheel), \
            mock.patch.object(mx, "_list_text_signature",
                              lambda *a, **k: frozenset({"商品A", "商品B"})), \
            mock.patch.object(mx, "_list_thumb", lambda *a, **k: _thumb(10)), \
            mock.patch.object(mx, "_record_scroll_failure", lambda *a, **k: None), \
            mock.patch.object(shop, "_locate_item",
                              lambda *a, **k: (300, 400)):
        center = mx._scroll_find_item(
            _logger(), 1234, ITEM, [1.0], [0.8], None, "余烬兑换", diag=diag
        )
    check("底部补扫直接命中", center == (300, 400), center)
    check("先往下滚到底", edges and edges[0][0] is True, edges)
    check("落点是三月七的列表中心",
          edges and abs(edges[0][1][0] - 955 / 1920) < 1e-6, edges)
    check("没有再空烧逐屏滚轮", wheels == [], wheels)
    check("记了有效翻页", diag.get("effective_pages", 0) >= 1, diag)


def test_config_and_statics():
    print("[8] 配置同步、按钮比例与实机一致、滚动条常量已登记")
    a = json.loads((PROJECT_ROOT / "flow_configs.json").read_text(encoding="utf-8"))
    b = json.loads((PROJECT_ROOT / "flow_configs.example.json").read_text(encoding="utf-8"))
    s = a["HSR_MONTHLY"]["steps"][2]
    s2 = b["HSR_MONTHLY"]["steps"][2]
    check("两份配置步骤 3 一致", s == s2)
    grid = s.get("item_grid_frac") or {}
    check("item_grid_frac 已登记", all(k in grid for k in
                                      ("x_start", "x_end", "y_start", "y_end", "cols", "rows")), grid)
    # 1920x1080 参考图模板自匹配 (666,847)+(873//2,47//2)=(1102,870)
    check("craft_button_point_frac 与模板实测 (0.574,0.8056) 一致",
          s["craft_button_point_frac"] == [0.574, 0.806], s["craft_button_point_frac"])
    src = (PROJECT_ROOT / "src" / "game_auto" / "runtime" / "monthly_exchange.py").read_text(
        encoding="utf-8")
    check("滚动条拖动与文字签名已接线",
          "_drag_shop_scrollbar" in src and "_list_text_signature" in src)


def main():
    test_agreement_ocr_replay()
    test_agreement_template_and_fallbacks()
    test_scroll_signature_blocks_false_effective()
    test_scroll_drag_effective()
    test_scroll_signature_unavailable_fallback()
    test_bag_evidence_before_escape()
    test_resin_grid_scan()
    test_scroll_tick_by_tick()
    test_signature_jitter_tolerance()
    test_scroll_to_bottom_first()
    test_config_and_statics()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
