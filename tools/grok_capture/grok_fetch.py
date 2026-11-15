# -*- coding: utf-8 -*-
"""Grok 会话抓取：全局清单 → 逐条 meta + responses，按项目归属落 raw 树。

入口选**全局清单**而不是逐 workspace 枚举：实测 `GET /rest/app-chat/conversations`
（pageSize=60，无 kind/filter）一次返回全部 20 条，含 4 条项目内会话；而
`conversations?workspaceId=` 只报 1 条——有 3 条项目会话挂在已不可见的工作区上，
按 workspace 枚举会漏。归属以 `conversations_v2/<cid>` 的 `workspaces` 为准。

落盘：
  raw/grok/catalog.json                     全局清单 + 分组（导出脚本的唯一入口）
  raw/grok/<workspaceId|_personal>/<cid>/meta.json
  raw/grok/<workspaceId|_personal>/<cid>/responses.json
meta/responses 已存在则跳过（断点续抓），--force 覆盖。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from patchright.sync_api import sync_playwright  # noqa: E402

from grok_cdp import (  # noqa: E402
    CDP, RAW, apply_env, find_grok_page, human_pause, log, new_page, page_json,
)

BASE = "https://grok.com"
GROK_RAW = os.path.join(RAW, "grok")
PAGE_SIZE = 60
PERSONAL = "_personal"
PERSONAL_NAME = "未归档聊天"


def jdump(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def jload(path: str):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def paths(key: str, cid: str) -> tuple:
    d = os.path.join(GROK_RAW, key, cid)
    return os.path.join(d, "meta.json"), os.path.join(d, "responses.json")


def fetch_catalog(page, force: bool) -> dict:
    path = os.path.join(GROK_RAW, "catalog.json")
    if os.path.exists(path) and not force:
        cat = jload(path)
        if cat.get("conversations"):
            log(f"清单命中缓存：{len(cat['conversations'])} 条会话")
            return cat

    convs, token, page_no = [], "", 0
    while page_no < 20:
        page_no += 1
        url = f"{BASE}/rest/app-chat/conversations?pageSize={PAGE_SIZE}"
        if token:
            url += f"&pageToken={token}"
        obj = page_json(page, url)
        got = obj.get("conversations") or []
        convs.extend(got)
        token = obj.get("nextPageToken") or ""
        log(f"  清单第 {page_no} 页 +{len(got)} 条（累计 {len(convs)}）")
        if not token or not got:
            break
        human_pause(2.0, 3.5)

    workspaces = page_json(
        page, f"{BASE}/rest/workspaces?pageSize=50&kind=WORKSPACE_KIND_ALL"
    ).get("workspaces") or []
    return {"workspaces": workspaces, "conversations": convs, "groups": [],
            "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S")}


def known_keys(cat: dict) -> dict:
    """上次运行留下的 cid → 目录 key 映射，用于直接命中缓存。"""
    out = {}
    for g in cat.get("groups") or []:
        for c in g.get("conversations") or []:
            out[c["conversationId"]] = g.get("workspaceId") or PERSONAL
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="忽略缓存重抓")
    ap.add_argument("--pause", type=float, default=2.0, help="会话间隔基线秒（外加抖动）")
    ap.add_argument("--only", action="append", default=[], help="只抓指定会话 id 前缀")
    args = ap.parse_args()

    os.makedirs(GROK_RAW, exist_ok=True)
    t0 = time.time()
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP)
        page = find_grok_page(browser) or new_page(browser)
        apply_env(page)
        try:
            page.bring_to_front()
        except Exception:
            pass

        cat = fetch_catalog(page, args.force)
        convs = cat["conversations"]
        cached = {} if args.force else known_keys(cat)
        log(f"清单 {len(convs)} 条会话，开始逐条抓取")

        groups: dict = {}
        fetched = hit = failed = 0
        for i, c in enumerate(convs, 1):
            cid = c["conversationId"]
            if args.only and not any(cid.startswith(x) for x in args.only):
                continue
            key = cached.get(cid)
            meta = None
            if key:
                mp, rp = paths(key, cid)
                if os.path.exists(mp) and os.path.exists(rp):
                    meta, hit = jload(mp), hit + 1
                    log(f"[{i}/{len(convs)}] 缓存 {cid[:8]}")

            if meta is None:
                try:
                    meta = page_json(page, f"{BASE}/rest/app-chat/conversations_v2/{cid}"
                                           f"?includeWorkspaces=true&includeTaskResult=true")
                    human_pause(1.2, 2.6)
                    resp = page_json(page, f"{BASE}/rest/app-chat/conversations/{cid}/responses")
                except Exception as e:
                    failed += 1
                    log(f"[{i}/{len(convs)}] 失败 {cid[:8]}: {e}")
                    time.sleep(args.pause * 2)
                    continue
                wss = (meta.get("conversation") or {}).get("workspaces") or []
                key = (wss[0].get("workspaceId") if wss else None) or PERSONAL
                mp, rp = paths(key, cid)
                jdump(mp, meta)
                jdump(rp, resp)
                fetched += 1
                log(f"[{i}/{len(convs)}] +{len(resp.get('responses') or []):>3} 条 "
                    f"{os.path.getsize(rp) // 1024:>5}KB  "
                    f"{(meta.get('conversation') or {}).get('title', '')[:42]}")

            conv = meta.get("conversation") or {}
            wss = conv.get("workspaces") or []
            g = groups.setdefault(key, {
                "workspaceId": None if key == PERSONAL else key,
                "workspaceName": PERSONAL_NAME, "conversations": []})
            if wss:
                g["workspaceName"] = wss[0].get("name") or PERSONAL_NAME
            g["conversations"].append({
                "conversationId": cid,
                "title": conv.get("title") or c.get("title") or cid[:8],
                "createTime": conv.get("createTime") or c.get("createTime"),
                "modifyTime": conv.get("modifyTime") or c.get("modifyTime"),
            })
            time.sleep(args.pause + (1.0 if i % 5 == 0 else 0.0))

        cat["groups"] = sorted(groups.values(),
                               key=lambda g: (g["workspaceId"] is None, g["workspaceName"]))
        jdump(os.path.join(GROK_RAW, "catalog.json"), cat)
        n = sum(len(g["conversations"]) for g in cat["groups"])
        log(f"完成：{n} 条会话 / {len(cat['groups'])} 个分组（新抓 {fetched}、缓存 {hit}、"
            f"失败 {failed}），耗时 {int(time.time() - t0)}s")
        for g in cat["groups"]:
            log(f"  - {g['workspaceName']}: {len(g['conversations'])} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
