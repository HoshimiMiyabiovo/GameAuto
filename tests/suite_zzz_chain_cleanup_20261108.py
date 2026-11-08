# -*- coding: utf-8 -*-
"""绝区零直连链路「杀得到」回归（2026-11-08）。

背景：OneDragon-Launcher.exe 只是引导器（拉起安装目录自带 .venv 的
pythonw.exe 跑一条龙链路后 ~50 秒自退），任务收尾按 PID / 按快照名
都只能杀到引导器。2026-11-08 09:21 实证：用户退出 GUI 后链路"复活"
自己跑完整条龙，把游戏留在功能菜单（当天 #219/#220 事故链根源之一）。

修法：games/zzz.py default_app 配 cleanup_cmd_patterns（与异环 ok-nte /
鸣潮 ok-ww / 终末地 ok-ef 同款机制）。automation_controller 的按命令行
补刀通道在 5 个杀点都会消费它：启动前清理 / 用户停止 / 监控超时 /
游戏退出收尾 / 脚本报错止损（该循环不受 include_script 门控）。
pythonw.exe 在 WRAPPER_PROCESS_NAMES 里禁止裸名杀，特征串取安装目录名
ZenlessZoneZero-OneDragon（解释器绝对路径必含它，其他 Python 进程没有）。

覆盖：
1. 模板层：default_app 配了 cleanup_cmd_patterns，特征串非裸名且可作正则；
2. 生效层：默认配置 ⊕ 本机 app_config.json 后仍然存在（防 _merge_defaults
   的 list 整体覆盖吃掉模板值——launcher_options 已踩过同款坑）；
3. 行为层：_kill_related_processes 真的杀 命中特征串的 pythonw；不杀
   无特征串的 pythonw（裸名防护不被绕过）；跳过自身 PID；
   include_script=False（启动前清理的调用口）同样生效。

跑法：.venv\\Scripts\\python.exe tests\\suite_zzz_chain_cleanup_20261108.py
"""

import io
import json
import os
import re
import sys
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

FAILURES = []


def check(label, ok, detail=None):
    mark = "PASS" if ok else "FAIL"
    print(f"  {mark} {label}" + (f"  ← {detail}" if not ok and detail is not None else ""))
    if not ok:
        FAILURES.append(label)


def test_template_has_pattern():
    print("[1] games/zzz.py default_app 配了 cleanup_cmd_patterns")
    from game_auto.games.zzz import PROFILE

    pats = PROFILE.default_app.get("cleanup_cmd_patterns") or []
    check("cleanup_cmd_patterns 恰好一条", len(pats) == 1, pats)
    if not pats:
        return
    p = pats[0]
    check("name=pythonw.exe", p.get("name") == "pythonw.exe", p.get("name"))
    cmd = str(p.get("cmdline") or "")
    check("特征串含安装目录名（非裸名）", "ZenlessZoneZero-OneDragon" in cmd, cmd)
    try:
        re.compile(cmd)
        check("cmdline 可作正则编译", True)
    except re.error as exc:
        check("cmdline 可作正则编译", False, str(exc))


def test_effective_config_keeps_pattern():
    print("[2] 生效配置（默认 ⊕ 本机 app_config）保留 cleanup_cmd_patterns")
    from game_auto.games.zzz import PROFILE
    from game_auto.settings import manager as cm

    tmpl = PROFILE.default_app["cleanup_cmd_patterns"]
    merged = cm._merge_defaults(cm._default_config(), {})
    check("纯默认生效配置含 pattern",
          merged["apps"]["ZenlessZoneZero"].get("cleanup_cmd_patterns") == tmpl)

    cfg_path = PROJECT_ROOT / "app_config.json"
    if not cfg_path.exists():
        print("  SKIP 本机无 app_config.json（干净 clone 场景）")
        return
    local = json.loads(cfg_path.read_text(encoding="utf-8"))
    merged2 = cm._merge_defaults(cm._default_config(), local)
    check("⊕ 本机 app_config 后 pattern 未丢（list 整体覆盖陷阱）",
          merged2["apps"]["ZenlessZoneZero"].get("cleanup_cmd_patterns") == tmpl,
          merged2["apps"]["ZenlessZoneZero"].get("cleanup_cmd_patterns"))


ONEDRAGON_PY = r"D:\down\ZZZ\ZenlessZoneZero-OneDragon\.venv\scripts\pythonw.exe"


class FakeProc:
    """psutil.Process 最小桩：只提供 find_processes / snapshot_process 用到的面。"""

    def __init__(self, pid, name, cmdline, exe=""):
        self.pid = pid
        self.info = {
            "name": name,
            "exe": exe,
            "cmdline": cmdline,
            "create_time": 1000.0,
        }

    def name(self):
        return self.info["name"]

    def exe(self):
        return self.info["exe"]

    def cmdline(self):
        return list(self.info["cmdline"])

    def create_time(self):
        return self.info["create_time"]


def make_config():
    """忠实还原 ZZZ 直连的生效 config 面貌：launcher 字段是游戏 exe（历史包袱），
    顶层无 script_process_name（快照名盖掉一切按名路径）——即"引导器空枪"现状。"""
    return {
        "type": "script",
        "launcher": r"E:\Program Files\Game\ZenlessZoneZero Game\ZenlessZoneZero.exe",
        "game": {"process_name": "ZenlessZoneZero.exe"},
        "cleanup_cmd_patterns": [
            {"name": "pythonw.exe", "cmdline": "ZenlessZoneZero-OneDragon"},
        ],
    }


def make_ctl():
    import logging

    from game_auto.runtime import automation_controller as ac

    ctl = ac.AutomationController.__new__(ac.AutomationController)
    ctl.logger = logging.getLogger("test.zzz_chain_cleanup")
    ctl.stop_current_task = False
    return ctl


def run_kill(ctl, table, config, **kwargs):
    """在 psutil 边界打桩跑真 _kill_related_processes，返回被杀 PID 集合。

    只 patch psutil.process_iter（喂假进程表）与 safe_kill_process（记名），
    find_processes 的名字等值 + cmdline 正则匹配走真实实现。
    """
    from game_auto.runtime import automation_controller as ac

    killed = []
    with patch.object(ac.psutil, "process_iter",
                      side_effect=lambda attrs=None: iter(table)), \
         patch.object(ac.ProcessSupervisor, "safe_kill_process",
                      side_effect=lambda pid: killed.append(pid)):
        ctl._kill_related_processes(config, None, **kwargs)
    return set(killed)


def test_behavior_kills_chain_pythonw():
    print("[3] 行为：命中特征串的 pythonw 被杀，无特征串的不杀，自身 PID 跳过")
    ctl = make_ctl()
    config = make_config()

    chain = FakeProc(101, "pythonw.exe", [ONEDRAGON_PY, "zzz_od\\application\\zzz_one_dragon_app.py"])
    innocent = FakeProc(102, "pythonw.exe", [r"C:\other\pythonw.exe", "-m", "unrelated"])
    own = FakeProc(os.getpid(), "pythonw.exe", [ONEDRAGON_PY, "self"])
    table = [chain, innocent, own]

    killed = run_kill(ctl, table, config)
    check("链路本体 pythonw（cmdline 含 OneDragon 目录）被杀", 101 in killed, killed)
    check("无特征串的 pythonw 不被裸名误杀", 102 not in killed, killed)
    check("自身 PID 永不自杀", os.getpid() not in killed, killed)


def test_behavior_preclean_path_also_kills():
    print("[4] 行为：include_script=False（启动前清理口）同样补刀链路")
    ctl = make_ctl()
    config = make_config()

    chain = FakeProc(201, "pythonw.exe", [ONEDRAGON_PY, "zzz_od"])
    table = [chain]

    killed = run_kill(ctl, table, config, include_game=False, include_script=False)
    check("启动前清理也杀得到残留链路", 201 in killed, killed)


def main():
    test_template_has_pattern()
    test_effective_config_keeps_pattern()
    test_behavior_kills_chain_pythonw()
    test_behavior_preclean_path_also_kills()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED {len(FAILURES)}: {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
