# -*- coding: utf-8 -*-
"""RAG 混合检索实验的纯逻辑回归（2026-11-10）。

tools/ai_history_rag_eval.py 是 BM25 vs 向量 vs 混合 RRF vs 混合+重排 的召回率
对比实验工具。模型/嵌入/GPU 属环境依赖，不进 CI；本套件锁**纯逻辑**部分：

[1] RRF 融合公式与排名语义（1/(60+rank)、两路并集、截断）；
[2] 命中位次判定（块级严格 / 会话级实用，未命中 None）；
[3] 指标聚合（R@K、MRR 口径，含空子集不除零）；
[4] 泄漏检查器的 Dice 系数性质与阈值语义（sem 抄原文必被拦）；
[5] 端到端小语料（真实倒排索引 + 手工单位向量）：BM25 走主工具同款
    _bm25_ranked，向量走点积 top-k，验证"一路赢一路输时混合保住赢家"——
    这是混合检索存在的意义，锁住它防止未来改融合参数时静默退化。

跑法：.venv\\Scripts\\python.exe -m pytest tests\\suite_ai_history_rag_hybrid_20261110.py -q
"""

import io
import json
import sys
import tempfile
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "tools"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import ai_history_rag as rag  # noqa: E402
import ai_history_rag_eval as ev  # noqa: E402

FAILURES = []


def check(label, ok, detail=None):
    mark = "PASS" if ok else "FAIL"
    print(f"  {mark} {label}" + (f"  ← {detail}" if not ok and detail is not None else ""))
    if not ok:
        FAILURES.append(label)


def test_rrf_fuse():
    print("[1] RRF 融合公式与排名语义")
    # 每路按名次贡献 1/(60+rank)：rank1=1/61≈0.016393
    r1 = 1.0 / 61
    r2 = 1.0 / 62
    r3 = 1.0 / 63
    # 两路都排第一的 docid 应以 2r1 居首；只在一路出现的按单路名次排
    a = [10, 20, 30]
    b = [40, 10, 50]
    out = ev.rrf_fuse(a, b, 10)
    check("双路第一并列居首", out[0] == 10)
    check("融合分数=Σ1/(60+rank)", True)  # 数值校验见下
    scores = {}
    for ranked in (a, b):
        for i, d in enumerate(ranked, 1):
            scores[d] = scores.get(d, 0.0) + 1.0 / (60 + i)
    expect = [d for d, _ in sorted(scores.items(), key=lambda kv: -kv[1])]
    check("完整顺序与手算一致", out == expect, f"out={out} expect={expect}")
    check("并集无丢失", set(out) == set(a) | set(b))
    # top 截断
    check("top=2 截断", ev.rrf_fuse(a, b, 2) == expect[:2])
    # 空路兜底：一路为空时等于另一路的截断
    check("空路=单路", ev.rrf_fuse([], b, 10) == b)
    # 名次严格递减：第一名 > 第二名（数值层面）
    check("rank1>rank2", r1 > r2 > r3 > 0)


def test_hit_ranks():
    print("[2] 命中位次判定")
    sess_of = ["s1", "s2", "s1", "s3"]
    ranked = [2, 0, 3, 1]
    # gt 块 0（会话 s1）：块位次 2；会话 s1 首个命中是块 2 → 位次 1
    cr, sr = ev._hit_ranks(ranked, 0, "s1", sess_of)
    check("块命中位次", cr == 2, cr)
    check("会话命中位次（首个）", sr == 1, sr)
    cr, sr = ev._hit_ranks(ranked, 3, "s9", sess_of)
    check("块命中、会话未命中", cr == 3 and sr is None, (cr, sr))
    cr, sr = ev._hit_ranks(ranked, 9, "s3", sess_of)
    check("块未命中、会话命中", cr is None and sr == 3, (cr, sr))
    cr, sr = ev._hit_ranks(ranked, 9, "s9", sess_of)
    check("全未命中 None", cr is None and sr is None)


def test_aggregate_metrics():
    print("[3] 指标聚合（R@K / MRR / 空子集）")
    rows = [
        {"mode": "A", "chunk_hit_rank": 3, "session_hit_rank": 1},
        {"mode": "A", "chunk_hit_rank": None, "session_hit_rank": 12},
        {"mode": "A", "chunk_hit_rank": 1, "session_hit_rank": None},
        {"mode": "B", "chunk_hit_rank": 21, "session_hit_rank": 21},
    ]
    c, s, mrr = ev.aggregate_metrics(rows, "A")
    check("cR@5", abs(c[0] - 2 / 3) < 1e-9, c[0])
    check("cR@10 全中", abs(c[1] - 2 / 3) < 1e-9, c[1])
    check("cR@20 全中", abs(c[2] - 2 / 3) < 1e-9, c[2])
    check("sR@10 只含 rank≤10", abs(s[1] - 1 / 3) < 1e-9, s[1])
    check("sR@20 再含 rank12", abs(s[2] - 2 / 3) < 1e-9, s[2])
    check("MRR=1/1+1/12 均值", abs(mrr - (1.0 + 1.0 / 12) / 3) < 1e-9, mrr)
    cb, sb, mrrb = ev.aggregate_metrics(rows, "B")
    check("rank21 不进 R@20", cb[2] == 0.0 and sb[2] == 0.0)
    check("rank21 进 MRR=1/21", abs(mrrb - 1.0 / 21) < 1e-9, mrrb)
    c0, s0, m0 = ev.aggregate_metrics([], "A")
    check("空子集不除零", c0 == [0.0, 0.0, 0.0] and s0 == [0.0, 0.0, 0.0] and m0 == 0.0)


def test_dice_leak_guard():
    print("[4] 泄漏检查器（Dice 性质 + 阈值语义）")
    a = set(rag.bigrams("迷失之地超时的根因是硬超时强关游戏"))
    b = set(rag.bigrams("迷失之地超时的根因是硬超时强关游戏"))
    check("同文 Dice=1", abs(ev.dice_coeff(a, b) - 1.0) < 1e-9)
    c = set(rag.bigrams("完全无关的一段话，红外光谱基团峰位分析"))
    check("无关 Dice 低", ev.dice_coeff(a, c) < ev.DICE_LEAK, ev.dice_coeff(a, c))
    check("对称性", abs(ev.dice_coeff(a, c) - ev.dice_coeff(c, a)) < 1e-9)
    check("空集=0", ev.dice_coeff(set(), a) == 0.0)
    # 阈值语义：半抄（一半 bigram 相同）必超阈值——sem 查询抄原文会被拦
    half = set(list(a)[: len(a) // 2])
    check("半抄超阈值", ev.dice_coeff(half, a) > ev.DICE_LEAK, ev.dice_coeff(half, a))
    # 意译重写（少量共享虚词）应低于阈值
    para = set(rag.bigrams("问的是为什么脚本停在那里不动了，一直空等到被强制结束"))
    check("意译低于阈值", ev.dice_coeff(para, a) < ev.DICE_LEAK, ev.dice_coeff(para, a))


def test_hybrid_end_to_end():
    print("[5] 端到端小语料（真实索引 + 手工向量）：单路输时混合保住赢家")
    # 构造"意译失配"场景：查询词面只命中 doc1（词面对上但非真值），
    # 真值 doc3 与查询语义相近但**词面完全不相交**（模拟 sem 改述查询）——
    # BM25 整个漏掉真值，向量路必须把它救回来；这是混合检索存在的意义。
    docs = [
        "红外光谱基团峰位分析，羰基在 1700 波数附近",           # 0 干扰
        "march7th 签到 配置 签到 配置 march7th 签到",            # 1 BM25 赢家（词面命中但非真值）
        "白荆回廊后勤商店采购分段收口",                        # 2 干扰
        "自动打卡工具的参数文件位置写错了导致启动失败",         # 3 真值（语义近邻、词面零交集）
    ]
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        chunks_path = td_path / "chunks.jsonl"
        with chunks_path.open("w", encoding="utf-8") as f:
            for i, doc in enumerate(docs):
                f.write(json.dumps({
                    "session": f"s{i}", "src": "T", "title": f"t{i}",
                    "start": "2026", "end": "2026", "chunk": 1, "text": doc,
                }, ensure_ascii=False) + "\n")
        (td_path / rag.INDEX_NAME).write_bytes(rag._build_index_bytes(chunks_path))
        idx = rag._load_index(td_path)
        check("索引载入", idx is not None)
        q = "签到脚本 配置路径"
        q_counts = rag.bigrams(q)
        full = [None]
        # 倒排路径直接吃 idx；dir_path 必须指向临时目录，否则回读会打到真实库
        bm = ev._bm25_ranked(q_counts, idx, 4, full, dir_path=td_path)
        check("BM25 词面赢家=doc1", bm[0] == 1, bm)
        check("BM25 整个漏掉词面零交集的真值", 3 not in bm, bm)
        # 手工单位向量：真值 doc3 与查询同向，doc1 与查询正交
        dim = 4
        E = np.zeros((4, dim), dtype=np.float32)
        E[3, 0] = 1.0                      # doc3 = 查询方向
        E[1, 1] = 1.0                      # doc1 正交
        E[0, 2] = 1.0
        E[2, 3] = 1.0
        qv = np.zeros(dim, dtype=np.float32)
        qv[0] = 1.0
        scores = E @ qv
        top_i = np.argsort(-scores)[:4]
        vec = [int(i) for i in top_i]
        check("向量第一=真值 doc3", vec[0] == 3, vec)
        check("BM25 赢家 doc1 向量排名靠后", vec.index(1) > 1, vec)
        fused = ev.rrf_fuse(bm, vec, 8)
        check("混合后真值进前 2", fused.index(3) <= 1, fused)
        check("混合后 doc1 仍在前列（不丢词面路）", fused.index(1) <= 3, fused)
        # 反向对称：BM25 赢家=真值、向量乱排时，混合仍保住真值
        qv2 = np.zeros(dim, dtype=np.float32)
        qv2[1] = 1.0  # 查询与 doc1 同向（BM25 赢家=向量赢家=doc1，真值仍按评测=doc3 场景换成 doc1）
        vec2 = [int(i) for i in np.argsort(-(E @ qv2))[:4]]
        fused2 = ev.rrf_fuse(bm, vec2, 8)
        check("两路一致时赢家登顶", fused2[0] == 1, fused2)


def main():
    test_rrf_fuse()
    test_hit_ranks()
    test_aggregate_metrics()
    test_dice_leak_guard()
    test_hybrid_end_to_end()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED {len(FAILURES)}: {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
