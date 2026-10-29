"""绝区零 / 崩铁「商店 → 兑换商店 → 目标页签」每月兑换通用流程测试（2026-10-25）。

覆盖两套配置（都是 action=monthly_exchange，差异只在 step 参数）：
- 绝区零 ZZZ_MONTHLY：ESC → 商城(24HOUR) → 信源清单 → 信号残响
- 崩铁 HSR_MONTHLY：ESC → 商店 → 兑换商店 → 余烬兑换

重点（用户 2026-10-25 两次强调「一定要进入余烬兑换/信号残响」）：
页签未选中态模板不消失（点击后没真的切过去）必须整步失败，且不能进入购买。

用例：
1. 空清单只记待定，不按 ESC、不点开商店（两游戏）
2. 主世界完整链路：ESC → 入口 → 导航 → 页签 → 逐轮购买 → ESC 回主世界
3. 已在 ESC 菜单不多按 ESC
4. 已达上限（点商品无弹窗）中断剩余次数且不计失败
5. 找不到目标页签：整步失败，不进入购买
6. 商品定位失败：该项失败，其余继续
7. 用户停止
8. 回不到主世界：购买成功照报，但摘要标注
9. 确认模板缺失：直接失败，不点任何东西
10. 页签已是选中态（未选中态模板不命中）：OCR 依据继续
11. 崩铁导航限定左侧区域（避开面包屑「兑换商店」）
12. 页签点击后未切换：整步失败，零购买（核心防护）
13. 配置静态：两流程 actions/顺序/空清单/模板存在/区域合法
"""

import copy
import logging
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT.parent))

from game_auto.runtime import foreground_flow_runner as ffr  # noqa: E402
from game_auto.runtime import monthly_exchange as mx  # noqa: E402
from game_auto.runtime import zzz_monthly_shop as shop  # noqa: E402
from game_auto.runtime.foreground_flow_runner import _load_flow_config, _resolve_exe_path  # noqa: E402
from game_auto.services.launch_task_catalog import (  # noqa: E402
    STANDALONE_TASK_APPS,
    STANDALONE_TASK_ORDER,
)
from game_auto.settings import APPS  # noqa: E402

failures = []

SCALES = [1.0, 1.25, 1.5, 0.8, 0.67]
CONFS = [0.88, 0.84, 0.8]

ENTRY = (1377, 970)
NAV = (184, 572)
TAB = (1375, 161)
TAB_MID = (665, 132)          # 崩铁：星芒兑换(441,132) 与 星尘之礼(889,132) 的中点
TAB_LEFT = (441, 132)
TAB_RIGHT = (889, 132)
GENS_TAB = (898, 128)         # 原神：星尘兑换页签
GENS_TAB_MID = (898, 127)     # 原神：星辉兑换(557,128) 与 原石购买(1239,127) 的中点
GENS_LEFT = (557, 128)
GENS_RIGHT = (1239, 127)
CONFIRM = (1065, 760)
ITEM = (900, 500)
MAIN_MARK = (1800, 1000)

TAB_POINTS = {TAB, TAB_MID, GENS_TAB, GENS_TAB_MID}

ZZZ = {
    "entry_template": "绝区零-商城入口.png",
    "entry_text": "商城",
    "nav_text": "信源清单",
    "tab_text": "信号残响",
    "tab_template": "绝区零-信号残响页签.png",
    "exit_marker": "绝区零-主菜单按钮.png",
    "confirm": "绝区零-商店-确认.png",
    "tab_point": TAB,
}
HSR = {
    "entry_template": "",  # 崩铁商店入口只有 OCR「商店」，没有裁模板
    "entry_text": "商店",
    "nav_text": "兑换商店",
    "tab_text": "余烬兑换",
    "tab_template": "崩铁-余烬兑换页签.png",
    "exit_marker": "崩铁-主界面.png",
    "confirm": "",
    "tab_point": TAB,
}
GENSHIN = {
    "entry_template": "",  # 原神商城入口只有 OCR「商城」
    "entry_text": "商城",
    "nav_text": "尘辉兑换",
    "tab_text": "星尘兑换",
    "tab_template": "原神-星尘兑换页签.png",
    "exit_marker": "原神-主界面.png",
    "confirm": "",
    "tab_point": GENS_TAB,
}


def check(label, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}" + (f" — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


class FakeGame:
    """三块界面：主世界 / ESC 菜单（商店入口）/ 商店（左侧导航 + 顶部页签）。"""

    def __init__(self, game, *, page="main", nav_ok=True, tab_ok=True, tab_switch_ok=True,
                 tab_selected=False, tab_ocr_ok=True, tab_template_ok=True,
                 brightness_unavailable=False, brightness_selected=210.0, brightness_unselected=35.0,
                 forbidden_visible=(), no_popup_after=None, confirm_stuck=(),
                 missing_items=(), exit_ok=True):
        self.game = game
        self.page = page
        self.nav_ok = nav_ok
        self.tab_ok = tab_ok
        self.tab_switch_ok = tab_switch_ok
        self.tab_selected = tab_selected
        self.tab_ocr_ok = tab_ocr_ok
        self.tab_template_ok = tab_template_ok
        self.brightness_unavailable = brightness_unavailable
        self.brightness_selected = brightness_selected
        self.brightness_unselected = brightness_unselected
        self.forbidden_visible = list(forbidden_visible)
        self.no_popup_after = dict(no_popup_after or {})
        self.confirm_stuck = set(confirm_stuck)
        self.missing_items = set(missing_items)
        self.exit_ok = exit_ok
        self.popup = False
        self.current_item = None
        self.item_clicks = {}
        self.clicks = []
        self.esc_count = 0

    def locate_nav(self, template):
        base = os.path.basename(template or "")
        if base and base == self.game["entry_template"] and self.page == "menu":
            return ENTRY
        if base == self.game["tab_template"]:
            if self.page == "shop" and self.tab_ok and self.tab_template_ok and not self.tab_selected:
                return self.game["tab_point"]
            return None
        if base == self.game["exit_marker"]:
            return MAIN_MARK if (self.page == "main" and self.exit_ok) else None
        return None

    def ocr_nav(self, text):
        if text == self.game["entry_text"]:
            return ENTRY if self.page == "menu" else None
        if text == self.game["nav_text"]:
            return NAV if (self.page == "shop" and self.nav_ok) else None
        if text == self.game["tab_text"]:
            if self.page == "shop" and self.tab_ok and self.tab_ocr_ok:
                return TAB
            return None
        if text == "星芒兑换":
            return TAB_LEFT if (self.page == "shop" and self.tab_ok) else None
        if text == "星尘之礼":
            return TAB_RIGHT if (self.page == "shop" and self.tab_ok) else None
        if text == "星辉兑换":
            return GENS_LEFT if (self.page == "shop" and self.tab_ok) else None
        if text == "原石购买":
            return GENS_RIGHT if (self.page == "shop" and self.tab_ok) else None
        return None

    def locate_shop(self, template):
        base = os.path.basename(template or "")
        if "确认" in base:
            return CONFIRM if self.popup else None
        return None

    def ocr_shop(self, text):
        if text in self.missing_items:
            return None
        self.current_item = text
        return ITEM

    def wait_gone(self, template):
        if os.path.basename(template or "") == self.game["tab_template"]:
            return self.tab_selected
        if self.current_item in self.confirm_stuck:
            return False
        self.popup = False
        return True

    def click(self, x, y):
        self.clicks.append((x, y))
        if (x, y) == ENTRY:
            self.page = "shop"
        elif (x, y) == NAV:
            self.page = "shop"
        elif (x, y) in TAB_POINTS:
            if self.tab_switch_ok:
                self.tab_selected = True
        elif (x, y) == CONFIRM:
            self.popup = False
        elif (x, y) == ITEM:
            name = self.current_item
            n = self.item_clicks.get(name, 0) + 1
            self.item_clicks[name] = n
            limit = self.no_popup_after.get(name)
            self.popup = not (limit is not None and n > limit)
        else:
            raise AssertionError(f"意外点击坐标: {(x, y)}")

    def escape(self):
        self.esc_count += 1
        if self.page == "shop":
            self.page = "menu"
        elif self.page == "menu":
            self.page = "main"
        else:
            self.page = "menu"


def run_case(step, game, fake, stop_after_clicks=None):
    state = {"stop": False}
    regions = []

    def stop_check():
        return state["stop"]

    def fake_click(hwnd, x, y, clicks=1, interval=0.2, background=False):
        fake.click(x, y)
        if stop_after_clicks is not None and len(fake.clicks) >= stop_after_clicks:
            state["stop"] = True

    def fake_region_ocr(logger, hwnd, text, timeout_sec, region_frac, stop_check=None, interval=0.5):
        regions.append(list(region_frac))
        return fake.ocr_nav(text)

    saved = {
        "locate": mx._locate,
        "ocr": mx._locate_ocr,
        "region": mx._locate_ocr_region,
        "esc": mx._press_escape,
        "sleep": mx._sleep,
        "gone": mx._wait_gone,
        "shop_locate": shop._locate,
        "shop_ocr": shop._locate_ocr,
        "shop_gone": shop._wait_gone,
        "click": ffr._click_window_local,
        "time_sleep": time.sleep,
        "brightness": mx._tab_brightness,
        "visible": mx._visible_texts,
    }
    mx._locate = lambda hwnd, template, scales, confs, timeout, stop_check=None, interval=0.5: fake.locate_nav(template)
    mx._locate_ocr = lambda logger, hwnd, text, timeout, stop_check=None, interval=0.5: fake.ocr_nav(text)
    mx._locate_ocr_region = fake_region_ocr
    mx._press_escape = lambda hwnd: fake.escape()
    mx._sleep = lambda seconds, stop_check=None: None
    mx._wait_gone = lambda hwnd, template, scales, confs, timeout, stop_check=None, interval=0.5: fake.wait_gone(template)
    mx._tab_brightness = lambda logger, hwnd, center: (
        None if fake.brightness_unavailable
        else (fake.brightness_selected if fake.tab_selected else fake.brightness_unselected)
    )
    mx._visible_texts = lambda logger, hwnd: [
        {"text": t, "confidence": 0.8} for t in fake.forbidden_visible
    ]
    shop._locate = lambda hwnd, template, scales, confs, timeout, stop_check=None, interval=0.5: fake.locate_shop(template)
    shop._locate_ocr = lambda logger, hwnd, text, timeout, stop_check=None, interval=0.5: fake.ocr_shop(text)
    shop._wait_gone = lambda hwnd, template, scales, confs, timeout, stop_check=None, interval=0.5: fake.wait_gone(template)
    ffr._click_window_local = fake_click
    time.sleep = lambda s: None
    try:
        ok, msg = mx.buy_monthly_exchange(
            logging.getLogger("test"), 1234, step,
            scales=SCALES, confidences=CONFS, stop_check=stop_check,
        )
    finally:
        mx._locate = saved["locate"]
        mx._locate_ocr = saved["ocr"]
        mx._locate_ocr_region = saved["region"]
        mx._press_escape = saved["esc"]
        mx._sleep = saved["sleep"]
        mx._wait_gone = saved["gone"]
        shop._locate = saved["shop_locate"]
        shop._locate_ocr = saved["shop_ocr"]
        shop._wait_gone = saved["shop_gone"]
        ffr._click_window_local = saved["click"]
        time.sleep = saved["time_sleep"]
        mx._tab_brightness = saved["brightness"]
        mx._visible_texts = saved["visible"]
    return ok, msg, fake, regions


ZZZ_DEFAULT_ITEMS = [
    {"name": "加密母带", "count": 2},
    {"name": "原装母带", "count": 1},
]


def zzz_step(items=None):
    """绝区零参数直接从 flow_configs 读（含 forbidden_texts），只替换商品清单。"""
    step = copy.deepcopy(load_step("ZZZ_MONTHLY", "信号残响每月兑换"))
    step["items"] = copy.deepcopy(ZZZ_DEFAULT_ITEMS if items is None else items)
    return step


def load_step(flow_key, name):
    return next(s for s in _load_flow_config(flow_key)["steps"] if s.get("name") == name)


def hsr_step(items=None):
    """崩铁参数取自 flow_configs（含 nav_region_frac），确认模板借后勤商店的只为跑状态机。"""
    step = copy.deepcopy(load_step("HSR_MONTHLY", "余烬兑换每月兑换"))
    step["confirm_template"] = "模板\\绝区零-商店-确认.png"
    if items is not None:
        step["items"] = items
    return step


def genshin_step(items=None):
    """原神参数取自 flow_configs（含 forbidden_texts / 亮度阈值 150），确认模板同样只为跑状态机。"""
    step = copy.deepcopy(load_step("GENSHIN_MONTHLY", "星尘兑换每月兑换"))
    step["confirm_template"] = "模板\\绝区零-商店-确认.png"
    if items is not None:
        step["items"] = items
    return step


print("== 用例1：空清单只记待定（三游戏）==")
for tag, step, game in (("绝区零", zzz_step(), ZZZ), ("崩铁", hsr_step([]), HSR), ("原神", genshin_step([]), GENSHIN)):
    step["items"] = []
    ok, msg, fake, _ = run_case(step, game, FakeGame(game))
    check(f"{tag} 空清单成功", ok is True, msg)
    check(f"{tag} 摘要写明待定", "待定" in msg, msg)
    check(f"{tag} 不按 ESC 不点开商店", fake.esc_count == 0 and not fake.clicks, f"esc={fake.esc_count} clicks={fake.clicks}")

print()
print("== 用例2：主世界完整链路（绝区零）==")
ok, msg, fake, _ = run_case(zzz_step(), ZZZ, FakeGame(ZZZ, page="main"))
check("整体成功", ok is True, msg)
check("摘要含 加密母带 2/2", "加密母带: 2/2" in msg, msg)
check("摘要含 原装母带 1/1", "原装母带: 1/1" in msg, msg)
expected = [ENTRY, NAV, TAB, ITEM, CONFIRM, ITEM, CONFIRM, ITEM, CONFIRM]
check("点击顺序：入口→导航→页签→商品→确认", fake.clicks == expected, f"{fake.clicks}")
check("ESC 次数=3（打开菜单1 + 退出2）", fake.esc_count == 3, f"esc={fake.esc_count}")
check("结束后回到主世界", fake.page == "main", fake.page)

print()
print("== 用例3：已在 ESC 菜单不多按（绝区零）==")
ok, msg, fake, _ = run_case(zzz_step(), ZZZ, FakeGame(ZZZ, page="menu"))
check("整体成功", ok is True, msg)
check("ESC 次数=2（仅退出用）", fake.esc_count == 2, f"esc={fake.esc_count}")

print()
print("== 用例4：已达上限（第2次点商品起无弹窗）==")
step = zzz_step()
step["items"] = [{"name": "加密母带", "count": 3}]
ok, msg, fake, _ = run_case(step, ZZZ, FakeGame(ZZZ, page="menu", no_popup_after={"加密母带": 1}))
check("上限不计失败", ok is True, msg)
check("摘要含 1/3（已达上限或无弹窗）", "加密母带: 1/3（已达上限或无弹窗）" in msg, msg)

print()
print("== 用例4b：绝区零仍能看到信号余波商品 → 必须失败 ==")
ok, msg, fake, _ = run_case(
    zzz_step(), ZZZ, FakeGame(ZZZ, page="menu", forbidden_visible=["硫磺石"]),
)
check("看到硫磺石 → 整步失败", ok is False, msg)
check("失败原因写明错页签", "检测到「硫磺石」，当前不在目标页签" in msg, msg)
check("错页签时零购买", all(c not in (ITEM, CONFIRM) for c in fake.clicks), f"{fake.clicks}")

print()
print("== 用例5：找不到目标页签（绝区零）==")
ok, msg, fake, _ = run_case(zzz_step(), ZZZ, FakeGame(ZZZ, page="menu", tab_ok=False))
check("整步失败", ok is False, msg)
check("错误说明页签未找到", "未找到「信号残响」页签" in msg, msg)
check("未进入购买", all(c not in (ITEM, CONFIRM) for c in fake.clicks), f"{fake.clicks}")

print()
print("== 用例6：商品 OCR 定位失败 ==")
ok, msg, fake, _ = run_case(zzz_step(), ZZZ, FakeGame(ZZZ, page="menu", missing_items={"加密母带"}))
check("失败", ok is False, msg)
check("摘要含定位失败", "加密母带: 0/2（商品定位失败" in msg, msg)
check("其余商品继续 原装母带 1/1", "原装母带: 1/1" in msg, msg)

print()
print("== 用例7：用户停止 ==")
ok, msg, fake, _ = run_case(zzz_step(), ZZZ, FakeGame(ZZZ, page="menu"), stop_after_clicks=2)
check("返回失败", ok is False, msg)
check("摘要以用户停止开头", msg.startswith("用户停止"), msg[:30])

print()
print("== 用例8：退出时一直看不到主世界标志 ==")
ok, msg, fake, _ = run_case(zzz_step(), ZZZ, FakeGame(ZZZ, page="menu", exit_ok=False))
check("购买成功仍报成功", ok is True, msg)
check("摘要标注未确认返回主界面", "未确认返回主界面" in msg, msg)
check("ESC 次数=3（从菜单出发全部用于退出重试）", fake.esc_count == 3, f"esc={fake.esc_count}")

print()
print("== 用例9：确认模板缺失直接失败 ==")
step = zzz_step()
step.pop("confirm_template")
ok, msg, fake, _ = run_case(step, ZZZ, FakeGame(ZZZ, page="menu"))
check("返回失败", ok is False, msg)
check("说明模板缺失", "确认按钮模板缺失" in msg, msg)
check("零点击零 ESC", not fake.clicks and fake.esc_count == 0, f"{fake.clicks} esc={fake.esc_count}")

print()
print("== 用例10：页签已是选中态（模板不命中，亮度确认）==")
ok, msg, fake, _ = run_case(zzz_step(), ZZZ, FakeGame(ZZZ, page="menu", tab_selected=True))
check("整体成功", ok is True, msg)
check("已选中时不再重复点击页签", TAB not in fake.clicks, f"{fake.clicks}")

print()
print("== 用例11+12：崩铁链路（区域导航 / 页签必须真的切换）==")
ok, msg, fake, regions = run_case(
    hsr_step([{"name": "测试商品", "count": 1}]), HSR, FakeGame(HSR, page="main"),
)
check("崩铁完整链路成功", ok is True, msg)
check("点击顺序：商店→兑换商店→余烬兑换→商品→确认",
      fake.clicks == [ENTRY, NAV, TAB, ITEM, CONFIRM], f"{fake.clicks}")
check("导航走左侧区域定位（避开面包屑）", regions == [[0.0, 0.18, 0.2, 0.95]], f"{regions}")
check("结束后回到主世界", fake.page == "main", fake.page)

ok, msg, fake, _ = run_case(
    hsr_step([{"name": "测试商品", "count": 1}]), HSR,
    FakeGame(HSR, page="main", tab_switch_ok=False),
)
check("页签未切换 → 整步失败", ok is False, msg)
check("失败原因写明未切换", "未能切换到「余烬兑换」页签" in msg, msg)
check("页签未切换时零购买（不得在错误页签买东西）",
      all(c not in (ITEM, CONFIRM) for c in fake.clicks), f"{fake.clicks}")

print()
print("== 用例11b：已选中 + 目标页签 OCR 认错（中点定位 + 亮度确认）==")
ok, msg, fake, _ = run_case(
    hsr_step([{"name": "测试商品", "count": 1}]), HSR,
    FakeGame(HSR, page="menu", tab_selected=True, tab_ocr_ok=False),
)
check("整体成功", ok is True, msg)
check("按相邻页签中点判定，且已选中不重复点击", TAB_MID not in fake.clicks, f"{fake.clicks}")
check("购买了商品", ITEM in fake.clicks, f"{fake.clicks}")

print()
print("== 用例11c：模板失效 + OCR 认错 + 未选中（中点点击 + 亮度复核）==")
ok, msg, fake, _ = run_case(
    hsr_step([{"name": "测试商品", "count": 1}]), HSR,
    FakeGame(HSR, page="menu", tab_ocr_ok=False, tab_template_ok=False),
)
check("整体成功", ok is True, msg)
check("点击的是相邻页签中点", TAB_MID in fake.clicks, f"{fake.clicks}")
check("购买了商品", ITEM in fake.clicks, f"{fake.clicks}")

print()
print("== 用例11d：亮度读不到且模板不命中 → 不得放行 ==")
ok, msg, fake, _ = run_case(
    hsr_step([{"name": "测试商品", "count": 1}]), HSR,
    FakeGame(HSR, page="menu", tab_selected=True, tab_ocr_ok=False, brightness_unavailable=True),
)
check("整步失败", ok is False, msg)
check("说明无法确认页签亮度", "无法读取「余烬兑换」页签亮度" in msg, msg)
check("零购买", all(c not in (ITEM, CONFIRM) for c in fake.clicks), f"{fake.clicks}")

print()
print("== 用例11e：崩铁仍能看到星芒兑换五星光锥 → 必须失败 ==")
ok, msg, fake, _ = run_case(
    hsr_step([{"name": "测试商品", "count": 1}]), HSR,
    FakeGame(HSR, page="menu", forbidden_visible=["银河铁道之夜"]),
)
check("看到五星光锥 → 整步失败", ok is False, msg)
check("失败原因写明错页签", "检测到「银河铁道之夜」，当前不在目标页签" in msg, msg)
check("错页签时零购买", all(c not in (ITEM, CONFIRM) for c in fake.clicks), f"{fake.clicks}")

print()
print("== 用例12：原神星尘兑换链路 + 错页签拦截 ==")
ok, msg, fake, _ = run_case(
    genshin_step([{"name": "测试商品", "count": 1}]), GENSHIN,
    FakeGame(GENSHIN, page="main", brightness_selected=186.0, brightness_unselected=104.0),
)
check("原神完整链路成功", ok is True, msg)
check("点击顺序：商城→尘辉兑换→星尘兑换→商品→确认",
      fake.clicks == [ENTRY, NAV, GENS_TAB, ITEM, CONFIRM], f"{fake.clicks}")
check("结束后回到主世界", fake.page == "main", fake.page)

ok, msg, fake, _ = run_case(
    genshin_step([{"name": "测试商品", "count": 1}]), GENSHIN,
    FakeGame(GENSHIN, page="menu", forbidden_visible=["宗室长剑"]),
)
check("看到宗室武器 → 整步失败", ok is False, msg)
check("失败原因写明错页签", "检测到「宗室长剑」，当前不在目标页签" in msg, msg)
check("错页签时零购买", all(c not in (ITEM, CONFIRM) for c in fake.clicks), f"{fake.clicks}")

ok, msg, fake, _ = run_case(
    genshin_step([{"name": "测试商品", "count": 1}]), GENSHIN,
    FakeGame(GENSHIN, page="menu", tab_selected=True,
             brightness_selected=186.0, brightness_unselected=104.0),
)
check("已选中（亮度 186 ≥ 150）不再点击页签", ok is True and GENS_TAB not in fake.clicks, msg)

ok, msg, fake, _ = run_case(
    genshin_step([{"name": "测试商品", "count": 1}]), GENSHIN,
    FakeGame(GENSHIN, page="menu", tab_ocr_ok=False, tab_template_ok=False,
             brightness_selected=186.0, brightness_unselected=114.0),
)
check("模板失效+OCR认错 → 相邻页签中点点击后成功", ok is True and GENS_TAB_MID in fake.clicks, msg)

ok, msg, fake, _ = run_case(
    genshin_step([{"name": "测试商品", "count": 1}]), GENSHIN,
    FakeGame(GENSHIN, page="menu", tab_ocr_ok=False, tab_template_ok=False, tab_switch_ok=False,
             brightness_selected=186.0, brightness_unselected=114.0),
)
check("未选中页签亮度 114 < 150 → 不得误判为选中", ok is False, msg)
check("失败原因写明亮度不足", "亮度 114 < 150" in msg, msg)
check("零购买", all(c not in (ITEM, CONFIRM) for c in fake.clicks), f"{fake.clicks}")

print()
print("== 用例13：配置静态 ==")
zzz_cfg = _load_flow_config("ZZZ_MONTHLY")
zzz_names = [s.get("name") for s in zzz_cfg["steps"]]
zzz = load_step("ZZZ_MONTHLY", "信号残响每月兑换")
check("绝区零步骤顺序：等主界面 → 信号残响 → 打开功能菜单",
      zzz_names.index("信号残响每月兑换") == zzz_names.index("等待进入主界面") + 1
      and zzz_names.index("打开功能菜单") == zzz_names.index("信号残响每月兑换") + 1, f"{zzz_names}")
check("绝区零 action=monthly_exchange", zzz.get("action") == "monthly_exchange", zzz.get("action"))
check("绝区零清单为空（下月补）", zzz.get("items") == [], f"{zzz.get('items')}")
check("绝区零日志前缀", zzz.get("log_label") == "信源兑换", zzz.get("log_label"))
check("绝区零错页签拦截词齐备（信号余波商品，含 OCR 变体）",
      zzz.get("forbidden_texts") == ["硫磺石", "燃狱齿轮", "嵌合编译器", "啜泣摇篮", "嚼泣摇篮"],
      f"{zzz.get('forbidden_texts')}")
for key in ("shop_entry_template", "tab_template", "confirm_template", "exit_marker_template"):
    p = zzz.get(key) or ""
    check(f"绝区零 {key} 已锚定且存在", os.path.isabs(p) and os.path.isfile(p), p)

hsr_cfg = _load_flow_config("HSR_MONTHLY")
hsr_names = [s.get("name") for s in hsr_cfg["steps"]]
check("崩铁步骤顺序：进入主界面 → 余烬兑换",
      hsr_names == ["进入崩铁主界面", "余烬兑换每月兑换"], f"{hsr_names}")
check("崩铁进入主界面复用 starrail_enter_main",
      hsr_cfg["steps"][0].get("action") == "starrail_enter_main", hsr_cfg["steps"][0].get("action"))
hsr = load_step("HSR_MONTHLY", "余烬兑换每月兑换")
check("崩铁 action=monthly_exchange", hsr.get("action") == "monthly_exchange", hsr.get("action"))
check("崩铁日志前缀", hsr.get("log_label") == "余烬兑换", hsr.get("log_label"))
check("崩铁清单为空（下月补）", hsr.get("items") == [], f"{hsr.get('items')}")
check("崩铁确认模板故意留空（弹窗未截图）", hsr.get("confirm_template") == "", hsr.get("confirm_template"))
frac = hsr.get("nav_region_frac")
check("崩铁导航区域合法", isinstance(frac, list) and len(frac) == 4 and 0 <= frac[0] < frac[2] <= 1 and 0 <= frac[1] < frac[3] <= 1, f"{frac}")
check("崩铁配置了相邻页签兜底", hsr.get("tab_between_texts") == ["星芒兑换", "星尘之礼"],
      f"{hsr.get('tab_between_texts')}")
check("崩铁错页签拦截词齐备（星芒兑换五星光锥）",
      hsr.get("forbidden_texts") == ["银河铁道之夜", "时节不居", "但战斗还未结束", "无可取代的东西", "以世界之名"],
      f"{hsr.get('forbidden_texts')}")
for key in ("tab_template", "exit_marker_template"):
    p = hsr.get(key) or ""
    check(f"崩铁 {key} 已锚定且存在", os.path.isabs(p) and os.path.isfile(p), p)

gen_cfg = _load_flow_config("GENSHIN_MONTHLY")
check("原神月度流程可加载", gen_cfg is not None)
gen_names = [s.get("name") for s in (gen_cfg or {}).get("steps", [])]
check("原神步骤顺序：进入主界面 → 星尘兑换",
      gen_names == ["进入原神主界面", "星尘兑换每月兑换"], f"{gen_names}")
check("原神进入主界面复用 genshin_enter_main",
      (gen_cfg or {}).get("steps", [{}])[0].get("action") == "genshin_enter_main")
check("原神走 exe_from_game（启动游戏本体而不是 BetterGI）",
      gen_cfg.get("exe_from_game") == "GenshinImpact" and not gen_cfg.get("exe_from_app"),
      f"{gen_cfg.get('exe_from_game')}/{gen_cfg.get('exe_from_app')}")
gen_exe = _resolve_exe_path(gen_cfg, "GENSHIN_MONTHLY")
check("exe_from_game 解析出 YuanShen.exe", bool(gen_exe) and "YuanShen.exe" in str(gen_exe), str(gen_exe))

gen = load_step("GENSHIN_MONTHLY", "星尘兑换每月兑换")
check("原神 action=monthly_exchange", gen.get("action") == "monthly_exchange", gen.get("action"))
check("原神日志前缀", gen.get("log_label") == "星尘兑换", gen.get("log_label"))
check("原神清单为空（下月补）", gen.get("items") == [], f"{gen.get('items')}")
check("原神确认模板故意留空（弹窗未截图）", gen.get("confirm_template") == "", gen.get("confirm_template"))
check("原神亮度阈值 150（未选中页签背景比其它两游戏亮）",
      gen.get("tab_selected_brightness") == 150, gen.get("tab_selected_brightness"))
check("原神错页签拦截词齐备（宗室五件武器）",
      gen.get("forbidden_texts") == ["宗室长剑", "宗室大剑", "宗室猎枪", "宗室秘法录", "宗室长弓"],
      f"{gen.get('forbidden_texts')}")
check("原神配置相邻页签兜底", gen.get("tab_between_texts") == ["星辉兑换", "原石购买"],
      f"{gen.get('tab_between_texts')}")
for key in ("tab_template", "exit_marker_template"):
    p = gen.get(key) or ""
    check(f"原神 {key} 已锚定且存在", os.path.isabs(p) and os.path.isfile(p), p)

check("独立任务目录包含 GenshinMonthlyFlow", "GenshinMonthlyFlow" in STANDALONE_TASK_APPS)
check("独立任务顺序包含原神每月", "GenshinMonthlyFlow" in STANDALONE_TASK_ORDER)
check("merge 后 APPS 暴露 GenshinMonthlyFlow", "GenshinMonthlyFlow" in APPS)

bg_src = (PROJECT_ROOT / "runtime" / "background_template_runner.py").read_text(encoding="utf-8")
check("后台 runner 提供原神每月入口",
      "def run_genshin_monthly_background" in bg_src and '"GENSHIN_MONTHLY"' in bg_src)
batch_src = (PROJECT_ROOT / "runtime" / "task_batch_runner.py").read_text(encoding="utf-8")
check("批次执行器接线 GenshinMonthlyFlow",
      'app_key == "GenshinMonthlyFlow"' in batch_src and "run_genshin_monthly_background" in batch_src)

runner_src = (PROJECT_ROOT / "runtime" / "foreground_flow_runner.py").read_text(encoding="utf-8")
check("runner 注册 monthly_exchange", '"monthly_exchange"' in runner_src)
_builtin_count = runner_src.count('"monthly_exchange"')
check("runner 内建动作清单与分发包含该动作",
      "_BUILTIN_ACTIONS = (" in runner_src and _builtin_count >= 2,
      f"出现 {_builtin_count} 次")
check("旧 action zzz_signal_exchange 已移除", "zzz_signal_exchange" not in runner_src)

print()
if failures:
    print(f"共 {len(failures)} 项失败")
    sys.exit(1)
print("全部通过")
