# -*- coding: utf-8 -*-
"""把 raw/grok 下的原始响应导出成 Markdown + 结构化 JSON。

输入：raw/grok/catalog.json（grok_fetch.py 产出，含分组与逐会话索引）
输出（F:\\data\\grok_capture\\export）：
  <项目>/<日期>_<标题>.md     每会话一篇，正文逐字保留（不改写、不转义代码块）
  grok.json                   聚合库，字段与 F:\\data\\_ai_organize_tools\\build.py
                              的 add() 一致（id/src/title/ws/s/e/sk/n/chars/note/msgs），
                              可直接被那边的 do_grok() 读取并入总库
  index.md                    项目 × 会话清单

只读 raw 数据，可反复重跑（覆盖写）。
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

RAW_GROK = r"F:\data\grok_capture\raw\grok"
OUT = r"F:\data\grok_capture\export"
CST = timezone(timedelta(hours=8))
PERSONAL = "_personal"

ROLE = {"HUMAN": "用户", "ASSISTANT": "Grok", "SYSTEM": "系统", "TOOL": "工具"}


def jload(path: str):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def esc(s: str) -> str:
    """文件名安全化（保留中文，只干掉 Windows 非法字符）。"""
    s = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", s or "").strip()
    return re.sub(r"\s+", " ", s)[:60] or "untitled"


def iso(s: str) -> float:
    if not s:
        return 0.0
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def fmt(sec: float) -> str:
    if not sec:
        return "时间未知"
    return datetime.fromtimestamp(sec, CST).strftime("%Y-%m-%d %H:%M:%S")


def fmt_short(sec: float) -> str:
    if not sec:
        return "时间未知"
    return datetime.fromtimestamp(sec, CST).strftime("%Y-%m-%d %H:%M")


def asset_urls(r: dict) -> list:
    """收集消息里的附件 URL（生成图 / 上传图 / 文件 / 网页引用）。"""
    urls = []
    for u in r.get("generatedImageUrls") or []:
        if isinstance(u, str):
            urls.append(u)
    for key in ("imageAttachments", "fileAttachments", "fileUris"):
        for it in r.get(key) or []:
            if isinstance(it, str):
                urls.append(it)
            elif isinstance(it, dict):
                for k in ("url", "fileUri", "uri", "downloadUrl"):
                    if it.get(k):
                        urls.append(it[k])
                        break
    out, seen = [], set()
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def msg_dict(r: dict) -> tuple:
    # sender 大小写不统一（同一份数据里 ASSISTANT / assistant 都有），统一转大写再查表
    raw_who = (r.get("sender") or "").upper()
    who = ROLE.get(raw_who, r.get("sender") or "?")
    body = (r.get("message") or "").strip()
    ts = iso(r.get("createTime"))
    urls = asset_urls(r)
    if urls:
        body += ("\n\n" if body else "") + "\n".join(f"- 附件: {u}" for u in urls)
    if not body:
        body = "（本条无正文：可能只有图片/工具结果，原始 JSON 已保留）"
    return who, r.get("model") or "", body, ts


def write_md(path: str, title: str, ws_name: str, cid: str, rows: list, meta: dict) -> None:
    conv = meta.get("conversation") or {}
    t_start = iso(conv.get("createTime")) or (rows[0][3] if rows else 0)
    t_end = iso(conv.get("modifyTime")) or (rows[-1][3] if rows else 0)
    models = sorted({m for who, m, _, _ in rows if m and who != "用户"})
    lines = [f"# {title}", "",
             f"> 项目：{ws_name}　｜　会话 ID：`{cid}`　｜　消息：{len(rows)} 条　｜　"
             f"时间：{fmt(t_start)} → {fmt(t_end)}",
             f"> 模型：{', '.join(models) if models else '未记录'}　｜　"
             f"抓取：{datetime.now(CST):%Y-%m-%d %H:%M}（grok.com 网页接口）", "", "---", ""]
    for i, (who, model, body, ts) in enumerate(rows, 1):
        show_model = model if who != "用户" else ""
        head = f"## {i}. {who}" + (f" · {show_model}" if show_model else "") + f" · {fmt(ts)}"
        lines += [head, "", body.rstrip(), "", "---", ""]
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    os.replace(tmp, path)


def main() -> int:
    cat_p = os.path.join(RAW_GROK, "catalog.json")
    if not os.path.exists(cat_p):
        print("没有 catalog.json，先跑 grok_fetch.py")
        return 1
    cat = jload(cat_p)
    os.makedirs(OUT, exist_ok=True)

    library, index_rows = [], []
    used_names: dict = {}
    for g in cat.get("groups") or []:
        key = g.get("workspaceId") or PERSONAL
        ws_name = g.get("workspaceName") or key
        ws_dir = os.path.join(OUT, esc(ws_name))
        for c in g.get("conversations") or []:
            cid = c["conversationId"]
            d = os.path.join(RAW_GROK, key, cid)
            meta_p, resp_p = os.path.join(d, "meta.json"), os.path.join(d, "responses.json")
            if not (os.path.exists(meta_p) and os.path.exists(resp_p)):
                print(f"  [跳过] 缺 raw: {cid[:8]} {c.get('title', '')[:30]}")
                continue
            meta = jload(meta_p)
            resp = jload(resp_p)
            rows = [msg_dict(r) for r in (resp.get("responses") or [])]
            conv = meta.get("conversation") or {}
            title = conv.get("title") or c.get("title") or cid[:8]
            t_start = iso(conv.get("createTime")) or (rows[0][3] if rows else 0)
            day = datetime.fromtimestamp(t_start, CST).strftime("%Y%m%d") if t_start else "00000000"
            fname = f"{day}_{esc(title)}.md"
            if fname in used_names and used_names[fname] != cid:
                fname = f"{day}_{esc(title)}_{cid[:8]}.md"
            used_names[fname] = cid
            os.makedirs(ws_dir, exist_ok=True)
            write_md(os.path.join(ws_dir, fname), title, ws_name, cid, rows, meta)

            t_end = iso(conv.get("modifyTime")) or (rows[-1][3] if rows else 0)
            items = [{"r": "user" if who == "用户" else "ai",
                      "t": fmt_short(ts) if ts else "", "k": round(ts or 0), "x": body}
                     for who, _m, body, ts in rows]
            library.append({
                "id": "grok:" + cid[:8], "src": "Grok", "title": title, "ws": ws_name,
                "s": fmt_short(t_start), "e": fmt_short(t_end), "sk": round(t_end or t_start),
                "n": len(items), "chars": sum(len(i["x"]) for i in items),
                "note": f"grok.com 网页抓取（{ws_name}）",
                "msgs": items,
            })
            index_rows.append((ws_name, title, len(items),
                               sum(len(i["x"]) for i in items), fname, cid, t_start))
            print(f"  [ok] {ws_name}/{fname}  {len(items)} 条")

    lib_p = os.path.join(OUT, "grok.json")
    with open(lib_p, "w", encoding="utf-8") as f:
        json.dump(library, f, ensure_ascii=False, indent=1)

    order, by_ws = [], {}
    for row in index_rows:
        ws_name = row[0]
        if ws_name not in by_ws:
            by_ws[ws_name] = []
            order.append(ws_name)
        by_ws[ws_name].append(row)

    lines = ["# Grok 会话导出", "",
             f"生成时间：{datetime.now(CST):%Y-%m-%d %H:%M}　｜　会话 {len(index_rows)} 个　｜　"
             f"消息 {sum(r[2] for r in index_rows)} 条　｜　"
             f"字符 {sum(r[3] for r in index_rows):,}", ""]
    for ws_name in order:
        rows = sorted(by_ws[ws_name], key=lambda r: r[6] or 0, reverse=True)
        lines += [f"## {ws_name}（{len(rows)} 条）", "",
                  "| 标题 | 消息 | 字符 | 文件 |", "|---|---:|---:|---|"]
        for _ws, title, n, chars, fname, _cid, _ts in rows:
            lines.append(f"| {title} | {n} | {chars:,} | [{fname}]({esc(ws_name)}/{fname}) |")
        lines.append("")
    with open(os.path.join(OUT, "index.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"\n导出完成 → {OUT}")
    print(f"  会话 {len(library)} 个，总计 {sum(x[2] for x in index_rows)} 条消息，"
          f"聚合库 {os.path.getsize(lib_p) // 1024}KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
