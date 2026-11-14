"""2026-11-02 失败复盘修复的回归测试。

覆盖今天改的 5 件事：
  1. ZZZ 解析器假失败：同一任务「先失败后成功」不该再报 failed（连假了 14 天）
  2. 窗口归属校验：通用启动器进程名 + 标题不命中 → 不认这个窗口
  3. 鸣潮窗口未就绪 → 提前止损（静态写法锁已删：原断言锁注解/文案/变量名，
     重构必碎且无行为价值；行为由真机批次验证）
  4. 失败取证：无 hwnd 补全屏截图（静态写法锁已删，同上）
  5. 白荆失败落 flow_failures + 薅羊毛 MINIMAX 自愈函数（只留接线锚点）

跑法：.venv\\Scripts\\python.exe artifacts\\test_failures_20261102.py
"""

import io
import sys
import tempfile
from pathlib import Path

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


def test_zzz_parser_last_result_wins():
    print("[1] ZZZ 解析器：同一任务以最后一次结果为准")
    from game_auto.services.zzz_daily_parser import parse_zzz_log

    log_lines = [
        "[12:12:25.908] [operation.py 702] [ERROR]: 指令[ 等待大世界画面 ] 执行失败 返回状态 未到达大世界",
        "[12:12:27.152] [operation.py 702] [ERROR]: 指令[ 等待大世界画面 ] 执行失败 返回状态 未到达大世界",
        "[12:12:33.643] [operation.py 700] [INFO]: 指令[ 等待大世界画面 ] 执行成功 返回状态 大世界-普通",
        "[12:29:27.437] [operation.py 702] [ERROR]: 指令[ 迷失之地-邦布商店 ] 执行失败 返回状态 异常",
        "[12:29:27.742] [operation.py 702] [ERROR]: 指令[ 一条龙 ] 执行失败 返回状态 异常",
    ]
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "log.txt"
        p.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        data = parse_zzz_log(p)
    tasks = {t["task_name"]: t for t in (data or {}).get("tasks", [])}
    check("重试后成功的任务不再报 failed", "等待大世界画面" not in tasks
          or tasks["等待大世界画面"]["status"] != "failed", tasks.get("等待大世界画面"))
    check("真失败的任务仍然报 failed",
          tasks.get("迷失之地-邦布商店", {}).get("status") == "failed",
          tasks.get("迷失之地-邦布商店"))

    # 只有失败、没有成功时仍要报 failed
    only_fail = [
        "[12:12:25.908] [operation.py 702] [ERROR]: 指令[ 等待大世界画面 ] 执行失败 返回状态 未到达大世界",
        "[12:12:32.535] [operation.py 702] [ERROR]: 指令[ 等待大世界画面 ] 执行失败 返回状态 未到达大世界",
    ]
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "log.txt"
        p.write_text("\n".join(only_fail) + "\n", encoding="utf-8")
        data2 = parse_zzz_log(p)
    tasks2 = {t["task_name"]: t for t in (data2 or {}).get("tasks", [])}
    check("全程失败的任务报 failed",
          tasks2.get("等待大世界画面", {}).get("status") == "failed",
          tasks2.get("等待大世界画面"))


def test_window_ownership():
    print("[2] 窗口归属校验（通用启动器名需标题命中）")
    from unittest.mock import patch

    from game_auto.runtime import foreground_flow_runner as ffr

    def _owned(proc_name, title_matched, keywords=("无限暖暖", "Infinity Nikki")):
        with patch.object(ffr, "_process_name_of", lambda pid: proc_name):
            return ffr._is_window_owned(1234, title_matched, list(keywords))

    check("鸣潮启动器窗口（launcher_main.exe + 标题不符）不被认",
          _owned("launcher_main.exe", False) is False)
    check("标题命中就认", _owned("launcher_main.exe", True) is True)
    check("游戏专属进程名（xstarter.exe）不要求标题",
          _owned("xstarter.exe", False) is True)
    check("游戏本体进程名（Wuthering Waves.exe）不要求标题",
          _owned("wuthering waves.exe", False) is True)
    check("launcher.exe 也是通用名", _owned("launcher.exe", False) is False)
    check("没配 title_keywords 时不做归属限制",
          _owned("launcher_main.exe", False, ()) is True)


def test_baijing_evidence_and_wait():
    print("[5] 白荆：失败落 flow_failures（只留接线锚点）")
    from game_auto.runtime import baijing_flow as bf

    check("落库函数存在", hasattr(bf, "_record_baijing_flow_failure"))
    src = Path(bf.__file__).read_text(encoding="utf-8")
    check("白荆流程失败会调 flow_diagnostics 落库（def 之外有调用点）",
          src.count("_record_baijing_flow_failure(") >= 2)


def test_wool_minimax_diagnose():
    print("[6] 薅羊毛 MINIMAX：自愈/关闭函数存在 + 聚焦/局部坐标调用点（稳定锚点）")
    from game_auto.runtime import wool_claim_logs as wl

    for name in ("_click_minimax_sign_button", "_minimax_opened_card",
                 "dismiss_minimax_update_popup"):
        check(f"{name} 存在", hasattr(wl, name))
    src = Path(wl.__file__).read_text(encoding="utf-8")
    # 这两个是调用点而非本模块定义（助手从别处导入），只锚「确实在调」
    check("点击前先聚焦窗口", "_focus_window(" in src)
    check("点击用窗口内坐标", "_click_window_local(" in src)


def test_minimax_popup_geometry():
    print("[7] 公告弹窗几何常量（2026-11-02 实测）")
    from game_auto.runtime import wool_claim_logs as wl

    # 实测：标题文本框中心 (699,411)，关闭 X 在 (1333,349)，全屏 1936x1048
    dx = 1333 - 699
    dy = 349 - 411
    check("X 偏移水平常量与实测一致（±0.01）",
          abs(wl._UPDATE_POPUP_CLOSE_DX - dx / 1936) < 0.01,
          (wl._UPDATE_POPUP_CLOSE_DX, dx / 1936))
    check("X 偏移垂直常量与实测一致（±0.01）",
          abs(wl._UPDATE_POPUP_CLOSE_DY - dy / 1048) < 0.01,
          (wl._UPDATE_POPUP_CLOSE_DY, dy / 1048))
    check("公告标题识别词正确", wl._UPDATE_POPUP_TITLE == "更新已来")


def test_minimax_dismiss_step_wired():
    print("[8] 关公告步骤已接进 MINIMAX 流程（第一步）")
    import inspect
    import json

    from game_auto.runtime import foreground_flow_runner as ffr

    check("新动作已登记为内建动作", "wool_dismiss_update" in ffr._BUILTIN_ACTIONS)
    # 接线锚点：dispatch 函数里确有该动作的分支（函数级 getsource，
    # 只锁动作 ID 不锁调用格式/分支写法）
    check("dispatch 里有 wool_dismiss_update 分支",
          "wool_dismiss_update" in inspect.getsource(ffr._dispatch_step_action))
    for name in ("flow_configs.json", "flow_configs.example.json"):
        p = PROJECT_ROOT / name
        if not p.is_file():
            check(f"{name} 存在", False, p)
            continue
        cfg = json.loads(p.read_text(encoding="utf-8"))
        steps = (cfg.get("MINIMAX") or {}).get("steps") or []
        first = steps[0] if steps else {}
        check(f"{name}: MINIMAX 第一步是关公告弹窗",
              first.get("action") == "wool_dismiss_update", first)
        check(f"{name}: 关公告步骤是可选（没弹窗不算失败）",
              first.get("optional_if_missing") is True, first)
        check(f"{name}: 末步仍是日志核验",
              (steps[-1] if steps else {}).get("action") == "wool_verify_log", steps[-1] if steps else None)


def main():
    test_zzz_parser_last_result_wins()
    test_window_ownership()
    test_baijing_evidence_and_wait()
    test_wool_minimax_diagnose()
    test_minimax_popup_geometry()
    test_minimax_dismiss_step_wired()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
