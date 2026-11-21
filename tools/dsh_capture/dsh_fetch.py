# -*- coding: utf-8 -*-
"""DSH NEXT（DeepSeek Harness Desktop）本地会话抓取 → 对话总库 add() 同构 JSON。

背景（2026-11-20）：DSH NEXT 是 Electron 桌面客户端（anywhere-labs/
deepseek-harness-desktop），会话正文落在
`~/.dsh/sessions/<workspace-slug>/<session-id>/session.v4.jsonl.zstd`。
**格式坑**：文件是「每行 JSON 单独压成一个 zstd 帧、再把多帧顺序拼接」，不是单条
zstd 流——实测 474KB 的会话含 314 帧，用单帧 API 只能解出第一行。必须用
`backports.zstd.open(path, "rb")` 跨帧流式读（Python 3.13 无 stdlib compression.zstd，
3.14 才内置，故走 backports）。

同目录下的 `session.v4.jsonl` 只有会话头（127 字节），别读它。元数据投影在
`~/.dsh/storages/session_projcache/sessions/<session-id>.json`（title / turnOutline /
sessionStats），可作交叉校验，但**正文只在 zstd 里**，所以以正文为准。

记录类型（实测）：session / user/message / assistant/message（content 含 reasoning、
text、tool-call）/ tool/call / tool/result / turn|step/start|end / session/title /
model/selection / approval/* / job/* / llm/* / todo/write 等。

映射到 add() 契约（与 Kilo / 码道同款口径，2026-11-17 ①口径）：
  user/message 的 text            → user
  assistant/message 的 reasoning  → think
  assistant/message 的 text       → ai
  assistant 的 tool-call          → tool（🔧 单行摘要，600 截）
  tool/result 的正文              → output（add() 侧 20 万帽兜底）
  其余（system/developer/todo/approval/job/step/llm/attempt/request 等）跳过。
  时间取记录的 time（epoch 毫秒），会话头 createdAt 兜底。

模型：assistant/message 的 `source.model` 与 model/selection 的 `data.model` 都是真实
字段，按原值记录（口径铁律 3：有真实字段不臆测映射、不进 MODEL_ALIASES），
原始值随 raw 落盘，model_recover 侧另有 recover_dsh 逐条还原。

落盘（数据根 data/dsh_capture，gitignore 覆盖不入仓库）：
  raw/<session_id>/messages.json   原始记录序列（含模型字段，供 model_recover）
  export/dsh.json                  add() 同构扁平聚合（build.py do_dsh 唯一入口）
  export/<session_id>.md           逐会话 Markdown（人可读）

增量：dsh.json 按 id 合并去重，重跑即刷新。
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from datetime import datetime
from typing import Dict, List, Tuple

try:
    import backports.zstd as zstd
except ImportError:  # pragma: no cover - 运行期由 dependency_manager 兜底提示
    zstd = None

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "data", "dsh_capture")
RAW = os.path.join(ROOT, "raw")
EXPORT = os.path.join(ROOT, "export")
DSH_JSON = os.path.join(EXPORT, "dsh.json")

DEFAULT_SESSIONS_GLOB = os.path.join(os.path.expanduser("~"), ".dsh",
                                     "sessions", "*", "*", "session.v4.jsonl.zstd")
TOOL_RESULT_LIMIT = 600
TITLE_LIMIT = 70
WS_RE = re.compile(r"[\\/]GithubProject[\\/]([A-Za-z0-9_\-]+)")


def log(msg: str) -> None:
    print(msg, flush=True)


def jdump(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def find_sessions(root: str) -> List[str]:
    """所有会话 zstd 路径（glob 支持多 workspace）。"""
    return sorted(glob.glob(os.path.join(root, "*", "*", "session.v4.jsonl.zstd")))


def load_records(path: str) -> List[dict]:
    """跨帧读取会话全部 JSON 行。

    单帧 API 只能拿到第一行（2026-11-20 实锤），必须用 ZstdFile 流式跨帧；
    文件被客户端追加写了一半时按「读到哪里算哪里」处理，不抛断链。
    """
    if zstd is None:
        raise RuntimeError("缺少 backports.zstd（requirements.txt 已含，重装依赖即可）")
    out: List[dict] = []
    try:
        with zstd.open(path, "rb") as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line.decode("utf-8")))
                except (ValueError, UnicodeDecodeError):
                    continue
    except (OSError, zstd.ZstdError):
        # 半截帧/被占用：保留已读到的部分，不整会话丢弃
        pass
    return out


def _norm_ws(cwd: str) -> str:
    """工作区名：GithubProject 下的取仓库名，其余取目录末段（默认 workspace 也有名）。"""
    m = WS_RE.search(str(cwd or ""))
    if m:
        return m.group(1)
    parts = [p for p in re.split(r"[\\/]", str(cwd or "")) if p]
    return parts[-1] if parts else ""


def _tool_summary(name: str, arguments) -> str:
    """工具调用单行摘要：优先取参数里的 command/description 等首个非空字段。"""
    tool = str(name or "tool")
    try:
        inp = json.loads(arguments) if isinstance(arguments, str) else (arguments or {})
    except (ValueError, TypeError):
        inp = {}
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


def _content_text(content) -> str:
    """content 数组里所有 text 块按行拼；非文本块（图片等）忽略。"""
    if not isinstance(content, list):
        return str(content or "")
    return "\n\n".join(str(b.get("text") or "") for b in content
                       if isinstance(b, dict) and b.get("type") == "text").strip()


def convert_session(sid: str, ws_slug: str, records: List[dict]) -> dict:
    """记录序列 → add() 同构会话 dict。

    只接收正文类记录，其余（系统提示、审批、任务、步骤、重试等）不进总库，
    避免把 viewer 撑爆、把 RAG 检索淹没在噪声里。
    """
    header = next((r for r in records if r.get("type") == "session"), {})
    title_rec = next((r for r in records if r.get("type") == "session/title"), {})
    title = str((title_rec.get("data") or {}).get("title") or "").strip()

    records_out: List[Tuple[str, int, str]] = []
    models: List[str] = []
    first_user_text = ""

    # team 模式下用户消息会先以 agent/inbox/spliced 出现、随后再落一条 user/message
    # （同 id）。先收集 user/message 的 id，inbox 里同 id 的跳过，避免重复入库；
    # 只有 inbox、没有 user/message 的会话（纯 team 会话）靠 inbox 兜底。
    user_ids = {str((r.get("data") or {}).get("id") or "")
                for r in records if r.get("type") == "user/message"}
    user_ids.discard("")

    for rec in records:
        rtype = rec.get("type")
        ts = rec.get("time") or 0
        data = rec.get("data") or {}
        if rtype == "user/message":
            text = _content_text(data.get("content"))
            if text:
                records_out.append(("user", ts, text))
                if not first_user_text:
                    first_user_text = text
        elif rtype == "assistant/message":
            msg = data.get("message") or {}
            src = msg.get("source") or {}
            if src.get("model"):
                models.append(str(src["model"]))
            for block in msg.get("content") or []:
                if not isinstance(block, dict):
                    continue
                bt = block.get("type")
                if bt == "reasoning":
                    text = str(block.get("text") or "").strip()
                    if text:
                        records_out.append(("think", ts, text))
                elif bt == "text":
                    text = str(block.get("text") or "").strip()
                    if text:
                        records_out.append(("ai", ts, text))
                elif bt == "tool-call":
                    s = _tool_summary(block.get("name"), block.get("arguments"))
                    if s.strip():
                        records_out.append(("tool", ts, s))
        elif rtype == "tool/result":
            text = _content_text((data.get("message") or {}).get("content"))
            if text:
                records_out.append(("output", ts, text))
        elif rtype == "agent/inbox/spliced":
            for item in data.get("inserted") or []:
                if not isinstance(item, dict) or item.get("role") != "user":
                    continue
                if str(item.get("id") or "") in user_ids:
                    continue
                text = _content_text(item.get("content"))
                if text:
                    records_out.append(("user", ts, text))
                    if not first_user_text:
                        first_user_text = text
        elif rtype == "model/selection":
            if data.get("model"):
                models.append(str(data["model"]))
        # 其余类型一律跳过

    if not title:
        title = " ".join(first_user_text.split())[:TITLE_LIMIT]
    model_names = sorted(set(models))
    ts_list = [k for _r, k, _x in records_out if k]
    st = min(ts_list) if ts_list else (header.get("createdAt") or 0)
    en = max(ts_list) if ts_list else st

    def disp(k):
        return datetime.fromtimestamp(k / 1000).strftime("%Y-%m-%d %H:%M") if k else ""

    note = (f"DSH 本地 zstd 会话直读（~/.dsh/sessions）· {len(records_out)} 条入库记录"
            + (f" · 模型 {'/'.join(model_names[:3])}" if model_names else ""))
    return {
        "id": f"dsh:{sid}",
        "src": "DSH",
        "title": title or "(无标题)",
        "ws": _norm_ws(header.get("cwd") or ws_slug),
        "s": disp(st),
        "e": disp(en),
        "sk": en or st,
        "n": len(records_out),
        "chars": sum(len(x) for _r, _k, x in records_out),
        "note": note,
        "msgs": [{"r": r, "t": disp(k), "k": k, "x": x} for r, k, x in records_out],
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
    if os.path.isfile(DSH_JSON):
        try:
            data = json.load(open(DSH_JSON, encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - 聚合损坏则重建
            log(f"⚠ dsh.json 解析失败({exc})，重建。")
            data = []
    data = [s for s in data if s.get("id") != new_session["id"]]
    data.append(new_session)
    jdump(DSH_JSON, data)
    return len(data)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sessions-root", help="会话根目录（缺省 ~/.dsh/sessions，测试用）")
    ap.add_argument("--out-root", help="导出根目录（缺省 data/dsh_capture，测试用）")
    args = ap.parse_args(argv)

    global ROOT, RAW, EXPORT, DSH_JSON
    if args.out_root:
        ROOT = args.out_root
        RAW = os.path.join(ROOT, "raw")
        EXPORT = os.path.join(ROOT, "export")
        DSH_JSON = os.path.join(EXPORT, "dsh.json")

    root = args.sessions_root or os.path.join(os.path.expanduser("~"), ".dsh", "sessions")
    files = find_sessions(root)
    if not files:
        raise SystemExit(f"未找到 DSH 会话（{root}）——确认 DSH NEXT 跑过且产生过会话")
    os.makedirs(EXPORT, exist_ok=True)

    total = 0
    for path in files:
        sid = os.path.basename(os.path.dirname(path))
        ws_slug = os.path.basename(os.path.dirname(os.path.dirname(path)))
        records = load_records(path)
        if not records:
            log(f"  跳过空会话 {sid}")
            continue
        session = convert_session(sid, ws_slug, records)
        if not session["n"]:
            log(f"  跳过无正文会话 {sid}")
            continue
        os.makedirs(os.path.join(RAW, sid), exist_ok=True)
        jdump(os.path.join(RAW, sid, "messages.json"),
              {"session": next((r for r in records if r.get("type") == "session"), {}),
               "records": records})
        with open(os.path.join(EXPORT, f"{sid}.md"), "w", encoding="utf-8") as f:
            f.write(session_to_md(session))
        total = merge_export(session)
        log(f"  {session['title'][:40]} → {session['n']} 条 / {session['chars']} 字"
            f" → dsh.json（共 {total} 会话）")
    log("完成。上游聚合: " + DSH_JSON)
    return 0


if __name__ == "__main__":
    sys.exit(main())
