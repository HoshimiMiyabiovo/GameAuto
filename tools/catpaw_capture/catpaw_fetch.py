# -*- coding: utf-8 -*-
"""妙手（CatPaw / catpaw-moon）本地会话抓取 → 对话总库 add() 同构 JSON。

背景（2026-11-17）：妙手是 Electron 壳的 CatPaw，用户数据在
`%APPDATA%\\catpaw-moon\\catpaw-memory-<account>.db`（SQLite，非加密）。
`conversations`/`sessions` 存标题与时间，`ui_sdk_messages` 存逐条 payload：

  role=user      payload.content 是用户输入文本；
  role=assistant payload.content 是最终回答（很多中间轮为空）；
                 payload.reasoning.content 是思考过程；
  role=tool      payload.toolName + payload.toolResult（读文件/执行命令；
                 入库时截到 600 字，原文留在 raw/）；
  retry-banner   重试提示，跳过。

输出与 Trae/Grok 抓取同构（build.py 的 add() 契约）：
  data/catpaw_capture/raw/<conversation_id>.json   完整会话 payload（断点/排查）
  data/catpaw_capture/export/catpaw.json           扁平聚合（上游唯一入口）
  data/catpaw_capture/export/<conversation_id>.md  人可读 Markdown

增量：catpaw.json 按 id 合并去重；DB 被妙手占用时用 SQLite backup API 复制到
临时文件再读，不锁应用。数据含聊天正文，`/data/catpaw_capture/` 已在 .gitignore。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "data", "catpaw_capture")
RAW = os.path.join(ROOT, "raw")
EXPORT = os.path.join(ROOT, "export")
CATPAW_JSON = os.path.join(EXPORT, "catpaw.json")

DEFAULT_GLOB = os.path.join(
    os.environ.get("APPDATA", os.path.expanduser("~")),
    "catpaw-moon", "catpaw-memory-*.db")
TOOL_RESULT_LIMIT = 600
# 妙手客户端只存数字模型 ID；107 已用 /api/agent/maas/model-types 验证为
# LongCat-2.5；10002544 不在官方静态列表里，但用户确认它是 Auto 自动选择
# （客户端 model picker 显示 Auto，落库为内部路由 ID），按 Auto 标注。
MODEL_NAMES = {"107": "LongCat-2.5", "10002544": "Auto"}
WS_RE = re.compile(r"([A-Za-z]:[\\\\/]+GithubProject[\\\\/]+[A-Za-z0-9_\-]+)")
WS_CTX_RE = re.compile(r"Workspace directory:\s*([A-Za-z]:\\[^\s<\"]+)")


def model_label(model_id) -> str:
    mid = str(model_id or "").strip()
    if not mid or mid == "0":
        return ""
    return MODEL_NAMES.get(mid) or f"自定义模型（{mid}）"


def log(msg: str) -> None:
    print(msg, flush=True)


def jdump(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def disp(sec: int) -> str:
    if not sec:
        return ""
    return datetime.fromtimestamp(sec, timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")


def ms_to_epoch(value) -> int:
    try:
        return int(int(value) // 1000)
    except (TypeError, ValueError):
        return 0


def _text_of(value) -> str:
    """从字符串/富文本段里尽量取出纯文本。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "\n".join(t for t in (_text_of(x) for x in value) if t.strip())
    if isinstance(value, dict):
        for key in ("text", "text_content", "content", "value"):
            if key in value:
                got = _text_of(value[key])
                if got.strip():
                    return got
    return ""


def infer_workspace(blob: str) -> str:
    """从 sandbox_context / 工具文件路径推断工作区名。"""
    m = WS_CTX_RE.search(blob) or WS_RE.search(blob)
    if not m:
        return ""
    path = m.group(1).replace("/", "\\").rstrip("\\").rstrip(".,;:，。；：")
    return os.path.basename(path)


def parse_payload(row, include_tools: bool = True):
    """返回 [(role, epoch, text)]；角色取 user/think/ai/tool。"""
    try:
        p = json.loads(row["payload"])
    except (TypeError, ValueError):
        return []
    if not isinstance(p, dict):
        return []
    role = str(row["role"] or p.get("role") or "")
    ts = ms_to_epoch(row["created_at_ms"])
    out = []
    if role == "user":
        text = _text_of(p.get("content")).strip()
        if text:
            out.append(("user", ts, text))
    elif role == "assistant":
        reasoning = p.get("reasoning")
        if isinstance(reasoning, dict):
            think = _text_of(reasoning.get("content")).strip()
            if think:
                out.append(("think", ts, think))
        answer = _text_of(p.get("content")).strip()
        if answer:
            out.append(("ai", ts, answer))
    elif role == "tool" and include_tools:
        name = str(p.get("toolName") or p.get("tool_name") or "tool")
        result = _text_of(p.get("toolResult") or p.get("tool_result") or "")
        call = _text_of(p.get("modelData", {}).get("content") if isinstance(p.get("modelData"), dict) else "")
        head = f"[{name}]"
        if call:
            head += " " + call.replace("\n", " ")[:180]
        text = (head + "\n" + result).strip()
        if len(text) > TOOL_RESULT_LIMIT:
            text = text[:TOOL_RESULT_LIMIT] + " …"
        if text:
            out.append(("tool", ts, text))
        # 工具返回全文走 output 角色（2026-11-17 与 Claude/WorkBuddy 等①口径对齐），
        # add() 侧 20 万字帽兜底；完整 payload 原本就留 raw/。
        if result.strip():
            out.append(("output", ts, result))
    return out


def _columns(conn: sqlite3.Connection, table: str) -> set:
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.DatabaseError:
        return set()


def export_db(db_path: str, include_tools: bool = True) -> list:
    """读取一个 userData DB，返回 add() 同构会话列表。"""
    tmpdir = tempfile.mkdtemp(prefix="catpaw_")
    tmp_db = os.path.join(tmpdir, "memory.db")
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(tmp_db)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()

    conn = sqlite3.connect(tmp_db)
    conn.row_factory = sqlite3.Row
    try:
        if not _columns(conn, "ui_sdk_messages") or not _columns(conn, "sessions"):
            log(f"[跳过] {db_path}: 不是妙手会话库")
            return []
        deleted = set()
        if _columns(conn, "deleted_sessions"):
            deleted = {str(r[0]) for r in conn.execute("select session_id from deleted_sessions")}

        sessions = []
        for s in conn.execute(
            "select id, title, timestamp, updated_at, extra from sessions order by timestamp"
        ):
            sid = str(s["id"])
            if sid in deleted:
                continue
            msgs = []
            raw_rows = []
            for m in conn.execute(
                "select seq, message_id, role, payload, created_at_ms from ui_sdk_messages "
                "where conversation_id=? order by seq", (sid,)
            ):
                raw_rows.append({
                    "seq": m["seq"], "message_id": m["message_id"], "role": m["role"],
                    "payload": m["payload"], "created_at_ms": m["created_at_ms"],
                })
                msgs.extend(parse_payload(m, include_tools=include_tools))
            if not msgs:
                continue
            stamps = [ts for _r, ts, _x in msgs if ts]
            start = min(stamps) if stamps else ms_to_epoch(s["timestamp"])
            end = max(stamps) if stamps else ms_to_epoch(s["updated_at"] or s["timestamp"])
            blob = json.dumps(raw_rows, ensure_ascii=False)
            ws = infer_workspace(blob)
            title = str(s["title"] or "").strip() or "(妙手会话)"
            try:
                session_extra = json.loads(s["extra"] or "{}")
            except (TypeError, ValueError):
                session_extra = {}
            model_id = str(session_extra.get("persistedModelId") or "").strip()
            model_name = model_label(model_id)
            note = f"妙手本地会话库 {os.path.basename(db_path)} · {len(msgs)} 条有效消息"
            if model_name:
                note += f" · 模型：{model_name}"
            item = {
                "id": f"catpaw:{sid}",
                "src": "妙手",
                "title": title,
                "ws": ws,
                "model_id": model_id,
                "model": model_name,
                "s": disp(start),
                "e": disp(end),
                "sk": end or start,
                "n": len(msgs),
                "chars": sum(len(x) for _r, _t, x in msgs),
                "note": note,
                "msgs": [
                    {"r": r, "t": disp(t), "k": int(t or 0), "x": x}
                    for r, t, x in msgs
                ],
            }
            sessions.append(item)
            jdump(os.path.join(RAW, f"{sid}.json"), {
                "session": {k: s[k] for k in ("id", "title", "timestamp", "updated_at", "extra")},
                "messages": raw_rows,
            })
        return sessions
    finally:
        conn.close()
        try:
            os.remove(tmp_db)
            os.rmdir(tmpdir)
        except OSError:
            pass


def session_markdown(session: dict) -> str:
    lines = [f"# {session['title']}", "", f"- 来源: {session['note']}",
             f"- 工作区: {session['ws'] or '未知'}",
             f"- 模型: {session.get('model') or '未知'}", f"- 时间: {session['s']} ~ {session['e']}", ""]
    for m in session["msgs"]:
        who = {"user": "## 用户", "ai": "## 妙手", "think": "## 妙手 思考",
               "tool": "## 工具"}.get(m["r"], f"## {m['r']}")
        lines += [who, "", m["x"], ""]
    return "\n".join(lines)


def merge_export(sessions: list) -> tuple:
    existing = {}
    if os.path.exists(CATPAW_JSON):
        try:
            for s in json.load(open(CATPAW_JSON, encoding="utf-8")):
                existing[s.get("id")] = s
        except (ValueError, OSError) as exc:
            log(f"[警告] 已有 catpaw.json 读取失败（将整体重建）: {exc}")
    added = 0
    for s in sessions:
        if s["id"] not in existing:
            added += 1
        existing[s["id"]] = s
    merged = sorted(existing.values(), key=lambda s: -int(s.get("sk") or 0))
    jdump(CATPAW_JSON, merged)
    return merged, added


def main() -> int:
    global ROOT, RAW, EXPORT, CATPAW_JSON
    ap = argparse.ArgumentParser(description="妙手（CatPaw）本地会话抓取 → 对话总库同构 JSON")
    ap.add_argument("--db", action="append", default=[], help="指定 catpaw-memory-*.db（可多次）")
    ap.add_argument("--root", default="", help="覆盖数据根目录（默认 <仓库>/data/catpaw_capture）")
    ap.add_argument("--no-tools", action="store_true", help="不导出工具结果（默认导出前 600 字）")
    args = ap.parse_args()
    if args.root:
        ROOT = args.root
        RAW = os.path.join(ROOT, "raw")
        EXPORT = os.path.join(ROOT, "export")
        CATPAW_JSON = os.path.join(EXPORT, "catpaw.json")

    dbs = list(args.db) or [p for p in glob.glob(DEFAULT_GLOB)
                            if not p.lower().endswith("-anon.db")]
    dbs = [p for p in dbs if os.path.exists(p)]
    if not dbs:
        log(f"未找到妙手会话库：{DEFAULT_GLOB}")
        return 1

    sessions, failures = [], []
    for db in dbs:
        try:
            got = export_db(db, include_tools=not args.no_tools)
            log(f"[读取] {db}: {len(got)} 个会话")
        except Exception as exc:  # noqa: BLE001 - 单库失败不拖垮整批
            log(f"[失败] {db}: {exc}")
            failures.append(db)
            continue
        sessions.extend(got)

    if not sessions:
        log("没有解析出任何会话（数据库结构变化？看 raw/）")
        return 1

    for s in sessions:
        jdump(os.path.join(EXPORT, f"{s['id'].split(':', 1)[-1]}.md"), session_markdown(s))
    merged, added = merge_export(sessions)
    log(f"完成：本次解析 {len(sessions)} 个会话，catpaw.json 合并后 {len(merged)} 个（新增 {added}）")
    if failures:
        log(f"失败: {failures}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
