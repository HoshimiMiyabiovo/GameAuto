# -*- coding: utf-8 -*-
"""AI 对话史 RAG 召回率对比实验：BM25 基线 vs 纯向量 vs 混合 RRF vs 混合+重排。

主工具 ai_history_rag.py **保持不动**（BM25 基线必须干净）；本工具复用它的
bigrams / 倒排候选 / 精确 BM25 函数做模式 A，保证基线与线上 query 同口径。
模型走 ONNX（运行时已有的 onnxruntime-gpu，零 torch）；`tokenizers` 是 dev
依赖（requirements-dev.txt），只给本工具用。下载走 hf-mirror.com
（huggingface.co 直连不通），Xenova/* 是 transformers.js 官方 ONNX 导出。

四种模式（同一语料、同一评估集）：
  A  当前 BM25（倒排候选 → 精确复评，与 query 命令同路径）
  B  纯向量（bge-small-zh-v1.5，余弦 top-k）
  C  混合 RRF（BM25 top-100 ∪ 向量 top-100，score=Σ1/(60+rank)）
  D  混合 + 重排（C 的 top-50 过 bge-reranker-base）

子命令：
  fetch-models   下载两个模型的 ONNX + tokenizer.json 到 eval/models/，
                 首次算 SHA256 写 manifest.json，重跑按 manifest 校验（防半截文件）
  gen-sample     从 chunks.jsonl 分层抽样内容块（滤 base64 垃圾/过短块），
                 落 eval/sample_chunks.jsonl 供人工撰写查询
  check-queries  校验 queries.jsonl：qid 唯一 / 真值存在 / flavor 合法 /
                 语义型与真值块 bigram Dice 超 DICE_LEAK 判泄漏（防"查询抄原文"
                 让 BM25 白捡分，对比必须诚实）
  embed          全量块嵌入：CUDA EP、batch、fp16 memmap、断点续跑
                 （.done 记已完成行数；崩溃后重跑从断点续，绝不重嵌全库）
  run            四模式 × 全部查询，明细落 eval/run_details.jsonl，
                 控制台打分 flavor/总表（chunk R@K、session R@K、MRR、耗时）

数据流：chunks.jsonl ──embed──▶ embeddings.npy（行号=块行号=docid）
        queries.jsonl ──run──▶ run_details.jsonl ──▶ 指标表/报告

评估集口径：真值 = 源块 docid（严格）+ 会话 id（实用——工具以整会话导出为
单位，检索命中同会话任意块即算会话命中）。每个抽样块配对写 kw（关键词型，
含精确符号，BM25 主场）+ sem（语义改述型，刻意避开原文措辞，向量主场）
两条查询——**同一真值两种问法**，隔离"哪种检索赢"这一变量。
"""
import argparse
import hashlib
import json
import os
import random
import re
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "tools"))
import ai_history_rag as rag  # noqa: E402  复用 BM25/倒排/切块工具函数

# ── 实验常量 ─────────────────────────────────────────────────────────
EVAL_DIR = rag.OUT_DIR / "eval"
MODELS_DIR = EVAL_DIR / "models"
EMB_REPO = "Xenova/bge-small-zh-v1.5"      # 24M 参数中文嵌入，512 维，CLS 池化
RR_REPO = "Xenova/bge-reranker-base"       # 278M 参数 XLM-R 交叉编码器
HF_MIRROR = "https://hf-mirror.com"
QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："   # bge-zh 官方查询指令前缀
MAX_SEQ = 512                              # bge 系列推荐上限（超出截断）
RRF_K = 60                                 # RRF 平滑常数（论文默认）
FUSE_TOP = 100                             # BM25/向量各取 top-100 进融合
RERANK_TOP = 50                            # 融合 top-50 送交叉编码器精排
KS = (5, 10, 20)                           # Recall@K 的 K
DICE_LEAK = 0.12                           # sem 查询与真值块 bigram Dice 上限
SEED = 20261110
SAMPLE_N = 55                              # 抽样块数（每块配 kw+sem 两条查询）
SAMPLE_MIN_LEN = 400                       # 太短的块写不出像样的查询
B64_RUN = re.compile(r"[A-Za-z0-9+/]{80,}")  # 内嵌截图 base64 的垃圾块特征


def _read_queries() -> List[Dict]:
    path = EVAL_DIR / "queries.jsonl"
    if not path.exists():
        print(f"评估集不存在: {path}（先 gen-sample 抽样 + 人工撰写）")
        sys.exit(1)
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


# ── fetch-models ─────────────────────────────────────────────────────

_FILES = [
    (EMB_REPO, "onnx/model.onnx", "bge-small-zh-v1.5/model.onnx"),
    (EMB_REPO, "tokenizer.json", "bge-small-zh-v1.5/tokenizer.json"),
    (RR_REPO, "onnx/model.onnx", "bge-reranker-base/model.onnx"),
    (RR_REPO, "tokenizer.json", "bge-reranker-base/tokenizer.json"),
]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _download(url: str, dest: Path) -> None:
    """curl 子进程下载：-L 跟随 hf-mirror 的 307/CDN 重定向，--retry 自动断点续传，
    下载到 .tmp 再 os.replace（防半截文件被后续步骤当真）。

    为什么不用 urllib：本机代理只配在注册表（127.0.0.1:7897），urllib 经代理走
    CDN 时 60s 读超时实测复现；curl 走得通且自带 -C - 续传与 --retry。
    """
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    cmd = ["curl", "-L", "--fail", "--retry", "4", "--retry-all-errors",
           "--retry-delay", "3", "--connect-timeout", "30", "--max-time", "3600",
           "-C", "-", "-o", str(tmp), url]
    t0 = time.time()
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        tail = [ln for ln in (r.stderr or "").strip().splitlines() if ln.strip()][-3:]
        raise RuntimeError(f"curl 下载失败 rc={r.returncode}: {url}\n  " + "\n  ".join(tail))
    os.replace(tmp, dest)
    dt = max(1e-9, time.time() - t0)
    mb = dest.stat().st_size / (1 << 20)
    print(f"  {dest.name}: {mb:.0f}MB / {dt:.0f}s（{mb / dt:.1f}MB/s）")


def cmd_fetch_models(args: argparse.Namespace) -> int:
    manifest_path = MODELS_DIR / "manifest.json"
    manifest: Dict = {}
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for repo, src, rel in _FILES:
        dest = MODELS_DIR / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        url = f"{HF_MIRROR}/{repo}/resolve/main/{src}"
        entry = manifest.get(rel)
        if dest.exists() and entry:
            if _sha256(dest) == entry.get("sha256"):
                print(f"  已就绪且校验通过: {rel}（{dest.stat().st_size >> 20}MB）")
                continue
            print(f"  校验不符（疑似半截/被改），重下: {rel}")
        _download(url, dest)
        manifest[rel] = {"sha256": _sha256(dest), "size": dest.stat().st_size, "source": url}
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"模型齐了 → {MODELS_DIR}")
    return 0


# ── gen-sample ───────────────────────────────────────────────────────

def _chunk_is_usable(text: str) -> bool:
    """抽样准入：够长、非 base64 垃圾、有足够中文（查询要从可理解内容里出）。"""
    if len(text) < SAMPLE_MIN_LEN:
        return False
    if B64_RUN.search(text):
        return False
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return cjk >= 30


def cmd_gen_sample(args: argparse.Namespace) -> int:
    path = rag.OUT_DIR / "chunks.jsonl"
    if not path.exists():
        print("chunks.jsonl 不存在，先跑主工具 build")
        return 1
    by_session: Dict[str, List[int]] = defaultdict(list)
    sess_of_docid: Dict[int, str] = {}
    src_of: Dict[str, str] = {}
    title_of: Dict[str, str] = {}
    texts: Dict[int, str] = {}
    n_all = 0
    with path.open(encoding="utf-8") as f:
        for docid, line in enumerate(f):
            n_all += 1
            c = json.loads(line)
            t = c.get("text") or ""
            if _chunk_is_usable(t):
                sess = str(c.get("session"))
                by_session[sess].append(docid)
                sess_of_docid[docid] = sess
                src_of[sess] = str(c.get("src"))
                title_of[sess] = str(c.get("title"))
                texts[docid] = t
    rnd = random.Random(SEED)
    sessions = sorted(by_session)  # 先 sort 再 shuffle：跨进程可复现
    rnd.shuffle(sessions)
    picked: List[int] = []
    for sess in sessions:
        if len(picked) >= SAMPLE_N:
            break
        docids = sorted(by_session[sess])
        rnd.shuffle(docids)
        take = docids[:2]
        # 同会话取 2 块时隔 ≥3：相邻块正文大量重叠，查询会写得分不清
        if len(take) == 2 and abs(take[0] - take[1]) < 3:
            take = take[:1]
        picked.extend(take)
    picked = picked[:SAMPLE_N]
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    out = EVAL_DIR / "sample_chunks.jsonl"
    with out.open("w", encoding="utf-8") as f:
        for docid in picked:
            sess = sess_of_docid[docid]
            f.write(json.dumps({
                "docid": docid, "session": sess, "src": src_of[sess],
                "title": title_of[sess], "text": texts[docid],
            }, ensure_ascii=False) + "\n")
    srcs = defaultdict(int)
    for docid in picked:
        srcs[src_of[sess_of_docid[docid]]] += 1
    pool = sum(len(v) for v in by_session.values())
    print(f"候选块 {len(picked)} 个（可用块池 {pool}/{n_all}，覆盖会话 "
          f"{len({sess_of_docid[d] for d in picked})} 个，来源分布 {dict(srcs)}）→ {out}")
    print("下一步：通读 sample_chunks.jsonl，逐块写 kw+sem 两条查询 → queries.jsonl → check-queries")
    return 0


# ── check-queries ────────────────────────────────────────────────────

def dice_coeff(a: set, b: set) -> float:
    """Dice 系数：2|A∩B|/(|A|+|B|)，衡量查询与真值块用词重合度。"""
    if not a or not b:
        return 0.0
    return 2 * len(a & b) / (len(a) + len(b))


def cmd_check_queries(args: argparse.Namespace) -> int:
    queries = _read_queries()
    gt_docids = {q["gt_docid"] for q in queries}
    gt_texts: Dict[int, str] = {}
    sessions: Dict[int, str] = {}
    with (rag.OUT_DIR / "chunks.jsonl").open(encoding="utf-8") as f:
        for docid, line in enumerate(f):
            if docid in gt_docids:
                c = json.loads(line)
                gt_texts[docid] = c.get("text") or ""
                sessions[docid] = str(c.get("session"))
    problems: List[str] = []
    seen_qids = set()
    flavors: Dict[str, int] = defaultdict(int)
    rows = []
    for q in queries:
        qid = str(q.get("qid"))
        if qid in seen_qids:
            problems.append(f"{qid}: qid 重复")
        seen_qids.add(qid)
        docid = q.get("gt_docid")
        if docid not in gt_texts:
            problems.append(f"{qid}: gt_docid {docid} 不在语料")
            continue
        if sessions[docid] != str(q.get("gt_session")):
            problems.append(f"{qid}: gt_session 与语料不符（{q.get('gt_session')} ≠ {sessions[docid]}）")
        flavor = str(q.get("flavor"))
        flavors[flavor] += 1
        if flavor not in ("kw", "sem"):
            problems.append(f"{qid}: flavor 必须是 kw/sem，收到 {flavor}")
        if len(str(q.get("query", ""))) < 6:
            problems.append(f"{qid}: 查询太短")
        d = dice_coeff(set(rag.bigrams(str(q.get("query")))),
                       set(rag.bigrams(gt_texts[docid])))
        rows.append((qid, flavor, d))
        if flavor == "sem" and d > DICE_LEAK:
            problems.append(f"{qid}: sem 泄漏 Dice={d:.3f} > {DICE_LEAK}（查询抄了原文措辞，重写）")
    print(f"{'qid':<12}{'flavor':<6}{'Dice':>7}")
    for qid, flavor, d in rows:
        print(f"{qid:<12}{flavor:<6}{d:>7.3f}")
    sems = [d for _, f, d in rows if f == "sem"]
    kws = [d for _, f, d in rows if f == "kw"]
    if sems:
        print(f"sem Dice: 中位 {sorted(sems)[len(sems) // 2]:.3f} / 最大 {max(sems):.3f}（阈值 {DICE_LEAK}）")
    if kws:
        print(f"kw  Dice: 中位 {sorted(kws)[len(kws) // 2]:.3f} / 最小 {min(kws):.3f}"
              f"（kw 应显著更高，否则没起到'带精确词'作用）")
    print(f"flavor 计数: {dict(flavors)}")
    if problems:
        print("问题：")
        for p in problems:
            print(f"  ✗ {p}")
        return 1
    print("check-queries OK")
    return 0


# ── ONNX 会话 / 编码 ─────────────────────────────────────────────────

def _add_nvidia_dll_dirs() -> None:
    """把 pip 装的 CUDA/cuDNN DLL 目录加进进程 DLL 搜索路径。

    onnxruntime_gpu 的 providers_cuda.dll 依赖 cublasLt64_12.dll / cudnn64_9.dll
    等——pip 包（nvidia-cublas-cu12 等）落在 site-packages/nvidia/*/bin/，但
    ORT 不会自动搜那里，缺这一步 CUDA EP 必然创建失败回落 CPU（实测 64k 块
    13 块/s vs GPU 数十倍）。必须在创建 InferenceSession 之前调用。

    为什么 PATH 前置而不是 os.add_dll_directory：ORT 用
    LOAD_WITH_ALTERED_SEARCH_PATH 载 provider DLL，该搜索序**不走**
    add_dll_directory 注册的目录，只走老式 PATH 搜索——实测 add_dll_directory
    无效、PATH 前置生效。两招都上：PATH 给 provider 主依赖，add_dll_directory
    兜住走新式 flag 的传递依赖。
    """
    base = Path(sys.prefix) / "Lib" / "site-packages" / "nvidia"
    if not base.is_dir():
        return
    bin_dirs = [p / "bin" for p in sorted(base.iterdir()) if (p / "bin").is_dir()]
    for d in bin_dirs:
        try:
            os.add_dll_directory(str(d))
        except OSError:
            pass  # 目录不可用就跳过，交给 ORT 自己的报错说话
    prepended = os.pathsep.join(str(d) for d in bin_dirs)
    os.environ["PATH"] = prepended + os.pathsep + os.environ.get("PATH", "")


def _ort_session(model_path: Path):
    import onnxruntime as ort
    # CUDA 优先、CPU 兜底：CUDA DLL 加载失败时 ORT 会警告并回落，别静默——打印实际 provider
    _add_nvidia_dll_dirs()
    sess = ort.InferenceSession(str(model_path),
                                providers=["CUDAExecutionProvider", "CPUExecutionProvider"])
    providers = sess.get_providers()
    if providers[0] != "CUDAExecutionProvider":
        print(f"  [警告] CUDA EP 未生效，实际 {providers}（嵌入/重排将走 CPU，慢一个量级）")
    print(f"  ORT providers: {providers}（{model_path.parent.name}/{model_path.name}）")
    return sess


def _pad_batch(encs) -> Tuple[np.ndarray, np.ndarray]:
    """手工右填充（不开 tokenizers 的 enable_padding——批次内动态 pad 省算力）。"""
    maxlen = max(len(e.ids) for e in encs)
    ids = np.zeros((len(encs), maxlen), dtype=np.int64)
    mask = np.zeros((len(encs), maxlen), dtype=np.int64)
    for i, e in enumerate(encs):
        ln = len(e.ids)
        ids[i, :ln] = e.ids
        mask[i, :ln] = e.attention_mask
    return ids, mask


def _run_encoder(sess, feed_names: Sequence[str], ids: np.ndarray, mask: np.ndarray) -> np.ndarray:
    feed = {"input_ids": ids, "attention_mask": mask}
    if "token_type_ids" in feed_names:
        feed["token_type_ids"] = np.zeros_like(ids)
    out = sess.run(None, feed)
    return next(o for o in out if o.ndim == 3)  # last_hidden_state（不要 pooler tanh）


def _l2norm(vecs: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return vecs / norms


# ── embed ────────────────────────────────────────────────────────────

def cmd_embed(args: argparse.Namespace) -> int:
    from tokenizers import Tokenizer
    tok = Tokenizer.from_file(str(MODELS_DIR / "bge-small-zh-v1.5" / "tokenizer.json"))
    tok.enable_truncation(max_length=MAX_SEQ)
    sess = _ort_session(MODELS_DIR / "bge-small-zh-v1.5" / "model.onnx")
    feed_names = [i.name for i in sess.get_inputs()]
    # 块总数优先取索引头（免全文件重扫）；索引过期就数行
    idx = rag._load_index(rag.OUT_DIR)
    if idx is not None:
        n = idx["header"]["n"]
    else:
        n = sum(1 for l in (rag.OUT_DIR / "chunks.jsonl").open(encoding="utf-8") if l.strip())
    emb_path = EVAL_DIR / "embeddings.npy"
    done_path = EVAL_DIR / "embeddings.done"
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    # 先探一个样本拿维度（bge-small-zh=512，但写死会埋雷）
    ids, mask = _pad_batch([tok.encode("维度探针")])
    dim = _run_encoder(sess, feed_names, ids, mask).shape[-1]
    done = 0
    if emb_path.exists() and done_path.exists():
        done = int(done_path.read_text().strip() or 0)
        if done >= n:
            print(f"嵌入已完整（{done}/{n}），跳过。重嵌先删 embeddings.npy/.done")
            return 0
    mmap = np.lib.format.open_memmap(emb_path, mode="r+" if done else "w+",
                                     dtype=np.float16, shape=(n, dim))
    if done:
        print(f"断点续跑：从 {done}/{n} 继续")
    t0 = time.time()
    done0 = done
    batch = args.batch
    with (rag.OUT_DIR / "chunks.jsonl").open(encoding="utf-8") as f:
        buf_texts: List[str] = []
        for docid, line in enumerate(f):
            if docid < done:
                continue
            buf_texts.append(json.loads(line).get("text") or "")
            # 未满一批且后面还有块 → 继续攒；最后一块强制落批
            if len(buf_texts) < batch and docid + 1 < n:
                continue
            i0 = docid + 1 - len(buf_texts)
            encs = tok.encode_batch(buf_texts)
            ids, mask = _pad_batch(encs)
            vecs = _run_encoder(sess, feed_names, ids, mask)[:, 0, :].astype(np.float32)
            mmap[i0:docid + 1] = _l2norm(vecs).astype(np.float16)
            mmap.flush()
            done_path.write_text(str(docid + 1))  # 先写数据后写断点：崩溃最多重嵌一批
            buf_texts = []
            if (docid + 1) % (batch * 10) < batch:
                rate = (docid + 1 - done0) / max(1e-9, time.time() - t0)
                eta = (n - docid - 1) / max(1e-9, rate)
                print(f"  {docid + 1}/{n}  {rate:.0f} 块/s  ETA {eta / 60:.0f}min", flush=True)
    done_path.write_text(str(n))
    el = time.time() - t0
    print(f"嵌入完成：{n} 块 × {dim} 维 fp16（{emb_path.stat().st_size >> 20}MB，{el / 60:.1f}min）")
    return 0


# ── run ──────────────────────────────────────────────────────────────

def rrf_fuse(rank_a: Sequence[int], rank_b: Sequence[int], top: int) -> List[int]:
    """倒数排名融合：两路各按名次贡献 1/(60+rank)，docid 分数求和降序。"""
    scores: Dict[int, float] = defaultdict(float)
    for ranked in (rank_a, rank_b):
        for r, d in enumerate(ranked, 1):
            scores[d] += 1.0 / (RRF_K + r)
    return [d for d, _ in sorted(scores.items(), key=lambda kv: -kv[1])[:top]]


def _bm25_ranked(q_counts: Dict, idx, k: int, full_chunks: List,
                 dir_path: Optional[Path] = None) -> List[int]:
    """BM25 top-k（复用主工具倒排候选 + 精确复评；索引缺失回退全量扫）。

    dir_path 缺省 rag.OUT_DIR；测试注入临时目录时必须显式传入，否则回读块
    原文会打到真实库上（块行号错位）。
    """
    dp = dir_path if dir_path is not None else rag.OUT_DIR
    if idx is not None:
        cand = rag._index_candidates(idx, q_counts)
        if cand is not None:
            pairs = rag._load_chunks_by_ids(dp, idx, [d for d, _ in cand])
            return [d for _, d in rag._bm25_scores(q_counts, pairs, idx["header"]["avgdl"])[:k]]
    if full_chunks[0] is None:
        full_chunks[0] = rag._load_chunks()
    chunks = full_chunks[0]
    avgdl = sum(len(c["text"]) for c in chunks) / max(1, len(chunks))
    return [d for _, d in rag._bm25_scores(q_counts, list(enumerate(chunks)), avgdl)[:k]]


def _load_texts(docids: Sequence[int], idx, full_chunks: List,
                dir_path: Optional[Path] = None) -> List[str]:
    """按 docid 回读块正文（有索引走行偏移 seek，无索引兜底全量内存）。"""
    dp = dir_path if dir_path is not None else rag.OUT_DIR
    if idx is not None:
        pairs = rag._load_chunks_by_ids(dp, idx, list(docids))
        return [c["text"] for _, c in pairs]
    if full_chunks[0] is None:
        full_chunks[0] = rag._load_chunks()
    return [full_chunks[0][d]["text"] for d in docids]


def _rerank_scores(query: str, texts: List[str], tok, sess, feed_names: Sequence[str],
                   batch: int = 32) -> List[float]:
    """交叉编码器打分：query+chunk 成对进模型，logit 越大越相关。"""
    out: List[float] = []
    for i0 in range(0, len(texts), batch):
        pairs = [(query, t) for t in texts[i0:i0 + batch]]
        encs = tok.encode_batch(pairs)  # pair 编码：<s>q</s></s>t</s>（XLM-R 格式）
        ids, mask = _pad_batch(encs)
        feed = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in feed_names:
            feed["token_type_ids"] = np.zeros_like(ids)
        logits = sess.run(None, feed)[0]
        out.extend(float(x) for x in logits[:, 0])
    return out


def _hit_ranks(ranked: Sequence[int], gt_docid: int, gt_session: str,
               sess_of: Sequence[str]) -> Tuple[Optional[int], Optional[int]]:
    """(块命中位次, 首个会话命中位次)，1-based，未命中 None。"""
    chunk_rank = next((i + 1 for i, d in enumerate(ranked) if d == gt_docid), None)
    sess_rank = next((i + 1 for i, d in enumerate(ranked) if sess_of[d] == gt_session), None)
    return chunk_rank, sess_rank


def cmd_run(args: argparse.Namespace) -> int:
    from tokenizers import Tokenizer
    queries = _read_queries()
    idx = rag._load_index(rag.OUT_DIR)
    if idx is None:
        print("[警告] 倒排索引缺失/过期——BM25 走全量扫（结果口径不变，只是慢）")
    n = idx["header"]["n"] if idx else None
    # docid→session 一遍流式（只留 session 字符串，不驻留正文）
    sess_of: List[str] = []
    with (rag.OUT_DIR / "chunks.jsonl").open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                sess_of.append(str(json.loads(line).get("session")))
    if n is None:
        n = len(sess_of)
    assert n == len(sess_of), f"索引块数 {n} ≠ chunks 行数 {len(sess_of)}，重跑主工具 build"
    full_chunks: List = [None]  # 惰性全量加载（仅索引缺失兜底时用）

    # ── 向量路 ──
    E = np.load(EVAL_DIR / "embeddings.npy").astype(np.float32)
    assert E.shape[0] == n, f"嵌入 {E.shape[0]} 块 ≠ 语料 {n} 块（重跑 embed）"
    norms = np.linalg.norm(E, axis=1)
    assert abs(norms.mean() - 1.0) < 0.02, f"嵌入范数异常 mean={norms.mean():.3f}（重跑 embed）"
    emb_tok = Tokenizer.from_file(str(MODELS_DIR / "bge-small-zh-v1.5" / "tokenizer.json"))
    emb_tok.enable_truncation(max_length=MAX_SEQ)
    emb_sess = _ort_session(MODELS_DIR / "bge-small-zh-v1.5" / "model.onnx")
    emb_feed = [i.name for i in emb_sess.get_inputs()]
    # 查询端带 bge 官方指令前缀；语料端（建库时）不带——不对称是官方用法
    q_encs = emb_tok.encode_batch([QUERY_PREFIX + q["query"] for q in queries])
    q_ids, q_mask = _pad_batch(q_encs)
    Q = _l2norm(_run_encoder(emb_sess, emb_feed, q_ids, q_mask)[:, 0, :].astype(np.float32))

    # ── 重排路：先载好再计时（1GB 模型懒载若混进首查询会污染 D 的耗时统计）──
    rr_tok = Tokenizer.from_file(str(MODELS_DIR / "bge-reranker-base" / "tokenizer.json"))
    rr_tok.enable_truncation(max_length=MAX_SEQ)
    rr_sess = _ort_session(MODELS_DIR / "bge-reranker-base" / "model.onnx")
    rr_feed = [i.name for i in rr_sess.get_inputs()]

    details: List[Dict] = []
    lat: Dict[str, List[float]] = defaultdict(list)
    for qi, q in enumerate(queries):
        qv = Q[qi]
        q_counts = rag.bigrams(q["query"])
        # A：BM25（倒排候选 → 精确复评）
        t0 = time.perf_counter()
        bm = _bm25_ranked(q_counts, idx, FUSE_TOP, full_chunks)
        lat["A"].append(time.perf_counter() - t0)
        # B：向量余弦（语料已 L2 归一，点积=余弦）
        t0 = time.perf_counter()
        scores = E @ qv
        top_i = np.argpartition(-scores, min(FUSE_TOP, len(scores) - 1))[:FUSE_TOP]
        vec = [int(i) for i in top_i[np.argsort(-scores[top_i])]]
        lat["B"].append(time.perf_counter() - t0)
        # C：RRF 融合（并集全量融合，D 只取前 RERANK_TOP）
        t0 = time.perf_counter()
        fused = rrf_fuse(bm, vec, len(bm) + len(vec))
        lat["C"].append(time.perf_counter() - t0)
        # D：混合 top-50 过交叉编码器
        t0 = time.perf_counter()
        cand = fused[:RERANK_TOP]
        rr_scores = _rerank_scores(q["query"], _load_texts(cand, idx, full_chunks),
                                   rr_tok, rr_sess, rr_feed)
        rerank = [d for _, d in sorted(zip(rr_scores, cand), key=lambda kv: -kv[0])]
        lat["D"].append(time.perf_counter() - t0)

        ranked_by_mode = {"A": bm, "B": vec, "C": fused, "D": rerank}
        for mode, ranked in ranked_by_mode.items():
            cr, sr = _hit_ranks(ranked[:40], q["gt_docid"], q["gt_session"], sess_of)
            details.append({"qid": q["qid"], "flavor": q["flavor"], "mode": mode,
                            "gt_docid": q["gt_docid"], "gt_session": q["gt_session"],
                            "top20": ranked[:20], "chunk_hit_rank": cr, "session_hit_rank": sr})
        flag = lambda r: _hit_ranks(r, q["gt_docid"], q["gt_session"], sess_of)[1]  # noqa: E731
        print(f"  [{qi + 1}/{len(queries)}] {q['qid']} ({q['flavor']}) "
              f"A:{flag(bm)} B:{flag(vec)} C:{flag(fused)} D:{flag(rerank)}", flush=True)
    det_path = EVAL_DIR / "run_details.jsonl"
    det_path.write_text("\n".join(json.dumps(d, ensure_ascii=False) for d in details) + "\n",
                        encoding="utf-8")
    _print_report(details, lat)
    print(f"\n明细 → {det_path}")
    return 0


def aggregate_metrics(rows: List[Dict], mode: str) -> Tuple[List[float], List[float], float]:
    """聚合一种模式在给定查询子集上的指标：(chunk R@K 列表, session R@K 列表, 会话 MRR)。"""
    sub = [d for d in rows if d["mode"] == mode]
    if not sub:
        return [0.0] * len(KS), [0.0] * len(KS), 0.0
    c_at = [sum(1 for d in sub if d["chunk_hit_rank"] and d["chunk_hit_rank"] <= k) / len(sub)
            for k in KS]
    s_at = [sum(1 for d in sub if d["session_hit_rank"] and d["session_hit_rank"] <= k) / len(sub)
            for k in KS]
    mrr = sum(1.0 / d["session_hit_rank"] for d in sub if d["session_hit_rank"]) / len(sub)
    return c_at, s_at, mrr


def _print_report(details: List[Dict], lat: Dict[str, List[float]]) -> None:
    """指标表：chunk R@K（严格）/ session R@K（实用）/ MRR（会话口径）/ 平均耗时。"""
    modes = ["A", "B", "C", "D"]
    names = {"A": "BM25基线", "B": "纯向量", "C": "混合RRF", "D": "混合+重排"}

    scopes = [("全部", details)] + \
        [(f, [d for d in details if d["flavor"] == f]) for f in ("kw", "sem")]
    for scope, rows in scopes:
        nq = len([d for d in rows if d["mode"] == "A"])
        print(f"\n=== {scope}（{nq} 查询）===")
        print(f"{'模式':<10}{'cR@5':>7}{'cR@10':>7}{'cR@20':>7} | {'sR@5':>7}{'sR@10':>7}{'sR@20':>7} | {'MRR':>6} | {'ms':>7}")
        for m in modes:
            c, s, mrr = aggregate_metrics(rows, m)
            avg_ms = sum(lat[m]) / max(1, len(lat[m])) * 1000
            print(f"{names[m]:<10}" + "".join(f"{v:>7.1%}" for v in c) + " |"
                  + "".join(f"{v:>7.1%}" for v in s) + f" | {mrr:>6.3f} | {avg_ms:>7.0f}")


# ── main ─────────────────────────────────────────────────────────────

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("fetch-models", help="下载/校验嵌入与重排模型（hf-mirror）")
    sub.add_parser("gen-sample", help="分层抽样评估候选块")
    sub.add_parser("check-queries", help="校验评估集（泄漏/配对/真值）")
    e = sub.add_parser("embed", help="全量块嵌入（断点续跑）")
    e.add_argument("--batch", type=int, default=128)
    sub.add_parser("run", help="四模式检索对比")
    args = ap.parse_args(argv)
    return {"fetch-models": cmd_fetch_models, "gen-sample": cmd_gen_sample,
            "check-queries": cmd_check_queries, "embed": cmd_embed,
            "run": cmd_run}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
