# -*- coding: utf-8 -*-
"""把 model_length.json 渲染成单文件、可排序/筛选/搜索的 HTML 模型数据报告。

数据流：data/ai_history/model/model_length.json（tools/ai_history_model_length.py 的产物）
  → 展开成每模型一行的全字段数组，内嵌进 HTML 的 DATA 常量
  → data/ai_history/model/model_length_report.html（纯静态、零外部依赖、可离线打开）

为什么单独出报告：stdout 表格只放得下 top 20，用户要看「全部模型数据」时，
全量行 × 17 列在终端里没法比较；HTML 表格支持点表头排序、按具体模型/档位筛选、
按名字搜索，还能一眼看出哪些是档位标记（不是具体模型名）与零样本模型。

口径不在本工具重新定义，直接沿用 model_length.json 的 meta/totals，并提供切换：
主口径 = ai 正文 + think 思维链；宽口径 = 主口径 + output 工具返回（用户指出 output
里有模型写的代码补丁回显，但整体计入是上界估计）；tool 摘要 / user 是参考列；
「补丁回显」列单列 output 中带 diff/apply_patch 标记的字数。
长度是导出后正文字符数；模型归属是时间就近的近似对齐。

用法：python tools/ai_history_model_length_report.py
"""
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "data" / "ai_history" / "model" / "model_length.json"
OUT = PROJECT_ROOT / "data" / "ai_history" / "model" / "model_length_report.html"

HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI 对话史 · 全部模型数据</title>
<style>
:root{--bg:#f5f6f8;--panel:#fff;--line:#e3e6ea;--line2:#eef0f3;--text:#1f2328;
--muted:#6b7280;--faint:#9aa2ad;--blue:#2563eb;--blue-bg:#eaf1ff;--amber:#b45309}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
 font:13px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
header{background:var(--panel);border-bottom:1px solid var(--line);padding:18px 26px 14px}
h1{margin:0 0 4px;font-size:19px;font-weight:700}
.sub{color:var(--muted);font-size:12.5px}
.wrap{padding:18px 26px 40px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:10px;margin-bottom:14px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 15px}
.card b{display:block;font-size:21px;font-weight:700;line-height:1.3}
.card span{color:var(--muted);font-size:11.5px}
.note{background:#fffdf5;border:1px solid #f0e2b8;border-radius:9px;padding:10px 14px;
 font-size:12px;color:#6b5b31;margin-bottom:16px}
.note b{color:#5c4a1e}
.toolbar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:10px}
#q{padding:7px 11px;border:1px solid var(--line);border-radius:7px;background:#fff;
 outline:none;font-size:12.5px;min-width:230px}
#q:focus{border-color:var(--blue);box-shadow:0 0 0 3px var(--blue-bg)}
.btn{border:1px solid var(--line);background:#fff;color:var(--muted);padding:6px 11px;
 border-radius:7px;cursor:pointer;font-size:12px}
.btn.on{background:var(--blue);border-color:var(--blue);color:#fff}
select{padding:6px 9px;border:1px solid var(--line);border-radius:7px;background:#fff;
 font-size:12.5px;color:var(--text)}
.count{color:var(--faint);font-size:12px;margin-left:auto}
.tablebox{background:var(--panel);border:1px solid var(--line);border-radius:10px;overflow:auto;max-height:74vh}
table{border-collapse:collapse;width:100%;min-width:1180px;font-size:12px}
thead th{position:sticky;top:0;background:#f8fafc;border-bottom:1px solid var(--line);
 padding:8px 10px;text-align:right;white-space:nowrap;cursor:pointer;color:var(--muted);font-weight:600;z-index:2}
thead th:first-child,tbody td:first-child{text-align:left}
thead th:hover{color:var(--blue);background:var(--blue-bg)}
tbody td{padding:7px 10px;border-bottom:1px solid var(--line2);text-align:right;white-space:nowrap}
tbody tr:hover{background:#f7faff}
tbody tr:last-child td{border-bottom:0}
.name{font-weight:600;max-width:290px;overflow:hidden;text-overflow:ellipsis}
.tag{display:inline-block;border-radius:4px;padding:0 6px;font-size:10.5px;font-weight:600}
.tag.named{background:var(--blue-bg);color:#4338ca;border:1px solid #dfe4ff}
.tag.tier{background:#f3f4f6;color:var(--muted);border:1px solid #e5e7eb}
.tag.none{background:#fff1f1;color:#b45309;border:1px solid #fadcdc}
.pct{display:flex;align-items:center;gap:6px;justify-content:flex-end}
.pct .bar{width:52px;height:6px;background:#eef1f5;border-radius:3px;overflow:hidden}
.pct .fill{height:100%;background:var(--blue)}
.dim{color:var(--faint)}
footer{color:var(--faint);font-size:11.5px;margin-top:12px}
</style>
</head>
<body>
<header>
  <h1>AI 对话史 · 全部模型数据</h1>
  <div class="sub" id="subtitle"></div>
</header>
<div class="wrap">
  <div class="cards" id="cards"></div>
  <div class="note" id="note"></div>
  <div class="toolbar">
    <input id="q" placeholder="搜索模型名…" autocomplete="off">
    <button class="btn on" data-kind="all">全部</button>
    <button class="btn" data-kind="named">具体模型</button>
    <button class="btn" data-kind="tier">档位/默认</button>
    <button class="btn" data-kind="none">未归属</button>
    <button class="btn on" data-scope="primary">主口径</button>
    <button class="btn" data-scope="wide">含 output</button>
    <select id="sort">
      <option value="chars">按总字数</option>
      <option value="avg">按单条均长</option>
      <option value="n">按条数</option>
      <option value="think_pct">按思维链占比</option>
      <option value="sessions">按会话数</option>
      <option value="median">按中位长度</option>
      <option value="p90">按 P90</option>
      <option value="name">按名字</option>
    </select>
    <button class="btn" id="dir">降序 ↓</button>
    <span class="count" id="count"></span>
  </div>
  <div class="tablebox"><table><thead id="thead"></thead><tbody id="tbody"></tbody></table></div>
  <footer id="footer"></footer>
</div>
<script>
const DATA = /*__DATA__*/;
const META = /*__META__*/;
const TOTAL = /*__TOTAL__*/;
const COLS = [
  ["name","模型","str"],["kind","类型","kind"],["sessions","会话","num"],["n","条数","num"],
  ["chars","总字数","num"],["avg","均长","num"],["median","中位","num"],["p90","P90","num"],
  ["ai","ai 正文","num"],["think","思维链","num"],["think_pct","思维链占比","pct"],
  ["output","output 返回","num"],["patch","补丁回显","num"],
  ["tool","tool 摘要","num"],["user","user","num"],["aligned","逐条对齐","num"]
];
const state = {q:"", kind:"all", key:"chars", dir:-1, scope:"primary"};
const WIDE_MAP = {n:"wn", chars:"wchars", avg:"wavg", median:"wmedian", p90:"wp90", think_pct:"wthink_pct"};
const field = k => state.scope === "wide" && WIDE_MAP[k] ? WIDE_MAP[k] : k;
const fmt = n => Number(n||0).toLocaleString("zh-CN");
const esc = s => String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
function cell(row, key, type){
  const v = row[key];
  if(type === "kind") return '<span class="tag '+esc(v)+'">'+({named:"具体模型",tier:"档位",none:"未归属"}[v]||v)+'</span>';
  if(type === "str") return '<span class="name" title="'+esc(v)+'">'+esc(v)+'</span>';
  if(type === "pct") return '<span class="pct"><span class="bar"><span class="fill" style="width:'+v+'%"></span></span>'+v+'%</span>';
  return v === 0 ? '<span class="dim">0</span>' : fmt(v);
}
function updateCards(){
  const wide = state.scope === "wide";
  const msgs = wide ? TOTAL.wide_side_msgs : TOTAL.ai_side_msgs;
  const ch = wide ? TOTAL.wide_side_chars : TOTAL.ai_side_chars;
  const out = (TOTAL.roles.output || {}).chars || 0;
  const cards = [
    ["全库消息", fmt(TOTAL.all_msgs) + " 条", "导出正文 " + (TOTAL.all_chars/10000).toFixed(0) + " 万字"],
    [wide ? "宽口径" : "主口径", fmt(msgs) + " 条",
     (ch/10000).toFixed(0) + " 万字 · " + (wide ? "含 output" : "ai 正文 + 思维链")],
    ["思维链占比", (TOTAL.roles.think.chars / Math.max(1,ch) * 100).toFixed(1) + "%",
     fmt(TOTAL.roles.think.chars) + " 字"],
    ["补丁回显", fmt(TOTAL.patch_msgs) + " 条",
     (TOTAL.patch_chars/10000).toFixed(1) + " 万字 · 占 output " +
     (TOTAL.patch_chars / Math.max(1,out) * 100).toFixed(1) + "%"]
  ];
  document.getElementById("cards").innerHTML = cards.map(c =>
    '<div class="card"><b>'+c[1]+'</b><span>'+c[0]+' · '+c[2]+'</span></div>').join("");
}
function updateNote(){
  const wide = state.scope === "wide";
  document.getElementById("note").innerHTML = "<b>口径</b>：" + (wide
    ? "宽口径 = ai 正文 + think 思维链 + output 工具返回。output 混有模型写的代码补丁回显，但更多是文件内容读取、命令 stdout、日志与写入确认，整体计入是上界估计，不等于纯模型产出。"
    : "主统计 = ai 正文 + think 思维链；tool 调用摘要（上游截断成 200 字）、output 工具返回、user 用户消息只作参考列。")
    + "模型归属按「不晚于该消息的最后一条逐条模型标注」近似对齐，档位/默认标记不是具体模型名。"
    + "点击表头可排序，按钮可筛选与切换口径，输入框可搜索。";
}
function render(){
  updateCards(); updateNote();
  const q = state.q.trim().toLowerCase();
  let rows = DATA.filter(r => (state.kind === "all" || r.kind === state.kind) && (!q || r.name.toLowerCase().includes(q)));
  const k = field(state.key), dir = state.dir;
  rows.sort((a,b) => {
    const x = a[k], y = b[k];
    const c = (typeof x === "string") ? x.localeCompare(y,"zh-CN") : (x - y);
    return c * dir;
  });
  document.getElementById("thead").innerHTML = "<tr>" + COLS.map(([key,label]) =>
    "<th data-key='"+key+"'>"+label+(key === state.key ? (dir < 0 ? " ↓" : " ↑") : "")+"</th>").join("") + "</tr>";
  document.getElementById("tbody").innerHTML = rows.map(r =>
    "<tr>" + COLS.map(([key,,type]) => "<td>"+cell(r,field(key),type)+"</td>").join("") + "</tr>").join("");
  document.getElementById("count").textContent = "显示 " + rows.length + " / " + DATA.length + " 个模型";
  document.getElementById("dir").textContent = state.dir < 0 ? "降序 ↓" : "升序 ↑";
  document.querySelectorAll("th").forEach(th => th.onclick = () => {
    const key = th.dataset.key;
    if(state.key === key) state.dir = -state.dir;
    else { state.key = key; state.dir = (key === "name") ? 1 : -1; }
    const sel = document.getElementById("sort");
    if (sel.querySelector("option[value='"+key+"']")) sel.value = key;
    render();
  });
}
document.getElementById("subtitle").textContent = META.sessions + " 个会话 · " +
  META.length_note + " · 生成源 " + META.source;
document.getElementById("footer").textContent = "由 tools/ai_history_model_length_report.py 从 model_length.json 生成；" +
  "数字口径与 tools/ai_history_model_length.py verify 一致。";
document.getElementById("q").oninput = e => { state.q = e.target.value; render(); };
document.querySelectorAll("[data-kind]").forEach(b => b.onclick = () => {
  state.kind = b.dataset.kind;
  document.querySelectorAll("[data-kind]").forEach(x => x.classList.toggle("on", x === b));
  render();
});
document.querySelectorAll("[data-scope]").forEach(b => b.onclick = () => {
  state.scope = b.dataset.scope;
  document.querySelectorAll("[data-scope]").forEach(x => x.classList.toggle("on", x === b));
  render();
});
document.getElementById("sort").onchange = e => {
  state.key = e.target.value; state.dir = (state.key === "name") ? 1 : -1; render();
};
document.getElementById("dir").onclick = () => { state.dir = -state.dir; render(); };
render();
</script>
</body>
</html>
"""


def build_rows(data: dict) -> list:
    """把嵌套的 models 展开成前端表格用的扁平行，补齐参考列与占比。"""
    rows = []
    for name, m in data["models"].items():
        roles = m.get("roles") or {}

        def chars(role):
            return (roles.get(role) or {}).get("chars", 0)

        rows.append({
            "name": name, "kind": m["model_kind"], "sessions": m["sessions"],
            "n": m["n_msgs"], "chars": m["chars"], "avg": m["avg"],
            "median": m["median"], "p90": m["p90"],
            "ai": chars("ai"), "think": chars("think"), "tool": chars("tool"),
            "output": chars("output"), "user": chars("user"),
            "think_pct": round(chars("think") / m["chars"] * 100) if m["chars"] else 0,
            "wn": m["wide"]["n_msgs"], "wchars": m["wide"]["chars"],
            "wavg": m["wide"]["avg"], "wmedian": m["wide"]["median"],
            "wp90": m["wide"]["p90"],
            "wthink_pct": (round(chars("think") / m["wide"]["chars"] * 100)
                           if m["wide"]["chars"] else 0),
            "patch": m["patch"]["chars"], "patch_n": m["patch"]["n"],
            "aligned": m.get("align", {}).get("per_message", 0),
        })
    return rows


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if not SRC.exists():
        print(f"缺少 {SRC}，先跑 tools/ai_history_model_length.py scan")
        return 2
    with SRC.open(encoding="utf-8") as f:
        data = json.load(f)
    rows = build_rows(data)
    html = (HTML.replace("/*__DATA__*/", json.dumps(rows, ensure_ascii=False))
                .replace("/*__META__*/", json.dumps(data["meta"], ensure_ascii=False))
                .replace("/*__TOTAL__*/", json.dumps(data["totals"], ensure_ascii=False)))
    tmp = OUT.with_suffix(".html.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(html)
    os.replace(tmp, OUT)
    print(f"已生成 {OUT}（{len(rows)} 个模型 / {os.path.getsize(OUT) / 1024:.0f} KB）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
