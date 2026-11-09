# -*- coding: utf-8 -*-
"""ZZZ 冷启动加载容忍 回归（2026-11-09 晚误判实锤）。

背景：11-09 用户手动跑绝区零失败，20:03:23 取证帧是启动 23s 的 ZZ logo 页
（ref_zzz_logo_loading_20261109.jpg：zenLess zone zero / 绝·区·零），场景登记表
当 unknown 连续 2 轮速败 → 月/周商店全灭。修法是登记表加「logo/加载过场只等待
不点击」规则 + 按游戏进程龄的冷启动容忍窗。

同晚暖暖失败帧一度被误读成「主世界缺聊天气泡」并加过 HUD 热键签名通道；
用户纠正该帧实为**副本独立地图**（右上 ESC 退出图标，顶栏菜单行副本也有），
签名会把副本判真、抢在并行任务 8c4dd26 的副本 ESC 兜底前返回 ok —— 已撤回，
本套件 [4] 静态锁住撤回不被 reintroduce。

覆盖：
  [1] logo→登录→大世界：加载过场只等待不盲点
  [2] logo 卡死：90s 上限落取证失败，零点击
  [3] 启动容忍窗：冷启动 unknown 等待不速败；热启动 unknown 仍 2 轮速败
  [4] 接线静态：加载规则在 unknown 计数前、容忍窗按进程龄、暖暖签名通道已撤回

跑法：.venv\\Scripts\\python.exe -m pytest tests/suite_zzz_enter_loading_20261109.py -q
"""

import io
import sys
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from game_auto.runtime import flow_diagnostics as fd  # noqa: E402
from game_auto.runtime import foreground_flow_runner as ffr  # noqa: E402
from game_auto.runtime import zzz_scene as zs  # noqa: E402

FAILURES = []
LOG = __import__("logging").getLogger("zzz_loading_test")
LOG.addHandler(__import__("logging").NullHandler())


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}", flush=True)
    else:
        print(f"  FAIL {name} — {detail}", flush=True)
        FAILURES.append(name)


def _rows(*pairs):
    return [
        {"text": t, "confidence": c, "center": (x, y), "box": [x - 40, y - 15, x + 40, y + 15]}
        for t, c, x, y in pairs
    ]


# 11-09 20:03:23 取证帧 OCR 逐行转录（conf 照抄）
ZZZ_LOGO_ROWS = _rows(("Z", 0.92, 963, 514), ("绝·区·零", 0.99, 959, 727),
                      ("zenLess zone zero", 0.93, 958, 776))
ZZZ_LOGIN_ROWS = _rows(("点击进入游戏", 0.95, 960, 886))
ZZZ_WORLD_ROWS = _rows(("星期四", 0.95, 1800, 80))
ZZZ_UNKNOWN_ROWS = _rows(("诡屋惊魂记活动开启", 0.95, 960, 300))


class FakeTime:
    def __init__(self):
        self.t = 1000.0

    def time(self):
        return self.t

    def sleep(self, sec):
        self.t += sec


class ZzzMachine:
    def __init__(self, start, logo_to_login_after=0, unknown_to_world_after=0):
        self.state = start
        self.clicks = []
        self.escapes = []
        self.ocr_calls = 0
        self.logo_to_login_after = logo_to_login_after
        self.unknown_to_world_after = unknown_to_world_after

    def rows(self, logger, hwnd=None, **kwargs):
        self.ocr_calls += 1
        if self.state == "logo" and self.logo_to_login_after and self.ocr_calls >= self.logo_to_login_after:
            self.state = "login"
        if self.state == "unknown" and self.unknown_to_world_after and self.ocr_calls >= self.unknown_to_world_after:
            self.state = "world"
        return {"logo": ZZZ_LOGO_ROWS, "login": ZZZ_LOGIN_ROWS,
                "world": ZZZ_WORLD_ROWS, "unknown": ZZZ_UNKNOWN_ROWS}[self.state]

    def template(self, hwnd, template_path, scales, confidences, use_color=False):
        if "主界面-电台" in template_path:
            return (960, 540) if self.state == "world" else None
        return None

    def click(self, x, y):
        self.clicks.append((int(x), int(y)))
        if self.state == "login":
            self.state = "world"

    def escape(self, key, **kwargs):
        self.escapes.append(key)


def _zzz_harness(machine, age=None, clock=None):
    failures = []
    patches = [
        mock.patch.object(zs, "ocr_window_content", side_effect=machine.rows),
        mock.patch.object(zs, "_process_age_sec", return_value=age),
        mock.patch.object(ffr, "_locate_template_in_window", side_effect=machine.template),
        mock.patch.object(ffr, "_pyautogui_click_guarded", side_effect=machine.click),
        mock.patch.object(ffr, "press_key_scancode", side_effect=machine.escape),
        mock.patch.object(fd, "record_failure",
                          side_effect=lambda logger, fk, step, err, **k: failures.append((fk, step, err))),
    ]
    if clock is None:
        patches.append(mock.patch.object(zs.time, "sleep", lambda *_: None))
    else:
        patches.append(mock.patch.object(zs, "time", clock))
    for p in patches:
        p.start()
    try:
        ok, msg = zs.ensure_main_world(LOG, 1, scales=[1.0], confidences=[0.88],
                                       flow_key="ZZZ_TEST", context="测试校正")
    finally:
        for p in reversed(patches):
            p.stop()
    return ok, msg, failures


def test_zzz_loading_chain():
    print("[1] logo→登录→大世界：加载过场只等待不盲点")
    machine = ZzzMachine("logo", logo_to_login_after=3)
    ok, msg, failures = _zzz_harness(machine)
    check("冷启动链到达大世界", ok is True, msg)
    check("只点了登录按钮", machine.clicks == [(960, 886)], machine.clicks)
    check("logo 轮零按键零盲点", machine.escapes == [], machine.escapes)
    check("无失败取证", failures == [], failures)


def test_zzz_loading_stuck_caps():
    print("[2] logo 卡死：90s 上限落取证，零点击")
    machine = ZzzMachine("logo")
    ok, msg, failures = _zzz_harness(machine, clock=FakeTime())
    check("卡死判失败", ok is False, msg)
    check("失败说明是加载过场超上限", "加载过场" in msg and "90" in msg, msg)
    check("落了取证", len(failures) == 1 and failures[0][0] == "ZZZ_TEST", failures)
    check("零点击零按键", machine.clicks == [] and machine.escapes == [],
          (machine.clicks, machine.escapes))


def test_zzz_startup_grace():
    print("[3] 启动容忍窗：冷启动等待 / 热启动速败保留")
    machine = ZzzMachine("unknown", unknown_to_world_after=4)
    ok, msg, failures = _zzz_harness(machine, age=10.0, clock=FakeTime())
    check("冷启动黑屏等到主世界", ok is True, msg)
    check("容忍窗内不落失败取证", failures == [], failures)
    check("容忍窗内零盲点", machine.clicks == [] and machine.escapes == [], machine.clicks)
    machine = ZzzMachine("unknown")
    ok, msg, failures = _zzz_harness(machine, age=99999.0, clock=FakeTime())
    check("热启动 unknown 仍速败", ok is False and "未知场景" in msg, msg)
    check("热启动速败落取证", len(failures) == 1, failures)


def test_wiring_static():
    print("[4] 接线静态")
    zs_src = (PROJECT_ROOT / "src" / "game_auto" / "runtime" / "zzz_scene.py").read_text(encoding="utf-8")
    nl_src = (PROJECT_ROOT / "src" / "game_auto" / "runtime" / "nikki_launcher.py").read_text(encoding="utf-8")
    check("加载规则在 unknown 计数前",
          zs_src.index("_first_hit(results, LOADING_MARKERS)") < zs_src.index("unknown_streak += 1"))
    check("容忍窗按游戏进程龄", "_process_age_sec(hwnd)" in zs_src and "_STARTUP_GRACE_SEC" in zs_src)
    check("加载词有取证出处", "zenlesszonezero" in zs.LOADING_MARKERS and "绝·区·零" in zs.LOADING_MARKERS)
    check("暖暖 HUD 签名通道已撤回（副本会误判真）",
          "_hud_signature" not in nl_src and "MAIN_HUD_HOTKEYS" not in nl_src)
    check("暖暖副本 ESC 兜底仍在（并行 8c4dd26 重构为 Esc→确认返回链路）",
          "esc_region" in nl_src and "INSTANCE_CONFIRM_TEXTS" in nl_src and "_press_esc()" in nl_src)


def main():
    test_zzz_loading_chain()
    test_zzz_loading_stuck_caps()
    test_zzz_startup_grace()
    test_wiring_static()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())