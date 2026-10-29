"""绝区零后勤商店采购逻辑单测（2026-10-21，含 OCR 兜底）。

monkeypatch 匹配/点击/睡眠，验证 buy_shop_groups 的：
1. 正常路径：哨站后勤 2 商品按 count 逐轮「点商品→确认」，协会物资先切页签再买
2. OCR 兜底：仓鼠笼访问器无模板，靠 ocr_text 文字定位买到
3. 已达上限（日常业绩第4次起无弹窗，只买到3）：中断该商品剩余次数，不计失败
4. 确认后弹窗不关：该商品失败，整体 all_ok=False，后续商品继续
5. 模板与 OCR 都找不到：该商品记失败
6. 用户停止（第 5 次点击后）：返回 (False, "用户停止 | ...")

运行：cd /e/GithubProject && game_auto/.venv/Scripts/python.exe tests/suite_zzz_monthly_shop_20261021.py
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
from game_auto.runtime import zzz_monthly_shop as shop  # noqa: E402
from game_auto.runtime.foreground_flow_runner import _normalize_flow_paths  # noqa: E402

failures = []

SCALES = [1.0, 1.25, 1.5, 0.8, 0.67]
CONFS = [0.88, 0.84, 0.8]

CONFIRM_XY = (1065, 760)
ITEM_XY = (100, 100)
OCR_XY = (900, 500)
TAB_XY = (230, 302)


def check(label, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}" + (f" — {detail}" if detail else ""))
    if not cond:
        failures.append(label)


class FakeGame:
    """模拟商店界面：弹窗开合、上限、确认卡住、页签状态、OCR 文字。"""

    def __init__(self, *, no_popup_after=None, confirm_stuck_items=(), ocr_missing_items=()):
        self.popup_open = False
        self.no_popup_after = dict(no_popup_after or {})   # {商品名: 前N次出弹窗}
        self.confirm_stuck_items = set(confirm_stuck_items)
        self.ocr_missing_items = set(ocr_missing_items)    # OCR 也找不到的商品
        self.current_tab = "哨站后勤"
        self._current_item = None
        self._item_clicks = {}

    def locate(self, template):
        base = os.path.basename(template or "")
        if base == "绝区零-商店-确认.png":
            return CONFIRM_XY if self.popup_open else None
        if base == "绝区零-商店-哨站后勤页签.png":
            return (230, 237) if self.current_tab == "哨站后勤" else None
        if base == "绝区零-商店-协会物资页签.png":
            return TAB_XY if self.current_tab == "哨站后勤" else None
        return ITEM_XY

    def ocr_locate(self, text):
        if text in self.ocr_missing_items:
            return None
        return OCR_XY

    def click(self, x, y):
        if (x, y) == CONFIRM_XY:
            self.popup_open = False
            return
        if (x, y) == TAB_XY:
            self.current_tab = "协会物资"
            return
        # 点商品（含 OCR 文字位）
        name = self._current_item
        n = self._item_clicks.get(name, 0) + 1
        self._item_clicks[name] = n
        limit = self.no_popup_after.get(name)
        self.popup_open = not (limit is not None and n > limit)

    def wait_gone(self):
        if self._current_item in self.confirm_stuck_items:
            return False
        self.popup_open = False
        return True


def run_case(step, fake, stop_after_clicks=None):
    state = {"stop": False, "clicks": 0}
    items_by_template = {}
    ocr_by_text = {}
    for g in step.get("shop_groups", []):
        for it in g.get("items", []):
            items_by_template[os.path.basename(it.get("template", ""))] = it.get("name")
            if it.get("ocr_text"):
                ocr_by_text[it["ocr_text"]] = it.get("name")

    def stop_check():
        return state["stop"]

    def fake_locate(hwnd, template, scales, confidences, timeout_sec, stop_check=None, interval=0.5):
        base = os.path.basename(template or "")
        if base in items_by_template:
            fake._current_item = items_by_template[base]
        return fake.locate(template)

    def fake_locate_ocr(logger, hwnd, text, timeout_sec, stop_check=None, interval=0.5):
        fake._current_item = ocr_by_text.get(text, text)
        return fake.ocr_locate(text)

    def fake_wait_gone(hwnd, template, scales, confidences, timeout_sec, stop_check=None, interval=0.5):
        return fake.wait_gone()

    def fake_click(hwnd, x, y, clicks=1, interval=0.2, background=False):
        fake.click(x, y)
        state["clicks"] += 1
        if stop_after_clicks is not None and state["clicks"] >= stop_after_clicks:
            state["stop"] = True

    orig_locate, orig_locate_ocr = shop._locate, shop._locate_ocr
    orig_gone, orig_click = shop._wait_gone, ffr._click_window_local
    orig_sleep = time.sleep
    ffr._click_window_local = fake_click
    shop._locate = fake_locate
    shop._locate_ocr = fake_locate_ocr
    shop._wait_gone = fake_wait_gone
    time.sleep = lambda s: None
    try:
        ok, msg = shop.buy_shop_groups(
            logging.getLogger("test"), 1234, step,
            scales=SCALES, confidences=CONFS, stop_check=stop_check,
        )
    finally:
        shop._locate, shop._locate_ocr = orig_locate, orig_locate_ocr
        shop._wait_gone, ffr._click_window_local = orig_gone, orig_click
        time.sleep = orig_sleep
    return ok, msg, state["clicks"]


STEP = {
    "confirm_template": "模板\\绝区零-商店-确认.png",
    "shop_groups": [
        {
            "name": "哨站后勤",
            "tab_selected_template": "模板\\绝区零-商店-哨站后勤页签.png",
            "items": [
                {"name": "原装母带", "template": "模板\\绝区零-商店-原装母带.png", "count": 2},
                {"name": "日常业绩", "template": "模板\\绝区零-商店-日常业绩.png", "count": 10},
            ],
        },
        {
            "name": "协会物资",
            "tab_template": "模板\\绝区零-商店-协会物资页签.png",
            "items": [
                {"name": "邦布券", "template": "模板\\绝区零-商店-邦布券.png", "count": 5},
                {"name": "仓鼠笼访问器", "template": "模板\\绝区零-商店-仓鼠笼访问器.png", "ocr_text": "仓鼠笼访问器", "count": 1},
            ],
        },
    ],
}
step_norm = _normalize_flow_paths({"steps": [copy.deepcopy(STEP)]})["steps"][0]

print("== 用例1：正常路径（原装2 + 日常10 + 邦布5 + 仓鼠笼 OCR 兜底 1）==")
fake = FakeGame()
ok, msg, n_clicks = run_case(step_norm, fake)
check("全部买够 all_ok=True", ok is True, msg)
check("摘要含 原装母带 2/2", "原装母带: 2/2" in msg, msg)
check("摘要含 日常业绩 10/10", "日常业绩: 10/10" in msg, msg)
check("摘要含 邦布券 5/5", "邦布券: 5/5" in msg, msg)
check("摘要含 仓鼠笼访问器 1/1（OCR 兜底买到）", "仓鼠笼访问器: 1/1" in msg, msg)
check("点击次数 = 37（18轮×2 + 页签1）", n_clicks == 37, f"实际 {n_clicks}")

print()
print("== 用例2：已达上限（日常业绩前3次出弹窗，第4次起不出；移除仓鼠笼隔离变量）==")
step_no_hamster = copy.deepcopy(step_norm)
step_no_hamster["shop_groups"][1]["items"] = [
    i for i in step_no_hamster["shop_groups"][1]["items"] if i.get("name") != "仓鼠笼访问器"
]
fake = FakeGame(no_popup_after={"日常业绩": 3})
ok, msg, n_clicks = run_case(step_no_hamster, fake)
check("上限不计失败 all_ok=True", ok is True, msg)
check("摘要含 日常业绩 3/10", "日常业绩: 3/10" in msg, msg)
check("摘要含 邦布券 5/5（后续商品继续）", "邦布券: 5/5" in msg, msg)
check("点击次数 = 22（原装4+日常7+邦布10+页签1）", n_clicks == 22, f"实际 {n_clicks}")

print()
print("== 用例3：确认后弹窗不关（原装母带卡住）==")
fake = FakeGame(confirm_stuck_items={"原装母带"})
ok, msg, n_clicks = run_case(step_norm, fake)
check("弹窗不关算失败 all_ok=False", ok is False, msg)
check("摘要含 原装母带 0/2（确认后弹窗未关）", "原装母带: 0/2（确认后弹窗未关）" in msg, msg)
check("后续商品继续：日常业绩 10/10", "日常业绩: 10/10" in msg, msg)
check("仓鼠笼 OCR 兜底仍执行：1/1", "仓鼠笼访问器: 1/1" in msg, msg)
check("点击次数 = 35（原装2+日常20+邦布10+仓鼠笼2+页签1）", n_clicks == 35, f"实际 {n_clicks}")

print()
print("== 用例4：模板与 OCR 都找不到仓鼠笼访问器 ==")
fake = FakeGame(ocr_missing_items={"仓鼠笼访问器"})
ok, msg, n_clicks = run_case(step_norm, fake)
check("OCR 也未命中算失败 all_ok=False", ok is False, msg)
check("摘要含 仓鼠笼访问器 0/1（商品定位失败）", "仓鼠笼访问器: 0/1（商品定位失败（模板/OCR 均未命中））" in msg, msg)
check("其他商品不受影响：邦布券 5/5", "邦布券: 5/5" in msg, msg)

print()
print("== 用例5：用户停止（第 5 次点击后）==")
fake = FakeGame()
ok, msg, n_clicks = run_case(step_norm, fake, stop_after_clicks=5)
check("停止返回 all_ok=False", ok is False)
check("消息以「用户停止」开头", msg.startswith("用户停止"), msg[:40])

print()
if failures:
    print(f"共 {len(failures)} 项失败")
    sys.exit(1)
print("全部通过")
