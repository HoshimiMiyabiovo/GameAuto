# -*- coding: utf-8 -*-
"""AI 对话史 RAG：从外部对话导出中筛选本项目的会话，建本地检索库（data/ai_history）。

数据源：F:\\data\\AI对话汇总\\data.json（2026-11-09 00:08 重导出，514 会话 / 83,693 条
消息 / 约 7594 万字，外部目录不入库；ZCode 会话由 F:\\data\\_ai_organize_tools\\build.py
的 do_zcode 从 ~/.zcode/cli/db/db.sqlite 直读并入，全部会话进上游、相关的由本工具
双门控筛进 RAG）。本工具把**与本项目开发相关**的会话筛出、切块，落到
`data/ai_history/`（已 gitignore），并用纯标准库 BM25（字符二元组，中文友好）
做检索——不引新依赖、不占 GPU（显存留给 OCR）。

子命令：
  build    筛选 + 切块 + 写 chunks.jsonl / sessions.jsonl + 倒排索引 index.bin
  query    BM25 检索 top-k：tools/ai_history_rag.py query "异环 OCR 点击" [-k 8]
           --session <id> 整会话导出（打印全文）
  stats    库概况（会话数/块数/字数/来源分布/索引状态）
  selftest 切块/筛选不变量 + 倒排索引等价性断言

**倒排索引（2026-11-10）**：build 顺带生成 `index.bin`（词项 64 位哈希 bisect 表
+ 定长 (docid,tf) 倒排段 + 块行偏移，原子写）。query 先查倒排拿候选块（按
Σqtf×tf 代理权重截前 3000），再只对候选做精确 BM25 复评——64k 块单次
18.6s → 约 2-3s，且不再随语料线性恶化到不可用。索引缺失/与 chunks.jsonl
指纹不符/损坏时**自动回退全量扫**并提示重跑 build，工具永不因索引坏而不可用。

**不自动同步**：上游各会话库涨了，要先手跑外部 `F:\\data\\_ai_organize_tools\\build.py`
重导 data.json，再跑本工具 `build`；`build` 是**全量覆盖重建**（重写
chunks/sessions/index，不是增量追加、不做 id 去重），项目里没有 watcher / 定时
任务 / 启动钩子。2026-11-09 追平后当前库 91 会话 / 64030 块 / 约 6798 万字 /
chunks.jsonl 约 91MB（此前 43 / 11032 / 1165 万 / 16MB）。build 内存峰值约
1GB（源 JSON 对象图 + 倒排 postings），块数翻倍时注意内存。
"""
import argparse
import bisect
import hashlib
import json
import os
import re
import struct
import sys
import time
from array import array
from bisect import bisect_left
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

# ── 倒排索引（2026-11-10）────────────────────────────────────────────
INDEX_NAME = "index.bin"
INDEX_MAGIC = "GARAGIDX"
INDEX_VERSION = 2          # v2：倒排段为定长 (docid uint32, tf uint16) 对
MAX_CANDIDATES = 3000      # 候选块上限：倒排粗排（Σqtf×tf）截前 N，再精确 BM25 复评
K1, B = 1.5, 0.75          # BM25 参数（与历史全量扫口径一致）
_PACK = struct.Struct("<IH")


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
        score, reason = session_score(sess)  # 只算一次，meta 里直接复用（审计 P3）
        sess["_score"] = score
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
    # 原子写（审计 P2-2）：旧实现 "w" 直写，Ctrl-C/磁盘满/异常会留下截断
    # 索引被 query 端静默读走；先写 .tmp 全部成功后一起 os.replace 换入。
    sessions_tmp = OUT_DIR / "sessions.jsonl.tmp"
    chunks_tmp = OUT_DIR / "chunks.jsonl.tmp"
    try:
        with sessions_tmp.open("w", encoding="utf-8") as fs, chunks_tmp.open("w", encoding="utf-8") as fc:
            for sess in kept:
                meta = {
                    "id": sess.get("id"), "src": sess.get("src"),
                    "title": sess.get("title"), "start": sess.get("s"),
                    "end": sess.get("e"), "n_msgs": len(sess.get("msgs") or []),
                    "score": sess.get("_score", session_score(sess)[0]),
                }
                fs.write(json.dumps(meta, ensure_ascii=False) + "\n")
                for chunk in iter_chunks(sess):
                    fc.write(json.dumps(chunk, ensure_ascii=False) + "\n")
                    n_chunks += 1
                    n_chars += len(chunk["text"])
        os.replace(sessions_tmp, OUT_DIR / "sessions.jsonl")
        os.replace(chunks_tmp, OUT_DIR / "chunks.jsonl")
    finally:
        # 成功时两个 .tmp 已被 replace 走（exists 为 False），失败时清理半截文件
        for tmp in (sessions_tmp, chunks_tmp):
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
    print(f"筛选：{stats['total']} 会话 → 入库 {stats['kept']}"
          f"（工作区命中 {stats['kept_by_ws']}，关键词命中 {stats['kept'] - stats['kept_by_ws']}）")
    print(f"来源分布：{ {k[4:]: v for k, v in stats.items() if k.startswith('src:')} }")
    print(f"切块：{n_chunks} 块 / 约 {n_chars // 10000} 万字 → {OUT_DIR}")
    # 倒排索引（2026-11-10）：build 顺带生成，query 走"候选精确复评"快速路径
    if n_chunks:
        t0 = time.time()
        index_tmp = OUT_DIR / (INDEX_NAME + ".tmp")
        try:
            payload = _build_index_bytes(OUT_DIR / "chunks.jsonl")
            index_tmp.write_bytes(payload)
            os.replace(index_tmp, OUT_DIR / INDEX_NAME)
            hdr = json.loads(payload.split(b"\n", 1)[0].decode("utf-8"))
            print(f"索引：{hdr['n_terms']} 词项 / index.bin {len(payload) // 1024 // 1024}MB"
                  f"（{time.time() - t0:.0f}s）→ query 走倒排候选")
        except Exception as exc:
            print(f"索引：生成失败（query 将回退全量扫）：{exc}")
        finally:
            try:
                index_tmp.unlink(missing_ok=True)
            except OSError:
                pass
    return 0


# ── 倒排索引构建 / 载入 / 候选 ────────────────────────────────────────

def _hash_term(term: str) -> int:
    """词项 → 稳定 64 位哈希。禁用内置 hash()——PYTHONHASHSEED 跨进程随机，
    索引文件必须跨进程稳定，用 blake2b(8B)；2~3M 词项在 2^64 空间的碰撞
    概率 ~1e-7，即使碰撞也只是把两个词项的倒排合并（候选多几个假阳性，
    精确复评阶段自然过滤）。"""
    return int.from_bytes(hashlib.blake2b(term.encode("utf-8"), digest_size=8).digest(), "big")


def _build_index_bytes(chunks_path: Path) -> bytes:
    """流式扫 chunks.jsonl 建倒排索引，返回完整 index.bin 字节。

    文件结构：JSON 头（magic/版本/块数/avgdl/chunks 指纹/各段长度）+
    词项哈希表（array Q 升序，bisect 查找）+ 词项偏移（array Q，指向 blob
    内段落）+ 倒排 blob（每词项一段：定长 (docid uint32, tf uint16) 对，
    docid 为 chunks.jsonl 行号）+ 块行偏移（array Q，候选块按偏移 seek 回读）。

    内存峰值 ~500MB：词项 2~3M 个 × bytearray 段。流式读 chunks（不整载），
    段内 docid/tf 用 struct 定长对（无 delta/无 last 表，省一份 2.5M 词项
    级 dict）。索引不含 chunks 正文——候选块按行偏移回读原文做精确复评。
    """
    postings: Dict[int, bytearray] = {}
    chunk_offsets = array("Q")
    n = 0
    total_len = 0
    pack = _PACK.pack
    with chunks_path.open("rb") as f:
        while True:
            off = f.tell()
            line = f.readline()
            if not line:
                break
            sline = line.strip()
            if not sline:
                continue
            chunk_offsets.append(off)
            text = json.loads(sline).get("text") or ""
            total_len += len(text)
            docid = n
            n += 1
            for term, tf in bigrams(text).items():
                h = _hash_term(term)
                buf = postings.get(h)
                if buf is None:
                    buf = bytearray()
                    postings[h] = buf
                buf += pack(docid, tf)
    keys = array("Q", sorted(postings.keys()))
    term_offsets = array("Q", [0])
    blob = bytearray()
    for h in keys:
        seg = postings[h]
        blob += seg
        term_offsets.append(term_offsets[-1] + len(seg))
    del postings, seg
    header = {
        "magic": INDEX_MAGIC, "v": INDEX_VERSION, "n": n,
        # avgdl 必须全精度（json repr 可 round-trip）：存 round 会让索引路径
        # 的 BM25 分数与全量扫在第 7 位小数出现分歧，等价性断言失效
        "avgdl": total_len / max(1, n),
        "n_terms": len(keys),
        "chunks_mtime_ns": chunks_path.stat().st_mtime_ns,
        "chunks_size": chunks_path.stat().st_size,
        "keys_len": keys.itemsize * len(keys),
        "term_offsets_len": term_offsets.itemsize * len(term_offsets),
        "blob_len": len(blob),
        "chunk_offsets_len": chunk_offsets.itemsize * len(chunk_offsets),
    }
    out = bytearray()
    out += (json.dumps(header, ensure_ascii=False) + "\n").encode("utf-8")
    out += keys.tobytes()
    out += term_offsets.tobytes()
    out += bytes(blob)
    out += chunk_offsets.tobytes()
    return bytes(out)


def _load_index(dir_path: Path) -> Optional[Dict]:
    """载入倒排索引；缺失/损坏/与 chunks.jsonl 指纹不符 → None（调用方回退全量扫）。

    指纹 = chunks.jsonl 的 (mtime_ns, size)：build 重写 chunks 后旧索引即判
    过期，绝不出现"索引指向旧块行号"的错位检索。
    """
    path = dir_path / INDEX_NAME
    chunks_path = dir_path / "chunks.jsonl"
    try:
        cstat = chunks_path.stat()
    except OSError:
        return None
    try:
        with path.open("rb") as f:
            header = json.loads(f.readline().decode("utf-8"))
            payload = f.read()
        if header.get("magic") != INDEX_MAGIC or header.get("v") != INDEX_VERSION:
            return None
        if header.get("chunks_mtime_ns") != cstat.st_mtime_ns or header.get("chunks_size") != cstat.st_size:
            return None
        kl = header["keys_len"]
        ol = header["term_offsets_len"]
        bl = header["blob_len"]
        cl = header["chunk_offsets_len"]
        if len(payload) != kl + ol + bl + cl:
            return None
        keys = array("Q")
        keys.frombytes(payload[:kl])
        term_offsets = array("Q")
        term_offsets.frombytes(payload[kl:kl + ol])
        blob = payload[kl + ol:kl + ol + bl]
        chunk_offsets = array("Q")
        chunk_offsets.frombytes(payload[kl + ol + bl:])
        return {"header": header, "keys": keys, "term_offsets": term_offsets,
                "blob": blob, "chunk_offsets": chunk_offsets}
    except Exception:
        return None


def _index_candidates(idx: Dict, q_counts: Dict[str, int]) -> Optional[List[Tuple[int, float]]]:
    """查倒排得候选块 [(docid, 代理权重=Σqtf×tf)]，按权重降序截前 MAX_CANDIDATES。

    返回 None = 查询词没有任何词项在索引中（本次回退全量扫）。
    代理权重与精确 BM25 同向（qtf、tf 主导），只用于候选截断；命中块随后
    会载入原文做与全量扫完全一致的精确打分。
    """
    keys = idx["keys"]
    term_offsets = idx["term_offsets"]
    blob = idx["blob"]
    acc: Dict[int, float] = {}
    matched = 0
    for term, qtf in q_counts.items():
        h = _hash_term(term)
        i = bisect_left(keys, h)
        if i >= len(keys) or keys[i] != h:
            continue
        matched += 1
        seg = blob[term_offsets[i]:term_offsets[i + 1]]
        for docid, tf in _PACK.iter_unpack(seg):
            acc[docid] = acc.get(docid, 0.0) + qtf * tf
    if not matched:
        return None
    ranked = sorted(acc.items(), key=lambda kv: kv[1], reverse=True)
    return ranked[:MAX_CANDIDATES]


def _load_chunks_by_ids(dir_path: Path, idx: Dict, docids: List[int]) -> List[Tuple[int, Dict]]:
    """按块行偏移从 chunks.jsonl 回读候选块原文（不整载 91MB）。"""
    chunk_offsets = idx["chunk_offsets"]
    out: List[Tuple[int, Dict]] = []
    with (dir_path / "chunks.jsonl").open("rb") as f:
        for docid in docids:
            f.seek(chunk_offsets[docid])
            out.append((docid, json.loads(f.readline())))
    return out


def _bm25_scores(q_counts: Dict[str, int],
                 chunks: List[Tuple[int, Dict]], avgdl: float) -> List[Tuple[float, int]]:
    """简化 BM25 打分（tf 主导、固定 idf=1，与历史全量扫口径逐字一致）。

    chunks 为 (docid, chunk) 对；返回按分数降序的 [(score, docid)]。
    """
    scores: List[Tuple[float, int]] = []
    for docid, c in chunks:
        text = c.get("text") or ""
        tf_map = bigrams(text)
        dl = len(text)
        score = 0.0
        for term, qtf in q_counts.items():
            tf = tf_map.get(term, 0)
            if not tf:
                continue
            score += qtf * (tf * (K1 + 1)) / (tf + K1 * (1 - B + B * dl / avgdl))
        if score > 0:
            scores.append((score, docid))
    scores.sort(reverse=True)
    return scores


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
    if args.session:
        chunks = _load_chunks()
        if not chunks:
            print("检索库为空，先跑 build")
            return 1
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
    t0 = time.time()
    # 快路径：倒排候选 → 只载候选原文 → 精确 BM25（与全量扫同一打分函数）
    byid: Dict[int, Dict] = {}
    cand_n = 0
    hits: List[Tuple[float, int]] = []
    N = 0
    idx = _load_index(OUT_DIR)
    if idx is not None:
        cand = _index_candidates(idx, q_counts)
        if cand is not None:
            pairs = _load_chunks_by_ids(OUT_DIR, idx, [d for d, _ in cand])
            hits = _bm25_scores(q_counts, pairs, idx["header"]["avgdl"])[:args.k]
            byid = dict(pairs)
            cand_n = len(cand)
            N = idx["header"]["n"]
    if not N:
        # 慢路径兜底：索引缺失/过期/查询词全不在索引 → 全量扫（旧行为原样）
        chunks = _load_chunks()
        if not chunks:
            print("检索库为空，先跑 build")
            return 1
        N = len(chunks)
        avgdl = sum(len(c["text"]) for c in chunks) / max(1, N)
        hits = _bm25_scores(q_counts, list(enumerate(chunks)), avgdl)[:args.k]
        byid = {i: c for i, c in enumerate(chunks)}
        print("[提示] 本次走全量慢路径（索引缺失/过期，或查询词全不在索引）；重跑 build 生成 index.bin")
    hits = hits[:args.k]
    if not hits:
        print("无命中（换个词试试，或确认已 build）")
        return 0
    tag = f"倒排候选 {cand_n} 块" if cand_n else "全量扫描"
    print(f"top {len(hits)} / {N} 块（{tag}，{time.time() - t0:.1f}s）")
    for score, docid in hits:
        c = byid[docid]
        text = c["text"]
        # 摘出查询词附近上下文
        anchor = text.lower().find(norm(q)[:4]) if len(q) >= 2 else -1
        if anchor >= 0:
            lo = max(0, anchor - 80)
            excerpt = ("…" if lo else "") + text[lo:anchor + 220] + "…"
        else:
            excerpt = text[:260] + "…"
        print(f"\n#{docid} {c['start'][:7]} [{c['src']}] {c['title']} (score {score:.0f})")
        print(f"  {excerpt}")
    print(f"\n整会话导出：tools/ai_history_rag.py query --session {byid[hits[0][1]]['session']}")
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
    idx_path = OUT_DIR / INDEX_NAME
    if idx_path.exists():
        stale = "（指纹与 chunks.jsonl 不符，query 将回退全量扫）" if _load_index(OUT_DIR) is None else ""
        try:
            with idx_path.open("rb") as f:
                hdr = json.loads(f.readline().decode("utf-8"))
            print(f"索引: index.bin {idx_path.stat().st_size // 1024 // 1024}MB / "
                  f"{hdr.get('n_terms', '?')} 词项{stale}")
        except Exception:
            print(f"索引: index.bin 存在但头损坏{stale}（重跑 build 重建）")
    else:
        print("索引: 无（重跑 build 生成，否则 query 走全量慢路径）")
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
    _selftest_index_equivalence()
    print("selftest OK")
    return 0


def _selftest_index_equivalence() -> None:
    """倒排索引等价性：小语料（全候选）下索引路径与全量扫的 top-k 必须一致，
    且 chunks 指纹变化后索引必须判过期。"""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        docs = [
            "march7th 配置文件路径填错了，启动器读不到 自动签到 脚本",
            "红外光谱基团峰位分析，羰基在 1700 波数附近",
            "迷失之地超时的根因是 ScriptChainer 20 分钟硬超时强关游戏",
            "onnxocr v6 引擎在窗口 OCR 命中目标文字，置信度 0.99",
            "白荆回廊后勤商店采购按已购买分段收口，盲点坐标废除",
        ]
        chunks_path = td_path / "chunks.jsonl"
        with chunks_path.open("w", encoding="utf-8") as f:
            for i, doc in enumerate(docs):
                f.write(json.dumps({
                    "session": f"s{i}", "src": "T", "title": f"t{i}",
                    "start": "2026", "end": "2026", "chunk": 1, "text": doc,
                }, ensure_ascii=False) + "\n")
        (td_path / INDEX_NAME).write_bytes(_build_index_bytes(chunks_path))
        idx = _load_index(td_path)
        assert idx is not None, "索引应能载入"
        assert idx["header"]["n"] == len(docs), "块数应一致"
        avgdl = idx["header"]["avgdl"]
        all_chunks = [json.loads(l) for l in chunks_path.read_text(encoding="utf-8").splitlines() if l.strip()]
        for q in ("march7th 配置", "红外光谱", "迷失之地 超时", "onnxocr", "不存在的词xyzq"):
            qc = bigrams(q)
            cand = _index_candidates(idx, qc)
            if cand is None:
                hits_idx: List[Tuple[float, int]] = []
            else:
                pairs = _load_chunks_by_ids(td_path, idx, [d for d, _ in cand])
                hits_idx = _bm25_scores(qc, pairs, avgdl)
            hits_scan = _bm25_scores(qc, list(enumerate(all_chunks)), avgdl)
            assert [d for _, d in hits_idx] == [d for _, d in hits_scan], f"{q}: docid 顺序不一致"
            assert [round(s, 6) for s, _ in hits_idx] == [round(s, 6) for s, _ in hits_scan], f"{q}: 分数不一致"
        # 指纹校验：chunks.jsonl 变更后旧索引必须判过期
        all_chunks[0]["text"] += "追加内容"
        chunks_path.write_text(
            "".join(json.dumps(c, ensure_ascii=False) + "\n" for c in all_chunks), encoding="utf-8")
        assert _load_index(td_path) is None, "chunks 变更后索引应判过期"
        assert _load_index(td_path.parent / "no_such_dir") is None


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build", help="筛选+切块入库+倒排索引")
    q = sub.add_parser("query", help="BM25 检索（有索引走倒排候选，无索引全量扫）")
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
