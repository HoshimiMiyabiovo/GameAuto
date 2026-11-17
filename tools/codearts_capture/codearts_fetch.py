# -*- coding: utf-8 -*-
"""码道（CodeArts Agent）本地会话抓取 → 对话总库 add() 同构 JSON。

背景（2026-11-18）：码道桌面端是 VSCode fork（codearts-agent.exe），chatSessions
jsonl 只有用户请求骨架（response 全空、27 条 Interactive Simulation 空壳请求），
**对话正文在内核数据目录 `~/.codeartsdoer/codearts-data/opencode.db`**（内核
AgentKernel 是 opencode 变体，数据与 TeleAgent 的 teleagent.db 同款三表 schema：
session/message/part + data JSON，非加密，backup API 只读复制即可取）。
排查弯路记录：workspaceStorage 的 chatSessions/state.vscdb/chatEditingSessions
（106MB 纯文件快照）/exthost 日志都没有响应正文——别再挖那几个。

表结构（实测 v3.0.1）：
  session(id, title, directory, time_created epoch ms, parent_id, ...)
  message(id, session_id, time_created, data)      data: {role, modelID,
          providerID, time:{created,completed}, ...}（opencode 风格，assistant
          带 modelID/providerID/cost/tokens）
  part(id, message_id, data)                        data: {type: text|reasoning|
          tool|step-start|step-finish, ...}，tool 的 state.output 是完整返回

映射到 add() 契约（与 Kilo 同款口径，2026-11-18）：
  user.text → user；assistant.text → ai；assistant.reasoning → think；
  tool → 🔧 单行摘要（600 截）+ state.output 全文走 output 角色（20 万帽兜底）；
  step-start/step-finish 跳过。一条 assistant 消息拆多条共享 time_created。

模型：本机实测全部 assistant 消息 modelID=`deepseek-v4.1-flash-1017`（华为云
路由名；有真实字段按原值记录，不进 MODEL_ALIASES）。

落盘（数据根 data/teleagent_capture，gitignore 覆盖不入仓库）：
  raw/<session_id>/messages.json   原始三表数据（断点/排查）
  export/codearts.json             add() 同构扁平聚合（build.py do_codearts 唯一入口）
  export/<session_id>.md           逐会话 Markdown（人可读）

增量：codearts.json 按 id 合并去重，重跑即刷新。DB 被内核占用时用 SQLite
backup API 复制到临时文件再读（catpaw 同款），不锁应用。
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
    os.path.abspath(__file__)))), "data", "codearts_capture")
RAW = os.path.join(ROOT, "raw")
EXPORT = os.path.join(ROOT, "export")
CODEARTS_JSON = os.path.join(EXPORT, "codearts.json")

DEFAULT_GLOB = os.path.join(os.path.expanduser("~"), ".codeartsdoer",
                            "codearts-data", "opencode.db")
TOOL_RESULT_LIMIT = 600
WS_RE = re.compile(r"[\\/]GithubProject[\\/]([A-Za-z0-9_\-]+)")


def log(msg: str) -> None:
    print(msg, flush=True)


def jdump(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def find_dbs() -> list:
    """所有用户的 teleagent.db（glob 支持多用户目录）。"""
    return sorted(glob.glob(DEFAULT_GLOB))


def open_readonly_copy(db_path: str, tmpdir: str) -> str:
    """DB 被运行中应用占用 → SQLite backup API 复制到临时文件再读（catpaw 同款）。"""
    dst = os.path.join(tmpdir, os.path.basename(db_path))
    src = sqlite3.connect(f"file:{db_path.replace(chr(92), '/')}?mode=ro", uri=True)
    dst_c = sqlite3.connect(dst)
    src.backup(dst_c)
    dst_c.close()
    src.close()
    return dst


def load_all(db_path: str, tmpdir: str) -> Dict:
    """三表按 message_id 装配：{sid: {'session':…, 'items': [{'msg','ts_ms','parts'}]}}。

    排序按 time_created（message/part 表没有 sequence 列，2026-11-18 实测）；
    连接必须 try/finally 关——异常路径漏关会让临时目录 rmtree 报 WinError 32。
    """
    path = open_readonly_copy(db_path, tmpdir)
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    # order by 需要 sqlite 排序临时文件：本机 Git Bash TEMP=/tmp 不存在时
    # sqlite 的 GetTempPath 回退到无效路径，排序查询报 unable to open
    # （2026-11-18 实锤；11-16 conftest 的 TEMP 回退坑同源）。改内存排序。
    c.execute("PRAGMA temp_store = MEMORY")
    try:
        sessions: Dict = {}
        msg_index: Dict = {}
        for s in c.execute("select id, title, directory, time_created, time_updated from session"):
            sid = str(s["id"])
            sessions[sid] = {"session": dict(s), "items": []}
        for m in c.execute("select id, session_id, time_created, data from message "
                           "order by time_created"):
            try:
                data = json.loads(m["data"] or "{}")
            except (ValueError, TypeError):
                continue
            sid = str(m["session_id"])
            if sid not in sessions:
                continue
            item = {"msg": data, "ts_ms": m["time_created"], "parts": []}
            sessions[sid]["items"].append(item)
            msg_index[str(m["id"])] = item
        for p in c.execute("select message_id, data from part order by time_created"):
            try:
                data = json.loads(p["data"] or "{}")
            except (ValueError, TypeError):
                continue
            item = msg_index.get(str(p["message_id"]))
            if item is not None:
                item["parts"].append(data)
        return sessions
    finally:
        c.close()


def _norm_ws(directory: str) -> str:
    m = WS_RE.search(str(directory or ""))
    return m.group(1) if m else ""


def _tool_summary(part: dict) -> str:
    tool = str(part.get("tool") or "tool")
    state = part.get("state") or {}
    inp = state.get("input") or {}
    if isinstance(inp, dict):
        desc = ""
        for k in ("command", "description", "file_path", "query", "pattern", "url", "path", "name"):
            if inp.get(k):
                desc = str(inp[k]).splitlines()[0]
                break
    else:
        desc = str(inp)
    desc = " ".join(desc.split())[:TOOL_RESULT_LIMIT]
    return f"🔧 {tool}  {desc}"[:TOOL_RESULT_LIMIT + 50]


def msg_to_records(msg: dict, ts_ms, parts: list):
    """一条 message（含 parts）→ [(role, epoch_sec, text)]（Kilo 同款映射）。"""
    ts = round((ts_ms or 0) / 1000)
    role = str(msg.get("role") or "")
    records = []
    if role == "user":
        texts = [p.get("text") or "" for p in parts if p.get("type") == "text"]
        text = "\n\n".join(x for x in texts if x.strip())
        if text.strip():
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
                # 工具返回全文走 output 角色（2026-11-17 与①口径对齐），
                # add() 侧 20 万字帽兜底，完整 part 留 raw/。
                state = part.get("state") or {}
                out = state.get("output")
                if isinstance(out, (dict, list)):
                    out = json.dumps(out, ensure_ascii=False)
                out = str(out or "").strip()
                if out:
                    records.append(("output", ts, out))
            # step-start/step-finish 跳过
    return records


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

    note = (f"码道内核本地 SQLite 直读（~/.codeartsdoer/codearts-data/opencode.db）"
            f"· {len(entry['items'])} 条消息 / {len(records)} 条入库记录"
            + (f" · 模型 {'/'.join(model_names[:3])}" if model_names else ""))
    return {
        "id": f"codearts:{sid}",
        "src": "码道",
        "title": str(session.get("title") or "(无标题)"),
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
    if os.path.isfile(CODEARTS_JSON):
        try:
            data = json.load(open(CODEARTS_JSON, encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - 聚合损坏则重建
            log(f"⚠ codearts.json 解析失败({exc})，重建。")
            data = []
    data = [s for s in data if s.get("id") != new_session["id"]]
    data.append(new_session)
    jdump(CODEARTS_JSON, data)
    return len(data)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", help="显式指定 teleagent.db 路径（缺省 glob 全用户）")
    args = ap.parse_args(argv)

    dbs = [args.db] if args.db else find_dbs()
    if not dbs:
        raise SystemExit("未找到 opencode.db（~/.codeartsdoer/codearts-data/opencode.db）"
                         "——确认码道 Agent 内核跑过（该库由 AgentKernel 创建）")
    # 显式落在数据根下：Git Bash 的 TEMP=/tmp 不存在时 tempfile 会回退 cwd
    # （2026-11-16 conftest 同款坑），且异常路径的句柄残留不该崩主流程
    os.makedirs(ROOT, exist_ok=True)
    td = tempfile.mkdtemp(prefix="teleagent_", dir=ROOT)
    try:
        for db in dbs:
            user = os.path.basename(os.path.dirname(os.path.dirname(os.path.abspath(db))))
            log(f"读取 {db} (用户 {user}) ...")
            # 杀软实时扫描可能短暂锁住新复制的临时库（2026-11-18 实测：
            # 同一连接前一条查询成功、下一条报 unable to open），带间隔重试
            sessions = None
            for attempt in range(1, 4):
                try:
                    sessions = load_all(db, td)
                    break
                except sqlite3.OperationalError as exc:
                    if attempt == 3:
                        raise
                    log(f"  读取失败（第 {attempt}/3 次）: {exc}，2s 后重拷重试")
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
                    f" → codearts.json（共 {total} 会话）")
    finally:
        shutil.rmtree(td, ignore_errors=True)
    log("完成。上游聚合: " + CODEARTS_JSON)
    return 0


if __name__ == "__main__":
    sys.exit(main())
