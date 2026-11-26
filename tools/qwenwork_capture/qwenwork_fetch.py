# -*- coding: utf-8 -*-
"""千问办公（QwenWork 桌面端）本地会话抓取 → 对话总库 add() 同构 JSON。

数据位置（2026-11-26 实测，QwenWork 2.0.0）：
  `~/.qwenwork/.harness/<engine>/<profile>/qwenwork.db`
  本机为 `~/.qwenwork/.harness/flash-cli/qwenwork/qwenwork.db`（SQLite，WAL）。
  schema 为 opencode 变体：
    session(id, title, directory, parent_id, model JSON, time_created ms, ...)
    message(id, session_id, time_created ms, data JSON)   data: {role, modelID,
            providerID, time:{created,completed}, tokens, ...}
    part(id, message_id, time_created ms, data JSON)      data: {type: text|reasoning|
            tool|step-start|step-finish|patch, ...}
  只有 UI 壳（electron-user-data）没有正文；GUI 日志也不含对话。

映射（与码道/TeleAgent 同款口径，①口径 2026-11-18）：
  user.text → user；assistant.text → ai；assistant.reasoning → think；
  tool → 🔧 单行摘要（600 截）+ state.output 全文 → output（20 万帽由 build.add 兜底）。
  step-start/step-finish/patch 跳过。

宿主注入块（user 消息里 `<workspace_context>` / `<system-reminder>` /
`<context_checkpoint>`）抓取侧剥掉，只留用户真正输入；否则 RAG 会被环境说明、
技能清单和迁移 checkpoint 正文灌满，用户提问被淹没。剥完为空的消息（纯提醒）不入库。

模型：assistant 逐条 `data.modelID`（本机 deepseek-flash，真实字段按原值记录，
不进 MODEL_ALIASES）；会话级 model JSON 只作兜底展示，不入模型统计。

落盘（data/qwenwork_capture 已 gitignore）：
  raw/<session_id>/messages.json   原始三表数据
  export/qwenwork.json             扁平聚合（build.py do_qwenwork 唯一入口）
  export/<session_id>.md           逐会话 Markdown

读取：直接以 mode=ro 打开运行中的库（WAL 内容可见，不写应用数据）；被锁时
回退为 db/wal/shm 三文件副本。子代理会话（parent_id 非空）不入库。
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from datetime import datetime
from typing import Dict

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "data", "qwenwork_capture")
RAW = os.path.join(ROOT, "raw")
EXPORT = os.path.join(ROOT, "export")
QWENWORK_JSON = os.path.join(EXPORT, "qwenwork.json")

DEFAULT_GLOB = os.path.join(os.path.expanduser("~"), ".qwenwork", ".harness",
                            "*", "*", "qwenwork.db")
TOOL_RESULT_LIMIT = 600
WS_RE = re.compile(r"[\\/]GithubProject[\\/]([A-Za-z0-9_\-]+)")
# 宿主每轮塞进 user 消息的上下文块：不是用户输入，入库前剥掉
INJECT_BLOCK_RE = re.compile(
    r"<(workspace_context|system-reminder|context_checkpoint|environment_context)"
    r"\b[^>]*>.*?</\1\s*>", re.S | re.I)
# MCP 工具清单是宿主以纯文本拼在 user 消息里的（不在任何标签内），
# 形如「Available MCP servers:」…「Available tools:」+ mcp__ 列表行
MCP_BLOCK_RE = re.compile(
    r"^Available MCP servers:.*?\n(?:mcp__[^\n]*\n)+", re.S | re.M)
NEW_SESSION_TITLE_RE = re.compile(r"^New session\b", re.I)


def log(msg: str) -> None:
    print(msg, flush=True)


def jdump(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def find_dbs() -> list:
    return sorted(glob.glob(DEFAULT_GLOB))


def _open_ro(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect("file:" + path.replace("\\", "/") + "?mode=ro",
                           uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    # 排序临时文件：Git Bash TEMP=/tmp 不存在时 sqlite 会回退到无效路径
    # （2026-11-18 直跑套件实锤），一律内存排序
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _copy_trio(db_path: str, tmpdir: str) -> str:
    """db + wal + shm 整组复制（运行中应用短暂锁库时的回退读法）。"""
    dst = os.path.join(tmpdir, os.path.basename(db_path))
    for suffix in ("", "-wal", "-shm"):
        src = db_path + suffix
        if os.path.isfile(src):
            shutil.copy2(src, dst + suffix)
    return dst


def _connect(db_path: str, tmpdir: str) -> sqlite3.Connection:
    try:
        conn = _open_ro(db_path)
        conn.execute("select 1 from session limit 1").fetchone()
        return conn
    except sqlite3.OperationalError:
        log("  只读连接被拒，回退为文件副本读取")
        return _open_ro(_copy_trio(db_path, tmpdir))


def load_all(db_path: str, tmpdir: str) -> Dict:
    """三表装配：{sid: {'session': row, 'items': [{'msg','ts_ms','parts'}]}}。"""
    conn = _connect(db_path, tmpdir)
    try:
        sessions: Dict = {}
        for s in conn.execute("select id, title, directory, parent_id, model, "
                              "time_created, time_updated from session"):
            sid = str(s["id"])
            if s["parent_id"]:
                continue  # 子代理会话（delegationDepth>0）不单独入库
            sessions[sid] = {"session": dict(s), "items": []}
        msg_index: Dict = {}
        for m in conn.execute("select id, session_id, time_created, data from message "
                              "order by time_created, id"):
            try:
                data = json.loads(m["data"] or "{}")
            except (ValueError, TypeError):
                continue
            item = msg_index.get(str(m["id"]))
            if item is None:
                sid = str(m["session_id"])
                if sid not in sessions:
                    continue
                item = {"msg": data, "ts_ms": m["time_created"], "parts": []}
                sessions[sid]["items"].append(item)
                msg_index[str(m["id"])] = item
        for p in conn.execute("select message_id, data from part "
                              "order by time_created, id"):
            try:
                data = json.loads(p["data"] or "{}")
            except (ValueError, TypeError):
                continue
            item = msg_index.get(str(p["message_id"]))
            if item is not None:
                item["parts"].append(data)
        return {sid: e for sid, e in sessions.items() if e["items"]}
    finally:
        conn.close()


def _norm_ws(directory: str) -> str:
    m = WS_RE.search(str(directory or ""))
    return m.group(1) if m else ""


def clean_user_text(text) -> str:
    """剥掉宿主注入的上下文块，返回用户真正输入（可能为空）。"""
    s = INJECT_BLOCK_RE.sub("", str(text or ""))
    s = MCP_BLOCK_RE.sub("", s)
    s = s.replace("\r\n", "\n").strip()
    return s


def _tool_summary(part: dict) -> str:
    tool = str(part.get("tool") or "tool")
    state = part.get("state") or {}
    inp = state.get("input") or {}
    desc = ""
    if isinstance(inp, dict):
        for k in ("command", "file_path", "path", "query", "pattern", "url",
                  "prompt", "description", "name"):
            if inp.get(k):
                desc = str(inp[k]).splitlines()[0]
                break
    else:
        desc = str(inp)
    desc = " ".join(desc.split())[:TOOL_RESULT_LIMIT]
    status = str(state.get("status") or "")
    suffix = "" if status in ("", "completed") else f" [{status}]"
    return f"🔧 {tool}  {desc}{suffix}"[:TOOL_RESULT_LIMIT + 50]


def msg_to_records(msg: dict, ts_ms, parts: list):
    """一条 message（含 parts）→ [(role, epoch_sec, text)]。"""
    ts = round((ts_ms or 0) / 1000)
    role = str(msg.get("role") or "")
    records = []
    if role == "user":
        texts = [p.get("text") or "" for p in parts if p.get("type") == "text"]
        text = clean_user_text("\n\n".join(t for t in texts if t.strip()))
        if text:
            records.append(("user", ts, text))
    elif role == "assistant":
        for part in parts:
            pt = part.get("type")
            if pt == "reasoning":
                text = str(part.get("text") or "").strip()
                if text:
                    records.append(("think", ts, text))
            elif pt == "text":
                text = str(part.get("text") or "").strip()
                if text:
                    records.append(("ai", ts, text))
            elif pt == "tool":
                s = _tool_summary(part)
                if s.strip():
                    records.append(("tool", ts, s))
                state = part.get("state") or {}
                out = state.get("output")
                if isinstance(out, (dict, list)):
                    out = json.dumps(out, ensure_ascii=False)
                out = str(out or "").strip()
                if out:
                    records.append(("output", ts, out))
            # step-start/step-finish/patch 无正文，跳过
    return records


def _pick_title(session: dict, records: list) -> str:
    """GUI 未命名会话（New session - <时间>）用首条用户输入兜底，便于归档检索。"""
    title = str(session.get("title") or "").strip()
    if title and not NEW_SESSION_TITLE_RE.match(title):
        return title
    first = next((x for r, _k, x in records if r == "user"), "")
    first = " ".join(first.split())
    return first[:60] or title or "(千问办公会话)"


def convert_session(sid: str, entry: dict) -> dict:
    session = entry["session"]
    records = []
    models = []
    for item in entry["items"]:
        records.extend(msg_to_records(item["msg"], item["ts_ms"], item["parts"]))
        if item["msg"].get("role") == "assistant" and item["msg"].get("modelID"):
            models.append(str(item["msg"]["modelID"]))
    model_names = sorted(set(models))
    ts_list = [k for _r, k, _x in records if k]
    st = min(ts_list) if ts_list else 0
    en = max(ts_list) if ts_list else 0

    def disp(k):
        return datetime.fromtimestamp(k).strftime("%Y-%m-%d %H:%M") if k else ""

    note = (f"千问办公本地 SQLite 直读（~/.qwenwork/.harness/*/*/qwenwork.db）"
            f"· {len(entry['items'])} 条消息 / {len(records)} 条入库记录"
            f"· 已剥宿主注入块"
            + (f" · 模型 {'/'.join(model_names[:3])}" if model_names else ""))
    return {
        "id": f"qwenwork:{sid}",
        "src": "千问办公",
        "title": _pick_title(session, records),
        "ws": _norm_ws(session.get("directory")),
        "s": disp(st),
        "e": disp(en),
        "sk": en or st,
        "n": len(records),
        "chars": sum(len(x) for _r, _k, x in records),
        "note": note,
        "msgs": [{"r": r, "t": disp(k), "k": k, "x": x} for r, k, x in records],
    }


def session_to_md(session: dict) -> str:
    lines = [f"# {session['title']}", "",
             f"- src: {session['src']}  id: {session['id']}  ws: {session['ws'] or '-'}",
             f"- 时间: {session['s']} ~ {session['e']}  记录: {session['n']} 条 / {session['chars']} 字",
             f"- 来源: {session['note']}", ""]
    for m in session["msgs"]:
        lines.append(f"## {m['r']} @ {m['t']}")
        lines.append("")
        lines.append(m["x"])
        lines.append("")
    return "\n".join(lines)


def merge_export(new_session: dict) -> int:
    data = []
    if os.path.isfile(QWENWORK_JSON):
        try:
            data = json.load(open(QWENWORK_JSON, encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - 聚合损坏则重建
            log(f"⚠ qwenwork.json 解析失败({exc})，重建。")
            data = []
    data = [s for s in data if s.get("id") != new_session["id"]]
    data.append(new_session)
    jdump(QWENWORK_JSON, data)
    return len(data)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", help="显式指定 qwenwork.db（缺省 glob ~/.qwenwork/.harness）")
    args = ap.parse_args(argv)

    dbs = [args.db] if args.db else find_dbs()
    if not dbs:
        raise SystemExit("未找到 qwenwork.db（~/.qwenwork/.harness/*/*/qwenwork.db）"
                         "——确认千问办公跑过至少一次对话")
    os.makedirs(ROOT, exist_ok=True)
    td = tempfile.mkdtemp(prefix="qwenwork_", dir=ROOT)
    try:
        for db in dbs:
            log(f"读取 {db} ...")
            sessions = None
            for attempt in range(1, 4):
                try:
                    sessions = load_all(db, td)
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
                os.makedirs(os.path.join(RAW, sid), exist_ok=True)
                os.makedirs(EXPORT, exist_ok=True)
                jdump(os.path.join(RAW, sid, "messages.json"),
                      {"session": entry["session"],
                       "messages": [{"msg": it["msg"], "ts_ms": it["ts_ms"],
                                     "parts": it["parts"]} for it in entry["items"]]})
                with open(os.path.join(EXPORT, f"{sid}.md"), "w", encoding="utf-8") as f:
                    f.write(session_to_md(session))
                total = merge_export(session)
                log(f"  {session['title'][:40]} → {session['n']} 条 / {session['chars']} 字"
                    f" → qwenwork.json（共 {total} 会话）")
    finally:
        shutil.rmtree(td, ignore_errors=True)
    log("完成。上游聚合: " + QWENWORK_JSON)
    return 0


if __name__ == "__main__":
    sys.exit(main())
