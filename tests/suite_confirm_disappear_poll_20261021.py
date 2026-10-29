"""confirm_disappear 轮询行为回归（2026-10-21 Qoder 假失败事故）。

事故：Qoder 领取成功后弹窗要 ~30s 才关闭，而 confirm_disappear 只在 +3s
单次检查 → 连报 4 次"元素仍存在" → 假失败（服务端 7.5s 前就 CLAIMED 了）。
修正：改为每 2s 轮询直到 timeout_sec。

跑法：.venv/Scripts/python.exe tests/suite_confirm_disappear_poll_20261021.py
"""
import sys

sys.path.insert(0, "E:/GithubProject")

import logging

from game_auto.runtime import foreground_flow_runner as ffr

LOG = logging.getLogger("t")


class FakeClock:
    """可控的 _check_element_exists 序列。"""

    def __init__(self, exists_pattern):
        self.pattern = list(exists_pattern)
        self.calls = 0

    def __call__(self, **kwargs):
        self.calls += 1
        idx = min(self.calls - 1, len(self.pattern) - 1)
        return self.pattern[idx], "fake"


def main():
    ok = True

    def check(label, cond):
        nonlocal ok
        print(("PASS" if cond else "FAIL"), label)
        ok = ok and cond

    orig_check = ffr._check_element_exists
    orig_wait = ffr._wait_any_template
    try:
        # 1) 元素延迟消失（第 2 次轮询才没了）→ 必须等到并成功
        ffr._check_element_exists = FakeClock([True, False])
        ffr._wait_any_template = lambda *a, **k: True
        t0 = __import__("time").time()
        success, reason = ffr.confirm_disappear(
            logger=LOG, hwnd=1, step_name="延迟消失",
            template="模板\\x.png", target_text="领取",
            wait_time=3, timeout_sec=8, confirm_templates=[],
            scales=[1.0], confidences=[0.9], enable_ocr=False,
        )
        elapsed = __import__("time").time() - t0
        check("延迟消失 → 成功（轮询等到）", success is True)
        check("确实轮询了多次（非单次检查）", ffr._check_element_exists.calls >= 2)
        check("在超时前返回（%.1fs < 8s）" % elapsed, elapsed < 8)

        # 2) 永不消失 → 超时失败（不能早退）
        ffr._check_element_exists = FakeClock([True])
        success, reason = ffr.confirm_disappear(
            logger=LOG, hwnd=1, step_name="永不消失",
            template="模板\\x.png", target_text="领取",
            wait_time=3, timeout_sec=2, confirm_templates=[],
            scales=[1.0], confidences=[0.9], enable_ocr=False,
        )
        check("永不消失 → 超时失败", success is False)
        check("失败原因带超时信息", "2" in reason)

        # 3) 消失 + 确认模板出现 → 成功
        ffr._check_element_exists = FakeClock([True, False])
        ffr._wait_any_template = lambda *a, **k: True
        success, reason = ffr.confirm_disappear(
            logger=LOG, hwnd=1, step_name="消失且确认模板",
            template="模板\\x.png", target_text="领取",
            wait_time=3, timeout_sec=8, confirm_templates=["模板\\done.png"],
            scales=[1.0], confidences=[0.9], enable_ocr=False,
        )
        check("消失+确认模板 → 成功", success is True)

        # 4) 消失但确认模板没出现 → 失败（保守）
        ffr._check_element_exists = FakeClock([False])
        ffr._wait_any_template = lambda *a, **k: False
        success, reason = ffr.confirm_disappear(
            logger=LOG, hwnd=1, step_name="消失无确认模板",
            template="模板\\x.png", target_text="领取",
            wait_time=3, timeout_sec=8, confirm_templates=["模板\\done.png"],
            scales=[1.0], confidences=[0.9], enable_ocr=False,
        )
        check("消失但确认模板缺失 → 失败", success is False)

        # 5) 一开始就消失 → 立即成功（不浪费超时）
        ffr._check_element_exists = FakeClock([False])
        t0 = __import__("time").time()
        success, reason = ffr.confirm_disappear(
            logger=LOG, hwnd=1, step_name="立即消失",
            template="模板\\x.png", target_text="领取",
            wait_time=3, timeout_sec=30, confirm_templates=[],
            scales=[1.0], confidences=[0.9], enable_ocr=False,
        )
        check("立即消失 → 快速成功（%.1fs）" % (__import__("time").time() - t0),
              success is True and __import__("time").time() - t0 < 3)
    finally:
        ffr._check_element_exists = orig_check
        ffr._wait_any_template = orig_wait

    print("\nALL PASS" if ok else "\nHAS FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
