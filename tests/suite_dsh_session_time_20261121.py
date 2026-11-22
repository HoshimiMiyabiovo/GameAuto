# -*- coding: utf-8 -*-
"""DSH 会话时间单位回归（2026-11-21）。

背景：外部分析工具 `F:\\data\\_ai_organize_tools\\build.py` 的 `add()` 只认 epoch 秒，
而 DSH 抓取导出的 `k/sk` 是 epoch **毫秒**。`do_dsh` 原样转发导致：
  - 总览页全部 DSH 卡片显示“时间未知”；
  - `sk` 大三个数量级，DSH 会话被排序到最前面；
  - 消息时间与 RAG 块 `[时间]` 头同样失效。
本套件锁 `do_dsh` 的毫秒→秒归一化，以及 RAG 切块能拿到正常时间头。

build.py 在本机 F:（不在仓库），缺失时 SKIP。
跑法：.venv\\Scripts\\python.exe tests\\suite_dsh_session_time_20261121.py
"""

import json
import os
import sys
import tempfile
import types
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

# 沙箱兼容：tempfile 目录创建改默认权限（0700 受保护 DACL 在沙箱里写不进）
from sandbox_temp import install as _install_sandbox_temp

_install_sandbox_temp()
sys.path.insert(0, str(PROJECT_ROOT / "tools"))

import ai_history_rag as rag  # noqa: E402

FAILURES = []


def check(label, ok, detail=None):
    mark = "PASS" if ok else "FAIL"
    print(f"  {mark} {label}" + (f"  ← {detail}" if not ok and detail is not None else ""))
    if not ok:
        FAILURES.append(label)


def test_do_dsh_time(work_dir: Path):
    print("[1] build.py do_dsh 毫秒→秒")
    build_py = Path(os.environ.get("AI_ORGANIZE_TOOLS") or r"F:\data\_ai_organize_tools") / "build.py"
    if not build_py.exists():
        print(f"  SKIP 本机工具链不存在：{build_py}")
        return
    work_dir.mkdir(parents=True, exist_ok=True)

    # 直接 exec 源码，避免 import 机制往被拦截的 F: 写 __pycache__。
    build = types.ModuleType("ai_organize_build_dsh_time")
    build.__file__ = str(build_py)
    exec(compile(build_py.read_text(encoding="utf-8"), str(build_py), "exec"), build.__dict__)

    ms = 1795259722366  # 2026-11-21 19:15 本地时间，epoch 毫秒
    data = [{
        "id": "dsh:test-session",
        "src": "DSH",
        "title": "时间单位测试",
        "ws": "game_auto",
        "s": "2026-11-21 19:15",
        "e": "2026-11-21 20:50",
        "sk": ms,
        "n": 2,
        "chars": 4,
        "note": "",
        "msgs": [
            {"r": "user", "t": "2026-11-21 19:15", "k": ms, "x": "问题"},
            {"r": "ai", "t": "2026-11-21 20:50", "k": ms + 5000, "x": "回答"},
        ],
    }]
    dsh_json = work_dir / "dsh.json"
    dsh_json.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    old_json = build.DSH_JSON
    try:
        build.DSH_JSON = str(dsh_json)
        build.SESSIONS.clear()
        build.do_dsh()
    finally:
        build.DSH_JSON = old_json

    check("do_dsh 产出一个会话", len(build.SESSIONS) == 1, len(build.SESSIONS))
    if not build.SESSIONS:
        return
    sess = build.SESSIONS[0]
    check("会话开始时间已解析", sess["s"].startswith("2026-11-21"), sess["s"])
    check("会话结束时间已解析", sess["e"].startswith("2026-11-21"), sess["e"])
    check("排序键已归一到秒", 1_700_000_000 < sess["sk"] < 2_000_000_000, sess["sk"])
    check("消息时间非空",
          all(str(m.get("t") or "").startswith("2026-11-21") for m in sess["msgs"]),
          [m.get("t") for m in sess["msgs"]])
    check("消息 k 已归一到秒",
          all(1_700_000_000 < (m.get("k") or 0) < 2_000_000_000 for m in sess["msgs"]),
          [m.get("k") for m in sess["msgs"]])

    chunks = list(rag.iter_chunks(sess))
    check("RAG 切块成功", bool(chunks), len(chunks))
    if chunks:
        check("RAG 块文本带正常时间头",
              chunks[0]["text"].startswith("[2026-11-21"), chunks[0]["text"][:40])
        check("RAG 会话 start/end 正常",
              chunks[0]["start"].startswith("2026-11-21")
              and chunks[0]["end"].startswith("2026-11-21"),
              (chunks[0]["start"], chunks[0]["end"]))


def main():
    base = PROJECT_ROOT / "data" / "cache"
    base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="dsh_time_", dir=base) as td:
        test_do_dsh_time(Path(td))
    print("-" * 60)
    if FAILURES:
        print(f"FAILED {len(FAILURES)}: {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
