# -*- coding: utf-8 -*-
"""把 AI 对话史还原出的模型字段注入上游「AI 对话全览」展示页。

背景：外部 ``F:\\data\\_ai_organize_tools\\build.py`` 生成的 ``AI对话全览.html``
是 ``viewer_template.html`` + gzip/base64 内嵌数据（约 43MB 单文件）。
导出时每条消息只留 ``r/t/k/x``，会话最多在 note 里带一个模型名；模型还原结果在
``tools/ai_history_model_recover.py`` 的 ``data/ai_history/model/*.jsonl``。

本工具不改 107MB 的 data.json，也不重新解析任何客户端日志，只做三件事：
  1. 把 ``session_models.jsonl``（真实模型名/分布/口径）与 ``turn_models.jsonl``
     （逐条时间线，每会话截取前 TL_LIMIT 条）压成 JSON，写进模板预留的
     ``<script id="model-data">`` 块；
  2. 把恢复工具的 ``MODEL_ALIASES``（Qoder 内部代号）以及数据里出现过的
     ``copilot/`` 前缀名写成 ``<script id="model-aliases">`` 块——前端用它把
     上游 note 里的旧代号也替换成真实名字，单一来源不重复维护；
  3. 若目标 HTML 是旧模板生成的（缺任一数据块），先从旧 HTML 抽出 B64，
     用新模板重建页面再填数据，旧页面一次升级到新渲染逻辑。

幂等：重复运行只替换两个数据块内容，其余字节不动。写盘走 .tmp + os.replace，
中途失败不会留下半截 HTML。重跑上游 build.py 后会自动调用本脚本（见其 main 末尾），
手动刷新也可以直接跑：``python tools/ai_history_model_inject.py``。

注意：项目 .venv 的解释器（及其子进程）写 F:\\data 会 PermissionError，
本脚本要用系统 python 直接跑；build.py 本身要写 F:\\data，正常重跑也用系统 python。

子命令/参数：
  (默认)            注入并原子替换目标 HTML
  --html PATH       目标展示页（默认 F:\\data\\AI对话汇总\\AI对话全览.html）
  --template PATH   模板（默认 F:\\data\\_ai_organize_tools\\viewer_template.html）
  --verify          只校验，不写盘：两个数据块可解析、渲染逻辑在位、B64 完好
  --rebuild         强制用模板重建页面（改了 viewer_template.html 的渲染逻辑后用）
"""
import argparse
import collections
import json
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Tuple

try:
    # 别名表只在恢复工具里定义一处；缺失时降级为「不改别名」，页面仍可用
    from ai_history_model_recover import MODEL_ALIASES, canonical_model
except ImportError:
    MODEL_ALIASES = {}

    def canonical_model(name: str) -> str:
        return str(name or "")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MODEL_DIR = PROJECT_ROOT / "data" / "ai_history" / "model"
SESSION_FILE = MODEL_DIR / "session_models.jsonl"
TURN_FILE = MODEL_DIR / "turn_models.jsonl"
# 汇总目录/工具链目录可经环境变量覆盖（与 build.py 同口径，缺省=本机现值）
DEFAULT_HTML = Path(os.environ.get("AI_ORGANIZE_OUTDIR") or r"F:\data\AI对话汇总") / "AI对话全览.html"
DEFAULT_TEMPLATE = Path(os.environ.get("AI_ORGANIZE_TOOLS") or r"F:\data\_ai_organize_tools") / "viewer_template.html"

BLOCK_RE = re.compile(r'(<script id="model-data"[^>]*>)(.*?)(</script>)', re.S)
ALIAS_BLOCK_RE = re.compile(r'(<script id="model-aliases"[^>]*>)(.*?)(</script>)', re.S)
B64_START = 'const B64 = "'
TL_LIMIT = 40          # 每会话注入的逐条时间线上限（明细面板只展示前 12 条）
# 渲染逻辑锚点：确认目标页面确实是带模型字段/别名替换的新模板
RENDER_ANCHORS = ("const MODELS =", "modelbox", "s.model_kind", "const ALIAS =")
# 页面主显示里不该再出现的内部代号（raw 字段里保留不算）
FORBIDDEN_DISPLAY = ("dfmodel", "qfmodel", "lmodel", "copilot/", "chat-flash")


def load_index() -> Tuple[Dict[str, dict], dict, Dict[str, str]]:
    """读恢复产物，构造前端索引与别名表；返回 (索引, 统计, 别名表)。"""
    if not SESSION_FILE.exists():
        print(f"缺少 {SESSION_FILE}，先跑 tools/ai_history_model_recover.py scan")
        sys.exit(2)
    index: Dict[str, dict] = {}
    aliases: Dict[str, str] = dict(MODEL_ALIASES)
    with SESSION_FILE.open(encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            r = json.loads(line)
            raw = r.get("models_raw") or {}
            # 通用规则：Copilot 带的 copilot/ 前缀一律去掉（底层模型名）
            for name in raw:
                if str(name).lower().startswith("copilot/"):
                    aliases[name] = canonical_model(name)
            ms = collections.Counter()
            for name, count in (r.get("models") or {}).items():
                ms[canonical_model(name)] += count
            index[r["id"]] = {
                "m": canonical_model(r.get("primary_model", "")),
                "ms": dict(ms),
                "raw": raw,
                "k": r.get("model_kind", "none"),
                "lv": r.get("level", "none"),
                "src": r.get("source", ""),
                "n": r.get("n_modeled", 0),
                "u": r.get("usage_requests", 0),
                "tl": [],
            }
    turns = 0
    if TURN_FILE.exists():
        with TURN_FILE.open(encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                t = json.loads(line)
                rec = index.get(t["id"])
                if rec is None or len(rec["tl"]) >= TL_LIMIT:
                    continue
                rec["tl"].append([t.get("ts", ""), canonical_model(t.get("model", ""))])
                turns += 1
    stats = {
        "sessions": len(index),
        "named": sum(1 for v in index.values() if v["k"] == "named"),
        "tier": sum(1 for v in index.values() if v["k"] == "tier"),
        "timeline_rows": turns,
        "aliases": len(aliases),
    }
    return index, stats, aliases


def payload(obj: dict) -> str:
    """JSON 序列化；转义 '<' 防止模型名里出现 </script> 提前闭合脚本块。"""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")


def extract_b64(html: str) -> str:
    """从已生成的页面里取出内嵌的 gzip+base64 数据（不解压，避免动 107MB 正文）。"""
    start = html.find(B64_START)
    if start < 0:
        raise ValueError("页面里找不到 const B64，可能不是本模板生成的展示页")
    start += len(B64_START)
    end = html.find('";', start)
    if end < 0:
        raise ValueError("B64 字符串结束标记缺失，页面可能被截断")
    return html[start:end]


def fill_blocks(html: str, blocks: List[Tuple[re.Pattern, str]]) -> str:
    """按位置从后往前替换数据块内容，避免多次替换造成偏移。"""
    edits = []
    for rx, text in blocks:
        m = rx.search(html)
        if m:
            edits.append((m.start(2), m.end(2), text))
    for start, end, text in sorted(edits, key=lambda x: -x[0]):
        html = html[:start] + text + html[end:]
    return html


def build_html(html_path: Path, template_path: Path, data: str,
               alias_data: str, force_rebuild: bool = False) -> Tuple[str, str]:
    """返回 (新 HTML 文本, 模式说明)；不写盘。"""
    tpl = template_path.read_text(encoding="utf-8")
    if "/*__B64__*/" not in tpl:
        raise ValueError("模板缺少 /*__B64__*/ 占位，无法重建页面")
    old = html_path.read_text(encoding="utf-8")
    blocks = [(BLOCK_RE, data), (ALIAS_BLOCK_RE, alias_data)]
    if not force_rebuild and all(rx.search(old) for rx, _ in blocks):
        new_html = fill_blocks(old, blocks)
        mode = "替换已有 model-data / model-aliases 块"
    else:
        b64 = extract_b64(old)
        new_html = tpl.replace("/*__B64__*/", b64)
        if not all(rx.search(new_html) for rx, _ in blocks):
            raise ValueError("新模板缺少 model-data / model-aliases 块")
        new_html = fill_blocks(new_html, blocks)
        mode = ("强制按模板重建 + 注入" if force_rebuild
                else "旧页面按新模板重建 + 注入")
        if b64 not in new_html:
            raise ValueError("重建后 B64 数据不一致，已中止")
    for anchor in RENDER_ANCHORS:
        if anchor not in new_html:
            raise ValueError(f"页面缺少渲染逻辑锚点 {anchor}，请先更新 viewer_template.html")
    if "/*__B64__*/" in new_html:
        raise ValueError("占位符未被替换，页面不可用")
    return new_html, mode


def cmd_inject(args) -> int:
    index, stats, aliases = load_index()
    data, alias_data = payload(index), payload(aliases)
    new_html, mode = build_html(args.html, args.template, data, alias_data,
                                getattr(args, "rebuild", False))
    tmp = args.html.with_name(args.html.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(new_html)
    os.replace(tmp, args.html)
    print(f"已注入 {args.html}（{mode}）")
    print(f"  会话 {stats['sessions']}（具体/别名 {stats['named']} · 档位 {stats['tier']}）"
          f" · 时间线 {stats['timeline_rows']} 条 · 真实名映射 {stats['aliases']} 条")
    print(f"  model-data {len(data) / 1024:.0f} KB · 页面 {os.path.getsize(args.html) / 1048576:.2f} MB")
    return 0


def cmd_verify(args) -> int:
    html = args.html.read_text(encoding="utf-8")
    block, ablock = BLOCK_RE.search(html), ALIAS_BLOCK_RE.search(html)
    if not block or not ablock:
        print("FAIL：页面缺少 model-data / model-aliases 数据块")
        return 1
    index, aliases = json.loads(block.group(2)), json.loads(ablock.group(2))
    for anchor in RENDER_ANCHORS:
        if anchor not in html:
            print(f"FAIL：缺少渲染逻辑 {anchor}")
            return 1
    leaked = [(sid, k) for sid, v in index.items() for k in (v.get("ms") or {})
              if any(x in str(k).lower() for x in FORBIDDEN_DISPLAY)]
    if leaked:
        print(f"FAIL：主显示仍有内部代号 {len(leaked)} 处，例如 {leaked[:3]}")
        return 1
    b64 = extract_b64(html)
    print(f"OK：两个数据块可解析，{len(index)} 会话 / {len(aliases)} 条真实名映射，"
          f"有模型字段 {sum(1 for v in index.values() if v.get('m'))} 个；"
          f"主显示无内部代号；B64 {len(b64) / 1048576:.1f} MB 完好")
    return 0


def main(argv: List[str] = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="把还原出的模型字段注入 AI 对话全览展示页")
    parser.add_argument("--html", type=Path, default=DEFAULT_HTML, help="目标展示页")
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE, help="viewer_template.html")
    parser.add_argument("--verify", action="store_true", help="只校验不写盘")
    parser.add_argument("--rebuild", action="store_true",
                        help="强制用模板重建页面（模板渲染逻辑改动后用）")
    args = parser.parse_args(argv)
    return cmd_verify(args) if args.verify else cmd_inject(args)


if __name__ == "__main__":
    sys.exit(main())
