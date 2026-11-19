# -*- coding: utf-8 -*-
"""AI 对话史「模型名」还原：从各客户端原始日志恢复被导出/建库丢掉的 model 字段。

背景：外部 ``F:\\data\\_ai_organize_tools\\build.py`` 生成 ``AI对话汇总/data.json`` 时，
每条消息只保留 ``r/t/k/x``，模型名最多以会话级 note 出现（「模型：xxx」或
fetch 侧「· 模型 xxx」无冒号格式，两种都认）；``tools/ai_history_rag.py build``
连 note 都没复制，所以 RAG 库 103 会话里查不到模型。
但原始日志/SQLite 里其实逐条带模型：ZCode 的 message.data / model_usage、
WorkBuddy 的 providerData.model、Qoder/Claude Code/MiniMax 的 message.model、
OpenCode 的 session_message.data.model、Copilot 的 requests[].modelId、
TeleAgent/码道内核三表的 message.modelID（opencode 变体，同 Kilo）等。

数据流（本工具全程只读原始文件，不改上游、不改 RAG 库）：
  上游 data.json（会话清单 + note，作为对齐基准）
  → 各来源恢复器按 build.py 的会话 ID 规则重建 key，抽取逐条/会话级模型
  → data/ai_history/model/session_models.jsonl（每会话一行，含 in_rag 标记）
  → data/ai_history/model/turn_models.jsonl（逐条模型记录，仅逐条来源）
  → stdout 覆盖率报告（全量口径 + RAG 口径 + 分来源 + 模型分布 + note 对比）

口径与坑：
- 会话 ID 必须复刻 build.py 的规则（前缀 + stem 截断），否则挂不上上游会话；
  匹配率会打印出来，低了说明规则或原始文件位置变了。
- 「逐条」指原始记录里模型标注的粒度，各来源不等价于导出消息数：ZCode 一条
  assistant 消息可能对应多次 model_usage 请求，WorkBuddy 一个 messageId 会拆成
  reasoning/message/function_call 多行（本工具按 messageId 去重）。所以报告以
  **会话覆盖率** 为准，逐条记录数只作规模参考，不做「消息级覆盖率」的假精确。
- 会话级来源（DeepSeek/Codex/Cursor/Copilot transcript）只能给一个模型或少量
  切换点；逐条来源也允许同一会话出现多个模型（换模型是常态）。
- 档位/默认标记（DeepSeek 的 default/expert/vision、Cursor 的 default）不是模型
  ID，单独记为 ``level="tier"``，与具体/别名模型名分开统计——不能把「有线索」
  说成「有模型名」，也不能当占位值丢掉（早期版本丢 default 误判 170 个会话）。
- 模型名输出统一走 ``canonical_model()``：Qoder 内部代号与 Copilot 的 copilot/
  前缀换成真实模型名，原值留在 ``models_raw`` / ``model_raw`` / ``note_model_raw``
  里可追溯。映射只改 ``MODEL_ALIASES`` 一处，展示页由注入脚本同步。
- Hermes 的 state.db 带回滚日志且目录只读，须复制到临时目录让 SQLite 完成恢复；
  临时副本用完即删，绝不改动原库。

子命令：
  scan    全量恢复并写两份 JSONL + 打印报告（全量覆盖，不做增量）
  verify  校验产物行数/字段/ID 唯一性（不重扫原始源）

用法：.venv/Scripts/python.exe -I tools/ai_history_model_recover.py scan
"""
import argparse
import collections
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# 数据源：默认 F: 汇总目录的 data.json；build.py 串链时用 AI_ORGANIZE_SOURCE
# 传**本轮暂存**的 data.json——否则 recover 读到上一轮拷回的旧库，模型恢复
# 永远落后一轮（2026-11-18 实锤：本轮 579 会话只恢复出 562）
SOURCE_JSON = Path(os.environ.get("AI_ORGANIZE_SOURCE") or os.path.join(
    os.environ.get("AI_ORGANIZE_OUTDIR") or r"F:\data\AI对话汇总", "data.json"))
RAG_SESSIONS = PROJECT_ROOT / "data" / "ai_history" / "sessions.jsonl"
OUT_DIR = PROJECT_ROOT / "data" / "ai_history" / "model"
SESSION_OUT = OUT_DIR / "session_models.jsonl"
TURN_OUT = OUT_DIR / "turn_models.jsonl"

ASUS = Path(r"F:\data\asus")
ZCODE_DB = Path(r"C:\Users\admin\.zcode\cli\db\db.sqlite")
WB_ROOT = Path(r"C:\Users\admin\.workbuddy")
WB_DB = WB_ROOT / "workbuddy.db"
QODER_ROOT = Path(r"C:\Users\admin\.qoder")
QODER_DB = Path(r"C:\Users\admin\AppData\Roaming\com.qoder.app.stable\main.sqlite")
OC_DB = Path(r"C:\Users\admin\.local\share\opencode\opencode.db")
OC_OLD_DB = Path(r"C:\Users\admin\opencode_backup_20260903_212346\data\opencode.db")
CLAUDE_LOCAL = Path(r"C:\Users\admin\.claude")
MM_ROOT = Path(r"C:\Users\admin\.minimax\v2\sessions")
HERMES_DB = Path(r"F:\Program Files\Hermes Agent CN Desktop\data\hermes-home\state.db")
CATPAW_ROOT = Path(os.environ.get("APPDATA", os.path.expanduser("~"))) / "catpaw-moon"
CATPAW_LOG_ROOT = Path(os.path.expanduser("~")) / ".meituan-catpaw"
TRAE_JSON = PROJECT_ROOT / "data" / "trae_capture" / "export" / "trae.json"
KILO_RAW = PROJECT_ROOT / "data" / "kilo_capture" / "raw"
# opencode 变体三表来源的抓取 raw（teleagent/codearts_fetch 导出，Kilo 是内核
# HTTP API 的另一形状，走 recover_kilo 自己的 raw 格式）。raw/<ses_id>/messages.json
TELEAGENT_RAW = PROJECT_ROOT / "data" / "teleagent_capture" / "raw"
CODEARTS_RAW = PROJECT_ROOT / "data" / "codearts_capture" / "raw"
# 来源固定模型（2026-11-17 用户拍板）：Trae 分享接口不带模型字段、本地正文加密，
# 但用户明确该来源跑的就是 deepseek-v4.1-flash——整源记账。只适用于**没有**
# 模型字段的来源；有字段的（如 Kilo 的逐条 modelID）一律用真实字段。
TRAE_FIXED_MODEL = "deepseek-v4.1-flash"
CATPAW_LOG_RE = re.compile(
    r"conversationId=([0-9a-f-]{36}).*?turn=(\d+).*?model=(\d+).*?\[LLM\] start")
CATPAW_TS_RE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+)")
CURSOR_DB = ASUS / "Cursor" / "User" / "globalStorage" / "state.vscdb"
DEEP_INDEX = Path(r"F:\data\deepseek-all-chats\sessions_index.json")
DEEP_MD = Path(r"F:\data\deepseek-all-chats\markdown")

# 模型名里的占位值：不是真实模型，恢复时剔除
PLACEHOLDER = {"", "?", "<synthetic>", "none", "null", "unknown", "default"}

# 客户端内部代号 → 真实模型名（2026-11-14 用户拍板）。
# Qoder 的三个键是 IDE 内部路由名；GitHub Copilot 的 copilot/ 前缀只是展示包装，
# 去掉前缀就是底层模型（gpt-5.4 / gpt-5.5 等）。映射只改这一处：恢复产物、
# 覆盖率报告与展示页（注入脚本从本模块导入）会一起生效；原始代号另存 models_raw
# / model_raw / note_model_raw，保留可追溯性。
MODEL_ALIASES = {
    "dfmodel": "deepseek-v4.1-flash-1017",
    "qfmodel": "qwen3.8-flash",
    "lmodel": "qwen4-max-preview (qoduck)",   # qoduck = qwen4-max-preview 匿名内测版
    # TeleAgent 的 chat-flash 是客户端内部路由名（非公开模型 ID），底层模型
    # 2026-11-19 用户拍板 = deepseek-v4.1-flash-1017（天翼词元平台公开列该模型，
    # 与码道内核记录的真实 ID 一致）；原值留在 models_raw / model_raw /
    # note_model_raw 可追溯。
    "chat-flash": "deepseek-v4.1-flash-1017",
    # 注意：Kilo 的 modelID（particle-ai/particle-1:free）**不在映射表**——它有
    # 真实逐条字段，按原值记（2026-11-17 用户纠正：手工指定模型只给没有字段的
    # 来源，如 Trae；有字段的来源用真实字段，不许臆测映射）。
}

# 展示大小写归一（2026-11-14）：各客户端对同一模型的写法大小写不一
# （ZCode "GLM-5.3-Flash" vs WorkBuddy "glm-5.3-flash"），不收编会让同名模型
# 在统计里分家（实测 122 条小写 glm-5.3-flash 与 1894 条大写分家）。
# 未命中此表的模型一律小写——确定性归一，宁可统一小写也不留两种写法。
MODEL_CASE = {
    "glm-5.3-flash": "GLM-5.3-Flash",
    "glm-5.3": "GLM-5.3",
    "ling-3.2-flash": "Ling-3.2-flash",
    "minimax-m3.1-flash": "MiniMax-M3.1-Flash",
    "longcat-2.5": "LongCat-2.5",
    "auto": "Auto",
}

# 妙手（CatPaw）客户端在 sessions.extra.persistedModelId 只存数字 ID。
# 107 已用 /api/agent/maas/model-types 验证为 LongCat-2.5（2026-11-17）；
# 10002544 是 Auto 自动选择（用户确认，客户端落库为内部路由 ID）。
CATPAW_MODEL_NAMES = {"107": "LongCat-2.5", "10002544": "Auto"}


def canonical_model(name: str) -> str:
    """把内部代号/带前缀的展示名归一成真实模型名，并做大小写归一。"""
    s = str(name or "").strip()
    low = s.lower()
    if low in MODEL_ALIASES:
        return MODEL_ALIASES[low]
    if low.startswith("copilot/"):
        return MODEL_CASE.get(low[len("copilot/"):], low[len("copilot/"):])
    return MODEL_CASE.get(low, low)


def _clean_model(value) -> str:
    """从各种形状里取模型名字符串；占位值统一返回空串。"""
    if isinstance(value, dict):
        for k in ("modelID", "modelId", "id", "model", "modelName", "name"):
            v = value.get(k)
            if isinstance(v, str) and v.strip():
                value = v
                break
        else:
            return ""
    s = str(value or "").strip()
    return "" if s.lower() in PLACEHOLDER else s


def _ro(path: Path) -> sqlite3.Connection:
    """只读打开 SQLite（URI mode=ro，避免误写客户端数据库）。"""
    return sqlite3.connect("file:" + str(path).replace("\\", "/") + "?mode=ro", uri=True)


def _epoch_key(value) -> int:
    """时间戳统一成 int 排序键；脏值归 0，不抛异常。"""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _rec(level: str, source: str, turns: List[Tuple[str, str]],
         n_records: int, extra: Optional[dict] = None) -> dict:
    """归一化一条恢复结果。turns 为 [(时间, 模型名)]，模型名为空的不算命中。"""
    hit: List[Tuple[str, str]] = []
    for ts, value in turns:
        name = _clean_model(value)
        if name:
            hit.append((ts, name))
    models = collections.Counter(m for _, m in hit)
    out = {
        "level": level,
        "source": source,
        "models": dict(models),
        "primary_model": models.most_common(1)[0][0] if models else "",
        "n_records": int(n_records),
        "n_modeled": len(hit),
        "turns": hit,
    }
    if extra:
        out.update(extra)
    return out


def _put(store: Dict[str, dict], key: str, rec: dict) -> None:
    """同一会话被多个恢复器命中时（如 DB 会话级 + 日志逐条）合并：逐条优先。"""
    old = store.get(key)
    if not old:
        store[key] = rec
        return
    rank = {"per_message": 2, "usage": 1, "session": 0, "tier": -1}
    if rank.get(rec["level"], 0) > rank.get(old["level"], 0):
        # 保留会话级兜底模型，但主记录换成逐条的
        fallback = old.get("fallback_model", "")
        rec["fallback_model"] = fallback
        if old.get("model_id") and not rec.get("model_id"):
            rec["model_id"] = old["model_id"]
        store[key] = rec
    else:
        # 逐条来源已经足够，会话级兜底只记 fallback，不并入 models——否则会把
        # 「DB 里记的会话模型」当成第二个模型，虚增换模型会话数与模型种数。
        if rec.get("primary_model"):
            old["fallback_model"] = rec["primary_model"]
        if old["level"] != "per_message" and rec["level"] != "per_message":
            merged = collections.Counter(old["models"]) + collections.Counter(rec["models"])
            old["models"] = dict(merged)
            old["primary_model"] = old.get("primary_model") or merged.most_common(1)[0][0]


def recover_zcode() -> Dict[str, dict]:
    """ZCode：message.data 逐条带 modelId/modelID/model.modelID；model_usage 逐请求带 model_id。"""
    out: Dict[str, dict] = {}
    if not ZCODE_DB.exists():
        return out
    c = _ro(ZCODE_DB)
    c.row_factory = sqlite3.Row
    by_sess = collections.defaultdict(list)
    n_rows = collections.Counter()
    # 大表 ORDER BY 会让 SQLite 落临时文件，本机只读连接下实测报
    # "unable to open database file"；改在 Python 侧稳定排序（7.7k 行，开销可忽略）。
    raw = list(c.execute("SELECT session_id, time_created, data FROM message"))
    raw.sort(key=lambda r: _epoch_key(r["time_created"]))
    for r in raw:
        sid = r["session_id"]
        try:
            o = json.loads(r["data"])
        except (ValueError, TypeError):
            continue
        role = o.get("role")
        n_rows[sid] += 1
        m = (o.get("modelId") or o.get("modelID")
             or _clean_model(o.get("model")) or _clean_model(o.get("modelSelection")))
        if role == "assistant":
            by_sess[sid].append((str(r["time_created"] or ""), m))
    for sid, turns in by_sess.items():
        _put(out, "zcode:" + sid,
             _rec("per_message", "zcode:message.data", turns, n_rows[sid],
                  {"assistant_rows": len(turns)}))
    # 逐请求用量表：给「一轮内多次模型调用」提供交叉校验，不覆盖上面的逐条主记录
    usage = collections.defaultdict(collections.Counter)
    for r in c.execute("SELECT session_id, model_id FROM model_usage"):
        m = _clean_model(r["model_id"])
        if m:
            usage[r["session_id"]][m] += 1
    for sid, cnt in usage.items():
        key = "zcode:" + sid
        if key in out:
            out[key]["usage_models"] = dict(cnt)
            out[key]["usage_requests"] = sum(cnt.values())
        else:
            _put(out, key, _rec("usage", "zcode:model_usage", [], sum(cnt.values()),
                               {"models": dict(cnt), "primary_model": cnt.most_common(1)[0][0]}))
    c.close()
    return out


def recover_workbuddy() -> Dict[str, dict]:
    """WorkBuddy：providerData.model 逐行带模型，按 messageId 去重还原每轮。"""
    out: Dict[str, dict] = {}
    files = sorted(WB_ROOT.glob(r"projects\**\*.jsonl"))
    for fp in files:
        sid = fp.stem
        seen: Dict[str, Tuple[str, str]] = {}
        n_lines = 0
        for line in fp.open(encoding="utf-8", errors="replace"):
            n_lines += 1
            try:
                o = json.loads(line)
            except (ValueError, TypeError):
                continue
            pd = o.get("providerData")
            if not isinstance(pd, dict):
                continue
            m = _clean_model(pd.get("model") or pd.get("requestModelId"))
            if not m:
                continue
            mid = str(pd.get("messageId") or o.get("id") or n_lines)
            # 同一 messageId 的 reasoning/message/function_call 是同一次模型调用
            seen.setdefault(mid, (str(o.get("timestamp") or ""), m))
        if seen:
            turns = sorted(seen.values(), key=lambda x: x[0])
            _put(out, "wb:" + sid[:8],
                 _rec("per_message", "workbuddy:providerData.model", turns, n_lines,
                      {"unique_calls": len(turns)}))
    # 会话级兜底：workbuddy.db 的 sessions.model（上游 note 就取自这里）
    if WB_DB.exists():
        c = _ro(WB_DB)
        try:
            for sid, model in c.execute("SELECT id, model FROM sessions"):
                m = _clean_model(model)
                if m:
                    _put(out, "wb:" + str(sid)[:8],
                         _rec("session", "workbuddy.db:sessions.model", [], 1,
                              {"models": {m: 1}, "primary_model": m}))
        finally:
            c.close()
    return out


def recover_qoder() -> Dict[str, dict]:
    """Qoder：CLI jsonl 的 message.model 逐条；IDE DB 的 chat_sessions.model 兜底。"""
    out: Dict[str, dict] = {}
    for fp in sorted(QODER_ROOT.glob(r"projects\**\*.jsonl")):
        sid = fp.stem
        turns: List[Tuple[str, str]] = []
        n_lines = 0
        for line in fp.open(encoding="utf-8", errors="replace"):
            n_lines += 1
            try:
                o = json.loads(line)
            except (ValueError, TypeError):
                continue
            if o.get("type") == "runtime-config":
                continue
            msg = o.get("message")
            if isinstance(msg, dict) and msg.get("role") in ("assistant", "ai"):
                turns.append((str(o.get("timestamp") or ""), _clean_model(msg.get("model"))))
        if turns:
            _put(out, "qoder:" + sid[:8],
                 _rec("per_message", "qoder:message.model", turns, n_lines,
                      {"assistant_rows": len(turns)}))
    if QODER_DB.exists():
        c = _ro(QODER_DB)
        try:
            for sid, model in c.execute("SELECT session_id, model FROM chat_sessions"):
                m = _clean_model(model)
                if m:
                    _put(out, "qoder:" + str(sid)[:8],
                         _rec("session", "qoder:main.sqlite/chat_sessions.model", [], 1,
                              {"models": {m: 1}, "primary_model": m}))
        finally:
            c.close()
    return out


def recover_opencode() -> Dict[str, dict]:
    """OpenCode：v2 主库 session_message.data.model.id；9-03 备份库 message.data.modelID。"""
    out: Dict[str, dict] = {}
    if OC_DB.exists():
        c = _ro(OC_DB)
        c.row_factory = sqlite3.Row
        by_sess = collections.defaultdict(list)
        n_rows = collections.Counter()
        for r in c.execute("SELECT session_id, time_created, type, data FROM session_message"):
            sid = r["session_id"]
            n_rows[sid] += 1
            try:
                o = json.loads(r["data"])
            except (ValueError, TypeError):
                continue
            m = _clean_model(o.get("model"))
            if m and r["type"] != "user":
                by_sess[sid].append((str(r["time_created"] or ""), m))
        for sid, turns in by_sess.items():
            _put(out, "oc:" + sid[:10],
                 _rec("per_message", "opencode:session_message.data.model", turns, n_rows[sid]))
        c.close()
    if OC_OLD_DB.exists():
        c = _ro(OC_OLD_DB)
        c.row_factory = sqlite3.Row
        by_sess = collections.defaultdict(list)
        n_rows = collections.Counter()
        for r in c.execute("SELECT session_id, time_created, data FROM message"):
            sid = r["session_id"]
            n_rows[sid] += 1
            try:
                o = json.loads(r["data"])
            except (ValueError, TypeError):
                continue
            m = _clean_model(o.get("modelID") or o.get("model"))
            if m and o.get("role") != "user":
                by_sess[sid].append((str(r["time_created"] or ""), m))
        for sid, turns in by_sess.items():
            _put(out, "ocold:" + sid[:10],
                 _rec("per_message", "opencode_old:message.data.modelID", turns, n_rows[sid]))
        c.close()
    return out


def _claude_models(fp: Path, key: str, out: Dict[str, dict], label: str) -> None:
    """Claude Code 系：assistant 行的 message.model（<synthetic> 为系统合成消息）。"""
    turns: List[Tuple[str, str]] = []
    n_lines = 0
    for line in fp.open(encoding="utf-8", errors="replace"):
        n_lines += 1
        try:
            o = json.loads(line)
        except (ValueError, TypeError):
            continue
        msg = o.get("message")
        if isinstance(msg, dict) and msg.get("role") == "assistant":
            turns.append((str(o.get("timestamp") or ""), _clean_model(msg.get("model"))))
    if turns:
        _put(out, key, _rec("per_message", label, turns, n_lines,
                            {"assistant_rows": len(turns)}))


def recover_claude() -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    for fp in sorted((ASUS / ".claude" / "projects").glob(r"**\*.jsonl")):
        sub = "subagents" in fp.parts
        key = "claude:" + fp.stem + ("-sub" if sub else "")
        _claude_models(fp, key, out, "claude_code:message.model")
    for fp in sorted((CLAUDE_LOCAL / "projects").glob(r"**\*.jsonl")):
        _claude_models(fp, "claude-vscode:" + fp.stem, out, "claude_vscode:message.model")
    return out


def recover_minimax() -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    for fp in sorted(MM_ROOT.glob(r"**\messages.jsonl")):
        sid = re.sub(r"\W", "", fp.parent.name)[-12:]
        turns: List[Tuple[str, str]] = []
        n_lines = 0
        for line in fp.open(encoding="utf-8", errors="replace"):
            n_lines += 1
            try:
                o = json.loads(line)
            except (ValueError, TypeError):
                continue
            msg = o.get("message")
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                turns.append((str(msg.get("timestamp") or ""), _clean_model(msg.get("model"))))
        if turns:
            _put(out, "mm:" + sid, _rec("per_message", "minimax:message.model", turns, n_lines))
    return out


def recover_hermes() -> Dict[str, dict]:
    """Hermes：sessions.model 会话级 + session_model_usage 按模型聚合调用次数。

    build.py 踩过同一个坑：原库带回滚日志且目录只读，必须复制到临时目录让 SQLite 恢复。
    """
    out: Dict[str, dict] = {}
    if not HERMES_DB.exists():
        return out
    tmpdir = tempfile.mkdtemp(prefix="ai_hist_model_")
    tmp = Path(tmpdir) / "state.db"
    try:
        shutil.copy2(HERMES_DB, tmp)
        for suffix in ("-wal", "-shm", "-journal"):
            src = Path(str(HERMES_DB) + suffix)
            if src.exists():
                shutil.copy2(src, Path(str(tmp) + suffix))
        c = sqlite3.connect(str(tmp))
        c.row_factory = sqlite3.Row
        for r in c.execute("SELECT id, model FROM sessions"):
            m = _clean_model(r["model"])
            if m:
                _put(out, "hermes:" + str(r["id"])[:16],
                     _rec("session", "hermes:sessions.model", [], 1,
                          {"models": {m: 1}, "primary_model": m}))
        usage = collections.defaultdict(collections.Counter)
        for r in c.execute("SELECT session_id, model, api_call_count FROM session_model_usage"):
            m = _clean_model(r["model"])
            if m:
                usage[r["session_id"]][m] += int(r["api_call_count"] or 0)
        for sid, cnt in usage.items():
            key = "hermes:" + str(sid)[:16]
            if key in out:
                rec = out[key]
                rec["usage_models"] = dict(cnt)
                rec["usage_requests"] = sum(cnt.values())
                # 会话级模型不能只写 1 次：session_model_usage 的 api_call_count
                # 才是真实请求数；没有逐轮时间戳，但不该把 262/504 次压成 1。
                rec["models"] = dict(cnt)
                rec["models_raw"] = dict(cnt)
                rec["primary_model"] = cnt.most_common(1)[0][0]
                rec["n_records"] = sum(cnt.values())
                rec["level"] = "usage"
                rec["source"] = "hermes:session_model_usage"
            else:
                _put(out, key, _rec("usage", "hermes:session_model_usage", [], sum(cnt.values()),
                                   {"models": dict(cnt), "primary_model": cnt.most_common(1)[0][0]}))
        c.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return out


def _copilot_replay(fp: Path):
    """复刻 build.py 的增量日志重放：kind0 快照 / kind1 set / kind2 append。"""
    obj = None
    for line in fp.open(encoding="utf-8", errors="ignore"):
        try:
            o = json.loads(line)
        except (ValueError, TypeError):
            continue
        kind, k, v = o.get("kind"), o.get("k"), o.get("v")
        if kind == 0:
            obj = v
        elif kind == 1 and isinstance(k, list) and k and obj is not None:
            cur = obj
            for seg in k[:-1]:
                if isinstance(cur, list) and isinstance(seg, int) and 0 <= seg < len(cur):
                    cur = cur[seg]
                elif isinstance(cur, dict):
                    cur = cur.setdefault(seg, {})
                else:
                    cur = None
                    break
            if isinstance(cur, dict):
                cur[k[-1]] = v
            elif isinstance(cur, list) and isinstance(k[-1], int) and 0 <= k[-1] < len(cur):
                cur[k[-1]] = v
        elif kind == 2 and isinstance(k, list) and obj is not None:
            cur = obj
            for seg in k:
                if isinstance(cur, list) and isinstance(seg, int) and 0 <= seg < len(cur):
                    cur = cur[seg]
                elif isinstance(cur, dict):
                    cur = cur.setdefault(seg, [])
                else:
                    cur = None
                    break
            if isinstance(cur, list) and isinstance(v, list):
                cur.extend(v)
    return obj


def recover_copilot() -> Dict[str, dict]:
    """Copilot Chat：重放后 requests[].modelId 是每个请求的模型（上游 note 只取首个）。"""
    out: Dict[str, dict] = {}
    for fp in sorted((ASUS / "workspaceStorage").glob(r"**\chatSessions\*.jsonl")):
        o = _copilot_replay(fp)
        if not isinstance(o, dict):
            continue
        sid = o.get("sessionId") or fp.stem
        turns = [(str(r.get("timestamp") or ""), _clean_model(r.get("modelId")))
                 for r in (o.get("requests") or []) if isinstance(r, dict)]
        if turns:
            _put(out, "copilot:" + str(sid),
                 _rec("per_message", "copilot:requests.modelId", turns, len(turns),
                      {"requests": len(turns)}))
    return out


def recover_cursor() -> Dict[str, dict]:
    """Cursor：bubbleId 的 modelInfo + createdAt 恢复逐条模型，composerData 兜底。

    约 1/4 会话只记 ``modelName=default``——那是「用默认模型」的档位标记，不是
    模型 ID。单独记为 tier，既不丢线索，也不混进具体模型名的分布里。
    2026-11-17：除会话级计数外，bubble 还带 createdAt；有具名模型的 bubble
    直接按时间还原逐条，别再把 150 个会话压成一条 session 记录。
    """
    out: Dict[str, dict] = {}
    if not CURSOR_DB.exists():
        return out
    c = _ro(CURSOR_DB)
    by_sess = collections.defaultdict(collections.Counter)
    turns_by_sess = collections.defaultdict(list)
    tiers = collections.Counter()
    for key, value in c.execute(
            "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%' "
            "OR key LIKE 'bubbleId:%'"):
        try:
            o = json.loads(value.decode("utf-8", "ignore") if isinstance(value, bytes) else value)
        except (ValueError, TypeError):
            continue
        if not isinstance(o, dict):
            continue
        parts = str(key).split(":")
        cid = parts[1] if len(parts) > 1 else ""
        if not cid:
            continue
        is_bubble = str(key).startswith("bubbleId:")
        for field in ("modelConfig", "modelInfo"):
            v = o.get(field)
            if not isinstance(v, dict):
                continue
            raw = str(v.get("modelName") or v.get("name") or v.get("model") or "")
            if raw.strip().lower() == "default":
                tiers[cid] += 1
            else:
                m = _clean_model(raw)
                if m:
                    by_sess[cid][m] += 1
                    if is_bubble and o.get("createdAt"):
                        turns_by_sess[cid].append((str(o["createdAt"]), m))
    c.close()
    for cid, cnt in by_sess.items():
        turns = sorted(turns_by_sess.get(cid, []), key=lambda p: p[0])
        if turns:
            _put(out, "cursor:" + cid,
                 _rec("per_message", "cursor:bubble.modelInfo+createdAt", turns,
                      len(turns), {"usage_requests": len(turns)}))
        else:
            _put(out, "cursor:" + cid,
                 _rec("session", "cursor:cursorDiskKV.modelInfo", [], sum(cnt.values()),
                      {"models": dict(cnt), "primary_model": cnt.most_common(1)[0][0],
                       "usage_requests": sum(cnt.values())}))
    for cid, n in tiers.items():
        if cid not in by_sess:
            _put(out, "cursor:" + cid,
                 _rec("tier", "cursor:modelInfo=default", [], n,
                      {"models": {"cursor:default": n}, "primary_model": "cursor:default"}))
    return out


def recover_codex() -> Dict[str, dict]:
    """Codex：session_meta / turn_context 的 payload.model，按 rollout 文件还原。"""
    out: Dict[str, dict] = {}
    for fp in sorted((ASUS / ".codex" / "sessions").glob(r"**\rollout-*.jsonl")):
        m = re.match(r"rollout-(\d{4}-\d{2}-\d{2})T([\d-]+)-(.+)$", fp.stem)
        uid = m.group(3) if m else fp.stem[-36:]
        turns: List[Tuple[str, str]] = []
        n_lines = 0
        for line in fp.open(encoding="utf-8", errors="replace"):
            n_lines += 1
            try:
                o = json.loads(line)
            except (ValueError, TypeError):
                continue
            payload = o.get("payload")
            if not isinstance(payload, dict):
                continue
            model = _clean_model(payload.get("model"))
            if model:
                turns.append((str(o.get("timestamp") or ""), model))
        if turns:
            _put(out, "codex:" + uid[:8],
                 _rec("per_message", "codex:session_meta.payload.model", turns, n_lines))
    return out


def recover_deepseek() -> Dict[str, dict]:
    """DeepSeek 网页版：导出 md 头部的「模型」是 default/vision/expert 模式档位。

    186 篇 md 头部都带会话 ID 与模型档位，按 build.py 的 ``deepseek:`` + id[:8]
    规则即可挂上上游；sessions_index.json 的 model_type 只是同源兜底。档位不是
    具体模型 ID，全部记 tier——早期按 _clean_model 会把 171 个 default 当占位
    值丢掉，误判成「无线索」。
    """
    out: Dict[str, dict] = {}
    for fp in sorted(DEEP_MD.glob("*.md")):
        with fp.open(encoding="utf-8", errors="replace") as f:
            head = f.read(800)
        m_id = re.search(r"会话 ID[^\w-]+([0-9a-f-]{36})", head)
        m_tier = re.search(r"\*\*模型\*\*[：:]\s*([^\s]+)", head)
        if not m_id or not m_tier:
            continue
        tier = m_tier.group(1).strip().lower()
        name = "deepseek:" + tier
        _put(out, "deepseek:" + m_id.group(1)[:8],
             _rec("tier", "deepseek:markdown 模式档位", [], 1,
                  {"models": {name: 1}, "primary_model": name}))
    if DEEP_INDEX.exists():
        try:
            rows = json.loads(DEEP_INDEX.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            rows = []
        for o in rows:
            sid = str(o.get("id") or "")
            tier = str(o.get("model_type") or "").strip().lower()
            if sid and tier:
                name = "deepseek:" + tier
                _put(out, "deepseek:" + sid[:8],
                     _rec("tier", "deepseek:sessions_index.model_type", [], 1,
                          {"models": {name: 1}, "primary_model": name}))
    return out


GROK_EXPORT = Path(r"F:\data\grok_capture\export")

# grok.com 抓取导出的 md 里每条 AI 回复标题行带模型角标：
#   `## 2. Grok · grok-4 · 2026-04-04 11:49:29`
# 会话头带 `会话 ID：`uuid``。grok.json 转发进 data.json 时只留 {r,t,k,x} 把模型
# 丢了（2026-11-15 发现 6 个 RAG Grok 会话无模型标记的根因），但抓取端写 md 时
# 是全量——从 md 直接恢复逐条模型，不用动抓取工具。
GROK_MSG_RE = re.compile(
    r"^##\s+\d+\.\s+Grok\s+·\s+([\w.\-]+)\s+·\s+(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
GROK_ID_RE = re.compile(r"会话 ID：`([0-9a-fA-F\-]+)`")


def recover_grok() -> Dict[str, dict]:
    """Grok：从抓取导出的 md 恢复逐条模型（grok.json 本身没有模型字段）。

    时间对齐口径：md 时间是本地墙钟，data.json 的 k 是 epoch 秒——同机同钟下
    datetime.timestamp() 转换误差远小于 lenscan 的 ±120s 容差。turns 统一写
    epoch 毫秒（与其他来源口径一致，lenscan 按 /1000 消费）。会话 key 复刻
    build.py 的 ``grok:`` + uuid[:8]。
    """
    out: Dict[str, dict] = {}
    if not GROK_EXPORT.exists():
        return out
    for md in sorted(GROK_EXPORT.glob("*/*.md")):
        try:
            text = md.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        mid = GROK_ID_RE.search(text)
        if not mid:
            continue
        turns: List[Tuple[str, str]] = []
        for line in text.splitlines():
            mm = GROK_MSG_RE.match(line)
            if not mm:
                continue
            try:
                epoch_ms = str(int(datetime.strptime(
                    mm.group(2), "%Y-%m-%d %H:%M:%S").timestamp() * 1000))
            except ValueError:
                continue
            turns.append((epoch_ms, mm.group(1)))
        if turns:
            _put(out, "grok:" + mid.group(1)[:8],
                 _rec("per_message", "grok:export-md", turns, len(turns)))
    return out


def recover_catpaw() -> Dict[str, dict]:
    """妙手（CatPaw）：会话级 persistedModelId + CLI 日志逐轮模型。

    SQLite 的 ui_sdk_messages 没有逐条模型字段，只有 sessions.extra.persistedModelId
    （会话级）。真正的逐轮证据在 `~/.meituan-catpaw/<uid>/logs/app*.log`：
    每个 `[LLM] start` 行带 conversationId/turn/model。先用日志恢复逐轮模型，
    再以会话级记录兜底；两者都归一成 `catpaw:<conversationId>`。
    107=LongCat-2.5（官方列表实查）；10002544=Auto（用户确认的自动选择）。
    """
    out: Dict[str, dict] = {}
    if not CATPAW_ROOT.exists():
        return out
    for db in sorted(CATPAW_ROOT.glob("catpaw-memory-*.db")):
        if db.name.lower().endswith("-anon.db"):
            continue
        try:
            c = _ro(db)
            c.row_factory = sqlite3.Row
            for r in c.execute("select id, extra from sessions"):
                try:
                    extra = json.loads(r["extra"] or "{}")
                except (TypeError, ValueError):
                    continue
                mid = str(extra.get("persistedModelId") or "").strip()
                if not mid or mid == "0":
                    continue
                name = CATPAW_MODEL_NAMES.get(mid) or f"自定义模型（{mid}）"
                ts = str(extra.get("timestamp") or "")
                _put(out, "catpaw:" + str(r["id"]),
                     _rec("session", "catpaw:sessions.persistedModelId",
                          [(ts, name)], 1, {"model_id": mid}))
            c.close()
        except sqlite3.Error:
            continue

    # 逐轮模型：同一 conversation 的每个 [LLM] start 是一次真实请求，不能只记 1 条。
    per_turn: Dict[str, List[Tuple[str, str]]] = collections.defaultdict(list)
    seen = set()
    for log in sorted(CATPAW_LOG_ROOT.glob("*/logs/app*.log")):
        try:
            with log.open(encoding="utf-8", errors="replace") as f:
                for line in f:
                    if "[LLM] start" not in line:
                        continue
                    mm = CATPAW_LOG_RE.search(line)
                    tm = CATPAW_TS_RE.match(line)
                    if not mm or not tm:
                        continue
                    cid, turn, mid = mm.group(1), mm.group(2), mm.group(3)
                    try:
                        ts = int(datetime.strptime(
                            tm.group(1), "%Y-%m-%d %H:%M:%S.%f").timestamp())
                    except ValueError:
                        ts = 0
                    # 同一 user turn 里会多次调模型（每次工具回填一轮）；按
                    # 时间戳逐次保留，不能用 (cid, turn, model) 去重，否则 341
                    # 次请求会被压成 93 个 turn。
                    dedup = (cid, ts, turn, mid)
                    if dedup in seen:
                        continue
                    seen.add(dedup)
                    per_turn[cid].append((
                        str(ts), CATPAW_MODEL_NAMES.get(mid) or f"自定义模型（{mid}）"))
        except OSError:
            continue
    for cid, turns in per_turn.items():
        turns.sort(key=lambda p: int(p[0] or 0))
        _put(out, "catpaw:" + cid,
             _rec("per_message", "catpaw:app.log:llm-caller", turns, len(turns)))
    return out


def recover_kilo() -> Dict[str, dict]:
    """Kilo Desktop：内核 API 原始响应 info.modelID 逐条（data/kilo_capture/raw/）。

    上游 id 是 kilo:<ses_id>，kilo_fetch 的 raw 目录名就是 ses_id。Kilo 有真实
    逐条模型字段（modelID，本机实测 particle-ai/particle-1:free），按原值记录
    ——不进 MODEL_ALIASES（2026-11-17 用户纠正：手工指定只给没有字段的来源）。
    ts 用内核 time.created（epoch 毫秒，turn_models 口径）。raw 被清理时靠
    load_existing 记忆沿用。
    """
    out: Dict[str, dict] = {}
    if not KILO_RAW.exists():
        return out
    for ses_dir in sorted(KILO_RAW.iterdir()):
        fp = ses_dir / "messages.json"
        if not fp.is_file():
            continue
        try:
            with fp.open(encoding="utf-8") as f:
                msgs = json.load(f)
        except (ValueError, OSError):
            continue
        turns: List[Tuple[str, str]] = []
        for msg in msgs or []:
            info = msg.get("info") or {}
            if info.get("role") != "assistant":
                continue
            created = (info.get("time") or {}).get("created") or 0
            turns.append((str(created or 0), _clean_model(info)))
        if turns:
            _put(out, "kilo:" + ses_dir.name,
                 _rec("per_message", "kilo:message.modelID", turns, len(msgs or [])))
    return out


def _recover_opencode_raw(raw_root: Path, prefix: str, source: str) -> Dict[str, dict]:
    """opencode 变体三表来源（TeleAgent/码道）共用的 raw 恢复器：
    raw/<ses_id>/messages.json = {session, messages:[{msg:{role, modelID, time:{created}}, ts_ms, parts}]}。

    assistant 逐条 modelID 按原值记录（口径铁律 3：有真实字段不臆测映射、不进
    MODEL_ALIASES，同 Kilo 的 particle-ai/particle-1:free）；ts 用 msg.time.created
    （epoch 毫秒，turn_models 口径，缺了退 ts_ms）。raw 被清理时靠 load_existing 记忆沿用。
    """
    out: Dict[str, dict] = {}
    if not raw_root.exists():
        return out
    for ses_dir in sorted(raw_root.iterdir()):
        fp = ses_dir / "messages.json"
        if not fp.is_file():
            continue
        try:
            with fp.open(encoding="utf-8") as f:
                dump = json.load(f)
        except (ValueError, OSError):
            continue
        msgs = dump.get("messages") or [] if isinstance(dump, dict) else (dump or [])
        turns: List[Tuple[str, str]] = []
        for m in msgs:
            if not isinstance(m, dict):
                continue
            msg = m.get("msg") if isinstance(m.get("msg"), dict) else m
            if msg.get("role") != "assistant":
                continue
            ts = (msg.get("time") or {}).get("created") or m.get("ts_ms") or 0
            turns.append((str(ts or 0), _clean_model(msg)))
        if turns:
            _put(out, prefix + ":" + ses_dir.name,
                 _rec("per_message", source, turns, len(msgs)))
    return out


def recover_teleagent() -> Dict[str, dict]:
    """TeleAgent：内核 SQLite 抓取 raw 的逐条 modelID（data/teleagent_capture/raw/）。

    本机实测全部 assistant modelID=`chat-flash`（TeleAgent 内部路由名，单一模型）。
    有真实字段按原值记录——不进 MODEL_ALIASES（2026-11-17 用户纠正，同 Kilo）。
    """
    return _recover_opencode_raw(TELEAGENT_RAW, "teleagent", "teleagent:message.modelID")


def recover_codearts() -> Dict[str, dict]:
    """码道：同 TeleAgent 形态，raw=data/codearts_capture/raw/。

    本机实测 assistant modelID=`deepseek-v4.1-flash-1017`（真实模型名，按原值记录）。
    """
    return _recover_opencode_raw(CODEARTS_RAW, "codearts", "codearts:message.modelID")


def recover_trae() -> Dict[str, dict]:
    """Trae：分享接口不带模型字段、本地正文加密，无逐条证据可恢复——
    按 TRAE_FIXED_MODEL（用户拍板 2026-11-17）整源记会话级模型。

    会话清单取 trae.json（trae_fetch 的聚合，id = trae:<share_id>）；
    会话级记录只落一条 turn（取末条消息时间，秒→毫秒对齐 turn_models 口径），
    不逐条伪造（没有逐条证据就不装作有）。
    """
    out: Dict[str, dict] = {}
    if not TRAE_JSON.exists():
        return out
    try:
        with TRAE_JSON.open(encoding="utf-8") as f:
            sessions = json.load(f) or []
    except (ValueError, OSError):
        return out
    for s in sessions:
        sid = str(s.get("id") or "")
        if not sid.startswith("trae:"):
            continue
        msgs = s.get("msgs") or []
        last_k = max((_epoch_key(m.get("k")) for m in msgs), default=0)
        _put(out, sid,
             _rec("session", "trae:user-pinned",
                  [(str(last_k * 1000), TRAE_FIXED_MODEL)] if last_k else [],
                  len(msgs),
                  {"models": {TRAE_FIXED_MODEL: len(msgs) or 1},
                   "primary_model": TRAE_FIXED_MODEL}))
    return out


RECOVERERS = (
    ("Grok", recover_grok),
    ("ZCode", recover_zcode),
    ("WorkBuddy", recover_workbuddy),
    ("Qoder", recover_qoder),
    ("OpenCode", recover_opencode),
    ("Claude Code", recover_claude),
    ("MiniMax Code", recover_minimax),
    ("Hermes Agent", recover_hermes),
    ("GitHub Copilot", recover_copilot),
    ("Cursor", recover_cursor),
    ("Codex", recover_codex),
    ("DeepSeek", recover_deepseek),
    ("妙手", recover_catpaw),
    ("Kilo", recover_kilo),
    ("TeleAgent", recover_teleagent),
    ("码道", recover_codearts),
    ("Trae", recover_trae),
)


def load_upstream() -> List[dict]:
    if not SOURCE_JSON.exists():
        print(f"上游数据不存在：{SOURCE_JSON}")
        sys.exit(2)
    with SOURCE_JSON.open(encoding="utf-8") as f:
        return json.load(f).get("sessions") or []


def note_model(note: str) -> str:
    s = str(note or "")
    m = re.search(r"模型[：:]\s*([^\s｜|]+)", s)
    if not m:
        # 无冒号形态（fetch 侧「· 模型 xxx」，Kilo/TeleAgent/码道 note 用）：
        # token 必须以 ASCII 开头，避免把「模型字段/模型名」这类散文词误当模型
        m = re.search(r"模型\s+([A-Za-z0-9][^\s｜|]*)", s)
    return m.group(1) if m else ""


def load_existing(session_path: Path = None, turn_path: Path = None):
    """读现有产物当作「记忆」，返回 ({id: row}, {id: [(ts, model_raw)]})。

    路径可传（单测用临时文件，避免动真实产物）。

    背景：2026-11-17 用户反馈「每次新导入都不加模型字段？没有记忆吗？」——根因是
    上游 build.py 重跑后没人触发本工具，新会话自然没模型。串链（build.py 自动调
    scan）解决"跑不跑"，这里的合并解决"别丢"：某次单个恢复器失败/原始日志被清
    时，仍沿用上次已还原的结果，而不是把会话降级成 level=none。
    """
    session_path = session_path or SESSION_OUT
    turn_path = turn_path or TURN_OUT
    rows = {}
    if session_path.exists():
        with session_path.open(encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                r = json.loads(line)
                rows[str(r.get("id"))] = r
    turns = collections.defaultdict(list)
    if turn_path.exists():
        with turn_path.open(encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                r = json.loads(line)
                turns[str(r.get("id"))].append(
                    (r.get("ts", 0), r.get("model_raw") or r.get("model") or ""))
    return rows, turns


def _rec_from_row(row: dict, turns: list) -> dict:
    """把记忆里的历史行折回恢复器记录结构（与 _rec 的字段口径一致）。"""
    return {
        "level": row.get("level") or "none",
        "source": row.get("source") or "",
        "models": dict(row.get("models_raw") or {}),
        "turns": list(turns),
        "n_records": row.get("n_records", 0),
        "n_modeled": row.get("n_modeled", 0),
        "usage_requests": row.get("usage_requests", 0),
        "fallback_model": row.get("fallback_model") or "",
    }


def _pick_rec(merged: dict, existing: dict, existing_turns: dict, sid: str,
              no_merge: bool = False):
    """取某个会话的记录：本次扫描优先；没扫到且允许合并时沿用历史记忆。

    返回 (rec, reused)。只沿用真恢复过的（level != none），否则等于把「本来就没
    模型」再抄一遍。抽成纯函数是为了能单测（合并分支在真实 scan 里要制造恢复器
    失败才触发得到）。
    """
    rec = merged.get(sid)
    if rec is None and not no_merge and sid in existing:
        prev = existing[sid]
        if (prev.get("level") or "none") != "none":
            return _rec_from_row(prev, existing_turns.get(sid, [])), True
    return rec, False


def cmd_scan(args) -> int:
    upstream = load_upstream()
    rag_ids = set()
    if RAG_SESSIONS.exists():
        with RAG_SESSIONS.open(encoding="utf-8") as f:
            rag_ids = {json.loads(line)["id"] for line in f if line.strip()}

    merged: Dict[str, dict] = {}
    for label, fn in RECOVERERS:
        try:
            got = fn()
        except (OSError, sqlite3.Error, ValueError) as e:
            # 单个来源缺失/损坏不该让整次恢复失败，明确报出即可
            print(f"  [跳过 {label}] {type(e).__name__}: {e}")
            continue
        for key, rec in got.items():
            # 有 assistant 行但模型名全被过滤（Claude 的 <synthetic> 等）不算恢复，
            # 否则会被算成「逐条来源」却拿不出任何模型名。
            if rec["level"] == "per_message" and not rec["n_modeled"]:
                continue
            _put(merged, key, rec)

    by_id = {str(s.get("id")): s for s in upstream}
    matched = [k for k in merged if k in by_id]
    # 记忆：本次没扫到、但上次已还原过的会话沿用旧结果（--no-merge 可关）。
    # 只沿用真恢复过的（level != none），否则等于把「本来就没模型」再抄一遍。
    existing, existing_turns = ({}, {})
    if not getattr(args, "no_merge", False):
        existing, existing_turns = load_existing()
    reused = 0
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    rows: List[dict] = []
    turns_rows = 0
    with SESSION_OUT.open("w", encoding="utf-8") as fs, TURN_OUT.open("w", encoding="utf-8") as ft:
        for s in upstream:
            sid = str(s.get("id"))
            rec, reused_here = _pick_rec(merged, existing, existing_turns, sid,
                                         no_merge=getattr(args, "no_merge", False))
            reused += 1 if reused_here else 0
            models_raw = rec.get("models", {}) if rec else {}
            # 输出统一写真实模型名；两个原始别名若映射到同一真名要合并计数。
            counter = collections.Counter()
            for raw_name, count in models_raw.items():
                counter[canonical_model(raw_name)] += count
            models = dict(counter)
            primary = counter.most_common(1)[0][0] if counter else ""
            note_raw = note_model(s.get("note"))
            row = {
                "id": sid,
                "src": s.get("src"),
                "in_rag": sid in rag_ids,
                "n_msgs": s.get("n"),
                "note_model": canonical_model(note_raw),
                "note_model_raw": note_raw,
                "level": rec["level"] if rec else "none",
                "model_kind": ("tier" if rec and rec["level"] == "tier"
                               else "named" if rec else "none"),
                "source": rec["source"] if rec else "",
                "primary_model": primary,
                "models": models,
                "models_raw": models_raw,
                "n_models": len(models),
                "n_records": rec.get("n_records", 0) if rec else 0,
                "n_modeled": rec.get("n_modeled", 0) if rec else 0,
                "switched": len(models) > 1,
                "usage_requests": rec.get("usage_requests", 0) if rec else 0,
                "fallback_model": canonical_model(rec.get("fallback_model", "")) if rec else "",
            }
            rows.append(row)
            fs.write(json.dumps(row, ensure_ascii=False) + "\n")
            if rec:
                for i, (ts, model) in enumerate(rec.get("turns", [])):
                    ft.write(json.dumps({"id": sid, "src": s.get("src"), "i": i,
                                        "ts": ts, "model": canonical_model(model),
                                        "model_raw": model,
                                        "source": rec["source"]}, ensure_ascii=False) + "\n")
                    turns_rows += 1
    print(f"已写 {SESSION_OUT}（{len(rows)} 会话）")
    print(f"已写 {TURN_OUT}（{turns_rows} 条逐条模型记录）")
    if reused:
        print(f"其中沿用历史记忆 {reused} 个会话（本次未扫到、上次已还原）")
    print(f"恢复器产出 {len(merged)} 个会话 key，精确匹配上游 {len(matched)} 个"
          f"（未匹配 {len(merged) - len(matched)} 个，多为未入选/只标题会话）")
    unmatched = [k for k in merged if k not in by_id]
    if unmatched:
        print("  未匹配样例：" + ", ".join(unmatched[:6]))
    report(rows)
    return 0


def _norm_model(s: str) -> str:
    """归一化模型名（去掉大小写/连字符/下划线），区分命名写法与实质差异。"""
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def report(rows: List[dict]) -> None:
    def summary(subset: List[dict], title: str) -> None:
        n = len(subset)
        per = sum(1 for r in subset if r["level"] in ("per_message", "usage"))
        sess = sum(1 for r in subset if r["level"] == "session")
        tier = sum(1 for r in subset if r["level"] == "tier")
        none = n - per - sess - tier
        named = n - none - tier
        print(f"\n## {title}（{n} 会话）\n")
        print(f"  有模型线索：{n - none}/{n} = {(n - none) / n:.1%}"
              f"；其中具体/别名模型名 {named}（逐条 {per}，会话级 {sess}），"
              f"仅档位/默认标记 {tier}，完全无 {none}")
        named_rows = [r for r in subset if r["model_kind"] == "named"]
        print(f"  会话内换过模型：{sum(1 for r in named_rows if r['switched'])} 个"
              f"（只在具体/别名模型名口径下统计）")
        aliased = sum(1 for r in subset
                      if any(canonical_model(k) != k for k in r.get("models_raw", {})))
        print(f"  已套用真实名映射：{aliased} 个会话"
              f"（Qoder 内部代号 / Copilot 前缀；原始名留在 models_raw）")
        print(f"  逐条模型标注记录：{sum(r['n_modeled'] for r in subset)} 条"
              f"（原始记录 {sum(r['n_records'] for r in subset)} 条，各来源粒度不等价）")
        note_rows = [r for r in subset if r["note_model"] and r["primary_model"]]
        diff = [r for r in note_rows
                if _norm_model(r["note_model"]) != _norm_model(r["primary_model"])]
        alias = [r for r in note_rows
                 if r["note_model"].lower() != r["primary_model"].lower()
                 and _norm_model(r["note_model"]) == _norm_model(r["primary_model"])]
        print(f"  上游 note 有模型名的 {len(note_rows)} 个：写法差异 {len(alias)} 个"
              f"（如 hy4free/hy4-free），实质不一致 {len(diff)} 个")
        print(f"  note 无模型名但成功恢复：{sum(1 for r in subset if not r['note_model'] and r['primary_model'])} 个")

    summary(rows, "全量上游口径")
    summary([r for r in rows if r["in_rag"]], "RAG 库口径")

    print("\n## 分来源（全量 / RAG）\n")
    print("| src | 全量 | 具体/别名 | 逐条 | 会话级 | 档位 | 无 | RAG | RAG有线索 | 换模型 |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    by_src = collections.defaultdict(list)
    for r in rows:
        by_src[r["src"]].append(r)
    for src, rs in sorted(by_src.items(), key=lambda kv: -len(kv[1])):
        rag = [r for r in rs if r["in_rag"]]
        def cnt(items, level):
            return sum(1 for r in items if r["level"] == level)
        named = sum(1 for r in rs if r["model_kind"] == "named")
        rag_ok = sum(1 for r in rag if r["level"] != "none")
        print(f"| {src} | {len(rs)} | {named} | {cnt(rs, 'per_message') + cnt(rs, 'usage')} | "
              f"{cnt(rs, 'session')} | {cnt(rs, 'tier')} | {cnt(rs, 'none')} | {len(rag)} | {rag_ok} | "
              f"{sum(1 for r in rs if r['model_kind'] == 'named' and r['switched'])} |")

    print("\n## 模型名分布（按会话主模型计数，仅 RAG 口径的具体/别名模型名）\n")
    dist = collections.Counter(r["primary_model"] for r in rows
                               if r["in_rag"] and r["model_kind"] == "named")
    for name, n in dist.most_common(20):
        print(f"  {n:>3}  {name}")
    print(f"  合计 {sum(dist.values())} 个会话 / {len(dist)} 种主模型名")
    tiers = collections.Counter(r["primary_model"] for r in rows
                                if r["in_rag"] and r["model_kind"] == "tier")
    print("  RAG 口径档位/默认标记：" + ", ".join(f"{k}×{v}" for k, v in tiers.most_common()))
    print("  注：dfmodel/qfmodel/lmodel/hy4free 等是客户端内部代号，不是公开模型 ID。")

    print("\n## 会话级 note 与实际主模型实质不一致样例（最多 8 条）\n")
    bad = [r for r in rows if r["note_model"] and r["primary_model"]
           and _norm_model(r["note_model"]) != _norm_model(r["primary_model"])]
    for r in bad[:8]:
        print(f"  [{r['src']}] note={r['note_model']} → 实际={r['primary_model']}"
              f"，模型集合={list(r['models'])[:4]}")
    if not bad:
        print("  （无）")


def cmd_verify(_args) -> int:
    if not SESSION_OUT.exists():
        print("先跑 scan")
        return 2
    rows = [json.loads(line) for line in SESSION_OUT.open(encoding="utf-8") if line.strip()]
    ids = [r["id"] for r in rows]
    assert len(ids) == len(set(ids)), "会话 ID 重复"
    assert all({"id", "src", "in_rag", "level", "primary_model", "models"} <= set(r) for r in rows)
    upstream = {str(s.get("id")) for s in load_upstream()}
    assert set(ids) == upstream, "产物会话集与上游不一致"
    turns = sum(1 for _ in TURN_OUT.open(encoding="utf-8") if _.strip())
    print(f"校验通过：{len(rows)} 会话，ID 唯一且与上游一致；逐条记录 {turns} 行")
    return 0


def main(argv=None) -> int:
    # Windows 控制台默认 GBK，报告里的中文与模型名在管道里会乱码
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="AI 对话史模型名还原（只读原始日志，产出覆盖率报告）")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_scan = sub.add_parser("scan", help="全量恢复并写 JSONL + 报告")
    p_scan.add_argument("--no-merge", action="store_true",
                        help="不沿用历史产物（默认合并：本次没扫到的会话沿用上次结果）")
    p_scan.set_defaults(func=cmd_scan)
    sub.add_parser("verify", help="校验产物").set_defaults(func=cmd_verify)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
