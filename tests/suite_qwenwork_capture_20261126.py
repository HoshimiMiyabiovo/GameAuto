# -*- coding: utf-8 -*-
"""千问办公（QwenWork）本地会话抓取回归（2026-11-26）。

用临时 SQLite 复现 qwenwork.db 的 session/message/part 三表，锁：
[1] 子代理会话（parent_id 非空）跳过、空会话跳过；
[2] user 消息剥掉宿主注入块（workspace_context / system-reminder /
    context_checkpoint / MCP 工具清单），只留用户真正输入；
[3] user/think/ai/tool/output 映射、工具返回全文走 output、step-* 跳过；
[4] 未命名会话（New session - …）用首条用户输入兜底标题；
[5] epoch 毫秒→秒、id/src/ws、合并幂等与损坏重建；
[6] recover_qwenwork 从 raw 逐条恢复真实 modelID。
"""

import io
import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "tools" / "qwenwork_capture"))
sys.path.insert(0, str(PROJECT_ROOT / "tools"))

from sandbox_temp import install as _install_sandbox_temp  # noqa: E402

_install_sandbox_temp()
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import ai_history_model_recover as mr  # noqa: E402
import qwenwork_fetch as qf  # noqa: E402

FAILURES = []

USER_TEXT = (
    "<workspace_context>\n- Workspace mode: general\n</workspace_context>\n\n"
    "<system-reminder>\nYou are operating in awareness mode.\n</system-reminder>\n\n"
    "Available MCP servers:\n"
    "- qwapp.demo.qwoffice: No description available\n\n"
    "Use qw-builtin_qwenwork_mcp_tool_list to see all tools.\n\n"
    "Available tools:\n"
    "mcp__qwapp.demo.qwoffice__read_word, mcp__qwapp.demo.qwoffice__save_document\n\n"
    "把 11-26 的日志总结一下"
)


def check(label, ok, detail=None):
    print(("  PASS " if ok else "  FAIL ") + label +
          ("" if ok or detail is None else f"  ← {detail}"))
    if not ok:
        FAILURES.append(label)


def make_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE session (
          id TEXT PRIMARY KEY, title TEXT, directory TEXT, parent_id TEXT,
          model TEXT, time_created INTEGER, time_updated INTEGER
        );
        CREATE TABLE message (
          id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, data TEXT
        );
        CREATE TABLE part (
          id TEXT PRIMARY KEY, message_id TEXT, time_created INTEGER, data TEXT
        );
        """
    )
    sessions = [
        ("ses_qw1", "New session - 2026-11-26T01:00:00.000Z",
         "E:/GithubProject/game_auto", None,
         json.dumps({"id": "deepseek-flash", "providerID": "deepseek"}), 1795626000000, 1795626600000),
        ("ses_child", "子代理会话", "E:/GithubProject/game_auto", "ses_qw1",
         "{}", 1795626000000, 1795626001000),
        ("ses_empty", "空会话", "E:/GithubProject/game_auto", None, "{}",
         1795626000000, 1795626001000),
    ]
    conn.executemany("INSERT INTO session VALUES (?,?,?,?,?,?,?)", sessions)

    msgs = [
        ("msg_u1", "ses_qw1", 1795626100000,
         {"role": "user", "model": {"modelID": "deepseek-flash"}, "time": {"created": 1795626100000}}),
        ("msg_a1", "ses_qw1", 1795626110000,
         {"role": "assistant", "modelID": "deepseek-flash", "providerID": "deepseek",
          "time": {"created": 1795626110000}}),
        ("msg_c1", "ses_child", 1795626120000, {"role": "user"}),
    ]
    conn.executemany("INSERT INTO message VALUES (?,?,?,?)", [
        (mid, sid, ts, json.dumps(data, ensure_ascii=False)) for mid, sid, ts, data in msgs
    ])

    parts = [
        ("p1", "msg_u1", 1795626100000, {"type": "text", "text": USER_TEXT}),
        ("p2", "msg_u1", 1795626100100, {"type": "step-start", "snapshot": "abc"}),
        ("p3", "msg_a1", 1795626110000, {"type": "reasoning", "text": "先看日志"}),
        ("p4", "msg_a1", 1795626110100, {"type": "tool", "tool": "bash",
                                        "callID": "c1", "state": {
                                            "status": "completed",
                                            "input": {"command": "Get-Content run.log"},
                                            "output": "line1\nline2"}}),
        ("p5", "msg_a1", 1795626110200, {"type": "text", "text": "日志正常"}),
        ("p6", "msg_a1", 1795626110300, {"type": "step-finish", "reason": "stop"}),
        ("p7", "msg_c1", 1795626120000, {"type": "text", "text": "子代理内容"}),
    ]
    conn.executemany("INSERT INTO part VALUES (?,?,?,?)", [
        (pid, mid, ts, json.dumps(data, ensure_ascii=False)) for pid, mid, ts, data in parts
    ])
    conn.commit()
    conn.close()


def test_capture_and_recover(tmp_path):
    print("[1] 三表解析、注入块剥离、角色映射、模型恢复")
    db = tmp_path / "qwenwork.db"
    make_db(str(db))
    sessions = qf.load_all(str(db), str(tmp_path))
    check("子代理/空会话不装配", set(sessions) == {"ses_qw1"}, list(sessions))
    check("消息按 time_created 装配", len(sessions["ses_qw1"]["items"]) == 2)

    session = qf.convert_session("ses_qw1", sessions["ses_qw1"])
    roles = [m["r"] for m in session["msgs"]]
    check("角色顺序 user→think→tool→output→ai", roles == ["user", "think", "tool", "output", "ai"], roles)
    check("id/src/ws", session["id"] == "qwenwork:ses_qw1"
          and session["src"] == "千问办公" and session["ws"] == "game_auto", session)
    check("未命名会话用首条用户输入兜底标题", session["title"] == "把 11-26 的日志总结一下",
          session["title"])
    user = next(m for m in session["msgs"] if m["r"] == "user")
    check("注入块已剥离、只留用户输入", user["x"] == "把 11-26 的日志总结一下", user["x"])
    check("MCP 工具清单未入库",
          "Available MCP servers" not in json.dumps(session, ensure_ascii=False)
          and "mcp__qwapp" not in json.dumps(session, ensure_ascii=False))
    check("epoch 毫秒→秒", session["sk"] < 4_000_000_000 and all(m["k"] < 4_000_000_000
                                                            for m in session["msgs"]))
    check("模型原值进 note", "deepseek-flash" in session["note"], session["note"])
    tool = next(m for m in session["msgs"] if m["r"] == "tool")
    check("tool 摘要含命令且受 600 字帽约束",
          "bash" in tool["x"] and "Get-Content run.log" in tool["x"]
          and len(tool["x"]) <= qf.TOOL_RESULT_LIMIT + 50, tool["x"])
    outputs = [m["x"] for m in session["msgs"] if m["r"] == "output"]
    check("工具返回全文走 output", outputs == ["line1\nline2"], outputs)

    print("[2] 聚合合并幂等 + 损坏重建")
    old = (qf.ROOT, qf.RAW, qf.EXPORT, qf.QWENWORK_JSON)
    out_dir = tmp_path / "out"
    qf.ROOT, qf.RAW, qf.EXPORT = str(tmp_path), str(tmp_path / "raw"), str(out_dir)
    qf.QWENWORK_JSON = str(out_dir / "qwenwork.json")
    try:
        check("首次合并 → 1 会话", qf.merge_export(session) == 1)
        check("同 id 重抓 → 替换仍 1 会话",
              qf.merge_export({**session, "title": "新标题"}) == 1)
        check("读到新内容",
              json.loads(Path(qf.QWENWORK_JSON).read_text(encoding="utf-8"))[0]["title"] == "新标题")
        Path(qf.QWENWORK_JSON).write_text("{broken", encoding="utf-8")
        check("聚合损坏 → 重建", qf.merge_export(session) == 1)
    finally:
        qf.ROOT, qf.RAW, qf.EXPORT, qf.QWENWORK_JSON = old

    print("[3] recover_qwenwork 逐条模型恢复")
    raw_root = tmp_path / "raw"
    raw_dir = raw_root / "ses_qw1"
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / "messages.json").write_text(
        json.dumps({"session": sessions["ses_qw1"]["session"],
                    "messages": [{"msg": it["msg"], "ts_ms": it["ts_ms"],
                                  "parts": it["parts"]}
                                 for it in sessions["ses_qw1"]["items"]]},
                   ensure_ascii=False), encoding="utf-8")
    old_raw = mr.QWENWORK_RAW
    mr.QWENWORK_RAW = raw_root
    try:
        recovered = mr.recover_qwenwork()
    finally:
        mr.QWENWORK_RAW = old_raw
    rec = recovered.get("qwenwork:ses_qw1") or {}
    check("recover_qwenwork 命中会话 id", bool(rec), recovered)
    check("逐条模型按原值记录", rec.get("level") == "per_message"
          and rec.get("primary_model") == "deepseek-flash", rec)


def main():
    tmp = tempfile.mkdtemp(prefix="qwenwork_cap_")
    try:
        test_capture_and_recover(Path(tmp))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("-" * 60)
    if FAILURES:
        print(f"FAILED {len(FAILURES)}: {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
