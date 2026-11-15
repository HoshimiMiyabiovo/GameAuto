# -*- coding: utf-8 -*-
"""Grok 网页会话抓取：CDP 连接本机专用 Edge，网络层取证 + 拟人滚动。

为什么不用 Playwright 自己 launch：`--enable-automation` 与 `navigator.webdriver`
是最廉价的风控信号。这里改为**手动拉起 Edge**（见 start_edge.py）后再 connect_over_cdp，
浏览器进程参数干净；再配合真实 Edge 内核与独立 profile，指纹一致性最好。

子命令：
  state   打印当前所有页面 URL/标题、登录态判定、截图（人工核对登录是否完成）
  probe   打开目标会话，慢速滚到底再滚到顶，把 /rest/ /graphql 响应体落到 raw/probe/
          （先取证再写解析器，不猜字段）
  dump    把 raw/probe/ 下的响应汇总成 summary.txt，便于人工读结构

数据根目录固定 F:\\data\\grok_capture（profile 含 cookie，不入 git 仓库）。
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timezone

from patchright.sync_api import sync_playwright

CDP = "http://127.0.0.1:9222"
ROOT = r"F:\data\grok_capture"
RAW = os.path.join(ROOT, "raw")
PROBE = os.path.join(RAW, "probe")
SHOTS = os.path.join(ROOT, "shots")
TZ = "America/Los_Angeles"          # 与代理出口（洛杉矶）一致，避免 tz 指纹矛盾
LOCALE = "en-US"

PROJECT_URL = ("https://grok.com/project/3ba638cb-810e-462a-9477-edbb14bce720"
               "?chat=001b5ccf-1274-47ed-bf57-d6375e618327")
WANT = re.compile(r"/(rest|graphql|api)/", re.I)


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def ensure_dirs() -> None:
    for d in (RAW, PROBE, SHOTS):
        os.makedirs(d, exist_ok=True)


def human_pause(a: float = 0.4, b: float = 1.2) -> None:
    time.sleep(random.uniform(a, b))


def find_grok_page(browser):
    pages = []
    for ctx in browser.contexts:
        pages.extend(ctx.pages)
    for p in pages:
        try:
            if "grok.com" in (p.url or ""):
                return p
        except Exception:
            continue
    return None


def new_page(browser):
    ctx = browser.contexts[0] if browser.contexts else browser.new_context()
    page = ctx.new_page()
    return page


_ENV_SESSIONS: dict = {}


def apply_env(page) -> None:
    """时区/语言/UA 语言与代理出口对齐。

    必须**持有** CDP session 不 detach —— detach 会立刻撤销 Emulation 覆盖
    （2026-11-15 实测：detach 后 navigator.language 仍是 zh-CN、tz 仍是 Asia/Shanghai）。
    覆盖是 per-target 的，换页要重新 apply。
    """
    try:
        s = page.context.new_cdp_session(page)
        s.send("Emulation.setTimezoneOverride", {"timezoneId": TZ})
        s.send("Emulation.setLocaleOverride", {"locale": LOCALE})
        ua = s.send("Runtime.evaluate", {"expression": "navigator.userAgent",
                                         "returnByValue": True})["result"]["value"]
        s.send("Emulation.setUserAgentOverride",
               {"userAgent": ua, "acceptLanguage": "en-US,en"})
        _ENV_SESSIONS[id(page)] = s
    except Exception as e:
        log(f"环境覆盖失败（不致命）: {e}")


def fingerprint(page) -> dict:
    js = """() => ({
        ua: navigator.userAgent,
        lang: navigator.language,
        langs: navigator.languages,
        tz: Intl.DateTimeFormat().resolvedOptions().timeZone,
        webdriver: navigator.webdriver,
        plugins: navigator.plugins.length,
        chrome: !!window.chrome,
        hw: navigator.hardwareConcurrency,
        mem: navigator.deviceMemory,
        screen: [screen.width, screen.height, devicePixelRatio],
    })"""
    try:
        return page.evaluate(js)
    except Exception as e:
        return {"error": str(e)}


def accept_cookies(page) -> None:
    """点掉 Cookie 同意条（真人也会点，不是伪装）。"""
    for label in ("接受所有 Cookie", "Accept all cookies", "Accept all", "全部接受"):
        try:
            btn = page.get_by_role("button", name=label, exact=False).first
            if btn.count() and btn.is_visible():
                btn.click(timeout=3000)
                log(f"已点 Cookie 同意: {label}")
                human_pause(0.6, 1.2)
                return
        except Exception:
            continue


def login_state(page) -> dict:
    """登录态判定：看登录词、输入框数、侧边栏词、项目不可用提示，四个信号综合。

    只看 URL 不可靠：grok.com 是 SPA，登录后 URL 不变；匿名态也可能给输入框。
    """
    info = {"url": page.url or "", "title": "", "logged_in": None, "hint": ""}
    try:
        info["title"] = page.title()
    except Exception:
        pass
    try:
        body = page.evaluate(
            "() => (document.body && document.body.innerText || '').slice(0, 4000)"
        )
    except Exception:
        body = ""
    low = body.lower()
    try:
        composer = page.locator("textarea, [contenteditable='true']").count()
    except Exception:
        composer = 0
    try:
        avatar = page.locator("img[alt*='avatar' i], img[alt*='profile' i], "
                              "[data-testid*='avatar'], [aria-label*='account' i]").count()
    except Exception:
        avatar = 0
    signin = any(w in low for w in ("sign in", "log in", "登录", "sign up", "注册"))
    sidebar = any(w in low for w in ("new chat", "新聊天", "新对话", "search chats", "搜索"))
    unavail = ("isn't available" in low) or ("不可用" in body) or ("无法访问" in body)
    info.update({"composer": composer, "avatar": avatar, "signin_word": signin,
                 "sidebar_word": sidebar, "project_unavailable": unavail})
    if "/login" in info["url"] or "/sign-in" in info["url"]:
        info["logged_in"], info["hint"] = False, "URL 在登录页"
    elif unavail:
        info["logged_in"], info["hint"] = False, "项目页返回不可用（多半未登录/无权限）"
    elif avatar > 0 and not signin:
        info["logged_in"], info["hint"] = True, "检测到头像"
    elif composer > 0 and sidebar and not signin:
        info["logged_in"], info["hint"] = True, "输入框 + 侧边栏"
    elif signin and composer == 0:
        info["logged_in"], info["hint"] = False, "页面出现登录词"
    else:
        info["hint"] = "信号冲突，需看截图"
    info["body_head"] = body[:600]
    return info


def cmd_state(_args) -> int:
    ensure_dirs()
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP)
        pages = []
        for ctx in browser.contexts:
            pages.extend(ctx.pages)
        log(f"浏览器 {browser.browser_type.name}，页面数 {len(pages)}")
        for i, pg in enumerate(pages):
            try:
                log(f"  [{i}] {pg.url}  |  {pg.title()[:60]}")
            except Exception as e:
                log(f"  [{i}] <读不到: {e}>")
        page = find_grok_page(browser) or (pages[0] if pages else new_page(browser))
        info = login_state(page)
        log(json.dumps(info, ensure_ascii=False, indent=2))
        shot = os.path.join(SHOTS, f"state_{datetime.now():%H%M%S}.png")
        try:
            page.screenshot(path=shot)
            log(f"截图 {shot}")
        except Exception as e:
            log(f"截图失败: {e}")
        return 0


def cmd_watch(args) -> int:
    """只读轮询登录态：不动页面、不改指纹。

    用户可能正卡在 Google OAuth 挑战里，这时改 UA/时区等于自找风控，所以这里
    只读 DOM 判定，登录确认后立刻退出，把动作留给下一步。
    """
    ensure_dirs()
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP)
        page = find_grok_page(browser) or new_page(browser)
        deadline = time.time() + args.wait
        last = ""
        while time.time() < deadline:
            try:
                st = login_state(page)
            except Exception as e:
                log(f"  读取失败（忽略）: {e}")
                time.sleep(args.every)
                continue
            if st["hint"] != last:
                log(f"  状态: {st['hint']}  标题={str(st['title'])[:50]}")
                last = st["hint"]
            if st["logged_in"]:
                log("登录已确认")
                return 0
            time.sleep(args.every)
        log("等待超时，仍未确认登录")
        return 3


FETCH_JS = """async (u) => {
  const r = await fetch(u, {credentials: 'include', headers: {'accept': '*/*'}});
  const t = await r.text();
  return {status: r.status, len: t.length, body: t};
}"""


def page_fetch(page, url: str) -> tuple:
    """页面内 GET，返回 (status, text)。同源 + 同 cookie + 同 TLS 指纹。"""
    r = page.evaluate(FETCH_JS, url)
    return r.get("status"), (r.get("body") or "")


def page_json(page, url: str):
    st, txt = page_fetch(page, url)
    if st != 200:
        raise RuntimeError(f"HTTP {st} @ {url}")
    return json.loads(txt)


def api_get(page, url: str, save_dir: str | None = None, tag: str = "") -> dict:
    """在页面上下文里 GET（同源 + 同 cookie + 同 TLS 指纹），返回摘要。

    不走 Python 的 requests：那等于用另一个 TLS 栈重放 cookie，最容易踩
    Cloudflare 的 cf_clearance 绑定。这里请求由页面自己发，和 App 的 XHR 同级。
    """
    t0 = time.time()
    r = page.evaluate(FETCH_JS, url)
    body = r.get("body") or ""
    summary = {"url": url, "status": r.get("status"), "len": r.get("len"),
               "ms": int((time.time() - t0) * 1000), "keys": None, "n": None}
    try:
        obj = json.loads(body)
        if isinstance(obj, dict):
            summary["keys"] = list(obj.keys())[:10]
            for k in ("responses", "conversations", "workspaces", "files"):
                if isinstance(obj.get(k), list):
                    summary["n"] = len(obj[k])
                    break
            for k in ("nextPageToken", "pageToken"):
                if obj.get(k):
                    summary["next"] = obj[k][:40]
        elif isinstance(obj, list):
            summary["n"] = len(obj)
    except Exception:
        summary["keys"] = "<非 JSON>"
    if save_dir and body:
        os.makedirs(save_dir, exist_ok=True)
        name = tag or f"{int(time.time())}"
        with open(os.path.join(save_dir, f"{name}.json"), "w", encoding="utf-8") as f:
            f.write(body)
    return summary


def cmd_apiget(args) -> int:
    ensure_dirs()
    outdir = os.path.join(RAW, "api")
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP)
        page = find_grok_page(browser) or new_page(browser)
        apply_env(page)
        for i, u in enumerate(args.url, 1):
            try:
                s = api_get(page, u, outdir, tag=f"{args.tag or 'x'}{i:02d}")
                log(json.dumps(s, ensure_ascii=False))
            except Exception as e:
                log(f"失败 {u[:90]}: {e}")
            time.sleep(args.pause)
        return 0


def cmd_login(args) -> int:
    """打开 grok.com，等用户手动登录，登录成功后顺手校验项目页可访问性。

    登录只在自动化窗口里做一次：cookie 留在专用 profile，后续运行直接复用。
    不导出 storage_state —— 那是明文凭据，落到磁盘上没好处。
    """
    ensure_dirs()
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP)
        page = find_grok_page(browser) or new_page(browser)
        apply_env(page)
        try:
            page.bring_to_front()
        except Exception:
            pass
        if "grok.com" not in (page.url or ""):
            log("打开 grok.com")
            page.goto("https://grok.com/", wait_until="domcontentloaded", timeout=60000)
            human_pause(1.5, 2.5)
        accept_cookies(page)
        log(f"请在 Edge 窗口里登录（X / Google / 邮箱均可），最多等 {args.wait}s …")
        deadline = time.time() + args.wait
        last_hint = ""
        while time.time() < deadline:
            st = login_state(page)
            if st["hint"] != last_hint:
                log(f"  状态: {st['hint']} (composer={st['composer']} avatar={st['avatar']})")
                last_hint = st["hint"]
            if st["logged_in"]:
                break
            time.sleep(3)
        else:
            shot = os.path.join(SHOTS, f"login_timeout_{datetime.now():%H%M%S}.png")
            page.screenshot(path=shot)
            log(f"等待超时，仍未确认登录。截图 {shot}")
            return 3

        log("登录已确认，校验项目页 …")
        page.goto(args.url, wait_until="domcontentloaded", timeout=90000)
        human_pause(3.0, 5.0)
        accept_cookies(page)
        st = login_state(page)
        shot = os.path.join(SHOTS, f"login_ok_{datetime.now():%H%M%S}.png")
        page.screenshot(path=shot)
        log(f"项目页状态: {st['hint']} / 标题={st['title']} / 截图 {shot}")
        if st["project_unavailable"]:
            log("⚠ 项目页仍不可用：不是登录问题，而是项目已删除或当前账号无权限")
            return 4
        log("就绪，可以跑 probe 了")
        return 0


def cmd_env(_args) -> int:
    ensure_dirs()
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP)
        page = find_grok_page(browser) or new_page(browser)
        apply_env(page)
        fp = fingerprint(page)
        log("指纹：\n" + json.dumps(fp, ensure_ascii=False, indent=2))
        log("登录态：" + json.dumps(
            {k: v for k, v in login_state(page).items() if k != "body_head"},
            ensure_ascii=False))
        return 0


def cmd_probe(args) -> int:
    ensure_dirs()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    outdir = os.path.join(PROBE, stamp)
    os.makedirs(outdir, exist_ok=True)
    index_path = os.path.join(outdir, "index.jsonl")
    seen: dict[str, int] = {}
    saved = 0
    idx = open(index_path, "w", encoding="utf-8")

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP)
        page = new_page(browser)
        apply_env(page)
        cdp = page.context.new_cdp_session(page)
        cdp.send("Network.enable", {})

        def on_resp(ev):
            nonlocal saved
            try:
                url = ev.get("response", {}).get("url", "")
                if not WANT.search(url):
                    return
                rid = ev["requestId"]
                if rid in seen:
                    return
                status = ev["response"].get("status", 0)
                mime = (ev["response"].get("mimeType") or "").lower()
                if status >= 300 or "json" not in mime:
                    return
                body = cdp.send("Network.getResponseBody", {"requestId": rid}).get("body", "")
                if not body or len(body) < 40:
                    return
                seen[rid] = 1
                saved += 1
                name = f"{saved:04d}_{re.sub(r'[^A-Za-z0-9]+', '_', url.split('?')[0])[-80:]}.json"
                with open(os.path.join(outdir, name), "w", encoding="utf-8") as f:
                    f.write(body)
                idx.write(json.dumps({"file": name, "status": status, "len": len(body),
                                      "url": url}, ensure_ascii=False) + "\n")
                idx.flush()
                log(f"  + {saved} {status} {len(body):>7}B {url[:110]}")
            except Exception:
                pass  # 事件回调里吞异常：个别 body 已释放是常态

        cdp.on("Network.responseReceived", on_resp)

        log(f"打开 {args.url}")
        page.goto(args.url, wait_until="domcontentloaded", timeout=60000)
        human_pause(2.5, 4.0)
        accept_cookies(page)
        page.mouse.move(700, 450)
        info = login_state(page)
        log(f"登录态: {json.dumps({k: v for k, v in info.items() if k != 'body_head'}, ensure_ascii=False)}")
        shot0 = os.path.join(SHOTS, f"probe_{stamp}_top.png")
        page.screenshot(path=shot0)
        log(f"首屏截图 {shot0}")
        if info["logged_in"] is False:
            log("未登录 —— 请在 Edge 窗口里手动登录，登录完成后重跑 probe。")
            idx.close()
            return 2

        # 慢速滚到底：触发历史消息加载
        log("向下滚动（拟人节奏）…")
        last_h = 0
        stable = 0
        for i in range(args.scrolls):
            step = random.randint(320, 620)
            page.mouse.wheel(0, step)
            human_pause()
            if i and i % 12 == 0:
                h = page.evaluate("() => document.body.scrollHeight")
                log(f"  scroll#{i} height={h} saved={saved}")
                if h == last_h:
                    stable += 1
                    if stable >= 2 and i > 24:
                        log("  高度连续不变，判定到底")
                        break
                else:
                    stable = 0
                last_h = h
                time.sleep(random.uniform(2.0, 4.0))
        # 回到顶部，触发更早消息（Grok 项目会话常需手动上滚加载）
        log("回到顶部继续上滚…")
        for i in range(args.scrolls // 2):
            page.mouse.wheel(0, -random.randint(400, 800))
            human_pause(0.5, 1.4)
            if i and i % 15 == 0:
                log(f"  up#{i} saved={saved}")
                time.sleep(random.uniform(1.5, 3.0))
        shot1 = os.path.join(SHOTS, f"probe_{stamp}_end.png")
        page.screenshot(path=shot1)
        log(f"结束截图 {shot1}")
        idx.close()
        log(f"共保存 {saved} 个响应 → {outdir}")
        return 0


def cmd_dump(_args) -> int:
    dirs = sorted(d for d in os.listdir(PROBE) if os.path.isdir(os.path.join(PROBE, d)))
    if not dirs:
        log("还没有 probe 数据")
        return 1
    d = os.path.join(PROBE, dirs[-1])
    log(f"汇总 {d}")
    rows = []
    for name in sorted(os.listdir(d)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(d, name)
        try:
            obj = json.load(open(path, encoding="utf-8"))
        except Exception as e:
            rows.append((name, 0, f"<解析失败 {e}>"))
            continue
        keys = list(obj.keys())[:12] if isinstance(obj, dict) else [f"list[{len(obj)}]"]
        rows.append((name, os.path.getsize(path), keys))
    out = os.path.join(d, "summary.txt")
    with open(out, "w", encoding="utf-8") as f:
        for name, size, keys in rows:
            f.write(f"{name}\t{size}B\t{keys}\n")
    for name, size, keys in rows[:40]:
        log(f"{size:>8}B  {name[:70]:<72} {keys}")
    log(f"写入 {out}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("state").set_defaults(func=cmd_state)
    sub.add_parser("env").set_defaults(func=cmd_env)
    p9 = sub.add_parser("apiget")
    p9.add_argument("--url", action="append", required=True)
    p9.add_argument("--tag", default="")
    p9.add_argument("--pause", type=float, default=1.5)
    p9.set_defaults(func=cmd_apiget)
    p0 = sub.add_parser("watch")
    p0.add_argument("--wait", type=int, default=900, help="最长等待秒数")
    p0.add_argument("--every", type=int, default=3, help="轮询间隔秒")
    p0.set_defaults(func=cmd_watch)
    p1 = sub.add_parser("login")
    p1.add_argument("--url", default=PROJECT_URL)
    p1.add_argument("--wait", type=int, default=420, help="等待手动登录的秒数")
    p1.set_defaults(func=cmd_login)
    p2 = sub.add_parser("probe")
    p2.add_argument("--url", default=PROJECT_URL)
    p2.add_argument("--scrolls", type=int, default=60)
    p2.set_defaults(func=cmd_probe)
    sub.add_parser("dump").set_defaults(func=cmd_dump)
    a = ap.parse_args()
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
