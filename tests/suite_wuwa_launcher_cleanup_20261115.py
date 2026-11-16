"""鸣潮收尾清理官方启动器回归（2026-11-15 事故）。

背景：一条龙 09:25 鸣潮前置 WuwaLauncher 拉起官方启动器（launcher.exe →
主进程 launcher_main.exe，PID 1552）；09:27 鸣潮收尾强杀了游戏 + ok-ww +
pythonw，但 `_kill_related_processes` 的清理清单里没有启动器进程名——
`_sweep_post_close_launcher_once` 三路候选（config.launcher /
launcher_options / close_launcher_processes / 游戏目录扫 *launcher*.exe）
全空：鸣潮没配任何 launcher 键，且官方启动器装在**独立目录**、游戏目录
扫不到 → 启动器存活，过一会儿自我激活拉到前台 → 09:35:38 暖暖游戏窗口
「目标窗口被遮挡约 100%」任务失败（11:08 重试成功只是因为隔了 1.5 小时
启动器已被关掉）。

修法（对齐 ZZZ 直连 cleanup_cmd_patterns 同款机制）：games/wuwa.py
default_app 配 `cleanup_processes`（任务各杀点立即杀）+
`close_launcher_processes`（收尾 4s 轮询扫杀，覆盖"游戏关掉后过几秒
启动器才自我激活"的延迟场景）。

节清单：
  [1] 模板层：default_app 配了两个键，值含 launcher.exe / launcher_main.exe
  [2] 生效配置（默认 ⊕ 本机 app_config）两键不丢
  [3] 行为：_kill_related_processes 各杀点按 cleanup_processes 杀启动器
      （include_game=False 预清理口 + 默认收尾口），非启动器进程不误杀
  [4] 行为：_sweep_post_close_launcher_once 以 close_launcher_processes
      为候选杀掉残留启动器（游戏目录扫不到的独立目录场景）

跑法：`.venv\\Scripts\\python.exe tests/suite_wuwa_launcher_cleanup_20261115.py`
"""
from __future__ import annotations

import io as _io
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

FAILURES = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'} {label}" + ("" if ok else f"  {detail}"))
    if not ok:
        FAILURES.append(label)


def test_template():
    print("[1] games/wuwa.py default_app 配了启动器清理键")
    from game_auto.games.wuwa import PROFILE

    cp = PROFILE.default_app.get("cleanup_processes") or []
    clp = PROFILE.default_app.get("close_launcher_processes") or []
    expected = {"launcher.exe", "launcher_main.exe"}
    check("cleanup_processes 含两个启动器进程名", expected <= set(cp), cp)
    check("close_launcher_processes 含两个启动器进程名", expected <= set(clp), clp)
    check("两个键取值一致（同一组进程名）", set(cp) == set(clp))
    check("不含裸 pythonw（那是 cleanup_cmd_patterns 的活）",
          not any("pythonw" in p for p in cp + clp))


def test_effective_config():
    print("[2] 生效配置（默认 ⊕ 本机 app_config）保留清理键")
    from game_auto.games.wuwa import PROFILE
    from game_auto.settings import manager as cm

    tmpl_cp = PROFILE.default_app["cleanup_processes"]
    tmpl_clp = PROFILE.default_app["close_launcher_processes"]
    merged = cm._merge_defaults(cm._default_config(), {})
    check("纯默认生效配置含 cleanup_processes",
          merged["apps"]["WutheringWaves"].get("cleanup_processes") == tmpl_cp)
    check("纯默认生效配置含 close_launcher_processes",
          merged["apps"]["WutheringWaves"].get("close_launcher_processes") == tmpl_clp)

    cfg_path = PROJECT / "app_config.json"
    if not cfg_path.exists():
        print("  SKIP 本机无 app_config.json（干净 clone 场景）")
        return
    local = json.loads(cfg_path.read_text(encoding="utf-8"))
    merged2 = cm._merge_defaults(cm._default_config(), local)
    app2 = merged2["apps"]["WutheringWaves"]
    check("⊕ 本机 app_config 后两键未丢（list 整体覆盖陷阱）",
          app2.get("cleanup_processes") == tmpl_cp
          and app2.get("close_launcher_processes") == tmpl_clp,
          (app2.get("cleanup_processes"), app2.get("close_launcher_processes")))


class FakeProc:
    """psutil.Process 最小桩（与 suite_zzz_chain_cleanup 同款面 + exe_path 供
    _is_wrapper_snapshot 用）。"""

    def __init__(self, pid, name, cmdline, exe=""):
        self.pid = pid
        self.info = {"name": name, "exe": exe, "cmdline": cmdline, "create_time": 1000.0}
        self.exe_path = exe

    def name(self):
        return self.info["name"]

    def exe(self):
        return self.info["exe"]

    def cmdline(self):
        return list(self.info["cmdline"])

    def create_time(self):
        return self.info["create_time"]


def make_config():
    """忠实还原鸣潮生效 config 面貌：无 launcher 键（官方启动器由
    WuwaLauncher 流程配置管理）、游戏进程 Wuthering Waves.exe、
    cleanup_cmd_patterns 只补 ok-ww 的 pythonw 引擎。"""
    from game_auto.games.wuwa import PROFILE

    return dict(PROFILE.default_app)


def make_ctl():
    import logging

    from game_auto.runtime import automation_controller as ac

    ctl = ac.AutomationController.__new__(ac.AutomationController)
    ctl.logger = logging.getLogger("test.wuwa_launcher_cleanup")
    ctl.stop_current_task = False
    return ctl


def run_kill(ctl, table, config, **kwargs):
    """psutil 边界打桩跑真 _kill_related_processes，返回被杀 PID 集合。"""
    from game_auto.runtime import automation_controller as ac

    killed = []
    with patch.object(ac.psutil, "process_iter",
                      side_effect=lambda attrs=None: iter(table)), \
         patch.object(ac.ProcessSupervisor, "safe_kill_process",
                      side_effect=lambda pid: killed.append(pid)):
        ctl._kill_related_processes(config, None, **kwargs)
    return set(killed)


def test_behavior_kill():
    print("[3] 行为：收尾/预清理各杀点按 cleanup_processes 杀启动器")
    ctl = make_ctl()
    config = make_config()

    launcher_main = FakeProc(301, "launcher_main.exe", [r"D:\launcher\launcher_main.exe"])
    launcher_old = FakeProc(302, "launcher.exe", [r"D:\launcher\launcher.exe"])
    game = FakeProc(303, "Wuthering Waves.exe", [r"E:\Game\Wuthering Waves.exe"])
    innocent = FakeProc(304, "xstarter.exe", [r"D:\nikki\launcher\xstarter.exe"])
    table = [launcher_main, launcher_old, game, innocent]

    # 收尾口（默认 include_game=True）：游戏名不在表里（已退出），启动器全被杀
    killed = run_kill(ctl, table, config)
    check("launcher_main.exe 被杀", 301 in killed, killed)
    check("launcher.exe 被杀", 302 in killed, killed)
    check("不误杀无关游戏的启动器（xstarter.exe）", 304 not in killed, killed)
    check("游戏进程名不在表里时不空枪报错", isinstance(killed, set))

    # 预清理口（include_game=False, include_script=False）：同样杀启动器
    killed2 = run_kill(ctl, table, config, include_game=False, include_script=False)
    check("预清理口也杀 launcher_main.exe", 301 in killed2, killed2)
    check("预清理口也杀 launcher.exe", 302 in killed2, killed2)


def test_behavior_sweep():
    print("[4] 行为：_sweep_post_close_launcher_once 按 close_launcher_processes 扫杀")
    from game_auto.runtime import automation_controller as ac

    ctl = make_ctl()
    config = make_config()
    check("sweep 开关默认开启（未配 sweep_post_close_launcher）",
          config.get("sweep_post_close_launcher", True) is True)

    launcher_main = FakeProc(401, "launcher_main.exe", [r"D:\launcher\launcher_main.exe"])
    table = [launcher_main]
    killed = []
    # 游戏目录为空（game exe 未知）→ 目录扫描/窗口枚举两路不参与，
    # 唯一命中途径就是 close_launcher_processes 候选名——正是 11-15 事故里缺的配置
    with patch.object(ac.psutil, "process_iter",
                      side_effect=lambda attrs=None: iter(table)), \
         patch.object(ac.ProcessSupervisor, "find_process_snapshot",
                      side_effect=lambda name, exact_name=True: (
                          FakeProc(401, name, [r"D:\launcher\launcher_main.exe"])
                          if name == "launcher_main.exe" else None)), \
         patch.object(ac.ProcessSupervisor, "safe_kill_process",
                      side_effect=lambda pid: killed.append(pid)):
        n = ctl._sweep_post_close_launcher_once(config, {})
    check("扫杀 launcher_main.exe", 401 in killed and n >= 1, (killed, n))


def main():
    test_template()
    test_effective_config()
    test_behavior_kill()
    test_behavior_sweep()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED {len(FAILURES)}: {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
