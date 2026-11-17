# -*- coding: utf-8 -*-
"""Kilo Desktop 会话抓取（本地内核 HTTP API → 对话总库 add() 同构 JSON）。

背景（2026-11-17）：Kilo Desktop（Electron 壳 v0.1.16）的对话正文不在任何本地
文件里——work-sessions.json 只有元数据、session-turn/ 空、Local Storage 只有
UI 状态、HTTP 缓存快照（_tmp_kilo_cache 46MB）里只有 SSE 事件流。正文唯一来源
是内核 `kilo.exe serve --hostname=127.0.0.1 --port=0`（port=0 → 系统分配临时
端口，本机实测 4096）起的本地 HTTP 服务：

  GET /session/<ses_id>/message?limit=N   消息列表（数组，limit 有效）
  GET /session/<ses_id>/model-usage       模型用量（本工具不用）

鉴权（两轮排查的结论，别再走 KILO_SESSION_EXPORT_AUTH_TOKEN 的弯路）：
  - 内核二进制里唯一的 KILO_SESSION_EXPORT_AUTH_TOKEN 是**出站上传**云端
    （supermassive-black-hole.kiloapps.io）用的，与本地 API 无关；
  - 本地 API 是 **HTTP Basic**：用户名固定 `kilo`、密码 = 内核环境变量
    `KILO_SERVER_PASSWORD`。实锤链：app.asar 启动代码把 KILO_SERVER_PASSWORD
    塞进内核环境（`KILO_SERVER_PASSWORD: e`）并自己保存 `{username: "kilo",
    password: n}` —— 客户端就是拿它 Basic 调内核的。实测
    `curl -u kilo:<PW>` 返回 200 / 97KB。
  - 密码获取：内核进程比普通权限高，OpenProcess 被拒 → 用管理员跑
    `tools/kilo_capture/kilo_env_dump.py`（读 PEB 环境块）落
    `data/kilo_capture/kernel_env.json`，本工具按 pid 校验后取用。内核重启
    会换密码与端口，重跑一次 dump 即可。

消息结构（实测实锤，opencode 风格）：
  {info: {role: user|assistant, time: {created, completed}（epoch 毫秒）,
          modelID, providerID, tokens, path: {cwd, root}},
   parts: [{type: "text", text} | {type: "reasoning", text} |
           {type: "tool", tool, state: {status, input: {command, description},
                                        output}} |
           {type: "patch", files: [...]} |
           {type: "step-start"|"step-finish", ...}（结构性，跳过）]}

映射到 add() 契约（r ∈ user/ai/think/tool，t=本地时间串，k=epoch 秒，x=正文）：
  user.text → user；assistant.text → ai；assistant.reasoning → think；
  tool → tool 摘要一行（名称/输入描述/状态/输出头，抓取侧截 600 字，
         build.py 对 tool 角色也截 600，双保险）；patch → tool 摘要（改动文件列表）。
  一条 assistant API 消息可拆多条记录，共享 info.time.created（Trae 同款口径）。

落盘（数据根 data/kilo_capture，gitignore 覆盖不入仓库）：
  raw/<ses_id>/messages.json    原始响应（断点/排查依据）
  export/kilo.json              add() 同构扁平聚合（build.py do_kilo 唯一入口）
  export/<ses_id>.md            逐会话 Markdown（人可读）

增量：kilo.json 按 id（kilo:<ses_id>）合并去重，重跑即刷新对应会话。
前提：内核在跑（Kilo Desktop 打开状态）；内核不在时给出可行动报错。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "data", "kilo_capture")
RAW = os.path.join(ROOT, "raw")
EXPORT = os.path.join(ROOT, "export")
KILO_JSON = os.path.join(EXPORT, "kilo.json")
KERNEL_ENV_JSON = os.path.join(ROOT, "kernel_env.json")

SESSIONS_FILE = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")),
                             "Kilo Desktop", "plugins", "kilo.work-sessions",
                             "work-sessions.json")

FETCH_LIMIT = 10000        # 单次拉取上限（本机两会话 13/550 条，远未触顶）
FETCH_RETRIES = 3          # 瞬时网络错误重试
TOOL_RESULT_LIMIT = 600    # tool 记录抓取侧截断（build.py 侧对 tool 同款 600）
WS_RE = re.compile(r"[\\/]GithubProject[\\/]([A-Za-z0-9_\-]+)")


def log(msg: str) -> None:
    print(msg, flush=True)


def jdump(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def find_kernel():
    """发现内核进程与端口：优先 cmdline 含 serve 的 kilo.exe；失败则反查
    127.0.0.1 监听者名字恰为 kilo.exe 的（注意 kilo-desktop.exe 的名字不等于
    kilo.exe，精确相等才能区分）。

    返回 (pid, port) 或 (None, None)。psutil.net_connections 普通权限可读本机
    TCP 表（实测），无需提权——提权只用于读环境变量里的密码。坑：内核是提权
    进程，process_iter 读它 cmdline 会 AccessDenied 被吞成空 → cmdline 过滤
    常态失效，监听反查是主路径（2026-11-17 实测）。
    """
    try:
        import psutil
    except ImportError:
        return None, None
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        if (proc.info.get("name") or "").lower() != "kilo.exe":
            continue
        if "serve" in " ".join(proc.info.get("cmdline") or []):
            kernel_pid = proc.info.get("pid")
            for conn in psutil.net_connections(kind="inet"):
                if (conn.status == psutil.CONN_LISTEN and conn.pid == kernel_pid
                        and conn.laddr and conn.laddr.ip == "127.0.0.1"):
                    return kernel_pid, conn.laddr.port
            return kernel_pid, None
    for conn in psutil.net_connections(kind="inet"):
        if not (conn.status == psutil.CONN_LISTEN and conn.laddr
                and conn.laddr.ip == "127.0.0.1"):
            continue
        try:
            if (psutil.Process(conn.pid).name() or "").lower() == "kilo.exe":
                return conn.pid, conn.laddr.port
        except Exception:  # noqa: BLE001 - 监听者已退出/权限不足则跳过
            continue
    return None, None


def load_password(explicit: str) -> str:
    """密码解析：--password > 环境变量 > kernel_env.json（校验 pid 未过期）。"""
    if explicit:
        return explicit.strip()
    env_pw = os.environ.get("KILO_SERVER_PASSWORD", "").strip()
    if env_pw:
        return env_pw
    if os.path.isfile(KERNEL_ENV_JSON):
        try:
            data = json.load(open(KERNEL_ENV_JSON, encoding="utf-8"))
            pid, _port = find_kernel()
            if pid and data.get("pid") == pid:
                pw = str((data.get("env") or {}).get("KILO_SERVER_PASSWORD") or "").strip()
                if pw:
                    return pw
            log(f"⚠ kernel_env.json 是旧内核（存 pid={data.get('pid')}，当前 pid={pid}）"
                "——内核重启会换密码，请重跑提权 dump。")
        except Exception as exc:  # noqa: BLE001 - 缓存文件损坏按未提供处理
            log(f"⚠ kernel_env.json 读取失败({exc})，忽略。")
    raise SystemExit(
        "缺少 KILO_SERVER_PASSWORD：内核重启会轮换密码与端口。请以管理员运行：\n"
        "  .venv\\Scripts\\python.exe tools\\kilo_capture\\kilo_env_dump.py\n"
        "（写 data/kilo_capture/kernel_env.json 后本工具自动取用）"
    )


def make_get(auth_header: str, timeout: int = 60):
    """注入式 GET（测试打桩点）：返回解析好的 JSON。"""
    def get_json(url: str):
        req = urllib.request.Request(url, headers={"Authorization": auth_header})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    return get_json


def fetch_session_messages(get_json, base_url: str, ses_id: str, limit: int = FETCH_LIMIT):
    """拉一个会话的全部消息（带重试）。limit 语义为「最多返回 N 条」，
    返回条数 == limit 时视为可能截断并告警（本机 550 条远未触顶）。"""
    url = f"{base_url}/session/{ses_id}/message?limit={limit}"
    last_exc = None
    for attempt in range(1, FETCH_RETRIES + 1):
        try:
            msgs = get_json(url)
            if isinstance(msgs, list) and len(msgs) >= limit:
                log(f"⚠ {ses_id}: 返回 {len(msgs)} 条 == limit，可能截断（加大 limit 重跑）")
            return msgs or []
        except Exception as exc:  # noqa: BLE001 - 瞬时网络错误重试
            last_exc = exc
            log(f"  请求失败（第 {attempt}/{FETCH_RETRIES} 次）: {exc}")
            time.sleep(1.5 * attempt)
    raise SystemExit(f"拉取 {ses_id} 失败（已重试 {FETCH_RETRIES} 次）: {last_exc}")


def _tool_summary(part: dict) -> str:
    """tool part → 一行摘要（抓取侧截 600；原文全量留在 raw/）。"""
    tool = str(part.get("tool") or "tool")
    state = part.get("state") or {}
    status = str(state.get("status") or "?")
    inp = state.get("input") or {}
    desc = str(inp.get("description") or inp.get("command") or inp.get("filePath")
               or inp.get("pattern") or "")
    if isinstance(desc, dict):
        desc = json.dumps(desc, ensure_ascii=False)
    desc = " ".join(str(desc).split())
    output = state.get("output")
    if isinstance(output, (dict, list)):
        output = json.dumps(output, ensure_ascii=False)
    out_head = " ".join(str(output or "").split())[:TOOL_RESULT_LIMIT]
    pieces = [f"[tool] {tool} ({status})"]
    if desc:
        pieces.append(desc[:TOOL_RESULT_LIMIT])
    if out_head:
        pieces.append(f"输出: {out_head}")
    return "\n".join(pieces)[:TOOL_RESULT_LIMIT + 200]


def _patch_summary(part: dict) -> str:
    """patch part → 改动文件摘要（不贴 diff，正文留给 ai/think 记录）。"""
    files = part.get("files") or []
    names = [str(f).replace("\\", "/").rsplit("/", 1)[-1] for f in files][:10]
    more = "" if len(files) <= 10 else f"（另 {len(files) - 10} 个文件）"
    return "[patch] 修改文件: " + ", ".join(names) + more


def msg_to_records(msg: dict):
    """一条 API 消息 → [(role, epoch_sec, text)]；结构性 part 跳过，空记录丢弃。

    一条 assistant 消息可拆多条（think/ai/tool），共享 info.time.created——
    Trae 抓取同款口径（ai/think part 共享时间戳）。
    """
    info = msg.get("info") or {}
    role = str(info.get("role") or "")
    created_ms = ((info.get("time") or {}).get("created")) or 0
    ts = round(created_ms / 1000) if created_ms else 0
    records = []
    if role == "user":
        texts = [p.get("text") or "" for p in msg.get("parts") or [] if p.get("type") == "text"]
        text = "\n\n".join(t for t in texts if t.strip())
        if text.strip():
            records.append(("user", ts, text))
    elif role == "assistant":
        for part in msg.get("parts") or []:
            ptype = part.get("type")
            if ptype == "reasoning":
                text = str(part.get("text") or "").strip()
                if text:
                    records.append(("think", ts, text))
            elif ptype == "text":
                text = str(part.get("text") or "").strip()
                if text:
                    records.append(("ai", ts, text))
            elif ptype == "tool":
                text = _tool_summary(part)
                if text.strip():
                    records.append(("tool", ts, text))
                # 工具返回全文走 output 角色（2026-11-17 与 Claude/WorkBuddy 等
                # ①口径对齐）；add() 侧 20 万字帽兜底，raw/ 留原始响应。
                state = part.get("state") or {}
                out = state.get("output")
                if isinstance(out, (dict, list)):
                    out = json.dumps(out, ensure_ascii=False)
                out = str(out or "").strip()
                if out:
                    records.append(("output", ts, out))
            elif ptype == "patch":
                text = _patch_summary(part)
                if text.strip():
                    records.append(("tool", ts, text))
            # step-start / step-finish / 其他未知类型：结构性，跳过
    return records


def ws_of(msgs) -> str:
    """从消息 path.cwd 提取工作区名（E:/GithubProject/game_auto → game_auto）。"""
    for msg in msgs or []:
        cwd = str(((msg.get("info") or {}).get("path") or {}).get("cwd") or "")
        m = WS_RE.search(cwd)
        if m:
            return m.group(1)
    return ""


def convert_session(ws_entry: dict, msgs) -> dict:
    """work-sessions 条目 + 原始消息 → add() 同构会话 dict。"""
    ses_id = (ws_entry.get("chatIds") or [ws_entry.get("id") or "unknown"])[0]
    records = []
    for msg in msgs or []:
        records.extend(msg_to_records(msg))
    models = sorted({str((m.get("info") or {}).get("modelID") or "")
                     for m in msgs or [] if (m.get("info") or {}).get("role") == "assistant"} - {""})
    ts_list = [k for _r, k, _x in records if k]
    st = min(ts_list) if ts_list else 0
    en = max(ts_list) if ts_list else 0
    disp = lambda k: datetime.fromtimestamp(k).strftime("%Y-%m-%d %H:%M") if k else ""
    n_raw = len(msgs or [])
    note = (f"Kilo Desktop 本地内核 API 直读（Basic kilo:KILO_SERVER_PASSWORD）"
            f"· {n_raw} 条 API 消息 / {len(records)} 条入库记录"
            + (f" · 模型 {'/'.join(models[:3])}" if models else ""))
    return {
        "id": f"kilo:{ses_id}",
        "src": "Kilo",
        "title": str(ws_entry.get("title") or "(无标题)"),
        "ws": ws_of(msgs),
        "s": disp(st),
        "e": disp(en),
        "sk": en or st,
        "n": len(records),
        "chars": sum(len(x) for _r, _k, x in records),
        "note": note,
        "msgs": [{"r": r, "t": disp(k), "k": k, "x": x} for r, k, x in records],
    }


def session_to_md(session: dict) -> str:
    """会话 dict → 人可读 Markdown（正文超长不丢，tool 已是摘要）。"""
    lines = [f"# {session['title']}",
             "",
             f"- src: {session['src']}  id: {session['id']}  ws: {session['ws'] or '-'}",
             f"- 时间: {session['s']} ~ {session['e']}  记录: {session['n']} 条 / {session['chars']} 字",
             f"- 来源: {session['note']}",
             ""]
    for m in session["msgs"]:
        lines.append(f"## {m['r']} @ {m['t']}")
        lines.append("")
        lines.append(m["x"])
        lines.append("")
    return "\n".join(lines)


def merge_export(new_session: dict) -> int:
    """kilo.json 按 id 合并去重（存在即整条替换刷新），返回当前总会话数。"""
    data = []
    if os.path.isfile(KILO_JSON):
        try:
            data = json.load(open(KILO_JSON, encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - 上游聚合损坏则重建
            log(f"⚠ kilo.json 解析失败({exc})，重建。")
            data = []
    data = [s for s in data if s.get("id") != new_session["id"]]
    data.append(new_session)
    jdump(KILO_JSON, data)
    return len(data)


def load_work_sessions(path: str = SESSIONS_FILE):
    """work-sessions.json → [(chat_id, ws_entry)]。元数据唯一本地来源。"""
    if not os.path.isfile(path):
        raise SystemExit(f"未找到工作会话元数据: {path}\n（确认 Kilo Desktop 安装并登录过）")
    data = json.load(open(path, encoding="utf-8"))
    out = []
    for ws_entry in data.get("workSessions") or []:
        for chat_id in ws_entry.get("chatIds") or []:
            out.append((chat_id, ws_entry))
    if not out:
        raise SystemExit("work-sessions.json 里没有会话（chatIds 空）。")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--password", help="KILO_SERVER_PASSWORD（缺省走环境变量/kernel_env.json）")
    ap.add_argument("--session", help="只抓指定 chat session id（缺省全部）")
    ap.add_argument("--limit", type=int, default=FETCH_LIMIT, help="单会话拉取条数上限")
    args = ap.parse_args(argv)

    pid, port = find_kernel()
    if not pid or not port:
        raise SystemExit("未发现 kilo.exe 内核（serve 模式）——先打开 Kilo Desktop。")
    password = load_password(args.password)
    auth = "Basic " + base64.b64encode(f"kilo:{password}".encode()).decode()
    get_json = make_get(auth)
    base_url = f"http://127.0.0.1:{port}"
    log(f"内核 pid={pid} port={port}")

    targets = load_work_sessions()
    if args.session:
        targets = [(sid, ws) for sid, ws in targets if sid == args.session]
        if not targets:
            raise SystemExit(f"work-sessions.json 里没有 session {args.session}")

    for sid, ws_entry in targets:
        log(f"拉取 {ws_entry.get('title') or sid} ({sid}) ...")
        msgs = fetch_session_messages(get_json, base_url, sid, args.limit)
        os.makedirs(os.path.join(RAW, sid), exist_ok=True)
        os.makedirs(EXPORT, exist_ok=True)
        jdump(os.path.join(RAW, sid, "messages.json"), msgs)
        session = convert_session(ws_entry, msgs)
        jdump(os.path.join(RAW, sid, "session.json"), session)
        with open(os.path.join(EXPORT, f"{sid}.md"), "w", encoding="utf-8") as f:
            f.write(session_to_md(session))
        total = merge_export(session)
        log(f"  {session['n']} 条记录 / {session['chars']} 字 → kilo.json（共 {total} 会话）")
    log("完成。上游聚合: " + KILO_JSON)
    return 0


if __name__ == "__main__":
    sys.exit(main())
