# -*- coding: utf-8 -*-
"""按模型估算「写了多少代码」：以工具调用总数 + ai 正文代码块数为正式主口径。

2026-11-14 用户拍板：衡量「每个模型写了多少代码」只看两个不受导出格式影响的指标——
tool_msgs（每条 tool 消息记一次调用，纯文本摘要的来源也算）与 ai_blocks（ai 正文
fenced code block 数）。其余字数类字段（code_chars / tool_code / patch_code /
think_code）保留作参考，不再作为排名依据。

数据流：
  上游 F:\\data\\AI对话汇总\\data.json（每条消息 r/t/k/x）
  + tools/ai_history_model_length.py 的模型对齐（load_models / pick_model / is_patch）
  → 逐消息抽取代码信号，按模型汇总
  → data/ai_history/model/model_code.json + stdout 报告

四条通道（为什么分开列，读表前必读）：
  1. ai 正文里的 fenced code block（```...```）：最干净、最像「模型贴出的代码」，
     但只有 843/16443 条 ai 消息里有——现代 agent 大多不把代码贴回回复里。
     过滤掉内容不足 8 字的空围栏（Copilot 导出的分隔噪声，全库 308 个）。
  2. tool 调用参数里的 command/content/new_str/code 等字段：模型真正写文件/跑脚本的
     地方；**上游把参数截断到约 200 字**，所以字符数只是下界。ZCode / GitHub Copilot
     的 tool 摘要是纯文本（形如「工具名 + 文件路径」），没有 JSON 字段，这两类来源的参数字数
     会系统性漏掉；因此另记与格式无关的 tool_msgs（全部工具调用条数）作最稳健代理。
  3. output 里带 diff --git / apply_patch 标记的补丁回显：模型写的代码改动被工具
     原样回显，整条字数含 diff 头与上下文行，不全是代码；与通道 2 可能重复。
  4. think 思维链里的 fenced code block：推理时的代码草稿，单列参考，不计入交付量。

参考字段 code_chars = 通道 1 + 2 + 3 的合计，是粗略上界（通道 2/3 可能重复计数同一
改动），单独看又是下界（导出截断），只用于量级参照，不参与排名。模型归属沿用
model_length.py 的时间就近近似对齐；双指标排名与「画像」一并写进产物 rankings。

子命令：
  scan     全量统计并写 model_code.json + 报告（--top 控制表格行数）
  verify   校验各通道字数守恒、主指标等于三通道之和，不重扫上游
"""
import argparse
import collections
import json
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Tuple

# -I 隔离模式下 sys.path 不含脚本目录，显式加入以复用同目录的模型对齐函数
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ai_history_model_length import load_models, pick_model, is_patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SOURCE_JSON = Path(r"F:\data\AI对话汇总\data.json")
MODEL_DIR = PROJECT_ROOT / "data" / "ai_history" / "model"
LENGTH_JSON = MODEL_DIR / "model_length.json"
OUT_JSON = MODEL_DIR / "model_code.json"
UNASSIGNED = "(未归属)"

FENCE_RE = re.compile(r"```[^\n]*\r?\n(.*?)```", re.S)
# tool 摘要形如 `🔧 Bash  {"command": "cd ...\nimport ...`，截断后 JSON 常不闭合，
# 正则允许值一直吃到文本末尾，取到的仍是「被截断后的字段值」，正好是下界。
CODE_FIELD_RE = re.compile(
    r'"(?:command|content|new_str|code|file_text|script)"\s*:\s*"((?:\\.|[^"\\])*)',
    re.S)


def fenced(text: str) -> Tuple[int, int, int]:
    """返回 (代码块数, 代码字符数, 代码行数)；无有效围栏返回全 0。

    GitHub Copilot 的导出里有 308 个空围栏（内容仅一个换行），全库占比 22%，
    且集中在 gpt-5.4；不过滤会把空分隔符当成代码块，直接污染主口径 2。
    """
    blocks = [b for b in FENCE_RE.findall(text) if len(b.strip()) >= 8]
    if not blocks:
        return 0, 0, 0
    return len(blocks), sum(len(b) for b in blocks), sum(b.count("\n") + 1 for b in blocks)


def tool_code(text: str) -> Tuple[int, int]:
    """返回 (含代码字段的调用数, 字段值字符数)；同一条消息多个字段累加字数、算一次调用。"""
    values = CODE_FIELD_RE.findall(text)
    if not values:
        return 0, 0
    return 1, sum(len(v) for v in values)


def new_agg() -> dict:
    return {"sessions": set(), "ai_code": 0, "ai_blocks": 0, "ai_lines": 0, "ai_msgs": 0,
            "tool_code": 0, "tool_calls": 0, "tool_msgs": 0,
            "patch_code": 0, "patch_msgs": 0,
            "think_code": 0, "think_blocks": 0,
            "code_chars": 0, "code_lines": 0, "kinds": collections.Counter()}


def cmd_scan(args) -> int:
    sessions, turns = load_models()
    with SOURCE_JSON.open(encoding="utf-8") as f:
        data = json.load(f)
    primary_chars = {}
    if LENGTH_JSON.exists():
        with LENGTH_JSON.open(encoding="utf-8") as f:
            primary_chars = {k: v["chars"] for k, v in json.load(f)["models"].items()}

    agg: Dict[str, dict] = {}
    totals = collections.Counter()
    by_src: Dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for s in data.get("sessions") or []:
        sid = str(s.get("id"))
        src = s.get("src")
        sm = sessions.get(sid, {})
        fallback = sm.get("primary_model") or ""
        kind = sm.get("model_kind", "none")
        pairs = turns.get(sid, [])
        ts_list = [p[0] for p in pairs]
        for m in s.get("msgs") or []:
            role = m.get("r")
            text = str(m.get("x") or "")
            model, _align = pick_model(m.get("k"), pairs, ts_list, fallback)
            if not model:
                model = UNASSIGNED
            a = agg.setdefault(model, new_agg())
            a["sessions"].add(sid)
            a["kinds"][kind] += 1
            if role in ("ai", "think"):
                blocks, chars, lines = fenced(text)
                if role == "ai":
                    a["ai_code"] += chars
                    a["ai_blocks"] += blocks
                    a["ai_lines"] += lines
                    a["ai_msgs"] += 1 if blocks else 0
                    totals["ai_code"] += chars
                    totals["ai_blocks"] += blocks
                else:
                    a["think_code"] += chars
                    a["think_blocks"] += blocks
                    totals["think_code"] += chars
            elif role == "tool":
                # 工具调用总数与摘要格式无关；字段解析只用于估计代码字数（部分来源为纯文本摘要）
                a["tool_msgs"] += 1
                totals["tool_msgs"] += 1
                by_src[src]["tool_msgs"] += 1
                calls, chars = tool_code(text)
                a["tool_calls"] += calls
                a["tool_code"] += chars
                totals["tool_calls"] += calls
                totals["tool_code"] += chars
                if calls:
                    by_src[src]["parsed"] += 1
            elif role == "output" and is_patch(text):
                a["patch_msgs"] += 1
                a["patch_code"] += len(text)
                totals["patch_msgs"] += 1
                totals["patch_code"] += len(text)

    models = {}
    for name, a in agg.items():
        a["code_chars"] = a["ai_code"] + a["tool_code"] + a["patch_code"]
        a["code_lines"] = a["ai_lines"]
        models[name] = {
            "model_kind": a["kinds"].most_common(1)[0][0] if a["kinds"] else "none",
            "sessions": len(a["sessions"]),
            "code_chars": a["code_chars"], "code_lines": a["code_lines"],
            "share_of_primary": (round(a["code_chars"] / primary_chars[name] * 100, 1)
                                 if primary_chars.get(name) else None),
            "ai_code": a["ai_code"], "ai_blocks": a["ai_blocks"], "ai_msgs": a["ai_msgs"],
            "tool_code": a["tool_code"], "tool_calls": a["tool_calls"],
            "tool_msgs": a["tool_msgs"],
            "patch_code": a["patch_code"], "patch_msgs": a["patch_msgs"],
            "think_code": a["think_code"], "think_blocks": a["think_blocks"],
        }
    # 两个主口径各自排名；前 10 交集标「双高」，单边进前 10 标对应行为画像
    by_tools = [k for k, _ in sorted(models.items(), key=lambda kv: -kv[1]["tool_msgs"])]
    by_blocks = [k for k, _ in sorted(models.items(), key=lambda kv: -kv[1]["ai_blocks"])]
    rank_tools = {k: i + 1 for i, k in enumerate(by_tools)}
    rank_blocks = {k: i + 1 for i, k in enumerate(by_blocks)}
    both = set(by_tools[:10]) & set(by_blocks[:10])
    for name, m in models.items():
        m["rank_tool_msgs"] = rank_tools[name]
        m["rank_ai_blocks"] = rank_blocks[name]
        m["profile"] = ("双高" if name in both else
                        "工具写入型" if rank_tools[name] <= 10 else
                        "贴代码型" if rank_blocks[name] <= 10 else "一般")
    payload = {
        "meta": {
            "source": str(SOURCE_JSON),
            "primary_metrics": ["tool_msgs", "ai_blocks"],
            "primary_note": "2026-11-14 用户拍板：用工具调用总数与 ai 正文代码块数衡量代码产出；"
                            "两者都不受上游摘要格式/200 字截断影响，字数类字段降为参考。",
            "channels": ["ai fenced code", "tool calls (all)", "tool params (truncated ~200 chars)",
                         "output patch echo", "think fenced code (reference only)"],
            "reference_metric": "code_chars = ai_code + tool_code + patch_code（含重复，粗上界，仅参考）",
            "caveat": "tool 参数上游截断到约 200 字（下界）；补丁回显含 diff 上下文；"
                      "同一改动可能同时出现在 tool 参数与 output 回显；ai 正文上限 14000 字",
            "align_rule": "沿用 model_length.py：不晚于消息时间的最后一条逐条模型标注",
        },
        "totals": dict(totals),
        "by_source": {k: dict(v) for k, v in by_src.items()},
        "rankings": {"by_tool_msgs": by_tools, "by_ai_blocks": by_blocks},
        "models": models,
    }
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    tmp = OUT_JSON.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, OUT_JSON)
    print(f"已写 {OUT_JSON}（{len(models)} 个模型名）")
    report(payload, args.top)
    return 0


def report(payload: dict, top: int) -> None:
    t, models = payload["totals"], payload["models"]
    print("\n## 主口径（用户拍板）：工具调用总数 + ai 正文代码块数\n")
    print(f"  工具调用总数 {t.get('tool_msgs', 0):,} 条；ai 正文代码块 "
          f"{t.get('ai_blocks', 0):,} 块 / {t.get('ai_code', 0):,} 字")
    print("  参考通道（字数类，不参与排名）："
          f"tool 参数 {t.get('tool_code', 0):,} 字（{t.get('tool_calls', 0):,} 次可解析）/ "
          f"补丁回显 {t.get('patch_code', 0):,} 字（{t.get('patch_msgs', 0):,} 条）/ "
          f"think 草稿 {t.get('think_code', 0):,} 字")
    low = [(s, c) for s, c in payload["by_source"].items()
           if c.get("tool_msgs", 0) >= 100 and c.get("parsed", 0) / c["tool_msgs"] < 0.2]
    if low:
        print("  摘要解析率偏低、字数类参考值会低估这些来源：" + ", ".join(
            f"{s} {c.get('parsed', 0)}/{c['tool_msgs']}"
            for s, c in sorted(low, key=lambda x: -x[1]["tool_msgs"])))

    def table(title: str, rows: List[dict], key: str, note: str) -> None:
        print(f"\n## {title}\n")
        print("| 模型 | 会话 | 工具调用(全) | ai 代码块 | 画像 | 估码字数(参考) | 工具参数 | 补丁回显 |")
        print("|---|---:|---:|---:|---|---:|---:|---:|")
        for r in sorted(rows, key=lambda x: -x[key])[:top]:
            m = models[r["name"]]
            print(f"| {r['name']} | {m['sessions']} | {m['tool_msgs']:,} | "
                  f"{m['ai_blocks']:,} | {m['profile']} | {m['code_chars']:,} | "
                  f"{m['tool_code']:,} | {m['patch_code']:,} |")
        if note:
            print(f"  {note}")

    all_rows = [{"name": k, "code_chars": v["code_chars"], "ai_blocks": v["ai_blocks"],
                 "tool_msgs": v["tool_msgs"]} for k, v in models.items()]
    named = [r for r in all_rows if r["name"] != UNASSIGNED]
    table("按工具调用总数（主口径 1）", named, "tool_msgs",
          "每条 tool 消息记一次调用，纯文本摘要的来源（ZCode / Copilot）也能计入，"
          "不受 200 字截断影响。")
    table("按 ai 正文代码块数（主口径 2）", named, "ai_blocks",
          "模型直接贴出的代码块，不受工具参数截断影响；会漏掉工具写入的代码，"
          "所以与口径 1 一起看。")
    print("\n## 双指标交叉（按工具调用总数排序）\n")
    print("| 模型 | 工具调用(排名) | ai 代码块(排名) | 画像 | 会话 |")
    print("|---|---:|---:|---|---:|")
    for name in payload["rankings"]["by_tool_msgs"][:top]:
        m = models[name]
        print(f"| {name} | {m['tool_msgs']:,} (#{m['rank_tool_msgs']}) | "
              f"{m['ai_blocks']:,} (#{m['rank_ai_blocks']}) | {m['profile']} | {m['sessions']} |")
    print("  画像：两指标都进前 10 = 双高；只有工具调用进前 10 = 工具写入型；"
          "只有代码块进前 10 = 贴代码型。")
    table("按估算代码字数（参考，非排名依据）", named, "code_chars",
          "含通道重复与 200 字截断，只作量级参照，不再用于判断「谁写代码多」。")
    if UNASSIGNED in models:
        print(f"\n  未归属：{models[UNASSIGNED]['code_chars']:,} 字（参考值）")


def cmd_verify(_args) -> int:
    if not OUT_JSON.exists():
        print("先跑 scan")
        return 2
    with OUT_JSON.open(encoding="utf-8") as f:
        p = json.load(f)
    t, models = p["totals"], p["models"]
    for channel, field in (("ai_code", "ai_code"), ("tool_code", "tool_code"),
                           ("patch_code", "patch_code")):
        assert sum(m[field] for m in models.values()) == t.get(channel, 0), f"{channel} 不守恒"
    assert sum(m["tool_msgs"] for m in models.values()) == t.get("tool_msgs", 0), "主口径1 工具调用总数不守恒"
    assert sum(m["ai_blocks"] for m in models.values()) == t.get("ai_blocks", 0), "主口径2 ai 代码块数不守恒"
    assert len(p["rankings"]["by_tool_msgs"]) == len(models), "工具调用排名不完整"
    assert len(p["rankings"]["by_ai_blocks"]) == len(models), "代码块排名不完整"
    for name, m in models.items():
        assert m["code_chars"] == m["ai_code"] + m["tool_code"] + m["patch_code"], name
    print(f"校验通过：{len(models)} 个模型名；主口径 工具调用 {t.get('tool_msgs', 0):,} 条 / "
          f"ai 代码块 {t.get('ai_blocks', 0):,} 块守恒，双排名齐全；"
          f"参考通道 ai {t.get('ai_code', 0):,} / tool {t.get('tool_code', 0):,} / "
          f"patch {t.get('patch_code', 0):,} 字守恒")
    return 0


def main(argv: List[str] = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="按模型估算写了多少代码（四条通道）")
    sub = parser.add_subparsers(dest="cmd")
    p_scan = sub.add_parser("scan", help="全量统计并写产物 + 报告")
    p_scan.add_argument("--top", type=int, default=20, help="表格行数上限（默认 20）")
    p_scan.set_defaults(func=cmd_scan)
    sub.add_parser("verify", help="校验各通道守恒").set_defaults(func=cmd_verify)
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        args = parser.parse_args(["scan", *(argv or [])])
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
