# -*- coding: utf-8 -*-
"""Trae CN 会话抓取（官方分享链接 → 对话总库 add() 同构 JSON）。

背景（2026-11-17）：Trae 本地把对话正文存在 SQLCipher 加密的
`%APPDATA%/Trae CN/ModularData/ai-agent/database.db`（密钥在 Rust 原生模块
ai_agent.dll 运行时传参，JS/环境变量/配置文件都拿不到），日志与 leveldb 均
无正文缓存——本地路线全断。唯一干净来源是 **官方分享页**：用户在 Trae 里对
会话点「分享」得到 `https://share.traecontent.cn/share/<ID>` 链接，该页的
两个同源接口 **公开无鉴权**（curl 实测 200）：

  GET https://share.traecontent.cn/api/remote/v1/share/<ID>            会话元数据
  GET https://share.traecontent.cn/api/remote/v1/share/<ID>/messages   消息列表

接口发现方式：浏览器打开分享页后 performance.getEntriesByType("resource")
里直接看到上述 URL；接口名与 share.getMessages 的契约见 Trae 安装目录
`resources/app/out` 前端 bundle（`share.getMessages: GET /api/remote/v1/share/
:share_session_id/messages`）。

消息结构（实测实锤）：
  user      content = JSON 字符串 -> [{type:"text", text_content}, ...]
  assistant content = JSON 字符串 -> {"messages":[{plan_item:{reasoning_content,
             tool_call_info:{name, params}}}], "task_id"}
             最终回答 markdown 在 tool_call_info.name=="finish" 的
             params.summary；其余 plan_item 是中间工具调用/推理。
  assistant 另有 chat_start_time/chat_end_time（epoch 毫秒）与 references
  （文件引用）。

落盘（数据根 data/trae_capture，gitignore 覆盖不入仓库）：
  raw/<share_id>/meta.json      会话元数据（导出脚本的断点续抓依据）
  raw/<share_id>/messages.json  原始响应（含分页合并结果）
  export/trae.json              add() 同构扁平聚合（汇总侧唯一入口）
  export/<share_id>.md          逐会话 Markdown（人可读，正文超长不丢）

增量：已抓过的 share_id 跳过（--force 覆盖）；trae.json 按 id 合并去重，
所以清单里可以一直堆链接，重跑不重复入库。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

# 数据根放仓库 data/（01-workflow：运行时数据统一 data/，gitignore 覆盖）。
# 注意：本机实测 python 对 F:\data 的任何写入都会被拒（bash mkdir 反而能过，
# 疑似安全软件按进程拦截），所以 grok_capture 那套 F:\data 根在这里行不通，
# Trae 没有浏览器 profile 需求，放 data/ 没有副作用。
ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "data", "trae_capture")
RAW = os.path.join(ROOT, "raw")
EXPORT = os.path.join(ROOT, "export")
TRAE_JSON = os.path.join(EXPORT, "trae.json")

BASE = "https://share.traecontent.cn/api/remote/v1/share"
LINKS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "share_links.txt")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
PAGE_SIZE = 100
MAX_PAGES = 50            # 分页兜底上限（100 条/页 * 50 = 5000 条消息，远超正常会话）
REQUEST_INTERVAL = 0.5    # 请求间隔：公开接口也要讲礼貌


def log(msg: str) -> None:
    print(msg, flush=True)


def jdump(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def extract_share_id(token: str) -> str:
    """从链接或裸 ID 提取 share_session_id（大写字母数字，可含连字符）。

    ID 字符集实测不只 [A-Z0-9]：2026-11-17 出现过 `A1H-90LCUV-ZCE` 这种带
    连字符的形态，字符类必须含 `-`，否则链接在第一段就被截断解析失败。
    """
    t = token.strip()
    m = re.search(r"/share/([A-Za-z0-9\-]{6,})", t)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9\-]{6,}", t):
        return t
    raise ValueError(f"无法从 {token!r} 提取分享 ID")


def load_links(path: str) -> list:
    ids, seen = [], set()
    if not os.path.exists(path):
        return ids
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                sid = extract_share_id(line)
            except ValueError as exc:
                log(f"[跳过] {exc}")
                continue
            if sid not in seen:
                seen.add(sid)
                ids.append(sid)
    return ids


def http_get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if not isinstance(payload, dict) or payload.get("code") != 0:
        raise RuntimeError(f"接口返回异常: {url} -> {str(payload)[:200]}")
    return payload.get("data") or {}


def fetch_messages(share_id: str) -> list:
    """拉全量消息；page_size=100 起步，has_more 时轮换分页策略。

    分页参数官方未文档化：先按游标语义试 cursor=<最后一条 message_id>，
    4xx 再退回 page=<页号>；两种都压不住 has_more 就截断并告警
    （raw 里保留已拿到的部分，宁可半份也不静默丢页）。
    """
    items = []
    cursor = None
    page = 1
    for _ in range(MAX_PAGES):
        url = f"{BASE}/{share_id}/messages?page_size={PAGE_SIZE}"
        if cursor:
            url += f"&cursor={urllib.request.quote(str(cursor))}"
        elif page > 1:
            url += f"&page={page}"
        data = http_get_json(url)
        batch = data.get("items") or []
        items.extend(batch)
        if not data.get("has_more") or not batch:
            break
        cursor = batch[-1].get("message_id")
        page += 1
        time.sleep(REQUEST_INTERVAL)
    else:
        log(f"[警告] {share_id} 达到分页上限 {MAX_PAGES} 页且仍有 has_more，结果被截断")
    return items


# ── 解析（纯函数，回归套件直接锁这里）────────────────────────────────

SHARE_ARTIFACT_RE = re.compile(r"\[([^\]]*)\]\(share-artifact://[^)]*\)")
TEXT_SEGMENT_KEYS = ("text_content", "text", "content")


def clean_share_artifact_links(text: str) -> str:
    """分享上下文的本地文件链接（share-artifact://...）在总库里是死链，
    只保留方括号里的文件名，避免入库正文满是无效 markdown 链接。"""
    return SHARE_ARTIFACT_RE.sub(lambda m: m.group(1) or "", text or "")


def parse_content_segments(content_str: str) -> str:
    """user 消息 content：JSON 字符串 -> [{type:"text", text_content}] -> 拼接文本。"""
    if not content_str:
        return ""
    try:
        segments = json.loads(content_str)
    except (ValueError, TypeError):
        return str(content_str)
    if isinstance(segments, dict):
        segments = segments.get("segments") or [segments]
    if not isinstance(segments, list):
        return str(segments)
    parts = []
    for seg in segments:
        if isinstance(seg, str):
            parts.append(seg)
            continue
        if not isinstance(seg, dict):
            continue
        stype = str(seg.get("type") or "").lower()
        if stype and stype != "text":
            # 图片/文件等非文本段：占位说明，不静默丢
            name = str(seg.get("file_name") or seg.get("name") or "")
            parts.append(f"[附件:{stype}{(' ' + name) if name else ''}]")
            continue
        for key in TEXT_SEGMENT_KEYS:
            v = seg.get(key)
            if isinstance(v, str) and v.strip():
                parts.append(v)
                break
    return "\n".join(p for p in parts if p.strip())


def parse_assistant_message(msg: dict) -> tuple:
    """assistant 消息 -> (思考文本, 最终回答 markdown)。

    最终回答在 content.messages 里 tool_call_info.name=="finish" 的
    params.summary；思考取该 finish 项的 reasoning_content（中间工具调用
    的逐段推理不入库——数据量大且检索价值低，原文留在 raw JSON）。
    没有 finish 项（任务中断等）时退化为拼接 thought 字段。
    """
    content = msg.get("content") or ""
    try:
        obj = json.loads(content) if isinstance(content, str) else content
    except (ValueError, TypeError):
        return "", str(content)[:4000]
    if not isinstance(obj, dict):
        return "", str(obj)[:4000]
    plan_items = []
    for m in obj.get("messages") or []:
        pi = (m or {}).get("plan_item") or {}
        if isinstance(pi, dict):
            plan_items.append(pi)
    think_text, answer = "", ""
    for pi in plan_items:
        tool = pi.get("tool_call_info") or {}
        if isinstance(tool, dict) and str(tool.get("name") or "").lower() == "finish":
            params = tool.get("params") or {}
            answer = str(params.get("summary") or "") if isinstance(params, dict) else ""
            think_text = str(pi.get("reasoning_content") or "")
            break
    if not answer:
        thoughts = [str(pi.get("thought") or "") for pi in plan_items]
        answer = "\n".join(t for t in thoughts if t.strip())
    return think_text.strip(), clean_share_artifact_links(answer).strip()


def iso_to_epoch(value: str) -> int:
    try:
        dt = datetime.fromisoformat(str(value).strip())
        return int(dt.timestamp())
    except (ValueError, TypeError):
        return 0


def ms_to_epoch(value) -> int:
    try:
        return int(int(value) // 1000)
    except (TypeError, ValueError):
        return 0


def disp(sec: int) -> str:
    if not sec:
        return ""
    return datetime.fromtimestamp(sec, timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")


WS_PATH_RE = re.compile(r"[A-Za-z]:[\\\\/]+GithubProject[\\\\/]+([A-Za-z0-9_\-]+)")


def infer_workspace(messages: list) -> str:
    """从会话里出现过的文件路径推断工作区名（RAG 双门控靠 ws 命中加分）。

    分享页没有工作区字段，但 assistant 的 references（文件引用）与正文里常带
    真实路径（如 e:\\GithubProject\\game_auto\\AGENTS.md）——提取 GithubProject
    下的目录名即可。不匹配返回空串（对应 2026-11-17 的教训：库街区 token/DID
    调试会话因为 ws 空走纯关键词线被 RAG 漏掉，而它就是 game_auto 的工作）。
    """
    blob = json.dumps(messages, ensure_ascii=False)
    m = WS_PATH_RE.search(blob)
    return m.group(1) if m else ""


def parse_session(meta: dict, messages: list) -> dict:
    """单会话 -> add() 同构扁平结构（与 grok.json 完全同构）。"""
    sid = str(meta.get("share_session_id") or "")
    title = str(meta.get("title") or "(Trae 会话)")
    msgs = []
    for m in messages:
        role = str(m.get("role") or "")
        if role == "user":
            ts = iso_to_epoch(m.get("created_at") or "")
            text = clean_share_artifact_links(parse_content_segments(m.get("content") or ""))
            if text.strip():
                msgs.append(("user", ts, text.strip()))
        elif role == "assistant":
            start_ms = m.get("chat_start_time")
            ts = ms_to_epoch(start_ms) or iso_to_epoch(m.get("created_at") or "")
            think, answer = parse_assistant_message(m)
            if think.strip():
                msgs.append(("think", ts, think.strip()))
            if answer.strip():
                msgs.append(("ai", ts, answer.strip()))
    chars = sum(len(x) for _r, _t, x in msgs)
    stamps = [t for _r, t, _x in msgs if t]
    note = (f"Trae 官方分享页抓取 share.traecontent.cn/share/{sid}"
            + (f" · 分享者 {meta.get('creator_name')}" if meta.get("creator_name") else "")
            + f" · {len(messages)} 条消息")
    return {
        "id": f"trae:{sid}",
        "src": "Trae",
        "title": title,
        "ws": infer_workspace(messages),
        "s": disp(min(stamps)) if stamps else "",
        "e": disp(max(stamps)) if stamps else "",
        "sk": max(stamps) if stamps else 0,
        "n": len(msgs),
        "chars": chars,
        "note": note,
        "msgs": [
            {"r": r, "t": disp(t), "k": int(t or 0), "x": x}
            for r, t, x in msgs
        ],
    }


def session_markdown(session: dict) -> str:
    lines = [f"# {session['title']}", "", f"- 来源: {session['note']}",
             f"- 时间: {session['s']} ~ {session['e']}", ""]
    for m in session["msgs"]:
        who = {"user": "## 用户", "ai": "## TRAE", "think": "## TRAE 思考"}.get(m["r"], f"## {m['r']}")
        lines += [who, "", m["x"], ""]
    return "\n".join(lines)


def merge_export(sessions: list) -> tuple:
    """trae.json 按 id 合并（新数据覆盖旧数据），返回 (合并后列表, 新增数)。"""
    existing = {}
    if os.path.exists(TRAE_JSON):
        try:
            for s in json.load(open(TRAE_JSON, encoding="utf-8")):
                existing[s.get("id")] = s
        except (ValueError, OSError) as exc:
            log(f"[警告] 已有 trae.json 读取失败（将整体重建）: {exc}")
    added = 0
    for s in sessions:
        if s["id"] not in existing:
            added += 1
        existing[s["id"]] = s
    merged = sorted(existing.values(), key=lambda s: -int(s.get("sk") or 0))
    jdump(TRAE_JSON, merged)
    return merged, added


def fetch_one(share_id: str, force: bool) -> dict:
    """抓单个分享：raw 断点续抓，返回解析后的扁平会话（失败返回 None）。"""
    raw_dir = os.path.join(RAW, share_id)
    meta_path = os.path.join(raw_dir, "meta.json")
    msg_path = os.path.join(raw_dir, "messages.json")
    if force or not (os.path.exists(meta_path) and os.path.exists(msg_path)):
        log(f"[抓取] {share_id} ...")
        meta = http_get_json(f"{BASE}/{share_id}")
        time.sleep(REQUEST_INTERVAL)
        messages = fetch_messages(share_id)
        jdump(meta_path, meta)
        jdump(msg_path, {"data": {"items": messages}})
        log(f"[落盘] {share_id}: {len(messages)} 条消息")
    else:
        log(f"[跳过] {share_id}（raw 已存在，--force 覆盖）")
        meta = json.load(open(meta_path, encoding="utf-8"))
        messages = json.load(open(msg_path, encoding="utf-8"))["data"]["items"]
    session = parse_session(meta, messages)
    if not session["msgs"]:
        log(f"[警告] {share_id} 没有解析出任何消息（结构变化？看 raw/{share_id}/messages.json）")
        return None
    jdump(os.path.join(EXPORT, f"{share_id}.md"), session_markdown(session))
    return session


def main() -> int:
    global ROOT, RAW, EXPORT, TRAE_JSON
    ap = argparse.ArgumentParser(description="Trae 分享会话抓取 → 对话总库同构 JSON")
    ap.add_argument("ids", nargs="*", help="分享链接或 ID（留空读 share_links.txt）")
    ap.add_argument("--force", action="store_true", help="忽略 raw 缓存重新抓取")
    ap.add_argument("--root", default="", help="覆盖数据根目录（默认 <仓库>/data/trae_capture）")
    args = ap.parse_args()
    if args.root:
        ROOT = args.root
        RAW = os.path.join(ROOT, "raw")
        EXPORT = os.path.join(ROOT, "export")
        TRAE_JSON = os.path.join(EXPORT, "trae.json")

    tokens = list(args.ids) or load_links(LINKS_FILE)
    if not tokens:
        log(f"没有输入：命令行传链接/ID，或在 {LINKS_FILE} 里每行贴一条分享链接")
        return 1

    sessions, failures = [], []
    for token in tokens:
        try:
            sid = extract_share_id(token)
        except ValueError as exc:
            log(f"[跳过] {exc}")
            failures.append(token)
            continue
        try:
            session = fetch_one(sid, args.force)
        except Exception as exc:  # noqa: BLE001 - 单个失败不拖垮整批
            log(f"[失败] {sid}: {exc}")
            failures.append(sid)
            continue
        if session:
            sessions.append(session)

    try:
        merged, added = merge_export(sessions)
    except PermissionError as exc:
        # 本机 python 对 F:\data 写入会被拒（见 ROOT 注释）；data/ 根不该发生，
        # 真遇到说明安全软件按进程拦截——给出明确出路而不是裸 traceback。
        log(f"[失败] 写 {TRAE_JSON} 被拒绝: {exc}")
        log("      换 --root 指到可写目录重跑；raw 已落盘的部分不丢。")
        return 1
    log(f"完成：本次解析 {len(sessions)} 个会话，trae.json 合并后 {len(merged)} 个（新增 {added}）")
    if failures:
        log(f"失败/跳过: {failures}")
        if not sessions:
            return 1  # 一个都没解析出来：让调用方（批处理/CI）看见失败
    return 0


if __name__ == "__main__":
    sys.exit(main())
