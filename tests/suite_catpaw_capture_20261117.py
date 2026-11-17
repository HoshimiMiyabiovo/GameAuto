# -*- coding: utf-8 -*-
"""妙手（CatPaw）本地会话抓取回归（2026-11-17）。

锁 tools/catpaw_capture/catpaw_fetch.py 的契约：
[1] 从 catpaw-memory-*.db 的 sessions/ui_sdk_messages 解析 user/think/ai/tool；
[2] 工具结果按 600 字截断，retry-banner 不入库；
[3] 从 sandbox_context 的 Workspace directory 推断工作区名；
[4] 输出为 build.py add() 同构结构（id/src/title/ws/s/e/sk/n/chars/note/msgs）。
"""
import io
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tools" / "catpaw_capture"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import catpaw_fetch as cf  # noqa: E402

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
        create table sessions (id text primary key, title text, timestamp integer,
                               updated_at integer, extra text);
        create table ui_sdk_messages (conversation_id text, seq integer, message_id text,
                                      role text, payload text, created_at_ms integer,
                                      updated_at_ms integer, schema_version integer);
        create table deleted_sessions (session_id text, conversation_id text, deleted_at integer);
        """
    )
    conn.execute(
        "insert into sessions values (?,?,?,?,?)",
        ("conv-1", "测试会话", 1794645124965, 1794645200000,
         json.dumps({"projectId": "p1", "status": "success"}, ensure_ascii=False)),
    )
    rows = [
        ("user", {"role": "user", "content": "把项目日志总结一下"},
         "2026-11-17T10:00:00Z"),
        ("assistant", {"role": "assistant", "finished": True,
                       "reasoning": {"content": "先看日志", "finished": True},
                       "content": ""}, "2026-11-17T10:00:05Z"),
        ("tool", {"role": "tool", "toolName": "read_file",
                  "toolResult": "E:\\GithubProject\\game_auto\\AGENTS.md " + "x" * 900,
                  "modelData": {"content": "{\"target_file\":\"AGENTS.md\"}"}},
         "2026-11-17T10:00:06Z"),
        ("assistant", {"role": "assistant", "finished": True,
                       "reasoning": {"content": "日志看完了"},
                       "content": "## 结论\n今天日常 7/8 通过。"},
         "2026-11-17T10:00:09Z"),
        ("retry-banner", {"role": "retry-banner"}, "2026-11-17T10:00:10Z"),
    ]
    from datetime import datetime
    for seq, (role, payload, iso) in enumerate(rows, 1):
        ts = int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)
        conn.execute(
            "insert into ui_sdk_messages values (?,?,?,?,?,?,?,1)",
            ("conv-1", seq, f"m{seq}", role, json.dumps(payload, ensure_ascii=False), ts, ts),
        )
    conn.commit()
    conn.close()


def test_export_db():
    print("[1] 解析 user/think/ai/tool + 工作区推断")
    with tempfile.TemporaryDirectory() as td:
        db = str(Path(td) / "catpaw-memory-test.db")
        make_db(db)
        got = cf.export_db(db)
    check("解析出 1 个会话", len(got) == 1, len(got))
    if not got:
        return
    s = got[0]
    check("id 带 catpaw: 前缀", s["id"] == "catpaw:conv-1", s.get("id"))
    check("src=妙手", s["src"] == "妙手", s.get("src"))
    check("标题", s["title"] == "测试会话", s.get("title"))
    check("工作区推断 game_auto", s["ws"] == "game_auto", s.get("ws"))
    roles = [m["r"] for m in s["msgs"]]
    check("user/think/ai/tool/output 顺序（工具返回走 output 角色）",
          roles == ["user", "think", "tool", "output", "think", "ai"], roles)
    out_m = [m for m in s["msgs"] if m["r"] == "output"][0]
    check("output 是 toolResult 全文（不被 600 截）",
          len(out_m["x"]) > cf.TOOL_RESULT_LIMIT, len(out_m["x"]))
    tool = [m for m in s["msgs"] if m["r"] == "tool"][0]
    check("tool 结果截 600 字", len(tool["x"]) <= cf.TOOL_RESULT_LIMIT + 4, len(tool["x"]))
    check("retry-banner 不入库", "retry-banner" not in roles)
    check("add() 字段齐全",
          all(k in s for k in ("id", "src", "title", "ws", "s", "e", "sk", "n", "chars", "note", "msgs")),
          sorted(s.keys()))


def test_deleted_session():
    print("[2] deleted_sessions 过滤")
    with tempfile.TemporaryDirectory() as td:
        db = str(Path(td) / "catpaw-memory-test.db")
        make_db(db)
        conn = sqlite3.connect(db)
        conn.execute("insert into deleted_sessions values ('conv-1','conv-1',0)")
        conn.commit()
        conn.close()
        got = cf.export_db(db)
    check("被删除会话不导出", got == [], got)


def main():
    test_export_db()
    test_deleted_session()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED {len(FAILURES)}: {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
