"""鸣潮「等游戏窗口就绪」放宽的回归（2026-11-02 17:54 事故）。

当天 17:54:02 Wuthering Waves.exe 已起，120s 后仍判「窗口未就绪」并把正在
加载的游戏杀掉；用户实际已经看到启动画面。修复目标：
  - 默认 300s；
  - 窗口按进程树匹配（UE 窗口可能由子进程持有）；
  - 窗口出现过就放行（加载中切分辨率抖动不再算失败）；
  - 超时只拒绝拉脚本，不杀游戏进程。

跑法：.venv\\Scripts\\python.exe -m pytest tests/suite_wuwa_window_wait_20261102.py -q
"""

import io
import logging
import sys
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


class FakeTime:
    def __init__(self, start=1000.0):
        self.now = start

    def time(self):
        return self.now

    def sleep(self, sec):
        self.now += sec


class FakeProc:
    def __init__(self, pid, name, children=()):
        self.pid = pid
        self.info = {"name": name}
        self._children = list(children)

    def children(self, recursive=True):
        return list(self._children)


def make_ctl(stop=False):
    from game_auto.runtime import automation_controller as ac

    ctl = ac.AutomationController.__new__(ac.AutomationController)
    ctl.logger = logging.getLogger("test.wuwa")
    ctl.stop_current_task = stop
    return ctl


CFG = {"game": {"process_name": "Wuthering Waves.exe"}}


def wait_with(ctl, windows_fn, pids=(100,), rect=None, timeout=8, min_area=307200):
    from game_auto.runtime import automation_controller as ac

    fake = FakeTime()
    get_rect = rect or (lambda hwnd: (0, 0, 1920, 1080))
    with patch.object(ac, "time", fake), \
            patch.object(ctl, "_game_related_pids", lambda name: list(pids)), \
            patch.object(ctl, "_find_visible_windows_for_pids", windows_fn), \
            patch.object(ac.win32gui, "GetWindowRect", get_rect):
        result = ctl._wait_game_window_ready(CFG, "鸣潮", timeout=timeout, min_area=min_area)
    return result, fake.now - 1000.0


def test_stable_window_passes():
    print("[1] 窗口达标且稳定 → 立即放行")
    ctl = make_ctl()
    res, elapsed = wait_with(ctl, lambda pids: [(1920 * 1080, 9001)])
    check("返回 True", res is True, res)
    check("稳定后不再空等（<5s）", elapsed < 5, elapsed)


def test_loading_window_keeps_moving_is_not_killed():
    print("[2] 加载中窗口尺寸一直抖 → 超时也放行（旧逻辑判死）")
    ctl = make_ctl()
    counter = {"n": 0}

    def moving_rect(hwnd):
        counter["n"] += 1
        return (counter["n"], 0, 1920, 1080)

    res, elapsed = wait_with(
        ctl, lambda pids: [(1920 * 1080, 9001)], rect=moving_rect, timeout=8
    )
    check("返回 True（窗口已出现，交给脚本等加载）", res is True, res)
    check("等满了超时才放行", elapsed >= 8, elapsed)


def test_small_window_seen_counts_as_progress():
    print("[3] 有可见窗口但面积未达阈值 → 超时放行")
    ctl = make_ctl()
    res, _ = wait_with(ctl, lambda pids: [(320 * 240, 9001)], timeout=6)
    check("返回 True", res is True, res)


def test_never_window_is_none():
    print("[4] 进程在但从未出现窗口 → None（不拉脚本）")
    ctl = make_ctl()
    res, _ = wait_with(ctl, lambda pids: [], timeout=6)
    check("返回 None", res is None, res)


def test_process_gone_is_none():
    print("[5] 游戏进程退出 → None")
    ctl = make_ctl()
    res, _ = wait_with(ctl, lambda pids: [], pids=(), timeout=6)
    check("返回 None", res is None, res)


def test_user_stop():
    print("[6] 用户停止 → False")
    ctl = make_ctl(stop=True)
    res, elapsed = wait_with(ctl, lambda pids: [(1920 * 1080, 9001)], timeout=8)
    check("返回 False", res is False, res)
    check("立即退出", elapsed < 2, elapsed)


def test_child_process_window_matched():
    print("[7] 游戏窗口由子进程持有也能识别")
    from game_auto.runtime import automation_controller as ac

    ctl = make_ctl()
    procs = [FakeProc(100, "Wuthering Waves.exe", [FakeProc(200, "GameWindowHost.exe")])]
    fake = FakeTime()
    with patch.object(ac, "time", fake), \
            patch.object(ac.psutil, "process_iter", lambda attrs=None: procs), \
            patch.object(ctl, "_visible_window_candidates", lambda: [(800000, 9001, 200)]), \
            patch.object(ac.win32gui, "GetWindowRect", lambda hwnd: (0, 0, 1600, 900)):
        pids = ctl._game_related_pids("Wuthering Waves.exe")
        check("进程树包含子进程", pids == [100, 200], pids)
        res = ctl._wait_game_window_ready(CFG, "鸣潮", timeout=8)
    check("子进程窗口被认作游戏窗口", res is True, res)


def test_defaults_and_no_kill_on_failure():
    print("[8] 默认 300s + 失败不杀游戏")
    from game_auto.runtime import automation_controller as ac
    from game_auto.settings.defaults import DEFAULT_APPS

    check("默认等待改成 300s",
          DEFAULT_APPS["WutheringWaves"].get("game_window_wait_sec") == 300,
          DEFAULT_APPS["WutheringWaves"].get("game_window_wait_sec"))
    src = Path(ac.__file__).read_text(encoding="utf-8")
    check("调用点缺省值也是 300",
          'config.get("game_window_wait_sec", 300)' in src)
    start = src.index("if window_ready is None:")
    end = src.index("# 定义 script_type", start)
    branch = src[start:end]
    check("None 分支不再杀游戏进程",
          "_kill_related_processes" not in branch, branch)


def main():
    test_stable_window_passes()
    test_loading_window_keeps_moving_is_not_killed()
    test_small_window_seen_counts_as_progress()
    test_never_window_is_none()
    test_process_gone_is_none()
    test_user_stop()
    test_child_process_window_matched()
    test_defaults_and_no_kill_on_failure()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())