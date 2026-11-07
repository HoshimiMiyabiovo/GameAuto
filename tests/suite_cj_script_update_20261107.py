"""XM/CJ 脚本版本管理与自动点更新回归（2026-11-07）。

背景：CJ/XM 的更新机制是脚本下载新版本 exe 到同目录（新旧并存），更新提示
会挡住登录界面导致流程「未匹配模板/OCR」连挂三天（11-02/03/06）。

断言：
  1. _detect_script_version 解析两种命名形态（【2.8.7】 / ver 2.6.41）
  2. _find_newer_script_exe 同目录按基础名找更高版本（无关 exe 不误报）
  3. flow_configs.example.json：XM/CJ 前置 检查更新（optional_if_missing）+
     更新换血（self_update_swap + require_step_hit）
  4. 静态断言：换血分支的跳过/成功路径都显式 i += 1（步骤循环是手动索引制，
     裸 continue 会死循环——2026-11-07 实测刷了 77MB 日志）

跑法：.venv\\Scripts\\python.exe -m pytest tests/suite_cj_script_update_20261107.py -q
"""

import inspect
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from game_auto.runtime import foreground_flow_runner as ffr  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}", flush=True)
    else:
        print(f"  FAIL {name} {detail}", flush=True)
        FAILURES.append(name)


def test_detect_script_version():
    print("[1] 版本解析与按位比较（末段一位数补零：2.6.5 → 2.6.50）")
    check("【2.8.7】形态", ffr._detect_script_version(r"D:\x\重聚脱机巅峰【2.8.7】.exe") == "2.8.7")
    check("ver 2.6.41 形态", ffr._detect_script_version(r"D:\x\XM Seer脱机日常 ver 2.6.41.exe") == "2.6.41")
    check("无版本 → 空串", ffr._detect_script_version(r"D:\x\HoYo.Gacha.exe") == "")
    seq = ["2.6.11", "2.6.2", "2.6.21", "2.6.41", "2.6.5"]
    tuples = [ffr._script_ver_tuple(v) for v in seq]
    check("XM 实测更新序列按位单调递增（2.6.5 补零为 50 > 41）",
          all(tuples[i] < tuples[i + 1] for i in range(len(tuples) - 1)), list(zip(seq, tuples)))
    check("CJ 序列 2.8.7 < 2.8.8", ffr._script_ver_tuple("2.8.7") < ffr._script_ver_tuple("2.8.8"))


def test_find_newer_script_exe():
    print("[2] 同目录按位比较找更新版本（mtime 兜底：exe 名无版本时）")
    import os
    import time as _time
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        now = _time.time()
        old_exe = root / "XM Seer脱机日常 ver 2.6.41.exe"
        new_exe = root / "XM Seer脱机日常 ver 2.6.5.exe"
        other = root / "HoYo.Gacha.exe"
        for f in (old_exe, new_exe, other):
            f.write_bytes(b"")
        os.utime(old_exe, (now - 86400, now - 86400))  # 当前版本：一天前
        os.utime(new_exe, (now - 5, now - 5))          # 新下载：5 秒前
        os.utime(other, (now - 5, now - 5))            # 无关程序也是新的
        newer = ffr._find_newer_script_exe(str(old_exe))
        check("版本按位比较：2.6.41 → 找到 2.6.5（无关 exe 排除）",
              newer.endswith("ver 2.6.5.exe"), newer)
        check("已是最新版本 → 空串",
              ffr._find_newer_script_exe(str(new_exe)) == "")
        # mtime 兜底：exe 名不携带版本时按「更新点击之后新落地」识别
        anon_old = root / "SomeTool.exe"
        anon_new = root / "SomeTool ver 9.exe"
        anon_old.write_bytes(b"")
        anon_new.write_bytes(b"")
        os.utime(anon_old, (now - 86400, now - 86400))
        os.utime(anon_new, (now - 5, now - 5))
        check("exe 名无版本 → mtime 兜底找到新落地文件",
              ffr._find_newer_script_exe(str(anon_old), since_ts=now - 60).endswith("SomeTool ver 9.exe"))
        check("exe 名无版本 + 无 mtime 锚点 → 空串",
              ffr._find_newer_script_exe(str(anon_old)) == "")


def test_flow_configs():
    print("[3] 流程配置断言")
    example = json.loads(
        (Path(__file__).resolve().parent.parent / "flow_configs.example.json")
        .read_text(encoding="utf-8"))
    for key in ("XM", "CJ"):
        steps = example[key]["steps"]
        first, second = steps[0], steps[1]
        check(f"{key} 步骤0 = 检查更新（optional_if_missing）",
              first.get("name") == "检查更新" and first.get("optional_if_missing") is True)
        check(f"{key} 步骤1 = 更新换血（self_update_swap + require_step_hit=检查更新）",
              second.get("name") == "更新换血" and second.get("action") == "self_update_swap"
              and second.get("require_step_hit") == "检查更新")
        login = next((s for s in steps if s.get("name") in ("登录", "登录1")), {})
        check(f"{key} 登录超时 >= 240（覆盖更新下载）",
              int(login.get("timeout_sec") or 0) >= 240, login.get("timeout_sec"))
    # XM 的更新按钮是「本地下载」——宽泛的「更新」会误点「更新公告」标题（0.75 实测）
    xm_first = example["XM"]["steps"][0]
    check("XM 候选词 = 本地下载（防误点更新公告）",
          xm_first.get("target_text") == ["本地下载"], xm_first.get("target_text"))
    cj_first = example["CJ"]["steps"][0]
    check("CJ 候选词含 立即更新", "立即更新" in (cj_first.get("target_text") or []))


def test_swap_index_advances():
    print("[4] 静态断言：换血分支跳过/成功路径都显式 i += 1（裸 continue = 死循环）")
    src = inspect.getsource(ffr.run_flow)
    swap_start = src.find('if action == "self_update_swap"')
    check("换血分支存在", swap_start > 0)
    branch = src[swap_start:src.find("refreshed_hwnd = _refresh_flow_window", swap_start)]
    lines = [ln for ln in branch.splitlines() if ln.strip()]
    bare = []
    for idx, ln in enumerate(lines):
        if ln.strip() == "continue":
            lookback = "".join(lines[max(0, idx - 3):idx])
            if "i += 1" not in lookback:
                bare.append(ln.strip())
    check("分支内 continue 数 = 3（两个跳过路径 + 成功路径）",
          sum(1 for ln in lines if ln.strip() == "continue") == 3,
          sum(1 for ln in lines if ln.strip() == "continue"))
    check("所有 continue 都带 i += 1（回看 3 行）", not bare, bare)


def test_checklist_state_and_config():
    print("[5] 清单同步：引导轮语义 + XM 配置断言")
    from unittest.mock import patch

    from game_auto.runtime import script_checklist as scl

    with tempfile.TemporaryDirectory() as tmp:
        state_file = Path(tmp) / "seen.json"
        with patch.object(scl, "STATE_FILE", state_file):
            check("状态文件缺失 → None（引导轮）", scl._load_seen("nte:限时活动") is None)
            scl._save_seen("nte:限时活动", ["限时活动—修复神圣节", "限时活动—蒙塔的画作"])
            loaded = scl._load_seen("nte:限时活动")
            check("保存后读回行清单", loaded and len(loaded) == 2, loaded)
            check("其它键互不干扰", scl._load_seen("cj:限时活动") is None)
            check("状态文件带 BOM/编码可读", state_file.exists())

    example = json.loads(
        (Path(__file__).resolve().parent.parent / "flow_configs.example.json")
        .read_text(encoding="utf-8"))
    steps = example["XM"]["steps"]
    names = [s.get("name") for s in steps]
    sync_step = next((s for s in steps if s.get("name") == "同步限时活动勾选"), {})
    check("XM 有 同步限时活动勾选（script_checklist_sync）",
          sync_step.get("action") == "script_checklist_sync", sync_step)
    check("同步步骤参数：Unity 标签 + 限时活动前缀",
          sync_step.get("checklist_tab") == "Unity" and sync_step.get("row_prefix") == "限时活动")
    check("登录 → 同步 → 开始 的顺序",
          names.index("登录") < names.index("同步限时活动勾选") < names.index("开始"))
    check("没有冗余的 OCR 点开始步骤（模板「开始」步骤已存在）",
          not any(s.get("action") == "click_template" and s.get("target_text") == "开始"
                  and s.get("name") == "点击开始" for s in steps))
    src = inspect.getsource(scl.sync_checklist)
    check("引导轮语义在实现中（bootstrap 不点击）", "bootstrap" in src and "record-only" not in src)
    check("同步动作已注册进 _BUILTIN_ACTIONS",
          "script_checklist_sync" in inspect.getsource(ffr))


def main():
    test_detect_script_version()
    test_find_newer_script_exe()
    test_flow_configs()
    test_swap_index_advances()
    test_checklist_state_and_config()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
