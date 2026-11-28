# -*- coding: utf-8 -*-
"""白荆回廊午后茶憩：对话多屏时不再中途判失败（2026-11-28）。

现场 #533/#534：第 2 杯时尤尼的台词有好几屏（153231「前几天，我跟随β小队…」→
153318「我感觉有点奇怪…」），点对话框确实在往后推，但每杯只给 20s，脚本在对话
中途就判「未轮询到喝茶按钮」并取证失败。补丁：每杯预算改成"画面还在变就续命"
（静默 20s 才收，硬上限 90s），摘要等待 45s→90s。
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from game_auto.runtime import baijing_task_handlers as th  # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    if cond:
        print(f"  PASS {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL {name} {extra}")


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_progress_continues_while_text_changes():
    print("[1] 对话逐屏推进：画面一直变就一直点")
    clock = _Clock()
    tracker = th._TeaProgressTracker(now_fn=clock)
    alive = True
    for i in range(12):          # 12 屏 × 5s = 60s，超过旧的 20s 预算
        clock.t += 5.0
        alive = tracker.feed([f"台词第{i}句"])
    check("60s 内仍在推进", alive)
    check("未超硬上限", clock.t < tracker.max_total)


def test_static_frame_gives_up_after_idle():
    print("[2] 画面不变：静默 idle 秒后收手")
    clock = _Clock()
    tracker = th._TeaProgressTracker(now_fn=clock)
    check("首帧", tracker.feed(["尤尼："]))
    clock.t += 10.0
    check("10s 仍等", tracker.feed(["尤尼："]))
    clock.t += 15.0              # 累计静默 25s > 20s
    check("25s 后收手", not tracker.feed(["尤尼："]))


def test_hard_cap_wins_over_progress():
    print("[3] 一直变也不超过硬上限")
    clock = _Clock()
    tracker = th._TeaProgressTracker(idle=20.0, max_total=30.0, now_fn=clock)
    alive = True
    for i in range(10):
        clock.t += 5.0
        alive = tracker.feed([f"句{i}"])
    check("30s 后到顶", not alive)
    check("硬上限是 30s", tracker.max_total == 30.0)


def test_empty_frames_do_not_hold_forever():
    print("[4] 空帧（OCR 空）不能一直续命")
    clock = _Clock()
    tracker = th._TeaProgressTracker(now_fn=clock)
    tracker.feed([])
    clock.t += 10.0
    check("空帧 10s 仍等", tracker.feed([]))
    clock.t += 15.0
    check("空帧 25s 后收手", not tracker.feed([]))


def test_defaults_and_summary_budget():
    print("[5] 默认值与摘要等待预算")
    check("静默 20s", th.TEA_DRINK_POLL_SEC == 20.0)
    check("硬上限 90s", th.TEA_DRINK_POLL_MAX_SEC == 90.0)
    src = (PROJECT_ROOT / "src/game_auto/runtime/baijing_task_handlers.py").read_text(
        encoding="utf-8"
    )
    check("摘要等待已放宽到 90s", "timeout=90.0," in src)
    check("每杯循环用硬上限", "deadline = time.time() + TEA_DRINK_POLL_MAX_SEC" in src)
    check("失败文案不再写死 20s", "静默 {TEA_DRINK_POLL_SEC:.0f}s 才收" in src)


def main():
    test_progress_continues_while_text_changes()
    test_static_frame_gives_up_after_idle()
    test_hard_cap_wins_over_progress()
    test_empty_frames_do_not_hold_forever()
    test_defaults_and_summary_budget()
    print(f"\n{'FAILED: ' + ', '.join(FAILS) if FAILS else 'ALL PASS'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
