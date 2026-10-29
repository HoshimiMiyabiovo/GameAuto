"""_buy_one_target 调用顺序回归测试（2026-10-20「最大」数量按钮）。

打桩 baijing_shop 模块级依赖，断言顺序：
  点商品 → 等弹窗标记 → **点「最大」** → OCR「购买」→ 点购买 → 等结算 → 关弹窗
不依赖游戏窗口。跑法：.venv/Scripts/python.exe tests/suite_baijing_shop_max_qty_20261020.py
"""
import sys

sys.path.insert(0, "E:/GithubProject")

from game_auto.runtime import baijing_shop as shop


class DummyLogger:
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def debug(self, *a, **k): pass


def run_case(dialog_appears: bool):
    calls = []

    def fake_click(x, y):
        calls.append(("click", (x, y)))

    def fake_wait_markers(logger, hwnd, texts, **kw):
        calls.append(("wait_markers", tuple(texts)))
        return dialog_appears

    def fake_click_ratio(logger, hwnd, ratio, label=""):
        calls.append(("click_ratio", ratio, label))
        return (100, 200)

    def fake_ocr(logger, texts, hwnd, **kw):
        calls.append(("ocr", tuple(texts)))
        return {"center": (500, 600), "text": texts[0]}

    def fake_settled(logger, hwnd, **kw):
        calls.append(("settled",))
        return "purchased"

    def fake_dismiss(logger, hwnd):
        calls.append(("dismiss",))

    shop._pyautogui_click_guarded = fake_click
    shop._wait_page_markers = fake_wait_markers
    shop._click_ratio = fake_click_ratio
    shop._ocr_best_match = fake_ocr
    shop._wait_purchase_settled = fake_settled
    shop._dismiss_overlay = fake_dismiss
    shop.time.sleep = lambda s: None

    target = {"name": "茶憩通用邀请函", "center": (900, 700)}
    result = shop._buy_one_target(DummyLogger(), 12345, target)
    return result, calls


def main():
    ok = True

    def check(label, cond):
        nonlocal ok
        print(("PASS" if cond else "FAIL"), label)
        ok = ok and cond

    # 1) 正常路径：弹窗出现 → 必须先点最大、再点购买
    result, calls = run_case(dialog_appears=True)
    order = [c[0] for c in calls]
    check("返回 purchased", result == "purchased")
    check("点击序列 = click→wait_markers→click_ratio→ocr→click→settled→dismiss",
          order == ["click", "wait_markers", "click_ratio", "ocr", "click", "settled", "dismiss"])
    ratio_call = [c for c in calls if c[0] == "click_ratio"][0]
    check("最大按比例 (0.713,0.524)", tuple(ratio_call[1]) == (0.713, 0.524))
    check("最大在购买 OCR 之前", order.index("click_ratio") < order.index("ocr"))
    # 购买点击发生在 ocr 之后
    check("购买点击坐标来自 OCR", ("click", (500, 600)) in calls)

    # 2) 弹窗没出现：不应该点最大，直接 dismiss 返回 no_dialog
    result2, calls2 = run_case(dialog_appears=False)
    order2 = [c[0] for c in calls2]
    check("无弹窗返回 no_dialog", result2 == "no_dialog")
    check("无弹窗不点最大", "click_ratio" not in order2)
    check("无弹窗只 dismiss", order2 == ["click", "wait_markers", "dismiss"])

    print("\nALL PASS" if ok else "\nHAS FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
