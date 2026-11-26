# -*- coding: utf-8 -*-
"""商汤小浣熊本地会话抓取回归（2026-11-25）。

用临时 SQLite 复现 local-chat.sqlite3 的 sessions/messages schema，锁：
[1] active+visible 过滤、epoch 毫秒→秒、user/think/ai/tool/output 映射；
[2] 助手 content.stages 的最终文本/思维/工具调用/执行结果保留，UI 元数据跳过；
[3] id/src/ws/model 原值与 merge_export 幂等；
[4] recover_raccoon 从 raw 的 session.payload 恢复会话级真实模型。
"""

import io
import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "tools" / "raccoon_capture"))
sys.path.insert(0, str(PROJECT_ROOT / "tools"))

from sandbox_temp import install as _install_sandbox_temp

_install_sandbox_temp()
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import raccoon_fetch as rf  # noqa: E402
import ai_history_model_recover as mr  # noqa: E402

FAILURES = []


def check(label, ok, detail=None):
    print(("  PASS " if ok else "  FAIL ") + label +
          ("" if ok or detail is None else f"  ← {detail}"))
    if not ok:
        FAILURES.append(label)


def make_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE sessions (
          id TEXT PRIMARY KEY, title TEXT, available INTEGER, workspace_dir TEXT,
          created_at_ms INTEGER, updated_at_ms INTEGER, last_chatted_at_ms INTEGER,
          payload_json TEXT
        );
        CREATE TABLE messages (
          session_id TEXT, seq INTEGER, role TEXT, active INTEGER, visible INTEGER,
          created_at_ms INTEGER, updated_at_ms INTEGER, payload_json TEXT
        );
        """
    )
    session_payload = {
        "modelSelection": {"model": "sn-deepseek-v4-1-flash-1017"},
        "clientBinding": {"modelBinding": {"model": "sn-deepseek-v4-1-flash-1017"}},
    }
    conn.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)", (
        "ses_raccoon1", "测试小浣熊会话", 1, r"E:\GithubProject\game_auto",
        1795316896396, 1795322971844, 1795322971844,
        json.dumps(session_payload, ensure_ascii=False),
    ))
    user_payload = {
        "role": "user", "type": "text", "content": "把日志总结一下",
        "attachments": [{"name": "run.log", "size": 123}],
    }
    assistant_payload = {
        "role": "assistant", "type": "text",
        "content": {
            "status": "complete",
            "stages": [
                {"type": "think", "content": "先看日志"},
                {"type": "local_agent_tool_call", "content": {
                    "toolName": "bash", "input": {"command": "pwd"},
                    "output": r"E:\GithubProject\game_auto",
                }},
                {"type": "generate", "content": "正在检查", "phase": "commentary"},
                {"type": "execution", "content": "warning: demo"},
                {"type": "generate", "content": "总结完成", "phase": "final_answer"},
                {"type": "local_agent_progress", "content": {"latest": "噪声"}},
                {"type": "file", "content": {"filename": "demo.txt"}},
            ],
        },
    }
    rows = [
        (1, "user", 1, 1, 1795316896680, 1795316899205, user_payload),
        (2, "assistant", 1, 1, 1795316899300, 1795322971844, assistant_payload),
        (3, "assistant", 0, 1, 1795322971900, 1795322971900,
         {"role": "assistant", "content": "inactive"}),
        (4, "assistant", 1, 0, 1795322972000, 1795322972000,
         {"role": "assistant", "content": "hidden"}),
    ]
    for seq, role, active, visible, created, updated, payload in rows:
        conn.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?)", (
            "ses_raccoon1", seq, role, active, visible, created, updated,
            json.dumps(payload, ensure_ascii=False),
        ))
    conn.commit()
    conn.close()


def test_capture_and_recover(tmp_path):
    print("[1] SQLite 解析、stages 映射、模型恢复、合并")
    db = tmp_path / "local-chat.sqlite3"
    make_db(str(db))
    check("显式 DB 定位", rf.find_db(str(db)) == str(db.resolve()))
    copy_dir = tmp_path / "copy"
    copy_dir.mkdir()
    sessions = rf.load_all(str(db), str(copy_dir))
    check("只装载 1 个 available 会话", list(sessions) == ["ses_raccoon1"], list(sessions))
    check("active+visible 消息过滤", len(sessions["ses_raccoon1"]["messages"]) == 2,
          len(sessions["ses_raccoon1"]["messages"]))

    session = rf.convert_session("ses_raccoon1", sessions["ses_raccoon1"])
    roles = [m["r"] for m in session["msgs"]]
    check("角色顺序 user→think→tool→output→ai→output→ai",
          roles == ["user", "think", "tool", "output", "ai", "output", "ai"], roles)
    check("id/src/标题/ws", session["id"] == "raccoon:ses_raccoon1"
          and session["src"] == "小浣熊" and session["title"] == "测试小浣熊会话"
          and session["ws"] == "game_auto", session)
    check("epoch 毫秒已归一为秒", all(m["k"] < 4_000_000_000 for m in session["msgs"])
          and session["sk"] < 4_000_000_000, session["sk"])
    check("模型原值进 note", "sn-deepseek-v4-1-flash-1017" in session["note"], session["note"])
    user = next(m for m in session["msgs"] if m["r"] == "user")
    check("用户附件清单保留", "run.log" in user["x"], user["x"])
    tool = next(m for m in session["msgs"] if m["r"] == "tool")
    check("tool 摘要含命令且受 600 字帽约束", "bash" in tool["x"] and "pwd" in tool["x"]
          and len(tool["x"]) <= rf.TOOL_RESULT_LIMIT + 50, tool["x"])
    outputs = [m["x"] for m in session["msgs"] if m["r"] == "output"]
    check("工具返回/执行结果走 output 全文", r"E:\GithubProject\game_auto" in outputs
          and "warning: demo" in outputs, outputs)
    check("UI 元数据不入库", "噪声" not in json.dumps(session, ensure_ascii=False)
          and "demo.txt" not in json.dumps(session, ensure_ascii=False))

    old_json = rf.RACCOON_JSON
    rf.RACCOON_JSON = str(tmp_path / "raccoon.json")
    try:
        check("首次合并 → 1 会话", rf.merge_export(session) == 1)
        check("同 id 重抓 → 替换仍 1 会话",
              rf.merge_export({**session, "title": "新标题"}) == 1)
        check("读到新内容", json.loads((tmp_path / "raccoon.json").read_text(encoding="utf-8"))[0]["title"] == "新标题")
        (tmp_path / "raccoon.json").write_text("{broken", encoding="utf-8")
        check("聚合损坏 → 重建", rf.merge_export(session) == 1)
    finally:
        rf.RACCOON_JSON = old_json

    raw_root = tmp_path / "raw"
    raw_dir = raw_root / "ses_raccoon1"
    raw_dir.mkdir(parents=True)
    (raw_dir / "messages.json").write_text(
        json.dumps(sessions["ses_raccoon1"], ensure_ascii=False), encoding="utf-8")
    old_raw = mr.RACCOON_RAW
    mr.RACCOON_RAW = raw_root
    try:
        recovered = mr.recover_raccoon()
    finally:
        mr.RACCOON_RAW = old_raw
    rec = recovered.get("raccoon:ses_raccoon1") or {}
    check("recover_raccoon 命中会话 id", bool(rec), recovered)
    check("recover_raccoon 保留原始路由名（scan 阶段再 canonical）",
          rec.get("primary_model") == "sn-deepseek-v4-1-flash-1017", rec)
    check("canonical_model 把 sn- 前缀收敛到 deepseek-v4.1-flash-1017",
          mr.canonical_model(rec.get("primary_model")) == "deepseek-v4.1-flash-1017")


def main():
    tmp = tempfile.mkdtemp(prefix="raccoon_cap_")
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
