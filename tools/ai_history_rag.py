# -*- coding: utf-8 -*-
"""AI 对话史 RAG：从外部对话导出中筛选本项目的会话，建本地检索库（data/ai_history）。

数据源：F:\\data\\AI对话汇总\\data.json（2026-11-09 00:08 重导出，514 会话 / 83,693 条
消息 / 约 7594 万字，外部目录不入库；ZCode 会话由 F:\\data\\_ai_organize_tools\\build.py
的 do_zcode 从 ~/.zcode/cli/db/db.sqlite 直读并入，全部会话进上游、相关的由本工具
双门控筛进 RAG）。本工具把**与本项目开发相关**的会话筛出、切块，落到
`data/ai_history/`（已 gitignore），并用纯标准库 BM25（字符二元组，中文友好）
做检索——不引新依赖、不占 GPU（显存留给 OCR）。

子命令：
  build    筛选 + 切块 + 写 chunks.jsonl / sessions.jsonl
  query    BM25 检索 top-k：tools/ai_history_rag.py query "异环 OCR 点击" [-k 8]
           --session <id> 整会话导出（打印全文）
  stats    库概况（会话数/块数/字数/来源分布）
  selftest 切块与筛选不变量断言

**不自动同步**：上游各会话库涨了，要先手跑外部 `F:\\data\\_ai_organize_tools\\build.py`
重导 data.json，再跑本工具 `build`；`build` 是**全量覆盖重建**（`open("w")` 重写
chunks/sessions，不是增量追加、不做 id 去重），项目里没有 watcher / 定时任务 /
启动钩子。2026-11-09 追平后当前库 91 会话 / 64030 块 / 约 6798 万字 /
chunks.jsonl 约 91MB（此前 43 / 11032 / 1165 万 / 16MB）。query 无倒排、扫全库，
块数上万后单次约 14s，再要提速得另建索引或缓存 bigrams。
"""
import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

SOURCE_JSON = Path(r"F:\data\AI对话汇总\data.json")
OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "ai_history"
CHUNK_SIZE = 1200          # 块字符数（含角色/时间头）
CHUNK_OVERLAP = 150        # 相邻块重叠，防句子被切腰
WS_HINTS = ("game_auto", "game-auto", "gameauto")   # 覆盖连字符/下划线/无分隔变体
STRONG_WORDS = ("game_auto", "ok-ww", "ok-ef", "ok-nte", "自动化", "自动脚本", "签到脚本",
                "march7th", "bettergi", "onedragon", "one_dragon", "flow_configs",
                "ocr点击", "模板匹配", "点击序列", "后台模板", "采购步", "日活", "派遣",
                "日志解析", "进度解析", "启动器", "前台流程", "月商店", "恶名狩猎",
                "后勤商店", "抽卡分析", "模拟点击", "图像识别点击", "auto_battle",
                "游戏自动化", "自动战斗", "点击器", "按键精灵", "huskarui", "pyautogui",
                "onnxocr", "uipi")
GAME_WORDS = ("绝区零", "星穹铁道", "崩铁", "原神", "鸣潮", "白荆", "异环", "无限暖暖",
              "终末地", "明日方舟", "签到")
SCAN_CHARS = 8000          # 关键词扫描每会话正文前缀
KEYWORD_THRESHOLD = 5      # 纯关键词入选线（标题强词 3/词、正文 2/词、游戏名 1/词）
WS_BONUS = 30              # 工作区命中加分；需再凑 ≥1 内容分才入选（合计 ≥31）
WS_GATE = WS_BONUS + 1     # 双门控：工作区会话 (score≥31) 与纯关键词会话 (score≥5) 各走各的线
SCORE_THRESHOLD = KEYWORD_THRESHOLD  # 兼容旧名


def norm(s: str) -> str:
    return "".join(str(s or "").split()).lower()


def bigrams(text: str) -> Counter:
    """字符二元组计数：中文检索主力粒度（ASCII 长词另按整词加权）。"""
    t = norm(text)
    c = Counter(t[i:i + 2] for i in range(len(t) - 1))
    for w in re.findall(r"[a-z_]{3,}", t):
        c[w] += 2
    return c


def session_text(sess: dict, limit: Optional[int] = None) -> str:
    """拼接会话正文（带角色标记）；limit 限制返回前 N 字符。"""
    parts: List[str] = []
    total = 0
    for m in sess.get("msgs") or []:
        role = "问" if str(m.get("r")) == "user" else "答"
        t = str(m.get("x") or "")
        parts.append(f"{role}: {t}")
        total += len(t) + 4
        if limit and total >= limit:
            break
    return "\n".join(parts)[:limit] if limit else "\n".join(parts)


def session_score(sess: dict) -> Tuple[int, str]:
    """项目相关性打分：(分数, 命中原因)。

    标题命中 STRONG 词记 3 分（标题是最强信号）、正文 2 分、游戏名 1 分；
    工作区（Cursor/Claude Code 的 workspace 字段，含连字符变体）命中 +50 但
    仍需内容 ≥1 分——2026-11-08 实测：在 game_auto 工作区跑的"安装LaTeX组件"
    会话内容完全无关，纯工作区直通会把它放进来。
    """
    ws = norm(sess.get("ws") or "")
    title = norm(sess.get("title") or "")
    haystack = title + " " + norm(session_text(sess, SCAN_CHARS))
    score = 0
    hits: List[str] = []
    for w in STRONG_WORDS:
        if w in title:
            score += 3
            hits.append(w)
        elif w in haystack:
            score += 2
            hits.append(w)
    for w in GAME_WORDS:
        if w in haystack:
            score += 1
            hits.append(w)
    if any(h in ws for h in WS_HINTS):
        score += WS_BONUS
        hits = ["workspace"] + hits  # 放最前，防 hits[:6] 截断导致 ws_hit 判定失效
    return score, ",".join(hits[:6])


def iter_chunks(sess: dict, chunk_size: int = CHUNK_SIZE) -> Iterator[Dict]:
    """把一个会话切成带会话元数据的检索块（按消息顺序、超长消息内部再切）。"""
    base = {
        "session": sess.get("id"),
        "src": sess.get("src"),
        "title": sess.get("title"),
        "start": sess.get("s"),
        "end": sess.get("e"),
    }
    pieces: List[str] = []
    for m in sess.get("msgs") or []:
        role = "问" if str(m.get("r")) == "user" else "答"
        t = str(m.get("x") or "").strip()
        if not t:
            continue
        pieces.append(f"[{m.get('t','')}] {role}: {t}")
    if not pieces:
        return
    # 超长单条消息内部滑窗切块，块间留 overlap；尾部碎片太短直接丢弃
    expanded: List[str] = []
    for p in pieces:
        if len(p) <= chunk_size:
            expanded.append(p)
            continue
        step = max(1, chunk_size - CHUNK_OVERLAP)
        for i in range(0, len(p), step):
            piece = p[i:i + chunk_size]
            if len(piece) > 50:
                expanded.append(piece)
    buf: List[str] = []
    buf_len = 0
    idx = 0
    for p in expanded:
        if buf_len + len(p) > chunk_size and buf:
            idx += 1
            yield {**base, "chunk": idx, "text": "".join(buf)}
            buf, buf_len = [], 0
        buf.append(p)
        buf_len += len(p)
    if buf:
        idx += 1
        yield {**base, "chunk": idx, "text": "".join(buf)}


def cmd_build(args: argparse.Namespace) -> int:
    if not SOURCE_JSON.exists():
        print(f"数据源不存在: {SOURCE_JSON}")
        return 1
    data = json.loads(SOURCE_JSON.read_text(encoding="utf-8"))
    sessions = data.get("sessions") or []
    kept: List[Dict] = []
    stats = Counter()
    for sess in sessions:
        score, reason = session_score(sess)
        stats["total"] += 1
        ws_hit = "workspace" in reason
        # 双门控：纯关键词线 KEYWORD_THRESHOLD；工作区会话需再凑内容分（防
        # "开在 game_auto 工作区的无关会话"，2026-11-08 LaTeX 会话实锤）
        if (score >= WS_GATE) if ws_hit else (score >= KEYWORD_THRESHOLD):
            kept.append(sess)
            stats["kept"] += 1
            stats[f"src:{sess.get('src')}"] += 1
            if ws_hit:
                stats["kept_by_ws"] += 1
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    n_chunks = 0
    n_chars = 0
    with (OUT_DIR / "sessions.jsonl").open("w", encoding="utf-8") as fs, \
            (OUT_DIR / "chunks.jsonl").open("w", encoding="utf-8") as fc:
        for sess in kept:
            meta = {
                "id": sess.get("id"), "src": sess.get("src"),
                "title": sess.get("title"), "start": sess.get("s"),
                "end": sess.get("e"), "n_msgs": len(sess.get("msgs") or []),
                "score": session_score(sess)[0],
            }
            fs.write(json.dumps(meta, ensure_ascii=False) + "\n")
            for chunk in iter_chunks(sess):
                fc.write(json.dumps(chunk, ensure_ascii=False) + "\n")
                n_chunks += 1
                n_chars += len(chunk["text"])
    print(f"筛选：{stats['total']} 会话 → 入库 {stats['kept']}"
          f"（工作区命中 {stats['kept_by_ws']}，关键词命中 {stats['kept'] - stats['kept_by_ws']}）")
    print(f"来源分布：{ {k[4:]: v for k, v in stats.items() if k.startswith('src:')} }")
    print(f"切块：{n_chunks} 块 / 约 {n_chars // 10000} 万字 → {OUT_DIR}")
    return 0


def _load_chunks() -> List[Dict]:
    chunks: List[Dict] = []
    path = OUT_DIR / "chunks.jsonl"
    if not path.exists():
        return chunks
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                chunks.append(json.loads(line))
    return chunks


def cmd_query(args: argparse.Namespace) -> int:
    chunks = _load_chunks()
    if not chunks:
        print("检索库为空，先跑 build")
        return 1
    if args.session:
        found = [c for c in chunks if c.get("session") == args.session]
        if not found:
            print(f"未找到会话: {args.session}")
            return 1
        meta = {k: found[0].get(k) for k in ("src", "title", "start", "end")}
        print(f"=== {meta['title']} [{meta['src']}] {meta['start']} ~ {meta['end']} ===")
        for c in found:
            print(f"\n--- 块 {c['chunk']} ---\n{c['text']}")
        return 0
    q = args.query
    q_counts = bigrams(q)
    if args.exact:  # 追加整词精确加权（英文/数字串）
        q_counts[q.lower()] = q_counts.get(q.lower(), 0) + 5
    N = len(chunks)
    avgdl = sum(len(c["text"]) for c in chunks) / max(1, N)
    k1, b = 1.5, 0.75
    scores: List[Tuple[float, int]] = []
    for i, c in enumerate(chunks):
        tf_map = bigrams(c["text"])
        dl = len(c["text"])
        score = 0.0
        for term, qtf in q_counts.items():
            tf = tf_map.get(term, 0)
            if not tf:
                continue
            # 简化 BM25（tf 主导）：不预建倒排就没有真实 df，固定 idf=1 换
            # "扫描全库即查全库"的零索引架构——几千块规模下整查 <2s，够用
            score += qtf * (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * dl / avgdl))
        if score > 0:
            scores.append((score, i))
    scores.sort(reverse=True)
    hits = scores[:args.k]
    if not hits:
        print("无命中（换个词试试，或确认已 build）")
        return 0
    print(f"top {len(hits)} / {N} 块")
    for score, i in hits:
        c = chunks[i]
        text = c["text"]
        # 摘出查询词附近上下文
        anchor = text.lower().find(norm(q)[:4]) if len(q) >= 2 else -1
        if anchor >= 0:
            lo = max(0, anchor - 80)
            excerpt = ("…" if lo else "") + text[lo:anchor + 220] + "…"
        else:
            excerpt = text[:260] + "…"
        print(f"\n#{i} {c['start'][:7]} [{c['src']}] {c['title']} (score {score:.0f})")
        print(f"  {excerpt}")
    print(f"\n整会话导出：tools/ai_history_rag.py query --session {chunks[hits[0][1]]['session']}")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    chunks = _load_chunks()
    sessions = {}
    srcs = Counter()
    chars = 0
    for c in chunks:
        sessions[c.get("session")] = sessions.get(c.get("session"), 0) + 1
        srcs[str(c.get("src"))] += 1
        chars += len(c["text"])
    print(f"会话 {len(sessions)} / 块 {len(chunks)} / 约 {chars // 10000} 万字")
    print("来源分布:", dict(srcs))
    return 0


def cmd_selftest(args: argparse.Namespace) -> int:
    sess = {
        "id": "t:1", "src": "T", "title": "t", "s": "2026-01-01", "e": "2026-01-01",
        "msgs": [
            {"r": "user", "t": "t1", "x": "短消息一"},
            {"r": "assistant", "t": "t2", "x": "长" * (CHUNK_SIZE * 3)},
        ],
    }
    chunks = list(iter_chunks(sess))
    assert all(len(c["text"]) <= CHUNK_SIZE + CHUNK_OVERLAP for c in chunks), "块超长"
    assert len(chunks) >= 4, f"超长消息应切多块: {len(chunks)}"
    assert chunks[0]["text"].startswith("["), "块应带时间头"
    score, reason = session_score({"id": "x", "ws": r"E:\GithubProject\game_auto",
                                   "title": "march7th 配置", "msgs": [{"r": "user", "x": "march7th"}]})
    assert "workspace" in reason and score >= WS_GATE, f"工作区+内容命中: {score},{reason}"
    score_ws_only, _ = session_score({"id": "x2", "ws": r"E:\GithubProject\game_auto",
                                      "title": "随便聊聊", "msgs": [{"r": "user", "x": "你好呀"}]})
    assert score_ws_only < WS_GATE, f"工作区命中但内容无关应淘汰: {score_ws_only}"
    score2, _ = session_score({"id": "y", "ws": "", "title": "怎么换 March7th 配置自动签到",
                               "msgs": [{"r": "user", "x": "march7th 签到脚本"}]})
    assert score2 >= KEYWORD_THRESHOLD, f"标题+内容强词应入选: {score2}"
    score3, _ = session_score({"id": "z", "ws": "", "title": "红外光谱原理",
                               "msgs": [{"r": "user", "x": "红外光谱基团峰位"}]})
    assert score3 < KEYWORD_THRESHOLD, f"纯课程应淘汰: {score3}"
    print("selftest OK")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build", help="筛选+切块入库")
    q = sub.add_parser("query", help="BM25 检索")
    q.add_argument("query", help="查询词")
    q.add_argument("-k", type=int, default=8, help="top-k（默认 8）")
    q.add_argument("--session", help="整会话导出（按 session id）")
    q.add_argument("--exact", action="store_true", help="查询词整词加权")
    sub.add_parser("stats", help="库概况")
    sub.add_parser("selftest", help="不变量自检")
    args = ap.parse_args(argv)
    return {"build": cmd_build, "query": cmd_query, "stats": cmd_stats,
            "selftest": cmd_selftest}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
