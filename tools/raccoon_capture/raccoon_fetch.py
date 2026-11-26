# -*- coding: utf-8 -*-
"""商汤小浣熊本地会话抓取 → 对话总库 add() 同构 JSON。

真相源是桌面端 `~/.office-raccoon/user-data/local-chat.sqlite3`（schema v1：
sessions + messages），不是 AppData/Roaming 里的设备身份文件。助手正文是
`content.stages` 原生 envelope：think→think、generate/code→ai、
local_agent_tool_call→tool 摘要 + output 全文、execution→output；进度/展示元数据
跳过。时间列为 epoch 毫秒，导出前统一成秒；模型取会话 payload 的
modelSelection.model / clientBinding.modelBinding.model，按真实字段原值记录。

落盘：data/raccoon_capture/raw/<sid>/messages.json 留原始行与 payload；
export/raccoon.json 按会话 id 合并去重，另写逐会话 Markdown。SQLite 先用 backup
API 复制到 data/cache 再读，避免 WAL/占用问题，不锁应用、不改源库。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
import time
from datetime import datetime
from typing import Dict

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.join(REPO_ROOT, "data", "raccoon_capture")
RAW = os.path.join(ROOT, "raw")
EXPORT = os.path.join(ROOT, "export")
RACCOON_JSON = os.path.join(EXPORT, "raccoon.json")
DEFAULT_DB = os.path.join(os.path.expanduser("~"), ".office-raccoon", "user-data",
                          "local-chat.sqlite3")
TOOL_RESULT_LIMIT = 600
WS_RE = re.compile(r"[\\/]GithubProject[\\/]([A-Za-z0-9_\-]+)", re.IGNORECASE)


def log(msg: str) -> None:
    print(msg, flush=True)


def jdump(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _load_json(value, default):
    try:
        return json.loads(value or "{}")
    except (ValueError, TypeError):
        return default


def find_db(explicit: str | None = None) -> str:
    """定位本地会话库；显式路径优先，否则匹配桌面端实际 user-data 重定向。"""
    candidates = [explicit] if explicit else []
    if not explicit and os.environ.get("ELECTRON_USER_DATA_PATH"):
        candidates.append(os.path.join(os.environ["ELECTRON_USER_DATA_PATH"],
                                       "local-chat.sqlite3"))
    if not explicit:
        candidates.extend([
            DEFAULT_DB,
            os.path.join(os.environ.get("APPDATA", ""), "office-raccoon",
                         "local-chat.sqlite3"),
        ])
    for path in candidates:
        if path and os.path.isfile(path):
            return os.path.abspath(path)
    raise FileNotFoundError(
        "未找到小浣熊 local-chat.sqlite3；确认桌面端运行过，或用 --db 指定 "
        "~/.office-raccoon/user-data/local-chat.sqlite3"
    )


def open_readonly_copy(db_path: str, tmpdir: str) -> str:
    """用 SQLite backup API 复制一致快照，绕开运行中应用的 WAL/文件锁。"""
    dst = os.path.join(tmpdir, os.path.basename(db_path))
    src = sqlite3.connect(f"file:{db_path.replace(chr(92), '/')}?mode=ro", uri=True)
    dst_c = sqlite3.connect(dst)
    try:
        src.backup(dst_c)
    finally:
        dst_c.close()
        src.close()
    return dst


def load_all(db_path: str, tmpdir: str) -> Dict:
    """装载 available 会话及其 active+visible 消息，保留原始行与 payload。"""
    path = open_readonly_copy(db_path, tmpdir)
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA temp_store = MEMORY")
    try:
        sessions: Dict = {}
        for row in c.execute(
                "SELECT * FROM sessions WHERE available=1 ORDER BY last_chatted_at_ms DESC"):
            sid = str(row["id"])
            sessions[sid] = {
                "session": {"row": dict(row), "payload": _load_json(row["payload_json"], {})},
                "messages": [],
            }
        for row in c.execute(
                "SELECT * FROM messages WHERE active=1 AND visible=1 ORDER BY session_id, seq"):
            sid = str(row["session_id"])
            if sid not in sessions:
                continue
            sessions[sid]["messages"].append({
                "row": dict(row),
                "payload": _load_json(row["payload_json"], {}),
            })
        return sessions
    finally:
        c.close()


def _as_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _norm_ws(directory: str) -> str:
    match = WS_RE.search(str(directory or ""))
    return match.group(1) if match else ""


def _session_model(payload: dict) -> str:
    selection = payload.get("modelSelection") or {}
    if isinstance(selection, dict) and selection.get("model"):
        return str(selection["model"])
    binding = payload.get("clientBinding") or {}
    model_binding = binding.get("modelBinding") if isinstance(binding, dict) else {}
    if isinstance(model_binding, dict) and model_binding.get("model"):
        return str(model_binding["model"])
    return ""


def _attachment_text(payload: dict) -> str:
    lines = []
    for item in payload.get("attachments") or []:
        if not isinstance(item, dict):
            continue
        name = item.get("fileName") or item.get("name") or item.get("path") or item.get("url")
        if not name:
            continue
        meta = [str(item.get("mime") or ""),
                f"{item['size']} bytes" if item.get("size") else ""]
        lines.append(f"- {name}" + (f" ({' '.join(x for x in meta if x)})" if any(meta) else ""))
    return "\n".join(lines)


def _tool_summary(content: dict) -> str:
    tool = str(content.get("toolName") or content.get("tool") or "tool")
    inp = content.get("input")
    desc = ""
    if isinstance(inp, dict):
        for key in ("command", "description", "file_path", "query", "pattern", "url", "path", "name"):
            if inp.get(key):
                desc = str(inp[key]).splitlines()[0]
                break
    elif inp:
        desc = str(inp)
    if not desc and content.get("title"):
        desc = str(content["title"])
    desc = " ".join(desc.split())[:TOOL_RESULT_LIMIT]
    return f"🔧 {tool}  {desc}"[:TOOL_RESULT_LIMIT + 50]


def _assistant_records(payload: dict, ts: int):
    content = payload.get("content")
    if isinstance(content, str):
        return [("ai", ts, content)] if content.strip() else []
    if not isinstance(content, dict):
        return []
    stages = content.get("stages")
    if not isinstance(stages, list):
        text = _as_text(content).strip()
        return [("ai", ts, text)] if text else []

    records = []
    for stage in stages:
        if not isinstance(stage, dict):
            continue
        stage_type = stage.get("type")
        value = stage.get("content")
        if stage_type in ("think", "think_summary"):
            text = _as_text(value).strip()
            if text:
                records.append(("think", ts, text))
        elif stage_type in ("generate", "generated", "text", "code"):
            text = _as_text(value).strip()
            if text:
                records.append(("ai", ts, text))
        elif stage_type == "local_agent_tool_call":
            tool = value if isinstance(value, dict) else {}
            summary = _tool_summary(tool)
            if summary:
                records.append(("tool", ts, summary))
            output = _as_text(tool.get("output")).strip()
            if output:
                records.append(("output", ts, output))
        elif stage_type == "execution":
            text = _as_text(value).strip()
            if text:
                records.append(("output", ts, text))
    return records


def msg_to_records(row: dict, payload: dict):
    """一条数据库消息 → [(role, epoch_sec, text)]；只取当前可见有效消息。"""
    ts_ms = row.get("created_at_ms") or row.get("updated_at_ms") or 0
    ts = round(ts_ms / 1000) if ts_ms else 0
    role = str(row.get("role") or payload.get("role") or "")
    if role == "user":
        text = _as_text(payload.get("content")).strip()
        if not text:
            context = payload.get("message_config") or {}
            text = _as_text((context.get("inputContext") or {}).get("userRequest")).strip()
        attachments = _attachment_text(payload)
        if attachments:
            text = "\n\n".join(x for x in (text, "附件：", attachments) if x)
        return [("user", ts, text)] if text else []
    if role == "assistant":
        return _assistant_records(payload, ts)
    return []


def _disp(seconds: int) -> str:
    return datetime.fromtimestamp(seconds).strftime("%Y-%m-%d %H:%M") if seconds else ""


def convert_session(sid: str, entry: dict) -> dict:
    """把单会话原始行/payload 转成 build.py add() 同构扁平结构。"""
    session = entry["session"]
    row = session["row"]
    payload = session["payload"]
    records = []
    for message in entry["messages"]:
        records.extend(msg_to_records(message["row"], message["payload"]))

    model = _session_model(payload)
    ts_list = [k for _r, k, _x in records if k]
    created = round((row.get("created_at_ms") or 0) / 1000)
    updated = max(round((row.get("updated_at_ms") or 0) / 1000),
                  round((row.get("last_chatted_at_ms") or 0) / 1000))
    start = min([created] + ts_list) if (created or ts_list) else 0
    end = max([updated] + ts_list) if (updated or ts_list) else 0
    note = ("商汤小浣熊本地 SQLite 直读（~/.office-raccoon/user-data/local-chat.sqlite3）"
            f" · {len(entry['messages'])} 条消息 / {len(records)} 条入库记录"
            + (f" · 模型 {model}" if model else ""))
    return {
        "id": f"raccoon:{sid}",
        "src": "小浣熊",
        "title": str(row.get("title") or "(无标题)"),
        "ws": _norm_ws(row.get("workspace_dir")),
        "s": _disp(start),
        "e": _disp(end),
        "sk": end or start,
        "n": len(records),
        "chars": sum(len(x) for _r, _k, x in records),
        "note": note,
        "msgs": [{"r": r, "t": _disp(k), "k": k, "x": x} for r, k, x in records],
    }


def session_to_md(session: dict) -> str:
    lines = [f"# {session['title']}", "",
             f"- src: {session['src']}  id: {session['id']}  ws: {session['ws'] or '-'}",
             f"- 时间: {session['s']} ~ {session['e']}  记录: {session['n']} 条 / {session['chars']} 字",
             f"- 来源: {session['note']}", ""]
    for message in session["msgs"]:
        lines.extend([f"## {message['r']} @ {message['t']}", "", message["x"], ""])
    return "\n".join(lines)


def merge_export(new_session: dict) -> int:
    """按 id 替换重抓会话；聚合损坏时从当前源重建，不半新半旧。"""
    data = []
    if os.path.isfile(RACCOON_JSON):
        try:
            with open(RACCOON_JSON, encoding="utf-8") as f:
                data = json.load(f)
        except (ValueError, OSError) as exc:
            log(f"⚠ raccoon.json 解析失败({exc})，重建。")
            data = []
    data = [s for s in data if s.get("id") != new_session["id"]]
    data.append(new_session)
    jdump(RACCOON_JSON, data)
    return len(data)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", help="显式指定 local-chat.sqlite3 路径")
    args = parser.parse_args(argv)

    db_path = find_db(args.db)
    os.makedirs(ROOT, exist_ok=True)
    os.makedirs(RAW, exist_ok=True)
    os.makedirs(EXPORT, exist_ok=True)
    cache = os.path.join(REPO_ROOT, "data", "cache")
    os.makedirs(cache, exist_ok=True)
    tmpdir = os.path.join(cache, f"raccoon_{os.getpid()}_{int(time.time())}")
    os.makedirs(tmpdir, exist_ok=False)
    try:
        log(f"读取 {db_path} ...")
        sessions = None
        for attempt in range(1, 4):
            try:
                sessions = load_all(db_path, tmpdir)
                break
            except sqlite3.OperationalError as exc:
                if attempt == 3:
                    raise
                log(f"  读取失败（第 {attempt}/3 次）: {exc}，2s 后重试")
                time.sleep(2)
        for sid, entry in (sessions or {}).items():
            session = convert_session(sid, entry)
            if not session["n"]:
                log(f"  跳过空会话 {sid}")
                continue
            jdump(os.path.join(RAW, sid, "messages.json"), entry)
            with open(os.path.join(EXPORT, f"{sid}.md"), "w", encoding="utf-8") as f:
                f.write(session_to_md(session))
            total = merge_export(session)
            log(f"  {session['title'][:40]} → {session['n']} 条 / {session['chars']} 字"
                f" → raccoon.json（共 {total} 会话）")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    log("完成。上游聚合: " + RACCOON_JSON)
    return 0


if __name__ == "__main__":
    sys.exit(main())
