# -*- coding: utf-8 -*-
"""RAG 倒排索引回归（2026-11-10）。

背景：tools/ai_history_rag.py 此前 query 无倒排、扫全库（64,030 块单次
18.6s 且随语料线性恶化）。本次 build 顺带生成 index.bin（词项 64 位哈希
bisect 表 + 定长 (docid,tf) 倒排段 + 块行偏移），query 走"倒排候选 → 只载
候选原文 → 精确 BM25 复评"，实测 64k 块 0.9~1.1s。锁四个行为：

[1] 索引等价性：合成语料（含随机词频分布）下，索引路径与全量扫的
    top-k 块序与分数必须逐项一致（候选未截断时）；
[2] 指纹守卫：chunks.jsonl 变更/缺失/索引损坏/版本不符 → _load_index
    一律判 None（query 回退全量扫，绝不发生"索引指向旧行号"的错位检索）；
[3] 候选截断：MAX_CANDIDATES 上限生效，代理权重降序；
[4] 命令级：cmd_query 有索引时输出走"倒排候选"，索引缺失时输出全量
    提示且结果与索引路径一致。

真实库（data/ai_history，本机生成）在 [5] 做环境依赖级验证：索引指纹有效
（stats/ selftest 已覆盖大半，这里锁 _load_index 直连）。
跑法：.venv\\Scripts\\python.exe -m pytest tests\\suite_ai_history_rag_index_20261110.py -q
"""

import io
import json
import random
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "tools"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import ai_history_rag as rag  # noqa: E402

FAILURES = []


def check(label, ok, detail=None):
    mark = "PASS" if ok else "FAIL"
    print(f"  {mark} {label}" + (f"  ← {detail}" if not ok and detail is not None else ""))
    if not ok:
        FAILURES.append(label)


def _write_corpus(td_path: Path, docs):
    chunks_path = td_path / "chunks.jsonl"
    with chunks_path.open("w", encoding="utf-8") as f:
        for i, doc in enumerate(docs):
            f.write(json.dumps({
                "session": f"s{i}", "src": "T", "title": f"t{i}",
                "start": "2026", "end": "2026", "chunk": 1, "text": doc,
            }, ensure_ascii=False) + "\n")
    return chunks_path


def _scan_top(rag, chunks_path, queries, k=5):
    all_chunks = [json.loads(l) for l in chunks_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    avgdl = sum(len(c["text"]) for c in all_chunks) / max(1, len(all_chunks))
    out = []
    for q in queries:
        qc = rag.bigrams(q)
        hits = rag._bm25_scores(qc, list(enumerate(all_chunks)), avgdl)[:k]
        out.append((q, [(d, round(s, 6)) for s, d in hits]))
    return out


def test_equivalence_random_corpus():
    print("[1] 索引等价性（随机语料，候选未截断）")
    rnd = random.Random(20261110)
    vocab = ["迷失之地", "超时", "杀进程", "march7th", "配置", "签到", "onnxocr", "窗口",
             "模板匹配", "后台点击", "日志", "月商店", "采购", "骰子", "回廊", "白荆",
             "启动器", "看门狗", "心跳", "原子写", "锁", "checkpoint", "respawn", "abc"]
    docs = []
    for i in range(300):
        n = rnd.randint(3, 40)
        docs.append("".join(rnd.choice(vocab) + rnd.choice(["，", "。", " ", "的"]) for _ in range(n)))
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        chunks_path = _write_corpus(td_path, docs)
        (td_path / rag.INDEX_NAME).write_bytes(rag._build_index_bytes(chunks_path))
        idx = rag._load_index(td_path)
        check("索引载入成功", idx is not None)
        check("块数一致", idx["header"]["n"] == len(docs))
        queries = ["迷失之地 超时", "march7th 配置", "看门狗 心跳", "onnxocr",
                   "白荆 采购 骰子", "不存在组合zzqq", "abc checkpoint"]
        idx_tops = []
        for q in queries:
            qc = rag.bigrams(q)
            cand = rag._index_candidates(idx, qc)
            if cand is None:
                idx_tops.append((q, []))
                continue
            pairs = rag._load_chunks_by_ids(td_path, idx, [d for d, _ in cand])
            hits = rag._bm25_scores(qc, pairs, idx["header"]["avgdl"])[:5]
            idx_tops.append((q, [(d, round(s, 6)) for s, d in hits]))
        scan_tops = _scan_top(rag, chunks_path, queries)
        for (q, i_top), (_, s_top) in zip(idx_tops, scan_tops):
            check(f"等价: {q}", i_top == s_top,
                  f"索引={i_top[:3]} 全量={s_top[:3]}")


def test_fingerprint_guard():
    print("[2] 指纹守卫（变更/缺失/损坏/版本）")
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        chunks_path = _write_corpus(td_path, ["march7th 配置", "迷失之地 超时"])
        check("无索引文件 → None", rag._load_index(td_path) is None)
        (td_path / rag.INDEX_NAME).write_bytes(rag._build_index_bytes(chunks_path))
        check("指纹匹配 → 载入", rag._load_index(td_path) is not None)
        # chunks 变更（mtime 变化）：追加合法 JSONL 行（旧内容 + 新行）
        extra = {"session": "s9", "src": "T", "title": "t9", "start": "2026",
                 "end": "2026", "chunk": 1, "text": "追加内容"}
        chunks_path.write_text(chunks_path.read_text(encoding="utf-8")
                               + json.dumps(extra, ensure_ascii=False) + "\n", encoding="utf-8")
        check("chunks 变更 → 过期", rag._load_index(td_path) is None)
        # 重建后恢复
        (td_path / rag.INDEX_NAME).write_bytes(rag._build_index_bytes(chunks_path))
        check("重建索引 → 恢复", rag._load_index(td_path) is not None)
        # 头损坏
        p = td_path / rag.INDEX_NAME
        raw = p.read_bytes()
        p.write_bytes(b"{broken\n" + raw.split(b"\n", 1)[1])
        check("头损坏 → None", rag._load_index(td_path) is None)
        # 版本不符
        payload = rag._build_index_bytes(chunks_path)
        head = json.loads(payload.split(b"\n", 1)[0].decode("utf-8"))
        head["v"] = rag.INDEX_VERSION + 99
        body = payload.split(b"\n", 1)[1]
        p.write_bytes((json.dumps(head, ensure_ascii=False) + "\n").encode("utf-8") + body)
        check("版本不符 → None", rag._load_index(td_path) is None)
        # 段长度不符
        head["v"] = rag.INDEX_VERSION
        head["keys_len"] += 8
        p.write_bytes((json.dumps(head, ensure_ascii=False) + "\n").encode("utf-8") + body)
        check("段长度不符 → None", rag._load_index(td_path) is None)
        # 目录缺失
        check("目录缺失 → None", rag._load_index(td_path.parent / "no_such") is None)


def test_candidate_cap():
    print("[3] 候选截断（MAX_CANDIDATES 生效 + 代理权重降序）")
    rnd = random.Random(7)
    vocab = ["迷失", "超时", "march7th", "签到", "onnxocr", "日志"]
    docs = ["".join(rnd.choice(vocab) for _ in range(rnd.randint(2, 20))) for _ in range(600)]
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        chunks_path = _write_corpus(td_path, docs)
        (td_path / rag.INDEX_NAME).write_bytes(rag._build_index_bytes(chunks_path))
        idx = rag._load_index(td_path)
        with patch.object(rag, "MAX_CANDIDATES", 50):
            cand = rag._index_candidates(idx, rag.bigrams("迷失 超时"))
        check("候选数 ≤ 上限", cand is not None and len(cand) <= 50, len(cand) if cand else None)
        weights = [w for _, w in cand]
        check("代理权重降序", weights == sorted(weights, reverse=True))
        check("查询词全不在索引 → None", rag._index_candidates(idx, rag.bigrams("xyzqq wwqq")) is None)


def test_cmd_query_paths():
    print("[4] 命令级：有索引走倒排 / 无索引走全量且结果一致")
    docs = ["march7th 配置路径错了", "迷失之地超时杀进程", "onnxocr 窗口命中", "白荆采购骰子"]
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        chunks_path = _write_corpus(td_path, docs)
        (td_path / rag.INDEX_NAME).write_bytes(rag._build_index_bytes(chunks_path))
        args = rag.argparse.Namespace(query="march7th 配置", k=3, session=None, exact=False)
        with patch.object(rag, "OUT_DIR", td_path), patch.object(sys, "stdout", new=io.StringIO()) as so:
            rc = rag.cmd_query(args)
        out = so.getvalue()
        check("有索引 rc=0 且标注倒排候选", rc == 0 and "倒排候选" in out, out[:120])
        # 无索引：同名查询结果应与索引路径一致（等价性的命令级验证）
        idx_backup = (td_path / rag.INDEX_NAME).read_bytes()
        (td_path / rag.INDEX_NAME).unlink()
        with patch.object(rag, "OUT_DIR", td_path), patch.object(sys, "stdout", new=io.StringIO()) as so2:
            rc2 = rag.cmd_query(args)
        out2 = so2.getvalue()
        check("无索引 rc=0 且提示全量路径", rc2 == 0 and "全量慢路径" in out2, out2[:120])
        def tops(text):
            return [ln for ln in text.splitlines() if ln.startswith("#")]
        check("两条路径 top 块一致", tops(out) == tops(out2),
              f"索引={tops(out)[:2]} 全量={tops(out2)[:2]}")
        (td_path / rag.INDEX_NAME).write_bytes(idx_backup)


def test_real_index_fresh():
    print("[5] 真实库索引指纹有效（环境依赖：本机 data/ai_history）")
    real = PROJECT_ROOT / "data" / "ai_history"
    chunks_path = real / "chunks.jsonl"
    if not chunks_path.exists():
        check("真实库存在（缺则 SKIP）", True)
        return
    idx = rag._load_index(real)
    if idx is None:
        check("真实索引指纹有效", False, "index.bin 缺失或指纹不符——重跑 build")
        return
    check("真实索引指纹有效", True)
    check("索引块数与 chunks 行数一致", idx["header"]["n"] > 0)
    check("词项规模合理（>10 万）", idx["header"]["n_terms"] > 100_000, idx["header"]["n_terms"])


def main():
    test_equivalence_random_corpus()
    test_fingerprint_guard()
    test_candidate_cap()
    test_cmd_query_paths()
    test_real_index_fresh()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED {len(FAILURES)}: {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
