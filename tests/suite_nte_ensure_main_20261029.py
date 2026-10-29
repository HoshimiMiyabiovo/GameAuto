# -*- coding: utf-8 -*-
"""异环「进入游戏」兜底回归（2026-10-29）。

事故：游戏被留在标题页（底部「进入游戏」）时，ok-nte 的 LauncherTask
发现进程已存在就跳过启动/进入流程，随后每个日常任务 10 分钟超时，
报 `主界面检测失败`；GameAuto 的 nte_start 又因同一原因跳过。

本套件锁定新兜底 `runtime/nte_launcher.ensure_game_main`：
1. 游戏未运行 → 放行（交给 ok-nte 启动）；
2. 标题页 → 点「进入游戏」并等按钮消失；
3. 按钮一直在 → 诚实失败（不再白跑 45 分钟）；
4. 找不到按钮 → 视为已在主界面/加载中，零点击放行；
5. 停止回调、点击异常、默认配置接线、控制器调用点。

全部 mock，不碰真实窗口/鼠标。
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT.parent))

import game_auto.runtime.foreground_flow_runner as ffr  # noqa: E402
from game_auto.runtime import nte_launcher as nl  # noqa: E402
from game_auto.settings.defaults import DEFAULT_APPS  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


class Env:
    def __init__(self):
        self.clicks = []
        self.entries = []

    def install(self):
        self._orig = (nl.find_game_window, nl.locate_entry, ffr._focus_window, ffr._click_window_local)
        nl.find_game_window = lambda: self.hwnd
        nl.locate_entry = self.locate
        ffr._focus_window = lambda hwnd: None
        ffr._click_window_local = self.click

    def restore(self):
        nl.find_game_window, nl.locate_entry, ffr._focus_window, ffr._click_window_local = self._orig

    def locate(self, logger, hwnd):
        if self.entries:
            return self.entries.pop(0), "template"
        return None, ""

    def click(self, hwnd, x, y, clicks=1, interval=0.2, background=False):
        self.clicks.append((x, y))


def run_case(name, hwnd, entries, *, stop=False, raise_click=False, timeout=0.4, wait=0.15):
    env = Env()
    env.hwnd = hwnd
    env.entries = list(entries)
    env.install()
    if raise_click:
        def boom(*a, **k):
            raise RuntimeError("模拟点击失败")
        ffr._click_window_local = boom
    try:
        ok, msg = nl.ensure_game_main(
            __import__("logging").getLogger("test"),
            stop_check=(lambda: True) if stop else None,
            timeout_sec=timeout,
            click_wait_sec=wait,
        )
    finally:
        env.restore()
    return ok, msg, env


def main():
    print("[1] 游戏未运行 → 放行")
    ok, msg, env = run_case("no-win", 0, [])
    check("返回 True", ok is True)
    check("说明交给 ok-nte", "交给 ok-nte" in msg, msg)
    check("零点击", not env.clicks)

    print("[2] 标题页 → 点击并等按钮消失")
    ok, msg, env = run_case("entry", 123, [(960, 970)])
    check("返回 True", ok is True, msg)
    check("点击一次", len(env.clicks) == 1, env.clicks)
    check("点击坐标正确", env.clicks and env.clicks[0] == (960, 970), env.clicks)

    print("[3] 按钮一直不消失 → 诚实失败")
    ok, msg, env = run_case("stuck", 123, [(960, 970)] * 50)
    check("返回 False", ok is False, msg)
    check("说明停留在进入游戏界面", "停留在「进入游戏」界面" in msg, msg)
    check("至少点过一次", len(env.clicks) >= 1)

    print("[4] 找不到按钮 → 视为已在主界面，零点击")
    ok, msg, env = run_case("main", 123, [])
    check("返回 True", ok is True)
    check("零点击", not env.clicks)
    check("说明未检测到按钮", "未检测到" in msg, msg)

    print("[5] 停止回调 / 点击异常")
    ok, msg, env = run_case("stop", 123, [(960, 970)], stop=True)
    check("停止时返回 False", ok is False and "停止" in msg, msg)
    ok, msg, env = run_case("boom", 123, [(960, 970)], raise_click=True)
    check("点击异常返回 False", ok is False and "点击" in msg, msg)

    print("[6] 配置与接线")
    check("默认配置启用 nte 兜底",
          (DEFAULT_APPS.get("NevernessToEverness") or {}).get("post_launch_ensure_main") == "nte")
    controller_src = (PROJECT_ROOT / "runtime" / "automation_controller.py").read_text(encoding="utf-8")
    check("控制器调用 ensure_game_main", "ensure_game_main" in controller_src)
    check("控制器读 post_launch_ensure_main", "post_launch_ensure_main" in controller_src)
    check("进入游戏模板存在", Path(nl.ENTRY_TEMPLATE).is_file(), nl.ENTRY_TEMPLATE)
    check("游戏进程名为 HTGame.exe", nl.GAME_PROCESS == "HTGame.exe")

    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
