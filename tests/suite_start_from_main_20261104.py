"""2026-11-04 统一入口：所有流程优先从主世界/主界面开始。

用户要求（参照白荆那套）：流程不能假设自己从启动器/登录页/某个子页面开始，
先在主界面就直接跳过进入步骤，游戏进程已在运行就不再走启动器。

覆盖：
  - run_flow 的 start_from_main / skip_steps_if_process 守卫
  - ZZZ_MONTHLY 已在主世界 → 跳过「点击进入游戏」（电台模板）
  - NTE_MONTHLY 游戏已在运行 → 跳过启动器 1-4 步，「进入游戏」可选

跑法：.venv\\Scripts\\python.exe tests/suite_start_from_main_20261104.py
"""

import io
import json
import logging
import sys
from pathlib import Path
from unittest import mock

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


def _logger():
    logger = logging.getLogger("test_start_guard")
    logger.setLevel(logging.CRITICAL)
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


class FakeProc:
    def __init__(self, name):
        self.info = {"name": name}


def test_guard_logic():
    print("[1] 守卫逻辑")
    from game_auto.runtime import foreground_flow_runner as ffr

    cfg = {
        "start_from_main": {
            "template": "assets/templates\\绝区零-主界面-电台.png",
            "skip_to_step": 3,
        },
        "skip_steps_if_process": {"process_name": "HTGame.exe", "steps": [1, 2, 3, 4]},
    }
    with mock.patch.object(ffr, "_window_rect", lambda hwnd: (0, 0, 1920, 1080)), \
         mock.patch.object(ffr, "_locate_template_on_screen", lambda *a, **k: (100, 100)), \
         mock.patch.object(ffr.psutil, "process_iter", lambda attrs=None: []):
        start, skips = ffr._resolve_start_guard(cfg, "ZZZ_MONTHLY", 1, [1.0], [0.8], 7, _logger())
    check("主界面命中 → 从第 3 步开始（索引 2）", start == 2, start)
    check("进程不在 → 不跳步骤", skips == set(), skips)

    with mock.patch.object(ffr, "_window_rect", lambda hwnd: (0, 0, 1920, 1080)), \
         mock.patch.object(ffr, "_locate_template_on_screen", lambda *a, **k: None), \
         mock.patch.object(ffr.psutil, "process_iter", lambda attrs=None: [FakeProc("HTGame.exe")]):
        start, skips = ffr._resolve_start_guard(cfg, "NTE_MONTHLY", 1, [1.0], [0.8], 7, _logger())
    check("主界面未命中 → 从第 1 步开始", start == 0, start)
    check("游戏进程已在 → 跳过 1-4 步", skips == {0, 1, 2, 3}, skips)

    with mock.patch.object(ffr, "_window_rect", lambda hwnd: None), \
         mock.patch.object(ffr, "_locate_template_on_screen", lambda *a, **k: (1, 1)), \
         mock.patch.object(ffr.psutil, "process_iter", lambda attrs=None: [FakeProc("other.exe")]):
        start, skips = ffr._resolve_start_guard(cfg, "X", 1, [1.0], [0.8], 7, _logger())
    check("窗口无效不探测主界面", start == 0, start)
    check("进程不匹配不跳步骤", skips == set(), skips)


def test_flow_configs():
    print("[2] 流程配置")
    tpl = PROJECT_ROOT / "assets/templates/绝区零-主界面-电台.png"
    check("绝区零主界面模板存在", tpl.is_file(), tpl)

    for name in ("flow_configs.json", "flow_configs.example.json"):
        path = PROJECT_ROOT / name
        d = json.loads(path.read_text(encoding="utf-8"))
        zzz = d["ZZZ_MONTHLY"]
        guard = zzz.get("start_from_main") or {}
        check(f"{name}: ZZZ 配了 start_from_main",
              guard.get("skip_to_step") == 3 and "电台" in str(guard.get("template")), guard)
        nte = d["NTE_MONTHLY"]
        skip = nte.get("skip_steps_if_process") or {}
        check(f"{name}: NTE 配了 skip_steps_if_process",
              skip.get("process_name") == "HTGame.exe" and skip.get("steps") == [1, 2, 3, 4], skip)
        step6 = next((s for s in nte.get("steps") or [] if s.get("name") == "进入游戏"), {})
        check(f"{name}: 异环「进入游戏」可选（已在主世界时跳过）",
              step6.get("optional_if_missing") is True, step6.get("optional_if_missing"))


def test_flow_wiring():
    print("[3] run_flow 接线")
    src = (PROJECT_ROOT / "src/game_auto/runtime/foreground_flow_runner.py").read_text(
        encoding="utf-8"
    )
    check("run_flow 调用 _resolve_start_guard",
          "start_index, skip_step_indices = _resolve_start_guard(" in src)
    check("循环从 start_index 开始", "i = start_index" in src)
    check("循环内跳过启动器步骤",
          "if i in skip_step_indices:" in src and "因游戏进程已在运行跳过" in src)


def main():
    test_guard_logic()
    test_flow_configs()
    test_flow_wiring()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())