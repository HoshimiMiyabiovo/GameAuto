# -*- coding: utf-8 -*-
"""按模型统计 AI 对话史的消息长度（含思维链）。

数据流：
  上游 F:\\data\\AI对话汇总\\data.json（526 会话 / 9 万条消息，每条带 r/t/k/x）
  + 恢复产物 data/ai_history/model/session_models.jsonl（会话级真实模型名）
  + 恢复产物 turn_models.jsonl（逐条模型标注，带原始时间戳）
  → 把每条消息对齐到「当时生效的模型」→ 按模型汇总字数
  → data/ai_history/model/model_length.json + model_length_by_session.jsonl + stdout 报告

口径（读表前必读）：
- **主统计口径 = 模型生成的文本**：ai 正文 + think 思维链。上一轮强调的「包括思维链」
  即指 think 计入主口径。用户消息（user）、工具返回（output）与工具调用摘要（tool）
  都单列参考、不进主口径：tool 是上游把调用参数截断到 200 字再拼前缀生成的**摘要**
  （实测 7724 条恰好 208 字），算进主口径会把中位数钉死在 208，污染所有均长比较。
- **宽口径（并列，不覆盖主口径）= ai + think + output**：用户指出 output 工具返回里
  含模型写的代码修改/补丁回显，应当计入。但 output 同时混着文件内容读取、命令 stdout、
  日志、Traceback 与写入确认（全库 3,535 万字，是主口径的 87.5%），整体计入是**上界
  估计**，不等于纯模型产出。两套口径并列输出、报告可切换，默认仍是主口径。
- 另统计 patch 回显子集：output 中带 diff/apply_patch 标记的条数与字数（启发式，
  只扫正文前 600 字，用来估计「模型写的代码修改」规模，不进任何主口径）。
- **长度是导出后的正文字符数**：上游 build.py 对 user/ai 上限 14000 字截断、对
  think/output 超过 20 万字做中段省略，所以这是归档文本长度，不是原始 API 响应长度。
- **模型归属**：有逐条模型记录的会话，按时间取「不晚于该消息的最后一条模型标注」；
  只有会话级模型的会话（Cursor/Codex/DeepSeek/Hermes 等）整会话用主模型；
  档位/默认标记（deepseek:default、cursor:default）单列，不是具体模型名。
  各来源原始记录与导出消息粒度不等价，这是**近似对齐**，报告里给出回退比例。
- 用户消息与工具返回也会参与对齐比例统计，但不进主口径。

子命令：
  scan     全量统计并写两份产物 + 报告（默认；--top 控制表格行数）
  verify   校验产物总量守恒（各模型字数之和 = 全库 AI 侧字数），不重扫原始数据
"""
import argparse
import collections
import datetime
import json
import os
import statistics
import sys
from bisect import bisect_right
from pathlib import Path
from typing import Dict, List, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SOURCE_JSON = Path(r"F:\data\AI对话汇总\data.json")
MODEL_DIR = PROJECT_ROOT / "data" / "ai_history" / "model"
SESSION_FILE = MODEL_DIR / "session_models.jsonl"
TURN_FILE = MODEL_DIR / "turn_models.jsonl"
OUT_JSON = MODEL_DIR / "model_length.json"
OUT_BY_SESSION = MODEL_DIR / "model_length_by_session.jsonl"

AI_ROLES = ("ai", "think")                    # 模型生成的文本，主统计口径
WIDE_ROLES = ("ai", "think", "output")        # 宽口径：额外把工具返回算进来
REF_ROLES = ("tool", "output", "user")       # 只作参考，不进主口径
PATCH_MARKERS = ("diff --git", "@@ -", "+++ b/", "--- a/", "apply_patch", "replace_in_file")
UNASSIGNED = "(未归属)"


def to_epoch(value) -> float:
    """把各来源的时间戳归一成 epoch 秒；无法解析返回 0。

    数字串 >1e12 视为毫秒；ISO 字符串带 Z 转 UTC，无时区按本机 GMT+8
    （与上游 build.py 的 epoch() 口径一致）；脏值归 0 由调用方兜底。
    """
    s = str(value or "").strip()
    if not s:
        return 0.0
    if s.replace(".", "", 1).isdigit():
        x = float(s)
        if x > 1e12:
            return x / 1000.0
        if x > 1e8:
            return x
        return 0.0
    try:
        dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.timestamp()
    except (ValueError, TypeError):
        return 0.0


def load_models() -> Tuple[Dict[str, dict], Dict[str, List[Tuple[float, str]]]]:
    """读恢复产物：会话级主模型 + 按时间排序的逐条模型记录。"""
    if not SESSION_FILE.exists() or not TURN_FILE.exists():
        print("缺少恢复产物，先跑 tools/ai_history_model_recover.py scan")
        sys.exit(2)
    sessions = {}
    with SESSION_FILE.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                r = json.loads(line)
                sessions[r["id"]] = r
    turns = collections.defaultdict(list)
    with TURN_FILE.open(encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            t = json.loads(line)
            ts = to_epoch(t.get("ts"))
            turns[t["id"]].append((ts, t.get("model") or ""))
    for sid in turns:
        turns[sid].sort(key=lambda p: p[0])
    return sessions, turns


def pick_model(k, pairs, ts_list, fallback: str) -> Tuple[str, str]:
    """返回 (模型名, 对齐方式)。对齐方式用于报告里披露近似程度。"""
    if not pairs:
        return fallback, "session"
    if not k:
        return pairs[0][1] or fallback, "no_ts"
    i = bisect_right(ts_list, float(k)) - 1
    if i < 0:
        # 消息早于首条模型标注：用会话第一条模型，通常是会话开始时的模型
        return pairs[0][1] or fallback, "before_first"
    return pairs[i][1] or fallback, "per_message"


def is_patch(text: str) -> bool:
    """工具返回里是否像代码补丁回显；只扫开头，避免全量扫描 3,500 万字。"""
    head = text[:600]
    return any(k in head for k in PATCH_MARKERS)


def new_agg() -> dict:
    return {"sessions": set(), "n_msgs": 0, "chars": 0, "lengths": [],
            "wide_n": 0, "wide_chars": 0, "wide_lengths": [],
            "patch_n": 0, "patch_chars": 0,
            "roles": collections.defaultdict(lambda: [0, 0]),
            "align": collections.Counter(), "kinds": collections.Counter()}


def cmd_scan(args) -> int:
    sessions, turns = load_models()
    with SOURCE_JSON.open(encoding="utf-8") as f:
        data = json.load(f)

    agg: Dict[str, dict] = {}
    per_session: Dict[Tuple[str, str], dict] = {}
    totals = {"all_msgs": 0, "all_chars": 0, "ai_msgs": 0, "ai_chars": 0,
              "wide_msgs": 0, "wide_chars": 0, "patch_msgs": 0, "patch_chars": 0,
              "roles": collections.defaultdict(lambda: [0, 0]),
              "align": collections.Counter()}

    for s in data.get("sessions") or []:
        sid = str(s.get("id"))
        sm = sessions.get(sid, {})
        fallback = sm.get("primary_model") or ""
        kind = sm.get("model_kind", "none")
        pairs = turns.get(sid, [])
        ts_list = [p[0] for p in pairs]
        for m in s.get("msgs") or []:
            role = m.get("r")
            n = len(str(m.get("x") or ""))
            totals["all_msgs"] += 1
            totals["all_chars"] += n
            totals["roles"][role][0] += 1
            totals["roles"][role][1] += n
            model, align = pick_model(m.get("k"), pairs, ts_list, fallback)
            if not model:
                model, align = UNASSIGNED, "unassigned"
            totals["align"][align] += 1
            a = agg.setdefault(model, new_agg())
            a["kinds"][kind] += 1
            a["sessions"].add(sid)
            a["align"][align] += 1
            a["roles"][role][0] += 1
            a["roles"][role][1] += n
            key = (sid, model)
            ps = per_session.setdefault(key, {"id": sid, "src": s.get("src"),
                                              "model": model, "n_msgs": 0, "chars": 0,
                                              "wide_chars": 0, "patch_chars": 0,
                                              "think_chars": 0, "output_chars": 0,
                                              "user_chars": 0, "align": collections.Counter()})
            ps["align"][align] += 1
            if role == "think":
                ps["think_chars"] += n
            elif role == "output":
                ps["output_chars"] += n
            elif role == "user":
                ps["user_chars"] += n
            text = str(m.get("x") or "")
            if role in WIDE_ROLES:
                a["wide_n"] += 1
                a["wide_chars"] += n
                a["wide_lengths"].append(n)
                totals["wide_msgs"] += 1
                totals["wide_chars"] += n
                ps["wide_chars"] += n
            if role == "output" and is_patch(text):
                a["patch_n"] += 1
                a["patch_chars"] += n
                totals["patch_msgs"] += 1
                totals["patch_chars"] += n
                ps["patch_chars"] += n
            if role in AI_ROLES:
                a["n_msgs"] += 1
                a["chars"] += n
                a["lengths"].append(n)
                totals["ai_msgs"] += 1
                totals["ai_chars"] += n
                ps["n_msgs"] += 1
                ps["chars"] += n

    models = {}
    def stat(n_msgs: int, chars: int, lengths: list) -> dict:
        ordered = sorted(lengths)
        return {"n_msgs": n_msgs, "chars": chars,
                "avg": round(chars / n_msgs, 1) if n_msgs else 0.0,
                "median": round(statistics.median(ordered), 1) if ordered else 0.0,
                "p90": ordered[min(len(ordered) - 1, int(len(ordered) * 0.9))] if ordered else 0}

    for name, a in agg.items():
        models[name] = {
            "model_kind": a["kinds"].most_common(1)[0][0] if a["kinds"] else "none",
            "kind_mix": dict(a["kinds"]),
            "sessions": len(a["sessions"]),
            **stat(a["n_msgs"], a["chars"], a["lengths"]),
            "wide": stat(a["wide_n"], a["wide_chars"], a["wide_lengths"]),
            "patch": {"n": a["patch_n"], "chars": a["patch_chars"]},
            "roles": {r: {"n": v[0], "chars": v[1]} for r, v in a["roles"].items()},
            "align": dict(a["align"]),
        }
    payload = {
        "meta": {
            "source": str(SOURCE_JSON),
            "sessions": len(data.get("sessions") or []),
            "primary_roles": list(AI_ROLES),
            "wide_roles": list(WIDE_ROLES),
            "reference_roles": list(REF_ROLES),
            "patch_markers": list(PATCH_MARKERS),
            "align_rule": "不晚于消息时间的最后一条逐条模型标注；无逐条记录回退会话主模型",
            "length_note": "上游导出正文字符数（user/ai 14000 字截断、think/output 20 万字中段省略）",
        },
        "totals": {
            "all_msgs": totals["all_msgs"], "all_chars": totals["all_chars"],
            "ai_side_msgs": totals["ai_msgs"], "ai_side_chars": totals["ai_chars"],
            "wide_side_msgs": totals["wide_msgs"], "wide_side_chars": totals["wide_chars"],
            "patch_msgs": totals["patch_msgs"], "patch_chars": totals["patch_chars"],
            "roles": {r: {"n": v[0], "chars": v[1]} for r, v in totals["roles"].items()},
            "align": dict(totals["align"]),
        },
        "models": models,
    }
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    # 原子写：先写 .tmp 再替换，避免中途失败留下半截统计文件
    tmp = OUT_JSON.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, OUT_JSON)
    with OUT_BY_SESSION.open("w", encoding="utf-8") as f:
        for (sid, _model), ps in sorted(per_session.items()):
            ps["align"] = dict(ps["align"])
            f.write(json.dumps(ps, ensure_ascii=False) + "\n")
    print(f"已写 {OUT_JSON}（{len(models)} 个模型名）")
    print(f"已写 {OUT_BY_SESSION}（{len(per_session)} 行会话×模型明细）")
    report(payload, args.top)
    return 0


def report(payload: dict, top: int) -> None:
    t, models = payload["totals"], payload["models"]
    roles = t["roles"]
    think = roles.get("think", {}).get("chars", 0)
    output = roles.get("output", {}).get("chars", 0)
    ai = roles.get("ai", {}).get("chars", 0)
    tool = roles.get("tool", {}).get("chars", 0)
    print("\n## 口径与全库概览\n")
    print(f"  全库 {t['all_msgs']:,} 条 / {t['all_chars'] / 10000:,.0f} 万字（导出正文）")
    print(f"  主口径（模型生成：ai 正文 + think 思维链）{t['ai_side_msgs']:,} 条 / "
          f"{t['ai_side_chars'] / 10000:,.0f} 万字")
    print(f"    其中思维链 think {think / 10000:,.0f} 万字（占主口径 "
          f"{think / max(1, t['ai_side_chars']):.1%}）、ai 正文 {ai / 10000:,.0f} 万字")
    print(f"  参考（不计入主口径）：工具调用摘要 tool {tool / 10000:,.0f} 万字"
          f"（上游截断成 200 字摘要）、用户消息 "
          f"{roles.get('user', {}).get('chars', 0) / 10000:,.0f} 万字")
    print(f"  宽口径（ai + think + output）{t['wide_side_msgs']:,} 条 / "
          f"{t['wide_side_chars'] / 10000:,.0f} 万字"
          f"（比主口径多 {output / 10000:,.0f} 万字，是上界估计）")
    print(f"  其中 output 的工具返回 {output / 10000:,.0f} 万字；带补丁标记的回显 "
          f"{t['patch_msgs']:,} 条 / {t['patch_chars'] / 10000:,.1f} 万字"
          f"（占 output {t['patch_chars'] / max(1, output):.1%}，启发式识别）")
    print("  模型归属：" + "，".join(f"{k} {v:,} 条（{v / t['all_msgs']:.1%}）"
                                  for k, v in sorted(t["align"].items(), key=lambda kv: -kv[1])))

    def table(title: str, rows: List[dict], order, note: str, scope: str = "primary") -> None:
        print(f"\n## {title}\n")
        print("| 模型 | 会话 | 条数 | 总字数 | 均长 | 中位 | P90 | 思维链字数 | 思维链占比 |")
        print("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
        for r in order(rows)[:top]:
            m = models[r["name"]]
            s = m["wide"] if scope == "wide" else m
            th = m["roles"].get("think", {}).get("chars", 0)
            print(f"| {r['name']} | {m['sessions']} | {s['n_msgs']:,} | {s['chars']:,} | "
                  f"{s['avg']:.0f} | {s['median']:.0f} | {s['p90']:,} | {th:,} | "
                  f"{th / max(1, s['chars']):.0%} |")
        if note:
            print(f"  {note}")

    named = [{"name": k, "chars": v["chars"], "avg": v["avg"], "n": v["n_msgs"]}
             for k, v in models.items() if v["n_msgs"] > 0 and k != UNASSIGNED]
    table("按总字数（模型产出规模）", named,
          lambda rs: sorted(rs, key=lambda r: -r["chars"]),
          "总字数受会话数/任务量主导，比较「啰嗦程度」请看下一张表。")
    table("按单条均长（样本 ≥100 条）", [r for r in named if r["n"] >= 100],
          lambda rs: sorted(rs, key=lambda r: -r["avg"]),
          "均长 = 主口径字数 / 主口径条数；样本 <100 的模型已剔除。")
    wide = [{"name": k, "chars": v["wide"]["chars"], "avg": v["wide"]["avg"],
             "n": v["wide"]["n_msgs"]}
            for k, v in models.items() if v["wide"]["n_msgs"] > 0 and k != UNASSIGNED]
    table("宽口径总字数（含 output 工具返回）", wide,
          lambda rs: sorted(rs, key=lambda r: -r["chars"]),
          "output 混合了代码补丁回显与文件内容/命令输出/日志，整体计入是上界估计。",
          scope="wide")
    table("宽口径单条均长（样本 ≥100 条）", [r for r in wide if r["n"] >= 100],
          lambda rs: sorted(rs, key=lambda r: -r["avg"]),
          "含 output 后均长会被工具返回长度主导，横向比较前先看每模型的 output 字数。",
          scope="wide")
    tiers = [r for r in named if models[r["name"]]["model_kind"] == "tier"]
    if tiers:
        print("\n## 档位/默认标记（非具体模型名，单列）\n")
        for r in sorted(tiers, key=lambda x: -x["chars"]):
            m = models[r["name"]]
            print(f"  {r['name']}: {m['sessions']} 会话 / {m['n_msgs']:,} 条 / "
                  f"{m['chars']:,} 字 / 均长 {m['avg']:.0f}")
    if UNASSIGNED in models:
        print(f"\n  未归属：{models[UNASSIGNED]['n_msgs']:,} 条 / "
              f"{models[UNASSIGNED]['chars']:,} 字（无模型数据的会话）")


def cmd_verify(_args) -> int:
    if not OUT_JSON.exists():
        print("先跑 scan")
        return 2
    with OUT_JSON.open(encoding="utf-8") as f:
        p = json.load(f)
    t, models = p["totals"], p["models"]
    assert sum(m["n_msgs"] for m in models.values()) == t["ai_side_msgs"], "主口径条数不守恒"
    assert sum(m["chars"] for m in models.values()) == t["ai_side_chars"], "主口径字数不守恒"
    assert sum(m["wide"]["n_msgs"] for m in models.values()) == t["wide_side_msgs"], "宽口径条数不守恒"
    assert sum(m["wide"]["chars"] for m in models.values()) == t["wide_side_chars"], "宽口径字数不守恒"
    assert sum(m["patch"]["chars"] for m in models.values()) == t["patch_chars"], "补丁回显字数不守恒"
    for name, m in models.items():
        assert {"sessions", "n_msgs", "chars", "avg", "median", "roles", "align"} <= set(m), name
    with OUT_BY_SESSION.open(encoding="utf-8") as f:
        rows = sum(1 for line in f if line.strip())
    print(f"校验通过：{len(models)} 个模型名，主口径 {t['ai_side_msgs']:,} 条 / "
          f"{t['ai_side_chars']:,} 字守恒；会话×模型明细 {rows} 行")
    return 0


def main(argv: List[str] = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="按模型统计 AI 对话史消息长度（含思维链）")
    sub = parser.add_subparsers(dest="cmd")
    p_scan = sub.add_parser("scan", help="全量统计并写产物 + 报告")
    p_scan.add_argument("--top", type=int, default=20, help="表格行数上限（默认 20）")
    p_scan.set_defaults(func=cmd_scan)
    sub.add_parser("verify", help="校验产物总量守恒").set_defaults(func=cmd_verify)
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        args = parser.parse_args(["scan", *(argv or [])])
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
