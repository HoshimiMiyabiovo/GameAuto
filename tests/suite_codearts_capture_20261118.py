# -*- coding: utf-8 -*-
"""码道（CodeArts Agent）会话抓取回归（2026-11-18）。

背景：码道对话正文在内核数据目录 `~/.codeartsdoer/codearts-data/opencode.db`
（AgentKernel 是 opencode 变体，session/message/part 三表）。桌面端 chatSessions
jsonl 只是 UI 薄存档（response 全空）——排查弯路见 changelog。本套件锁
codearts_fetch 的解析契约（临时 DB 复现真实 schema，与 teleagent 同款三表）：
[1] 三表装配 + 映射（user/ai/think/tool 摘要 + output 全文，①口径对齐）；
[2] id 前缀 codearts: / src=码道 / modelID 原值（deepseek-v4.1-flash-1017）；
[3] merge_export 合并去重。

跑法：.venv\\Scripts\\python.exe -m pytest tests\\suite_codearts_capture_20261118.py -q
"""

import io
import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "tools" / "codearts_capture"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import codearts_fetch as cf  # noqa: E402

FAILURES = []


def check(label, ok, detail=None):
    mark = "PASS" if ok else "FAIL"
    print(f"  {mark} {label}" + (f"  ← {detail}" if not ok and detail is not None else ""))
    if not ok:
        FAILURES.append(label)


def make_db(path):
    """复现真实三表 schema（无 sequence 列）。"""
    c = sqlite3.connect(path)
    c.execute("create table session (id text primary key, project_id text, parent_id text, "
              "slug text, directory text, title text, version text, share_url text, "
              "summary_additions text, summary_deletions text, time_created integer, "
              "time_updated integer)")
    c.execute("create table message (id text primary key, session_id text, "
              "time_created integer, time_updated integer, data text)")
    c.execute("create table part (id text primary key, message_id text, session_id text, "
              "time_created integer, time_updated integer, data text)")
    sid = "ses_codearts1"
    c.execute("insert into session values (?,?,?,?,?,?,?,?,?,?,?,?)",
              (sid, None, None, "slug", "e:\\GithubProject\\game_auto", "测试会话",
               "1", None, None, None, 1794927099000, 1794927800000))
    msgs = [
        ("msg_u", 1794927099000,
         {"role": "user", "time": {"created": 1794927099000}},
         [{"type": "text", "text": "示例问题"}]),
        ("msg_a", 1794927100000,
         {"role": "assistant", "modelID": "deepseek-v4.1-flash-1017",
          "providerID": "huaweicloud", "time": {"created": 1794927100000}},
         [{"type": "step-start"},
          {"type": "reasoning", "text": "先问症状"},
          {"type": "tool", "tool": "grep", "state": {"input": {"pattern": "痛点"},
                                                     "output": "line 42"}},
          {"type": "text", "text": "缓解建议"}]),
    ]
    for mid, ts, data, parts in msgs:
        c.execute("insert into message values (?,?,?,?,?)", (mid, sid, ts, ts, json.dumps(data)))
        for i, part in enumerate(parts):
            c.execute("insert into part values (?,?,?,?,?,?)",
                      (f"{mid}_p{i}", mid, sid, ts + i, ts + i, json.dumps(part, ensure_ascii=False)))
    c.commit()
    c.close()


def test_load_convert_merge(tmp_path):
    print("[1] 三表装配 + 映射 + 合并")
    db = str(tmp_path / "opencode.db")
    make_db(db)
    copy_dir = tmp_path / "copy"
    copy_dir.mkdir()
    sessions = cf.load_all(db, str(copy_dir))
    check("会话装载", list(sessions) == ["ses_codearts1"], list(sessions))
    s = cf.convert_session("ses_codearts1", sessions["ses_codearts1"])
    roles = [m["r"] for m in s["msgs"]]
    check("映射顺序 user→think→tool→output→ai",
          roles == ["user", "think", "tool", "output", "ai"], roles)
    check("id/src/标题", s["id"] == "codearts:ses_codearts1"
          and s["src"] == "码道" and s["title"] == "测试会话",
          (s["id"], s["src"], s["title"]))
    check("modelID 原值 deepseek-v4.1-flash-1017（有字段不臆测）",
          "deepseek-v4.1-flash-1017" in s["note"], s["note"])
    out_m = [m for m in s["msgs"] if m["r"] == "output"]
    check("output 是工具返回全文", bool(out_m) and "line 42" in out_m[0]["x"], out_m)
    check("ws 从 directory 提取", s["ws"] == "game_auto", s["ws"])

    old_json = cf.CODEARTS_JSON
    cf.CODEARTS_JSON = str(tmp_path / "codearts.json")
    try:
        check("首次合并 → 1 会话", cf.merge_export(s) == 1)
        check("同 id 重抓 → 替换仍 1 会话",
              cf.merge_export({**s, "title": "新标题"}) == 1)
        got = json.loads((tmp_path / "codearts.json").read_text(encoding="utf-8"))
        check("读到新内容", got[0]["title"] == "新标题")
        (tmp_path / "codearts.json").write_text("{broken", encoding="utf-8")
        check("聚合损坏 → 重建", cf.merge_export(s) == 1)
    finally:
        cf.CODEARTS_JSON = old_json


def test_ws_rules():
    print("[2] ws 规则")
    check("码道工作空间目录（非 GithubProject）→ 空串",
          cf._norm_ws(r"C:\Users\admin\.codeartsdoer\CodeArts_Agent") == "")
    check("workspace 目录匹配 game_auto", cf._norm_ws("e:\\GithubProject\\game_auto") == "game_auto")


def main():
    # 手动 mkdtemp + 宽松清理：Windows 下刚关句柄的 sqlite 文件偶发被系统/杀软
    # 短暂锁定，TemporaryDirectory 严格清理会误报 WinError 32（产物是一次性的）
    td = tempfile.mkdtemp(prefix="codearts_cap_")
    try:
        test_load_convert_merge(Path(td))
    finally:
        import gc
        gc.collect()
        shutil.rmtree(td, ignore_errors=True)
    test_ws_rules()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED {len(FAILURES)}: {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
