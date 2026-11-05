"""无限暖暖「奇想日历领奖 + 晶石兑换/共鸣商店」回归（2026-11-01）。

背景：用户 2026-11-01 要求进入主界面后补一条游戏内流程——
L 打开奇想日历 → 奇迹之冠 → 奇迹之冠搭配赛 → 领取奖励（如有，奖励页按
「空白区域继续」收尾）→ 晶石兑换（每月刷新，换抽卡道具 共鸣水晶/启示水晶）。

覆盖：
  1. 真实截图 OCR 定标（主界面模板 / 日历页 / 奇迹之冠 hub / 领奖弹窗 / 奖励页 /
     搭配赛页 / 晶石兑换页 / 兑换确认弹窗 / 商城页 / 滚动后左列 / 共鸣商店两页签 /
     静谧水滴页）——截图缺失或引擎不可用时 SKIP
  2. 纯逻辑：_pick 精确优先与排除、_find_item_name 模糊唯一性、本月限购几何解析、
     配置 qty 解析
  3. 状态机全链路：领奖 + 晶石兑换 6 件 + ESC 回主界面；无可领奖励；点不掉领取按钮；
     无确认弹窗不报成功；本月已兑换跳过；本月已换完（already）；
     部分买到有硬失败不写月度标记；日历打不开不阻断；用户停止；
     交接回不到主界面 / 开局不在主界面 → 关闭游戏交给 whimbox 重新进
  4. 共鸣商店：H→商城→滚动找入口→静谧页签强校验→买 10 件；页签没切过去一件不买；
     滚动找不到入口；本月已完成不按 H；商城打不开不炸
  5. 接线静态断言：_BUILTIN_ACTIONS / _dispatch_step_action / flow_configs.example /
     defaults / _default_config

跑法：.venv\\Scripts\\python.exe tests\\suite_nikki_calendar_20261101.py
"""

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import game_auto.runtime.nikki_calendar as nc  # noqa: E402
from game_auto.settings import defaults  # noqa: E402

REF_DIR = PROJECT_ROOT / "data" / "artifacts"
REFS = {
    "main": REF_DIR / "ref_nikki_main_20261101.png",
    "calendar": REF_DIR / "ref_nikki_calendar_20261101.png",
    "hub": REF_DIR / "ref_nikki_crown_hub_20261101.png",
    "claim": REF_DIR / "ref_nikki_crown_claim_20261101.png",
    "reward": REF_DIR / "ref_nikki_reward_continue_20261101.png",
    "stage": REF_DIR / "ref_nikki_crown_stage_20261101.png",
    "exchange": REF_DIR / "ref_nikki_crystal_exchange_20261101.png",
    "dialog": REF_DIR / "ref_nikki_exchange_dialog_20261101.png",
    "shop": REF_DIR / "ref_nikki_shop_20261101.png",
    "shop_menu": REF_DIR / "ref_nikki_shop_menu_scrolled_20261101.png",
    "surge": REF_DIR / "ref_nikki_resonance_surge_20261101.png",
    "serene": REF_DIR / "ref_nikki_resonance_serene_20261101.png",
}
WIN = (0, 0, 1919, 1079)

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def _logger():
    import logging

    logger = logging.getLogger("test_nikki_calendar")
    logger.setLevel(logging.CRITICAL)
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


def _row(text, cx, cy, conf=0.8):
    return {
        "text": text,
        "confidence": conf,
        "center": (cx, cy),
        "box": (cx - 20, cy - 12, cx + 20, cy + 12),
    }


def _ocr_refs():
    """一次性跑 RapidOCR，返回 {tag: (rows, size)}；不可用返回 None。"""
    try:
        import numpy as np
        from PIL import Image

        from game_auto.runtime.ocr_click_tester import _get_rapidocr_engine
    except Exception as exc:  # noqa: BLE001
        print(f"  SKIP 依赖不可用: {exc}")
        return None
    engine = _get_rapidocr_engine()
    if not engine:
        print("  SKIP RapidOCR 不可用")
        return None
    out = {}
    for tag, path in REFS.items():
        if not path.exists():
            print(f"  SKIP 参考图不存在: {path.name}")
            continue
        img = Image.open(path).convert("RGB")
        w, h = img.size
        result, _ = engine(np.asarray(img))
        rows = []
        for box, text, confidence in result or []:
            x1 = min(p[0] for p in box)
            y1 = min(p[1] for p in box)
            x2 = max(p[0] for p in box)
            y2 = max(p[1] for p in box)
            rows.append(
                {
                    "text": str(text),
                    "confidence": float(confidence),
                    "box": (int(x1), int(y1), int(x2), int(y2)),
                    "center": (int((x1 + x2) // 2), int((y1 + y2) // 2)),
                }
            )
        out[tag] = (rows, (w, h))
    return out or None


def _points_near(a, b, tol=18):
    return abs(int(a[0]) - int(b[0])) <= tol and abs(int(a[1]) - int(b[1])) <= tol


class Harness:
    """用状态机模拟暖暖界面；click/press/scroll 改变状态，OCR 按状态返回文字行。"""

    def __init__(
        self,
        claim="once",
        crystal_limits=None,
        shop_limits=None,
        monthly_done=False,
        fail_open=False,
        no_confirm=False,
        already=False,
        fail_item=None,
        fail_shop=False,
        fail_scroll=False,
        fail_serene=False,
        fail_esc=False,
        start_state="main",
    ):
        self.state = start_state
        self.claim = claim  # "once" / "none" / "forever"
        self.crystal_limits = dict(crystal_limits or {"共鸣水晶": 3, "启示水晶": 3})
        if already:
            self.crystal_limits = {"共鸣水晶": 0, "启示水晶": 0}
        self.shop_limits = dict(shop_limits or {"启示水晶": 5, "共鸣水晶": 5})
        self.monthly = {
            nc.MONTHLY_META_KEY: bool(monthly_done),
            nc.RESONANCE_META_KEY: bool(monthly_done),
        }
        self.fail_open = fail_open
        self.no_confirm = no_confirm
        self.fail_item = fail_item
        self.fail_shop = fail_shop
        self.fail_scroll = fail_scroll
        self.fail_serene = fail_serene
        self.fail_esc = fail_esc
        self.clicks = []
        self.keys = []
        self.scrolls = []
        self.marked_keys = []
        self.failures = []
        self.closed = 0

    def monthly_done_for(self, meta_key):
        return bool(self.monthly.get(meta_key))

    def mark_monthly(self, meta_key):
        self.marked_keys.append(meta_key)
        self.monthly[meta_key] = True

    # —— 屏幕内容 ——
    def _exchange_rows(self):
        rows = [
            _row("晶石兑换", 181, 52),
            _row("晶石兑换", 960, 50),
            _row("3790", 1807, 47),
        ]
        for name, nxy, lxy in (
            ("共鸣水晶", (425, 381), (474, 187)),
            ("启示水晶", (698, 381), (749, 188)),
        ):
            rows.append(_row(name, nxy[0], nxy[1]))
            rows.append(_row(f"本月限购 {self.crystal_limits[name]}/3", lxy[0], lxy[1]))
        return rows

    def _resonance_rows(self, serene):
        rows = [
            _row("商城", 148, 51, 0.58),
            _row("购物车", 323, 50, 0.70),
            _row("澎湃浪花兑换", 601, 128, 0.85),
            _row("静谧水滴兑换", 959, 127, 0.80),
            _row("启示水晶", 560, 413),
            _row("共鸣水晶", 837, 414),
        ]
        if serene:
            rows += [
                _row(f"本月限购 {self.shop_limits['启示水晶']}/5", 615, 220, 0.86),
                _row(f"本月限购 {self.shop_limits['共鸣水晶']}/5", 892, 220, 0.82),
                _row("噗灵x2000", 1113, 413),
                _row("纯真丝线x10", 1391, 413),
                _row("万象星实×1250", 561, 725, 0.89),
                _row("璨光棱镜", 837, 726),
                _row("流辉棱镜", 1114, 726),
                _row("虹彩棱镜", 1389, 726),
            ]
        else:
            rows += [
                _row("慢慢恢复花露", 1390, 414),
                _row("精萃慢慢恢复花露", 1666, 413),
                _row("缓速花露", 561, 726),
            ]
        return rows

    def rows(self):
        s = self.state
        if s == "calendar":
            return [
                _row("每日灵感", 543, 178),
                _row("幻境挑战", 970, 350),
                _row("奇迹之冠", 971, 600),
                _row("星光的追寻", 991, 934),
            ]
        if s == "hub":
            return [
                _row("奇迹之冠", 180, 52),
                # 真实 OCR 框（ref_nikki_crown_hub_20261101.png 实测宽度 326），
                # 点击点是文字右侧的多边形，不是文字中心
                {
                    "text": "奇迹之冠搭配赛",
                    "confidence": 0.84,
                    "center": (531, 667),
                    "box": (369, 645, 695, 690),
                },
                _row("奇迹之冠争锋赛", 1514, 392, 0.78),
            ]
        if s == "stage_claim":
            return [
                _row("巅峰赛速报", 953, 274),
                _row("领取奖励", 961, 752),
                _row("第1场", 554, 416),
            ]
        if s == "reward":
            return [_row("恭喜获得", 961, 280), _row("F/空白区域继续", 1730, 1038, 0.76)]
        if s == "stage":
            return [
                _row("伊赞之士·奇迹之冠巅峰赛", 350, 173, 0.88),
                _row("第4场", 554, 416),
                _row("第5场", 1138, 457),
                _row("晶石兑换", 150, 775),
                _row("赛事嘉奖", 149, 896),
            ]
        if s == "exchange":
            return self._exchange_rows()
        if s == "shop":
            return [
                _row("商城", 148, 51, 0.58),
                _row("购物车", 323, 50, 0.70),
                _row("书间珍藏", 972, 128),
                _row("时忆长廊", 203, 789),
                _row("美鸭梨优选", 220, 896, 0.76),
            ]
        if s == "shop_scrolled":
            return [
                _row("商城", 148, 51, 0.58),
                _row("购物车", 323, 50, 0.70),
                _row("美鸭梨优选", 220, 310, 0.83),
                _row("闪闪宝藏罐", 220, 418),
                _row("服装商店", 202, 634),
                _row("星石获取", 202, 742),
                _row("共鸣商店", 202, 849, 0.79),
                _row("钻石小铺", 203, 957),
            ]
        if s == "resonance_surge":
            return self._resonance_rows(serene=False)
        if s == "resonance_serene":
            return self._resonance_rows(serene=True)
        if s.startswith("dialog_crystal:"):
            name = s.split(":", 1)[1]
            rows = self._exchange_rows()
            rows.append(_row("兑换", 961, 226, 0.66))  # 弹窗标题，conf 比按钮高
            if not self.no_confirm and name != self.fail_item:
                rows.append(_row("确认", 960, 810, 0.64))
            return rows
        if s.startswith("dialog_shop:"):
            name = s.split(":", 1)[1]
            rows = self._resonance_rows(serene=True)
            rows.append(_row("兑换", 961, 226, 0.66))
            if not self.no_confirm and name != self.fail_item:
                rows.append(_row("确认", 960, 810, 0.64))
            return rows
        if s.startswith("reward_crystal:") or s.startswith("reward_shop:"):
            return [_row("恭喜获得", 961, 280), _row("F/空白区域继续", 1730, 1038, 0.76)]
        return []

    # —— 动作 ——
    def ocr(self, logger, hwnd, region=None):
        return self.rows()

    def click(self, center):
        x, y = int(center[0]), int(center[1])
        self.clicks.append((x, y))
        s = self.state
        if s == "calendar" and _points_near((x, y), (971, 600)):
            self.state = "hub"
        elif s == "hub":
            # 真实界面：文字本身点不动（2026-11-02 用户实锤），
            # 要点文字左上方那块多边形玻璃碎片（亮白区约 x 450-650 / y 520-630）
            if 450 <= x <= 650 and 520 <= y <= 630:
                self.state = "stage_claim" if self.claim != "none" else "stage"
        elif s == "stage_claim" and _points_near((x, y), (961, 752)):
            self.state = "stage_claim" if self.claim == "forever" else "reward"
        elif s == "stage" and _points_near((x, y), (150, 775)):
            self.state = "exchange"
        elif s == "exchange":
            if _points_near((x, y), (425, 381)):
                self.state = "dialog_crystal:共鸣水晶"
            elif _points_near((x, y), (698, 381)):
                self.state = "dialog_crystal:启示水晶"
        elif s.startswith("dialog_crystal:") and _points_near((x, y), (960, 810)):
            name = s.split(":", 1)[1]
            self.crystal_limits[name] = max(0, self.crystal_limits[name] - 1)
            self.state = f"reward_crystal:{name}"
        elif s.startswith("reward_crystal:") and _points_near((x, y), (959, 884)):
            self.state = "exchange"
        elif s == "shop_scrolled" and _points_near((x, y), (202, 849)):
            self.state = "resonance_surge"
        elif s in ("resonance_surge", "resonance_serene") and _points_near((x, y), (959, 127)):
            if not self.fail_serene:
                self.state = "resonance_serene"
        elif s == "resonance_serene":
            if _points_near((x, y), (560, 413)):
                self.state = "dialog_shop:启示水晶"
            elif _points_near((x, y), (837, 414)):
                self.state = "dialog_shop:共鸣水晶"
        elif s.startswith("dialog_shop:") and _points_near((x, y), (960, 810)):
            name = s.split(":", 1)[1]
            self.shop_limits[name] = max(0, self.shop_limits[name] - 1)
            self.state = f"reward_shop:{name}"
        elif s.startswith("reward_shop:") and _points_near((x, y), (959, 884)):
            self.state = "resonance_serene"
        elif s == "reward" and _points_near((x, y), (959, 884)):
            self.state = "stage"

    def click_ratio(self, logger, hwnd, frac, label, unlock_mouse=False):
        # unlock_mouse 是主世界顶栏点击的 Alt 呼出开关；桩里只关心坐标与状态
        self.click((int(WIN[2] * frac[0]), int(WIN[3] * frac[1])))
        return True

    def click_with_alt(self, point):
        # 对应 nc._click_with_alt：真实实现按下 Alt → 点击 → 松开，桩里只转发点击
        self.click(point)

    def press_key(self, key):
        self.keys.append(key)
        if key.lower() == "l" and not self.fail_open:
            self.state = "calendar"
        elif key.lower() == "h" and not self.fail_shop:
            self.state = "shop"

    def scroll_at(self, logger, hwnd, point, clicks):
        self.scrolls.append((int(point[0]), int(point[1]), int(clicks)))
        if self.state == "shop" and clicks < 0 and not self.fail_scroll:
            self.state = "shop_scrolled"

    def press_esc(self):
        if self.fail_esc:
            return
        mapping = {
            "main": "main",
            "calendar": "main",
            "hub": "calendar",
            "stage": "hub",
            "stage_claim": "hub",
            "reward": "stage",
            "exchange": "stage",
            "shop": "main",
            "shop_scrolled": "main",
            "resonance_surge": "shop_scrolled",
            "resonance_serene": "shop_scrolled",
        }
        if self.state.startswith("dialog_crystal:") or self.state.startswith("reward_crystal:"):
            self.state = "exchange"
        elif self.state.startswith("dialog_shop:") or self.state.startswith("reward_shop:"):
            self.state = "resonance_serene"
        else:
            self.state = mapping.get(self.state, "main")

    def close(self):
        return self.state == "main"

    def close_game(self):
        self.closed += 1
        self.state = "closed"


class FakeTime:
    def __init__(self):
        self.now = 1000.0

    def time(self):
        self.now += 0.6
        return self.now

    def sleep(self, seconds):
        self.now += max(0.0, float(seconds))

    def strftime(self, fmt):
        return "2026-11"


def _run_flow(h: Harness, stop_check=None, plan=None, with_resonance=False):
    names = [
        "_ocr_full",
        "_click",
        "_click_with_alt",
        "_click_ratio",
        "_press_key",
        "_press_esc",
        "_focus",
        "_has_main",
        "_window_rect",
        "load_crystal_exchange_plan",
        "load_resonance_shop_plan",
        "_monthly_done",
        "_mark_monthly_done",
        "_record_failure",
        "_scroll_at",
        "_close_game_for_whimbox_restart",
        "_record_key_no_response",
        "time",
    ]
    if not with_resonance:
        names.append("_run_resonance_shop")
    orig = {name: getattr(nc, name) for name in names}
    try:
        nc._ocr_full = h.ocr
        nc._click = h.click
        nc._click_with_alt = h.click_with_alt
        nc._click_ratio = h.click_ratio
        nc._press_key = h.press_key
        nc._press_esc = h.press_esc
        nc._focus = lambda hwnd: None
        nc._has_main = lambda hwnd, scales, confidences: h.close()
        nc._window_rect = lambda hwnd: WIN
        nc.load_crystal_exchange_plan = lambda logger: plan or {
            "enabled": True,
            "items": [{"name": "共鸣水晶", "qty": 3}, {"name": "启示水晶", "qty": 3}],
        }
        nc.load_resonance_shop_plan = lambda logger: {
            "enabled": True,
            "items": [{"name": "启示水晶", "qty": 5}, {"name": "共鸣水晶", "qty": 5}],
        }
        nc._monthly_done = (
            lambda logger, meta_key=nc.MONTHLY_META_KEY: h.monthly_done_for(meta_key)
        )
        nc._mark_monthly_done = (
            lambda logger, meta_key=nc.MONTHLY_META_KEY: h.mark_monthly(meta_key)
        )
        nc._record_failure = lambda logger, step, message, hwnd, ocr_results=None: h.failures.append(
            (step, message)
        )
        nc._scroll_at = h.scroll_at
        nc._close_game_for_whimbox_restart = lambda logger: h.close_game()
        nc._record_key_no_response = lambda *a, **k: None
        nc.time = FakeTime()
        if not with_resonance:
            nc._run_resonance_shop = lambda logger, hwnd, stop_check=None: "共鸣商店：测试跳过"
        return nc.run_nikki_calendar_flow(_logger(), 123, [1.0], [0.8], stop_check=stop_check)
    finally:
        for name, value in orig.items():
            setattr(nc, name, value)


def test_main_hud_not_calendar():
    print("[1b] 主世界顶部 HUD 含「奇想日历」不能误判成日历页")
    # 2026-11-04 08:52 失败截图 OCR 实测：顶部整排图标被连读成
    # 「奇想之旅·活动大厅·奇想日历·无羁之心」，旧标题兜底直接判"已在日历页"，
    # 流程没按 L，后面全在主世界空找。
    main_rows = [
        _row("ESC", 920, 600, 0.61),
        _row("商城", 652, 55, 0.66),
        _row("共鸣", 723, 55, 0.66),
        _row("奇想之旅·活动大厅·奇想日历·无羁之心", 1010, 55, 0.77),
        _row("任务", 1330, 55, 0.67),
        _row("设计图", 1400, 55, 0.67),
        _row("换装", 1500, 55, 0.67),
        _row("暂无追踪中的任务", 200, 310, 0.89),
    ]
    check("主世界被识别（≥2 个 HUD 特征）", nc._is_main_world(main_rows) is True)
    check("含「奇想日历」也不判成日历页", nc._is_calendar_page(main_rows) is False)
    check("非主世界的纯标题仍然兜底",
          nc._is_calendar_page([_row("奇想日历", 100, 50)]) is True)


def test_reference_ocr(refs):
    print("[1] 真实截图 OCR 定标")
    if not refs:
        print("  SKIP 无可用参考图")
        return

    rows, size = refs["calendar"]
    rect = (0, 0, size[0], size[1])
    markers = nc._markers_present(rows, nc.CALENDAR_MARKERS)
    check("日历页四个特征全部识别", markers == set(nc.CALENDAR_MARKERS), markers)
    check("「奇迹之冠」区块可定位",
          nc._pick(rows, (nc.CROWN_ENTRY,), 0.5, region=nc._abs_region(rect, nc.CALENDAR_CROWN_REGION)) is not None)

    rows, size = refs["hub"]
    rect = (0, 0, size[0], size[1])
    pair = nc._pick(rows, (nc.PAIR_ENTRY,), 0.5, region=nc._abs_region(rect, nc.HUB_PAIR_REGION))
    check("「奇迹之冠搭配赛」可定位", pair is not None)
    check("hub 两个入口都能识别", len(nc._markers_present(rows, nc.CROWN_HUB_MARKERS)) == 2,
          nc._markers_present(rows, nc.CROWN_HUB_MARKERS))
    if pair:
        point = nc._pair_click_point(pair, rect)
        box = pair["box"]
        in_text = box[0] <= point[0] <= box[2] and box[1] <= point[1] <= box[3]
        check("搭配赛点击点落在文字左上方多边形内（不在文字上）",
              not in_text and 450 <= point[0] <= 650 and 520 <= point[1] <= 630
              and point[1] < box[1],
              (point, box))
        try:
            from PIL import Image

            pixel = Image.open(REFS["hub"]).convert("RGB").getpixel(point)
        except Exception as exc:  # noqa: BLE001
            print(f"  SKIP 像素采样不可用: {exc}")
        else:
            check("点击点在参考图上是亮白碎片（不是背景/文字）",
                  min(pixel) > 220, (point, pixel))

    rows, size = refs["claim"]
    check("领奖弹窗「巅峰赛速报」识别", nc._pick(rows, (nc.STAGE_CLAIM_MODAL,), 0.5) is not None)
    check("「领取奖励」可点击", nc._pick(rows, nc.CLAIM_TEXTS, 0.5) is not None)

    rows, size = refs["reward"]
    check("奖励页「空白区域继续」识别", nc._pick(rows, nc.CONTINUE_TEXTS, 0.5) is not None)
    check("空白点击点不在任何文字框内",
          not any(
              box[0] <= WIN[2] * nc.BLANK_CONTINUE_POINT[0] <= box[2]
              and box[1] <= WIN[3] * nc.BLANK_CONTINUE_POINT[1] <= box[3]
              for r in rows
              for box in [r["box"]]
          ))

    rows, size = refs["stage"]
    rect = (0, 0, size[0], size[1])
    check("搭配赛页「晶石兑换」入口可定位",
          nc._pick(rows, (nc.EXCHANGE_TITLE,), 0.5, region=nc._abs_region(rect, nc.STAGE_MENU_REGION)) is not None)
    check("搭配赛页「赛事嘉奖」识别", nc._pick(rows, ("赛事嘉奖",), 0.5) is not None)

    rows, size = refs["exchange"]
    rect = (0, 0, size[0], size[1])
    for name, want in (("共鸣水晶", 3), ("启示水晶", 3)):
        match = nc._find_item_name(rows, name, rect)
        check(f"兑换页「{name}」可定位", match is not None, name)
        if not match:
            continue
        limit = nc._read_limit_near(rows, match["center"], rect)
        check(f"「{name}」本月限购 = {want}/3", bool(limit) and limit[0] == want and limit[1] == 3, limit)

    rows, size = refs["dialog"]
    rect = (0, 0, size[0], size[1])
    region = nc._abs_region(rect, nc.DIALOG_REGION)
    confirm = nc._pick_confirm(rows, region)
    check("兑换弹窗找到底部「确认」", confirm is not None and confirm["text"] == "确认", confirm)
    if confirm:
        fx = confirm["center"][0] / size[0]
        fy = confirm["center"][1] / size[1]
        check("「确认」位置 ≈ (0.50,0.75)", abs(fx - 0.50) < 0.03 and abs(fy - 0.75) < 0.03, (fx, fy))
    title = nc._pick(rows, ("兑换",), 0.5, region=region)
    check("弹窗标题「兑换」也在判定区域内（防回归）", title is not None, title)
    if title and confirm:
        check("标题 conf 不低于按钮（证明不能按置信度选）",
              float(title["confidence"]) >= float(confirm["confidence"]),
              (title["confidence"], confirm["confidence"]))

    if "shop" in refs:
        rows, size = refs["shop"]
        region = nc._abs_region((0, 0, *size), nc.SHOP_MENU_REGION)
        check("商城页识别（商城+购物车）", nc._is_shop_page(rows))
        check("初始页「美鸭梨优选」可见",
              nc._pick(rows, (nc.SHOP_MENU_ITEM,), 0.45, region=region) is not None)
        check("初始页看不到「共鸣商店」（必须滚动）",
              nc._pick(rows, (nc.RESONANCE_ENTRY,), 0.5, region=region) is None)

    if "shop_menu" in refs:
        rows, size = refs["shop_menu"]
        region = nc._abs_region((0, 0, *size), nc.SHOP_MENU_REGION)
        check("滚动后「共鸣商店」出现在左列",
              nc._pick(rows, (nc.RESONANCE_ENTRY,), 0.5, region=region) is not None)

    if "surge" in refs:
        rows, size = refs["surge"]
        check("共鸣商店页两页签识别", nc._is_resonance_page(rows))
        check("澎湃页没有静谧专属商品（不能误判页签）", not nc._has_serene_markers(rows))

    if "serene" in refs:
        rows, size = refs["serene"]
        rect = (0, 0, *size)
        check("静谧页专属商品可识别（页签强校验）", nc._has_serene_markers(rows))
        for name in ("启示水晶", "共鸣水晶"):
            match = nc._find_item_name(rows, name, rect, nc.SHOP_GRID_REGION)
            check(f"静谧页「{name}」可定位", match is not None, name)
            if match:
                limit = nc._read_limit_near(rows, match["center"], rect)
                check(f"「{name}」本月限购 5/5",
                      bool(limit) and limit[0] == 5 and limit[1] == 5, limit)

    # 主界面模板（左下角聊天气泡）在真实截图上的匹配分
    if "main" in refs:
        try:
            import cv2
            import numpy as np
            from PIL import Image

            tpl_path = PROJECT_ROOT / "assets" / "templates" / "暖暖-主界面.png"
            check("主界面模板文件存在", tpl_path.exists(), str(tpl_path))
            if tpl_path.exists():
                shot = Image.open(REFS["main"]).convert("RGB")
                w, h = shot.size
                scr = cv2.cvtColor(np.asarray(shot), cv2.COLOR_RGB2GRAY)
                sub = scr[int(h * 0.70):h, 0:int(w * 0.30)]
                tpl = Image.open(tpl_path).convert("RGB")
                tg = cv2.cvtColor(np.asarray(tpl), cv2.COLOR_RGB2GRAY)
                score = float(cv2.matchTemplate(sub, tg, cv2.TM_CCOEFF_NORMED).max())
                check("主界面模板匹配 ≥ 0.80（阈值生效值）", score >= 0.80, f"score={score:.3f}")
        except Exception as exc:  # noqa: BLE001
            print(f"  SKIP 主界面模板校验失败: {exc}")


def test_helpers():
    print("[2] 纯逻辑")
    rows = [
        _row("兑换", 961, 226, 0.90),  # 弹窗标题：绝不能当按钮
        _row("确认", 960, 810, 0.64),
        _row("取消", 1070, 900),
    ]
    hit = nc._pick_confirm(rows, (0, 0, 1919, 1079))
    check("_pick_confirm 只认按钮「确认」，不点标题「兑换」", hit and hit["text"] == "确认", hit)
    hit = nc._pick(rows, ("确认兑换", "确认"), 0.5, region=(0, 0, 1919, 1079), exclude=nc.CANCEL_TEXTS)
    check("_pick 精确命中「确认」", hit and hit["text"] == "确认", hit)
    hit = nc._pick([_row("取消", 900, 700)], ("确认",), 0.5, exclude=nc.CANCEL_TEXTS)
    check("_pick 排除「取消」", hit is None, hit)
    check("_pick 区域过滤",
          nc._pick([_row("领取奖励", 100, 100)], nc.CLAIM_TEXTS, 0.5, region=(500, 500, 200, 200)) is None)

    rect = WIN
    items = [
        _row("共鸣水晶", 425, 381),
        _row("启示水晶", 698, 381),
        _row("本月限购 2/3", 474, 187),
        _row("本月限购 3/3", 749, 188),
    ]
    check("_find_item_name 精确命中 共鸣水晶",
          (nc._find_item_name(items, "共鸣水晶", rect) or {}).get("text") == "共鸣水晶")
    check("_read_limit_near 只取本卡上方的限购",
          nc._read_limit_near(items, (425, 381), rect)[0] == 2
          and nc._read_limit_near(items, (698, 381), rect)[0] == 3)
    ambiguous = [_row("共鸣水晶石", 425, 381), _row("共鸣水晶晶", 700, 381)]
    check("两个近似候选 → 宁可不买", nc._find_item_name(ambiguous, "共鸣水晶", rect) is None)

    import game_auto.settings.manager as manager

    orig = manager.load
    try:
        manager.load = lambda force=False: {
            "nikki": {
                "crystal_exchange": {
                    "enabled": True,
                    "items": [
                        {"name": "共鸣水晶", "qty": 3},
                        "启示水晶",
                        {"name": "坏数量", "qty": "x"},
                        {"name": "零数量", "qty": 0},
                        {"name": "", "qty": 3},
                    ],
                }
            }
        }
        plan = nc.load_crystal_exchange_plan(_logger())
        qty = {i["name"]: i["qty"] for i in plan["items"]}
        check("qty 解析：3 / 缺省 1 / 非法 1 / 0 兜到 1",
              qty == {"共鸣水晶": 3, "启示水晶": 1, "坏数量": 1, "零数量": 1}, qty)
        check("无名项被丢弃", len(plan["items"]) == 4, plan["items"])

        manager.load = lambda force=False: {"nikki": {"crystal_exchange": {"items": []}}}
        check("空清单 → items=[]", nc.load_crystal_exchange_plan(_logger())["items"] == [])
        manager.load = lambda force=False: {"nikki": {"crystal_exchange": {"enabled": False}}}
        check("enabled=false 被识别", nc.load_crystal_exchange_plan(_logger())["enabled"] is False)
    finally:
        manager.load = orig


def test_full_flow():
    print("[3] 状态机全链路")
    h = Harness()
    ok, msg = _run_flow(h)
    check("流程返回继续", ok is True, msg)
    check("领奖 1 次", "领奖 1 次" in msg, msg)
    check("兑换 6 件（3+3）", "已兑换 6 件" in msg, msg)
    check("共鸣水晶/启示水晶都买满", h.crystal_limits == {"共鸣水晶": 0, "启示水晶": 0}, h.crystal_limits)
    check("写月度完成标记", h.marked_keys.count(nc.MONTHLY_META_KEY) == 1, h.marked_keys)
    check("确认按钮点了 6 次", sum(1 for c in h.clicks if _points_near(c, (960, 810))) == 6, h.clicks)
    check("流程结束回到主界面", h.close(), h.state)
    check("没有失败记录", not h.failures, h.failures)

    h = Harness(claim="none")
    ok, msg = _run_flow(h)
    check("无可领奖励 → 跳过领奖", "无可领奖励" in msg, msg)
    check("无奖励仍执行兑换", "已兑换 6 件" in msg, msg)

    h = Harness(claim="forever")
    ok, msg = _run_flow(h)
    check("领取按钮点不掉 → 有界点击并记录", "仍有领取按钮" in msg and len(h.failures) == 2, (msg, h.failures))
    check("弹窗挡路时兑换如实跳过（不假成功、不阻断）",
          ok is True and "未找到「晶石兑换」入口" in msg, msg)

    h = Harness(no_confirm=True)
    ok, msg = _run_flow(h)
    check("无确认弹窗 → 不报成功", "未兑换到任何商品" in msg and "no_dialog" in msg, msg)
    check("无确认弹窗 → 不写月度标记", nc.MONTHLY_META_KEY not in h.marked_keys, h.marked_keys)
    check("无确认弹窗 → 有失败记录", len(h.failures) == 1, h.failures)

    h = Harness(monthly_done=True)
    ok, msg = _run_flow(h)
    check("本月已兑换 → 跳过兑换", "本月晶石兑换已完成" in msg, msg)
    check("跳过时不点晶石兑换入口", not any(_points_near(c, (150, 775)) for c in h.clicks), h.clicks)
    check("跳过时仍先领奖", "领奖 1 次" in msg, msg)

    h = Harness(already=True)
    ok, msg = _run_flow(h)
    check("本月已换完 → already 口径", "本月均已换完" in msg, msg)
    check("已换完也写月度标记", h.marked_keys.count(nc.MONTHLY_META_KEY) == 1, h.marked_keys)
    check("已换完不点确认按钮", not any(_points_near(c, (960, 810)) for c in h.clicks), h.clicks)

    h = Harness(fail_item="启示水晶")
    ok, msg = _run_flow(h)
    check("部分买到但有硬失败 → 不写月度标记",
          nc.MONTHLY_META_KEY not in h.marked_keys and "未写月度标记" in msg, (msg, h.marked_keys))
    check("已买到的件数如实记录", "已兑换 3 件" in msg and "共鸣水晶×3" in msg, msg)

    h = Harness(fail_open=True)
    ok, msg = _run_flow(h)
    check("日历打不开 → 不阻断（返回继续）", ok is True and "奇想日历未打开" in msg, msg)
    check("日历打不开 → 有失败记录", len(h.failures) == 1, h.failures)
    check("日历打不开 → 仍在主界面", h.close(), h.state)

    h = Harness()
    ok, msg = _run_flow(h, stop_check=lambda: True)
    check("用户停止 → 返回失败", ok is False and "用户请求停止" in msg, (ok, msg))

    # 交接规则（2026-11-01 用户叮嘱）：保证不了主界面就关游戏，让 whimbox 重新进
    h = Harness(fail_esc=True)
    ok, msg = _run_flow(h, plan={"enabled": True, "items": []}, with_resonance=True)
    check("交接回不到主界面 → 关闭游戏交给 whimbox",
          h.closed == 1 and "已关闭游戏" in msg, (h.closed, msg))
    check("关游戏后流程仍返回继续", ok is True, (ok, msg))

    h = Harness(start_state="stage")
    ok, msg = _run_flow(h, plan={"enabled": True, "items": []}, with_resonance=True)
    check("开局不在主界面 → 关闭游戏（不猜着操作）",
          h.closed == 1 and "未识别到暖暖主界面" in msg, (h.closed, msg))


def test_resonance_flow():
    print("[4] 商城 → 共鸣商店 → 静谧水滴兑换")
    empty_crystal = {"enabled": True, "items": []}

    h = Harness()
    ok, msg = _run_flow(h, plan=empty_crystal, with_resonance=True)
    check("流程返回继续", ok is True, msg)
    check("按 H 打开商城", "h" in [k.lower() for k in h.keys], h.keys)
    check("滚动后找到并进入共鸣商店", bool(h.scrolls) and "已兑换 10 件" in msg, (h.scrolls, msg))
    check("两张抽卡道具各买满 5 个",
          h.shop_limits == {"启示水晶": 0, "共鸣水晶": 0}, h.shop_limits)
    check("共鸣商店写了月度标记", nc.RESONANCE_META_KEY in h.marked_keys, h.marked_keys)
    check("确认按钮点了 10 次",
          sum(1 for c in h.clicks if _points_near(c, (960, 810))) == 10, h.clicks)
    check("流程结束回到主界面", h.close(), h.state)
    check("没有失败记录", not h.failures, h.failures)

    h = Harness(fail_serene=True)
    ok, msg = _run_flow(h, plan=empty_crystal, with_resonance=True)
    check("没确认静谧页签 → 一件都不买",
          "未确认「静谧水滴兑换」页签" in msg and h.shop_limits == {"启示水晶": 5, "共鸣水晶": 5},
          (msg, h.shop_limits))
    check("没切页签不写月度标记", nc.RESONANCE_META_KEY not in h.marked_keys, h.marked_keys)
    check("没切页签有失败记录", len(h.failures) >= 1, h.failures)

    h = Harness(fail_scroll=True)
    ok, msg = _run_flow(h, plan=empty_crystal, with_resonance=True)
    check("滚动找不到 → 未找到入口", "未找到「共鸣商店」入口" in msg, msg)
    check("滚动有界（≤20 次）", 0 < len(h.scrolls) <= 20, len(h.scrolls))

    h = Harness(monthly_done=True)
    ok, msg = _run_flow(h, plan=empty_crystal, with_resonance=True)
    check("本月已完成 → 跳过", "本月共鸣商店已完成" in msg, msg)
    check("跳过时不按 H", "h" not in [k.lower() for k in h.keys], h.keys)

    h = Harness(fail_shop=True)
    ok, msg = _run_flow(h, plan=empty_crystal, with_resonance=True)
    check("商城打不开 → 记录失败但不炸", ok is True and "商城未打开" in msg, msg)
    check("商城打不开 → 有失败记录", len(h.failures) >= 1, h.failures)


def test_wiring():
    print("[5] 接线静态断言")
    from game_auto.runtime import foreground_flow_runner as ffr

    check("内建动作包含 nikki_calendar", "nikki_calendar" in ffr._BUILTIN_ACTIONS, ffr._BUILTIN_ACTIONS)
    src = Path(ffr.__file__).read_text(encoding="utf-8")
    check("dispatch 分支调用 run_nikki_calendar_flow", "run_nikki_calendar_flow" in src)

    cfg = json.loads((PROJECT_ROOT / "flow_configs.example.json").read_text(encoding="utf-8"))
    actions = [s.get("action") for s in cfg["NikkiLauncher"]["steps"]]
    check("NikkiLauncher 第二步是 nikki_calendar", actions == ["nikki_enter_game", "nikki_calendar"], actions)

    items = defaults.DEFAULT_NIKKI_CRYSTAL_EXCHANGE.get("items") or []
    check("默认清单只有两张抽卡道具",
          [i.get("name") for i in items] == ["共鸣水晶", "启示水晶"], items)
    check("两只各买满 3 件", [i.get("qty") for i in items] == [3, 3], items)

    shop_items = defaults.DEFAULT_NIKKI_RESONANCE_SHOP.get("items") or []
    check("共鸣商店默认只换启示水晶/共鸣水晶",
          [i.get("name") for i in shop_items] == ["启示水晶", "共鸣水晶"], shop_items)
    check("共鸣商店各买满 5 件", [i.get("qty") for i in shop_items] == [5, 5], shop_items)

    check("InfinityNikki 默认接线 NikkiLauncher",
          defaults.DEFAULT_APPS["InfinityNikki"].get("prelaunch_flow") == "NikkiLauncher")

    import game_auto.settings.manager as manager

    merged = manager._default_config()
    check("默认配置含 nikki.crystal_exchange",
          merged.get("nikki", {}).get("crystal_exchange", {}).get("items") is not None, merged.get("nikki"))
    check("默认配置含 nikki.resonance_shop",
          merged.get("nikki", {}).get("resonance_shop", {}).get("items") is not None, merged.get("nikki"))


def main():
    refs = _ocr_refs()
    test_reference_ocr(refs)
    test_main_hud_not_calendar()
    test_helpers()
    test_full_flow()
    test_resonance_flow()
    test_wiring()

    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
