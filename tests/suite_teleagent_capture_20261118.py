# -*- coding: utf-8 -*-
"""TeleAgent 会话抓取回归（2026-11-18）。

背景：TeleAgent 对话正文在 `~/.local/share/TeleAgent/users/*/teleagent.db`
（SQLite，opencode 系 session/message/part 三表，非加密，backup API 只读复制）。
本套件锁 teleagent_fetch 的解析契约（全部打桩，用临时 DB 复现真实 schema）：

[1] 三表装配：message/part 无 sequence 列按 time_created 排序、part 按
    message_id 挂回（2026-11-18 实测坑）；
[2] msg_to_records 映射：user/ai/think/tool 摘要 + state.output 全文走
    output 角色（①口径对齐）、step-* 跳过、时间戳 epoch 毫秒→秒；
[3] convert_session：id 前缀 teleagent: / ws 从 directory 提取 / note 含模型；
[4] merge_export 按 id 合并去重 + 聚合损坏重建；
[5] modelID 原值记录（chat-flash 是 TeleAgent 内部路由名，无别名依据不许
    臆测映射——2026-11-17 用户纠偏同款原则）。

跑法：.venv\\Scripts\\python.exe -m pytest tests\\suite_teleagent_capture_20261118.py -q
"""

import io
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "tools" / "teleagent_capture"))
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import teleagent_fetch as tf  # noqa: E402

FAILURES = []


def check(label, ok, detail=None):
    mark = "PASS" if ok else "FAIL"
    print(f"  {mark} {label}" + (f"  ← {detail}" if not ok and detail is not None else ""))
    if not ok:
        FAILURES.append(label)


def make_db(path):
    """按真实 schema 建三表（无 sequence 列！）并插一个会话的样例数据。"""
    c = sqlite3.connect(path)
    c.execute("create table session (id text primary key, project_id text, parent_id text, "
              "slug text, directory text, title text, version text, share_url text, "
              "summary_additions text, summary_deletions text, time_created integer, time_updated integer)")
    c.execute("create table message (id text primary key, session_id text, "
              "time_created integer, time_updated integer, data text)")
    c.execute("create table part (id text primary key, message_id text, session_id text, "
              "time_created integer, time_updated integer, data text)")
    sid = "ses_test123"
    c.execute("insert into session values (?,?,?,?,?,?,?,?,?,?,?,?)",
              (sid, None, None, "slug", "E:\\GithubProject\\game_auto", "测试会话",
               "1", None, None, None, 1794619468000, 1794619900000))
    msgs = [
        ("msg_u", 1794619469000,
         {"role": "user", "time": {"created": 1794619469000}},
         [{"type": "text", "text": "帮我调研一下"}]),
        ("msg_a", 1794619470000,
         {"role": "assistant", "modelID": "chat-flash", "providerID": "teleai",
          "time": {"created": 1794619470000}},
         [{"type": "step-start"},
          {"type": "reasoning", "text": "先拆解需求"},
          {"type": "tool", "tool": "read", "state": {"input": {"file_path": "a.py"},
                                                     "output": "def main():\n    pass\n"}},
          {"type": "step-start"},
          {"type": "text", "text": "调研结论如下"}]),
    ]
    for mid, ts, data, parts in msgs:
        c.execute("insert into message values (?,?,?,?,?)", (mid, sid, ts, ts, json.dumps(data)))
        for i, part in enumerate(parts):
            c.execute("insert into part values (?,?,?,?,?,?)",
                      (f"{mid}_p{i}", mid, sid, ts + i, ts + i, json.dumps(part, ensure_ascii=False)))
    c.commit()
    c.close()


def test_load_and_convert(tmp_path):
    print("[1] 三表装配 + 映射")
    db = str(tmp_path / "teleagent.db")
    make_db(db)
    # 复制目标必须独立子目录：tmpdir 与源库同目录同名时 backup 会覆盖源文件
    copy_dir = tmp_path / "copy"
    copy_dir.mkdir()
    sessions = tf.load_all(db, str(copy_dir))
    check("会话装载 1 个", list(sessions) == ["ses_test123"], list(sessions))
    entry = sessions["ses_test123"]
    check("2 条消息 / 第一条 1 个 part 第二条 5 个 part",
          len(entry["items"]) == 2 and len(entry["items"][0]["parts"]) == 1
          and len(entry["items"][1]["parts"]) == 5,
          [len(it["parts"]) for it in entry["items"]])

    s = tf.convert_session("ses_test123", entry)
    roles = [m["r"] for m in s["msgs"]]
    check("映射顺序 user→think→tool→output→ai（step-* 跳过）",
          roles == ["user", "think", "tool", "output", "ai"], roles)
    check("id 前缀 teleagent:", s["id"] == "teleagent:ses_test123", s["id"])
    check("src=TeleAgent / 标题", s["src"] == "TeleAgent" and s["title"] == "测试会话")
    check("ws 从 directory 提取", s["ws"] == "game_auto", s["ws"])
    check("note 含模型名 chat-flash 原值", "chat-flash" in s["note"], s["note"])
    out_m = [m for m in s["msgs"] if m["r"] == "output"]
    check("output 是工具返回全文（不被 600 截）",
          bool(out_m) and "def main():" in out_m[0]["x"] and len(out_m[0]["x"]) <= 40, out_m)
    check("msgs 记录含 r/t/k/x 四键", all(set(m) == {"r", "t", "k", "x"} for m in s["msgs"]))


def test_records_edge():
    print("[2] 边界：无正文用户消息 / 空会话")
    check("user 无 text part → 零记录",
          tf.msg_to_records({"role": "user"}, 1000, [{"type": "file"}]) == [])
    check("assistant 纯 step-* → 零记录",
          tf.msg_to_records({"role": "assistant"}, 1000,
                            [{"type": "step-start"}, {"type": "step-finish"}]) == [])
    check("tool 无 output → 只出摘要不出 output",
          [r for r, _k, _x in tf.msg_to_records(
              {"role": "assistant"}, 1000,
              [{"type": "tool", "tool": "ls", "state": {"input": {"command": "ls"}, "output": None}}])]
          == ["tool"])


def test_merge_and_ws(tmp_path):
    print("[3] merge_export / ws 规则")
    mk_file = tmp_path / "teleagent.json"
    old_json = tf.TELEAGENT_JSON
    tf.TELEAGENT_JSON = str(mk_file)
    base = {"id": "teleagent:a", "title": "旧", "msgs": []}
    check("首次合并 → 1 会话", tf.merge_export(base) == 1)
    check("同 id 重抓 → 替换仍 1 会话",
          tf.merge_export({"id": "teleagent:a", "title": "新", "msgs": []}) == 1)
    got = json.loads(mk_file.read_text(encoding="utf-8"))
    check("读到新内容", got[0]["title"] == "新", got[0]["title"])
    mk_file.write_text("{broken", encoding="utf-8")
    check("聚合损坏 → 重建不炸", tf.merge_export(base) == 1)
    tf.TELEAGENT_JSON = old_json
    check("ws 不匹配 GithubProject → 空串",
          tf._norm_ws(r"C:\Users\admin\.local\share\TeleAgent\TeleAgent的工作空间") == "",
          tf._norm_ws(r"C:\Users\admin\.local\share\TeleAgent\TeleAgent的工作空间"))
    check("ws 匹配 game_auto", tf._norm_ws("E:\\GithubProject\\game_auto\\x") == "game_auto")


def test_model_no_alias():
    print("[4] modelID 原值原则")
    note = tf.convert_session(
        "s", {"session": {"title": "t", "directory": ""},
              "items": [{"msg": {"role": "assistant", "modelID": "chat-flash"},
                         "ts_ms": 1000, "parts": []}]})["note"]
    check("note 用原始 modelID（chat-flash 无别名依据不映射）", "chat-flash" in note, note)


def main():
    with tempfile.TemporaryDirectory() as td:
        test_load_and_convert(Path(td))
    test_records_edge()
    with tempfile.TemporaryDirectory() as td:
        test_merge_and_ws(Path(td))
    test_model_no_alias()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED {len(FAILURES)}: {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
