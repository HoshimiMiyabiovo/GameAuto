# -*- coding: utf-8 -*-
"""AI 对话史行为分析：按会话提取「来源 × 任务类型」过程指标，产出 AI 路由先验素材。

数据流：data/ai_history/sessions.jsonl + chunks.jsonl（ai_history_rag.py build 的产物）
  → 按会话重组正文（剥离相邻块的 150 字符重叠，防关键词与消息标记重复计数）
  → 正则切消息（`[YYYY-MM-DD HH:MM] 问:/答:` 起头，其后无标记的块都是上一条消息的续段）
  → 每会话提取：用户轮次 / 修正事件与修正率 / 终态（收工·换方案·放弃·未标注）/
    任务类型（域关键词加权取 plurality，标题 ×3）
  → data/ai_history/behavior/sessions_metrics.jsonl + stdout markdown 表
模型维度：data/ai_history/model/session_models.jsonl（ai_history_model_recover.py）
  → 会话主模型/分布/是否换模型；msg_lens.jsonl（本工具 lenscan 产出）给出
    逐条消息长度×模型归属，table 据此出「模型 × 回复长度」

子命令：
  scan      全量重算指标并写 sessions_metrics.jsonl（与 rag build 同哲学：全量覆盖，
            不做增量；上游库重导后要重跑）
  lenscan   从上游 data.json 抽逐条消息长度（角色干净：ai/think/tool/user），
            按 session_models/turn_models 归属模型，写 behavior/msg_lens.jsonl
  table     src × 任务类型 聚合表（中位轮次/修正率/收工率/换方案率/放弃率）+
            来源总览 + 任务类型总览 + 模型总览 + 模型 × 任务 + 模型 × 回复长度 +
            来源 × 半年时段分布（控制辛普森悖论的时间分层）
  extremes  人工校验素材：修正率最高的格子、修正事件最多的会话（带样本引文）、
            终态=放弃 的会话清单

为什么做这个：给多智能体调度器的路由表提供**本机实证先验**（哪个来源在哪些任务上
省轮次、少返工）。结论按「季度过期」维护——模型 landscape 月月在变，表只是初值，
永远允许被用户显式路由覆盖。

**已知的三个口径硬伤**（读表前必读，详见对话结论）：
  [1] src ≠ 模型：src 记的是客户端/工作流（ZCode/WorkBuddy/Qoder…），同一客户端
      底下的模型会换。**2026-11-14 起模型维度已恢复**：data/ai_history/model/
      session_models.jsonl（ai_history_model_recover.py 产物）给出每会话的模型
      分布（92/103 逐条级、5 个会话级粗粒度、6 个只有档位无模型名），table 增
      设模型总览与模型 × 任务表——但 5 个会话级来源（DeepSeek/Codex/Cursor 等
      早期）漏会话中换模型，模型表对它们仍是粗粒度。
  [2] 任务难度不可比：2025 年的会话是零文档零测试时期的开荒，2026-11 的会话是
      约定+回归齐全时期的精修；跨时段直接排名会系统性冤枉早期来源。所以 table
      附带来源 × 半年分布，横向比较只应在同时段内部做。
  [3] sessions.jsonl 的 score 字段是**入选过滤的相关性分**（ai_history_rag.py 的
      工作区命中 +30 / 关键词计分），不是质量分——本工具记录但不使用它。

小样本警告：103 会话 ÷（来源 × 任务类型）后每格常是个位数，做不出统计显著，
只能当**定性画像**；extremes 子命令的存在就是为了让人工校验先于结论。

术语约定：
  用户轮次  = 连续 问: 消息合并成一轮（harness 注入的 # Environment 等系统块
              不算轮次、不参与修正检测——否则 ZCode 每会话固定多 1~2 轮噪声）
  修正事件  = 一轮用户消息命中修正词表（不对/报错/还是没/误判…）
  终态      = 末尾 5 轮从新到旧找显式收口（换方案 > 放弃 > 收工，首个命中生效）；
              全不命中时看最后一轮：有纠偏词 → 未收口（会话停在一条不满上），
              否则 → 顺延。顺延是 2026-11-14 人工校验揪出来的核心口径：用户的
              真实行为是"做好了直接派下一个任务"，全程不出现收口词——显式
              "收工"只是下限，顺延（无纠偏转向下一件事/快照时仍在进行）才是
              常态正向信号
  频道会话  = 跨度 >48h 的会话（WorkBuddy/Hermes 的日常询问频道一连续用几天，
              任务粒度 ≠ 会话粒度：轮次/终态只作参考，修正率仍可用）
"""
import argparse
import json
import re
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data" / "ai_history"
CHUNKS_PATH = DATA_DIR / "chunks.jsonl"
SESSIONS_PATH = DATA_DIR / "sessions.jsonl"
BEHAVIOR_DIR = DATA_DIR / "behavior"
METRICS_PATH = BEHAVIOR_DIR / "sessions_metrics.jsonl"
# 会话级模型还原数据（ai_history_model_recover.py 产物；缺失时模型维度优雅降级）
MODELS_PATH = DATA_DIR / "model" / "session_models.jsonl"
TURN_MODELS_PATH = DATA_DIR / "model" / "turn_models.jsonl"
# 上游导出（build.py 产物）：逐条消息带干净角色（r ∈ user/ai/think/tool）与正文 x。
# 只在 lenscan 读一次（112MB，整载内存峰值与 rag build 同量级约 1GB），
# 长度落成 sidecar 供 scan/table 反复消费，避免每次全量重读
SOURCE_JSON = Path(r"F:\data\AI对话汇总\data.json")
LENGTHS_PATH = BEHAVIOR_DIR / "msg_lens.jsonl"
# 换模型会话的逐条归属：消息时间戳与 turn_model 时间戳的最近邻容差（秒）。
# build.py 导出同一 assistant 消息的 ai/think part 共享同一时间戳，正常应精确命中；
# 容差只兜原始时钟粒度差，超过 120s 视为没对上，回落会话主模型
TS_MATCH_TOL = 120.0

# 消息标记：build 脚本统一写 `[YYYY-MM-DD HH:MM] 问:/答:`；冒号容忍全角
MSG_RE = re.compile(r"\[(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2})\] (问|答)[:：]")
# harness 注入的用户角色块（agent 框架塞给模型的上下文，不是人打的话）——
# 出现在消息体前缀才算，用户中途粘贴的正文里出现同串不受影响。
# <total_tokens> 是 ZCode 的 token 预算提醒（实测一个会话能塞几十条，
# 不滤会把 ZCode 轮次虚增到数倍）；裸文本 "The TodoWrite..." 是导出时
# 丢了 <system-reminder> 包裹的漏网块，按字面识别。
HARNESS_MARKERS = ("# Environment", "You have been invoked", "<system-reminder>",
                   "<command-name>", "<task-notification>", "Caveat:",
                   "<total_tokens>", "<conversation_history_summary",
                   "The TodoWrite tool hasn't been used recently")

# 任务分类词表：来自本仓库域词汇（games 注册表 / conventions / changelog）。
# 匹配一律走 lowercase 后子串计数（.lower() 不影响中文字符），大小写变体只写一份。
# 词表刻意避开单字歧义词（"锁"会命中"解锁"→"死锁/互斥/锁死"代替）。
TASK_CATEGORIES: Dict[str, Tuple[str, ...]] = {
    "OCR与视觉": ("ocr", "识别", "阈值", "截图", "认不出", "看不清", "onnxocr", "模板匹配"),
    "流程与模板点击": ("模板", "点击", "流程", "场景", "主界面", "页面判定", "弹窗",
                       "步骤", "run_flow", "启动器", "接管", "前台"),
    "UI与桥接": ("qml", "huskarui", "界面", "视图", "bridge", "webui", "桥接", "控件"),
    "并发与稳定性": ("并发", "死锁", "互斥", "加锁", "锁死", "卡死", "崩溃", "超时",
                     "死循环", "中断", "误杀", "孤儿", "显存", "内存泄漏", "挂了"),
    "调度与定时": ("调度", "定时", "补跑", "日历", "cron", "计划任务", "排队", "后台任务"),
    "签到与游戏日常": ("签到", "日常", "月度", "每日", "周常", "米游社", "库街区", "体力",
                       "月商店", "抽卡", "演算", "派遣"),
    "游戏专项与接入": ("白荆", "异环", "崩铁", "星铁", "星穹", "终末地", "暖暖", "nikki",
                       "鸣潮", "wuwa", "绝区零", "zzz", "okww", "ok-ww", "原神", "接入",
                       "登录", "账号", "exe_from"),
    "工程与AI基建": ("rag", "检索", "索引", "ai_history", "bm25", "测试", "回归", "套件",
                     "ci ", "文档", "changelog", "约定", "重构", "注释", "teleagent",
                     "调度器", "路由", "多智能体", "智能体", "prompt", "提示词", "模型"),
}
# 修正词表：用户对 AI 上一轮输出的不满信号。全多字词，避开"又/红/错"这类
# 单字高歧义命中；"没修好/没通过"与 DONE 的"修好了/通过了"互为否定对。
CORRECTION_KEYWORDS: Tuple[str, ...] = (
    "不对", "不行", "报错", "报错了", "错了", "还是没", "还是不", "又错", "又报",
    "没用", "无效", "失败", "误判", "不认", "没生效", "不生效", "回退", "白做",
    "白干", "坏了", "崩了", "乱改", "瞎改", "不对劲", "怎么还", "咋还", "还是出",
    "又卡", "还是卡", "没修好", "没解决", "有bug", "有 bug", "超时了", "没通过",
    "没跑过", "没好", "还是失败", "测试挂", "跑挂", "认不出来", "认不出",
)
# 终态词表。收工词刻意选带完成态后缀的（"通过"单独出现会命中"未通过"）。
DONE_KEYWORDS: Tuple[str, ...] = (
    "好了", "可以了", "搞定", "收工", "完美", "成了", "通过了", "跑通", "生效了",
    "没问题", "正常了", "稳了", "全过", "绿了", "修好了", "解决了", "done", "ok了",
)
PIVOT_KEYWORDS: Tuple[str, ...] = (
    "换个", "换方案", "换思路", "换模型", "换工具", "换成", "改用", "用回", "回滚",
    "换回来", "另想办法", "换个方式", "不如",
)
ABANDON_KEYWORDS: Tuple[str, ...] = (
    "算了", "放弃", "不搞了", "先不做", "以后再说", "搁置", "作罢", "先放着",
)
# 同一轮内同时命中多类终态词时的优先级：换方案/放弃词比收工词更"具体"，
# "好了…算了先放着"按放弃算而不是收工
TERMINAL_PRIORITY = ("换方案", "放弃", "收工")
# 频道会话判定线：WorkBuddy/Hermes 日常询问频道实测跨度 74~97h，ZCode 深夜
# 连跑的任务会话实测 ~24-30h——48h 卡在两者之间（校验依据见 2026-11-14 对话）
CHANNEL_MIN_HOURS = 48


def norm_lower(s: str) -> str:
    """统一 lowercase：中文原样保留，ASCII 大小写变体只维护一份词表。"""
    return (s or "").lower()


def half_year(date_str: str) -> str:
    """'2026-11-12 23:56' → '2026H2'；解析失败返回 '?'。"""
    m = re.match(r"(\d{4})-(\d{2})", (date_str or "").strip())
    if not m:
        return "?"
    year, month = int(m.group(1)), int(m.group(2))
    return f"{year}H{1 if month <= 6 else 2}"


def span_hours(start: str, end: str) -> Optional[float]:
    """会话跨度小时数；任一端解析失败返回 None（不武断判频道）。"""
    fmt = "%Y-%m-%d %H:%M"
    try:
        delta = (datetime.strptime((end or "").strip(), fmt)
                 - datetime.strptime((start or "").strip(), fmt)).total_seconds() / 3600
    except (ValueError, TypeError):
        return None
    return max(delta, 0.0)


def is_harness(body: str) -> bool:
    """harness 注入的系统上下文块：不算用户轮次、不参与修正检测。"""
    head = (body or "")[:400]
    return any(marker in head for marker in HARNESS_MARKERS)


def join_chunks(chunks: List[dict]) -> str:
    """按 chunk 序号拼接会话正文，剥离相邻块重叠前缀。

    ai_history_rag.py 切块带 CHUNK_OVERLAP=150 重叠：相邻块间后块开头 ≈ 前块结尾。
    不剥离会让跨块的关键词/标记数两遍；比对窗口取 200 字符、最短认 20——
    找不到 ≥20 的重叠就原样拼接（宁可少量重复计数，不可误删正文）。
    """
    ordered = sorted(chunks, key=lambda o: int(o.get("chunk", 0)))
    parts: List[str] = []
    prev = ""
    for o in ordered:
        t = o.get("text") or ""
        if prev:
            limit = min(200, len(prev), len(t))
            for k in range(limit, 19, -1):
                if t[:k] == prev[-k:]:
                    t = t[k:]
                    break
        parts.append(t)
        prev = t if t else prev  # 空文本块不更新 prev，保住有效比对基准
    return "".join(parts)


def split_messages(text: str) -> List[dict]:
    """按标记切消息。首标记前的正文（无角色头）丢弃——它是导出时的杂项前缀。"""
    msgs: List[dict] = []
    matches = list(MSG_RE.finditer(text))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[m.end():end].strip()
        msgs.append({"date": m.group(1), "time": m.group(2),
                     "role": m.group(3), "body": body})
    return msgs


def merge_user_turns(msgs: List[dict]) -> List[dict]:
    """连续 问: 消息（含 harness 块）合并为一轮；轮体只留非 harness 正文。

    为什么合并：ZCode 会话里 harness 的 # Environment 块是独立 问: 消息，
    紧跟在真实用户输入前后——不合并会把一轮拆成多轮，虚增轮次。
    轮保留 bodies 原始消息体列表（不预拼接）：修正引文要来自真正命中词表的
    那条消息，拼接后按 \\n 切会把用户粘贴的多行正文截断成无上下文的行。
    """
    turns: List[dict] = []
    bodies: List[str] = []
    date = time = ""
    for m in msgs:
        if m["role"] == "问":
            if not bodies:
                date, time = m["date"], m["time"]
            if m["body"] and not is_harness(m["body"]):
                bodies.append(m["body"])
        else:
            if bodies:
                turns.append({"date": date, "time": time, "bodies": bodies})
            bodies, date, time = [], "", ""
    if bodies:
        turns.append({"date": date, "time": time, "bodies": bodies})
    return turns


def classify_task(title: str, body: str) -> Tuple[str, float, List[Tuple[str, int]]]:
    """任务类型 = 加权关键词 plurality（标题 ×3，正文 ×1）。

    返回 (类别, 头名占比, top3 命中)。头名占比 <0.4 的会话在表里仍归头名，
    但 extremes 里可借占比识别"混合任务"（长会话跑题是常态）。
    """
    title_l, body_l = norm_lower(title), norm_lower(body)
    scores: Dict[str, int] = {}
    for cat, kws in TASK_CATEGORIES.items():
        s = sum(title_l.count(kw) * 3 for kw in kws) + sum(body_l.count(kw) for kw in kws)
        if s:
            scores[cat] = s
    if not scores:
        return "其他/未分类", 0.0, []
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    total = sum(scores.values()) or 1
    return ranked[0][0], ranked[0][1] / total, ranked[:3]


def _snippet(body: str, limit: int = 120) -> str:
    flat = re.sub(r"\s+", " ", body or "").strip()
    return flat[:limit] + ("…" if len(flat) > limit else "")


# 任务切分的话题词表：轮与轮之间"话题不相交"是新任务的最廉价信号
# （频道里"白荆为什么失败"→"异环接入怎么弄"这类切换）。词表来自域词汇，
# 意图是区分话题，不做语义理解——双方都没词（"那阈值呢"）就不判边界。
TOPIC_TOKENS: Tuple[str, ...] = (
    "白荆", "异环", "崩铁", "星铁", "星穹", "终末地", "暖暖", "nikki", "绝区零",
    "zzz", "鸣潮", "wuwa", "原神", "崩坏", "米游社", "库街区", "森空岛", "签到",
    "月商店", "抽卡", "日常", "月度", "体力", "演算", "派遣", "ocr", "识别",
    "模板", "qml", "huskarui", "界面", "视图", "webui", "桥接", "bridge",
    "调度", "定时", "补跑", "日历", "凭据", "dpapi", "日志", "统计", "进度",
    "日报", "更新", "下载", "关机", "重启", "积分", "teleagent", "薅羊毛",
    "qoder", "权益", "启动器", "弹窗", "主界面", "场景", "接入", "登录", "账号",
    "重构", "文档", "测试", "okww", "ok-ww",
)
# 任务段的间隔阈值：实测轮间隔中位 5~10min、>2h 仅占 8~15%（2026-11-14 校准），
# 2h 卡在"离开一会儿"与"换了件事"之间
TASK_GAP_HOURS = 2.0


def _topic_tokens(text: str) -> set:
    low = norm_lower(text)
    return {kw for kw in TOPIC_TOKENS if kw in low}


def segment_tasks(turns: List[dict]) -> List[dict]:
    """把会话切成近似任务段（v1 规则，宁粗勿细，任务数是**下限**）。

    边界判定（按序）：
      [1] 第一轮总是新任务；
      [2] 上一轮有显式收口/放弃词（好了/搞定/算了…）→ 新任务；
      [3] 与上一轮间隔 >2h → 新任务；
      [4] 当前轮话题词与当前任务段累计话题词不相交且双方非空 → 新任务
          （唯一能越过纠偏词的规则：频道里"异环干活中插一句白荆又失败了"
          是新事故，不是返工）。
    其余情况一律续当前任务——纠偏轮（"还是不行"）、无话题词的追问
    （"那阈值呢"）、换方案尝试都是同一任务的复杂度。
    """
    tasks: List[dict] = []
    cur = {"turns": 0, "corrections": 0, "tokens": set()}
    prev_dt = None
    prev_closed = False
    for t in turns:
        text = norm_lower("\n".join(t["bodies"]))
        toks = _topic_tokens(text)
        is_corr = any(kw in text for kw in CORRECTION_KEYWORDS)
        try:
            dt = datetime.strptime(f"{t['date']} {t['time']}", "%Y-%m-%d %H:%M")
        except (ValueError, TypeError, KeyError):
            dt = None
        boundary = False
        if prev_dt is None or prev_closed:
            boundary = True
        elif (dt is not None and prev_dt is not None
              and (dt - prev_dt).total_seconds() / 3600 > TASK_GAP_HOURS):
            boundary = True
        elif toks and cur["tokens"] and not (toks & cur["tokens"]):
            boundary = True  # 话题切换优先于纠偏续挂
        if boundary:
            if cur["turns"]:
                tasks.append({"turns": cur["turns"], "corrections": cur["corrections"]})
            cur = {"turns": 0, "corrections": 0, "tokens": set()}
        # 纠偏计数必须在边界 flush 之后：边界轮自身的纠偏属于新任务
        # （"白荆今天又失败了"开新段，那次失败是新任务的返工，不是旧任务的）
        if is_corr:
            cur["corrections"] += 1
        cur["turns"] += 1
        cur["tokens"] |= toks
        prev_dt = dt if dt is not None else prev_dt
        prev_closed = (any(kw in text for kw in DONE_KEYWORDS)
                       or any(kw in text for kw in ABANDON_KEYWORDS))
    if cur["turns"]:
        tasks.append({"turns": cur["turns"], "corrections": cur["corrections"]})
    return tasks


def _terminal_state(turns: List[dict]) -> str:
    """末尾 5 轮从新到旧找显式收口词（首个命中生效）；全不命中时看最后一轮。

    从新到旧：倒数第一轮才是"会话怎么结束的"；倒数第 3 轮的"好了"不该盖住
    最后一轮的"算了先放着"。全不命中时：最后一轮带纠偏词 → 未收口（会话
    停在一条不满上，不是正常收束）；否则 → 顺延（做好了直接派下一个，
    口径依据见 docstring 术语约定 / 2026-11-14 校验反馈）。
    """
    if not turns:
        return "未标注"
    for turn in reversed(turns[-5:]):
        text = norm_lower("\n".join(turn["bodies"]))
        if any(kw in text for kw in PIVOT_KEYWORDS):
            return "换方案"
        if any(kw in text for kw in ABANDON_KEYWORDS):
            return "放弃"
        if any(kw in text for kw in DONE_KEYWORDS):
            return "收工"
    last_text = norm_lower("\n".join(turns[-1]["bodies"]))
    if any(kw in last_text for kw in CORRECTION_KEYWORDS):
        return "未收口"
    return "顺延"


def session_metrics(sess: dict, chunks: List[dict],
                    model_rec: Optional[dict] = None) -> dict:
    """单会话全量指标。契约：只读，返回可 JSON 序列化的 dict。

    model_rec：session_models.jsonl 里该会话的模型还原记录（可选；缺省时
    模型字段降级为空串——模型数据是增强不是前置）。
    """
    text = join_chunks(chunks)
    msgs = split_messages(text)
    turns = merge_user_turns(msgs)

    corrections = 0
    samples: List[str] = []
    for turn in turns:
        # 逐消息体检测：引文来自真正命中词表的那条消息（轮可能合并多条消息，
        # 拼接后取头部会让 harness 残留/长杂项抢占样本，校验时像张冠李戴）
        hit_bodies = [b for b in turn["bodies"]
                      if any(kw in norm_lower(b) for kw in CORRECTION_KEYWORDS)]
        if hit_bodies:
            corrections += 1
            if len(samples) < 2:
                samples.append(_snippet(hit_bodies[0]))

    task, share, top_hits = classify_task(sess.get("title") or "", text)
    n_user = len(turns)
    tasks = segment_tasks(turns)
    span = span_hours(sess.get("start") or "", sess.get("end") or "")
    return {
        "id": sess.get("id"),
        "src": sess.get("src"),
        "title": sess.get("title"),
        "start": sess.get("start"),
        "end": sess.get("end"),
        "n_msgs": sess.get("n_msgs"),
        # 入选相关性分，口径见模块 docstring [3]；记录不使用
        "filter_score": sess.get("score"),
        "period": half_year(sess.get("start") or ""),
        "span_hours": round(span, 1) if span is not None else None,
        "channel": bool(span is not None and span > CHANNEL_MIN_HOURS),
        # 模型维度：只认 model_kind=="named" 的具体模型名；tier（DeepSeek 早期
        # 的 default/expert 档位）不是模型，宁可空串也不当模型名用
        "model": (model_rec.get("primary_model") or "") if (model_rec or {}).get("model_kind") == "named" else "",
        "model_level": (model_rec or {}).get("level") or "",
        "model_share": (
            round((model_rec.get("models") or {}).get(model_rec.get("primary_model"), 0)
                  / max(1, sum((model_rec.get("models") or {}).values())), 3)
        ) if (model_rec or {}).get("model_kind") == "named" else 0.0,
        "model_switched": bool((model_rec or {}).get("switched")),
        "n_user_turns": n_user,
        # 近似任务段（segment_tasks；口径见函数 docstring，任务数是下限）
        "n_tasks": len(tasks),
        "task_sizes": [t["turns"] for t in tasks],
        "task_corrections": [t["corrections"] for t in tasks],
        "n_ai_msgs": sum(1 for m in msgs if m["role"] == "答"),
        "corrections": corrections,
        "correction_rate": round(corrections / n_user, 4) if n_user else 0.0,
        "terminal": _terminal_state(turns),
        "task": task,
        "task_share": round(share, 3),
        "top_hits": [(c, n) for c, n in top_hits],
        "samples": samples,
    }


def load_inputs() -> Tuple[List[dict], Dict[str, List[dict]]]:
    """读 sessions + 按 session 分组的 chunks；chunk 侧多出的会话补骨架条目。"""
    sessions: List[dict] = []
    if SESSIONS_PATH.exists():
        with SESSIONS_PATH.open(encoding="utf-8") as f:
            sessions = [json.loads(l) for l in f if l.strip()]
    by_id = {s.get("id"): s for s in sessions}
    chunks_by_session: Dict[str, List[dict]] = defaultdict(list)
    if CHUNKS_PATH.exists():
        with CHUNKS_PATH.open(encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                o = json.loads(line)
                chunks_by_session[o["session"]].append(o)
    # chunks 里有而 sessions.jsonl 没有的会话（理论上不该有，防御性补骨架）
    for sid in chunks_by_session:
        if sid not in by_id:
            first = chunks_by_session[sid][0]
            sessions.append({"id": sid, "src": first.get("src"), "title": first.get("title"),
                             "start": first.get("start"), "end": first.get("end")})
    return sessions, chunks_by_session


def load_models() -> Dict[str, dict]:
    """读会话级模型还原数据（ai_history_model_recover.py 产物）。

    文件缺失/损坏时返回空 dict——模型维度整体降级为空串，不阻塞 scan
    （模型数据是可选增强，不是行为分析的前置）。
    """
    if not MODELS_PATH.exists():
        return {}
    out: Dict[str, dict] = {}
    with MODELS_PATH.open(encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            if o.get("id"):
                out[o["id"]] = o
    return out


def cmd_lenscan(_args) -> int:
    """逐条消息长度抽取 + 模型归属，写 msg_lens.jsonl。

    为什么不直接从 chunks 文本量长度：ai_history_rag.py build 把 think/tool
    一律渲染成 `答:`（rag 源码 93/143 行），chunk 口径的 AI 消息混着思考与
    工具摘要；data.json 的 r 字段才是干净角色。

    归属规则（宁可空也不误归）：
      单模型会话（named 且 n_models==1）→ 全部消息精确归主模型；
      换模型会话（per_message 且有 turn 记录）→ 按时间戳最近邻（±120s）归属，
        落不进容差回落主模型（主模型占绝对多数，偏差有界）；
      会话级/档位/无记录 → model=""（不给模型算长度，避免粗粒度冒充逐条）；
      user 消息 → 永远 model=""（人写的，不是模型输出）。
    tool 摘要是 build.py 生成的一行概述，不是模型输出——保留行（数工具调用
    密度用）但长度不进模型统计。
    """
    if not SOURCE_JSON.exists():
        print(f"上游导出不存在：{SOURCE_JSON}——先跑外部 build.py 重导")
        return 2
    rag_ids = set()
    if SESSIONS_PATH.exists():
        with SESSIONS_PATH.open(encoding="utf-8") as f:
            rag_ids = {json.loads(l)["id"] for l in f if l.strip()}
    model_map = load_models()
    turns: Dict[str, List[Tuple[float, str]]] = defaultdict(list)
    if TURN_MODELS_PATH.exists():
        with TURN_MODELS_PATH.open(encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                o = json.loads(line)
                try:
                    ts = float(o.get("ts")) / 1000.0  # 恢复器写 epoch 毫秒，data.json k 是秒
                except (TypeError, ValueError):
                    continue
                if o.get("model"):
                    turns[o["id"]].append((ts, o["model"]))
    for sid in turns:
        turns[sid].sort()

    print(f"整载 {SOURCE_JSON}（约 112MB，稍等）…")
    payload = json.loads(SOURCE_JSON.read_text(encoding="utf-8"))
    sess_list = payload.get("sessions") or []
    rows = matched = sessions_done = 0
    BEHAVIOR_DIR.mkdir(parents=True, exist_ok=True)
    with LENGTHS_PATH.open("w", encoding="utf-8") as f:
        for s in sess_list:
            sid = str(s.get("id") or "")
            if sid not in rag_ids:
                continue
            sessions_done += 1
            rec = model_map.get(sid) or {}
            models = rec.get("models") or {}
            single = rec.get("model_kind") == "named" and len(models) == 1
            primary = rec.get("primary_model") or ""
            per_msg = rec.get("level") == "per_message" and bool(turns.get(sid))
            tl = turns.get(sid) or []
            for m in s.get("msgs") or []:
                r = str(m.get("r") or "")
                if r not in ("user", "ai", "think", "tool"):
                    continue
                n = len(str(m.get("x") or ""))
                model, how = "", ""
                if r == "user":
                    pass  # 人写的，不归模型
                elif single:
                    model, how = primary, "single"
                elif per_msg:
                    ts = float(m.get("k") or 0)
                    best = min(tl, key=lambda t: abs(t[0] - ts), default=None)
                    if best and abs(best[0] - ts) <= TS_MATCH_TOL:
                        model, how = best[1], "ts"
                        matched += 1
                    else:
                        model, how = primary, "primary"
                row = {"id": sid, "r": r, "n": n, "m": model, "how": how}
                # clean() 截断标记（build.py MAXMSG=14000，保留头72%+尾28%）：恰好
                # 触顶即真实长度未知——长度统计要把这部分暴露出来而不是默默吸收
                if r in ("user", "ai") and n >= 14000:
                    row["cap"] = 1
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
                rows += 1
    print(f"msg_lens.jsonl：{sessions_done} 会话 / {rows} 条消息；"
          f"换模型会话里按时间戳精确归属 {matched} 条，其余归主模型或未恢复")
    return 0


def _load_len_summary() -> Dict[str, dict]:
    """读 msg_lens.jsonl 聚成会话级摘要（scan 用）；文件缺失返回空 dict 降级。"""
    if not LENGTHS_PATH.exists():
        return {}
    agg: Dict[str, dict] = defaultdict(
        lambda: {"ai": [], "user": [], "n_think": 0, "n_tool": 0, "n_cap": 0})
    with LENGTHS_PATH.open(encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            o = json.loads(line)
            a = agg[o["id"]]
            if o["r"] == "ai":
                a["ai"].append(o["n"])
                a["n_cap"] += o.get("cap", 0)
            elif o["r"] == "user":
                a["user"].append(o["n"])
            elif o["r"] == "think":
                a["n_think"] += 1
            elif o["r"] == "tool":
                a["n_tool"] += 1
    out: Dict[str, dict] = {}
    for sid, a in agg.items():
        out[sid] = {
            "n_ai_true": len(a["ai"]),
            "n_think": a["n_think"],
            "n_tool": a["n_tool"],
            "ai_len_median": statistics.median(a["ai"]) if a["ai"] else 0,
            "ai_len_total": sum(a["ai"]),
            "n_capped": a["n_cap"],
            "user_len_median": statistics.median(a["user"]) if a["user"] else 0,
        }
    return out


def _load_len_summary_defaults(summary: Optional[dict]) -> dict:
    """会话长度摘要的缺省壳：lenscan 没跑/会话无记录时保持字段齐全。"""
    return {
        "n_ai_true": (summary or {}).get("n_ai_true", 0),
        "n_think": (summary or {}).get("n_think", 0),
        "n_tool": (summary or {}).get("n_tool", 0),
        "ai_len_median": (summary or {}).get("ai_len_median", 0),
        "ai_len_total": (summary or {}).get("ai_len_total", 0),
        "n_capped": (summary or {}).get("n_capped", 0),
        "user_len_median": (summary or {}).get("user_len_median", 0),
    }


def cmd_scan(_args) -> int:
    sessions, chunks_by_session = load_inputs()
    model_map = load_models()
    len_summary = _load_len_summary()
    records = []
    for s in sessions:
        rec = session_metrics(s, chunks_by_session.get(s.get("id"), []),
                              model_map.get(s.get("id")))
        # 逐条长度摘要（lenscan 产物，缺失则缺省——模型维度增强，不是前置）
        rec.update(_load_len_summary_defaults(len_summary.get(s.get("id"))))
        records.append(rec)
    BEHAVIOR_DIR.mkdir(parents=True, exist_ok=True)
    with METRICS_PATH.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"指标已写 {METRICS_PATH}（{len(records)} 会话，全量覆盖）")
    by_src = defaultdict(int)
    for r in records:
        by_src[r["src"]] += 1
    for src, n in sorted(by_src.items(), key=lambda kv: -kv[1]):
        print(f"  {src}: {n}")
    return 0


def _load_metrics() -> List[dict]:
    if not METRICS_PATH.exists():
        print("先跑 scan：.venv/Scripts/python.exe tools/ai_history_behavior.py scan")
        sys.exit(2)
    with METRICS_PATH.open(encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def _filter_metrics(args) -> List[dict]:
    records = _load_metrics()
    if not args.include_subagent:
        records = [r for r in records if "子任务" not in (r["src"] or "")]
    if getattr(args, "src", None):
        records = [r for r in records if r["src"] == args.src]
    if getattr(args, "period", None):
        records = [r for r in records if r["period"] == args.period]
    return records


def _cell_stats(records: List[dict]) -> dict:
    """一格（src×task 或 src 全部）的聚合统计。修正率=池化（总修正/总轮次），
    不是各会话修正率的均值——单会话 0~1 轮的修正率没有意义，池化更稳。"""
    n = len(records)
    rounds = [r["n_user_turns"] for r in records if r["n_user_turns"] > 0]
    total_turns = sum(rounds)
    corr = sum(r["corrections"] for r in records)
    return {
        "n": n,
        "median_rounds": statistics.median(rounds) if rounds else 0,
        "corr_rate": corr / total_turns if total_turns else 0.0,
        "done": sum(1 for r in records if r["terminal"] == "收工") / n if n else 0.0,
        "cont": sum(1 for r in records if r["terminal"] == "顺延") / n if n else 0.0,
        "pivot": sum(1 for r in records if r["terminal"] == "换方案") / n if n else 0.0,
        "abandon": sum(1 for r in records if r["terminal"] == "放弃") / n if n else 0.0,
    }


def _pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def cmd_table(args) -> int:
    records = _filter_metrics(args)
    if not records:
        print("过滤后 0 会话——放宽 --src/--period 或加 --include-subagent")
        return 0
    cells: Dict[Tuple[str, str], List[dict]] = defaultdict(list)
    for r in records:
        cells[(r["src"], r["task"])].append(r)

    min_n = args.min_sessions
    print(f"\n## src × 任务类型（{len(records)} 会话；每格 <{min_n} 条不展示，"
          f"中位轮次=用户轮次中位数，修正率=池化）\n")
    print("> 终态口径：末 5 轮找显式收口词（换方案>放弃>收工）；无收口词且最后"
          "一轮无纠偏 = 顺延（做好了直接派下一个 / 快照时仍在进行，2026-11-14 "
          "校验修正）；最后一轮有纠偏 = 未收口。跨度>48h 的频道会话任务粒度≠"
          "会话粒度，轮次/终态只作参考。\n")
    print("| src | 任务类型 | 会话数 | 中位轮次 | 修正率 | 收工 | 顺延 | 换方案 | 放弃 |")
    print("|---|---|---:|---:|---:|---:|---:|---:|---:|")
    shown = [(k, _cell_stats(v)) for k, v in cells.items() if len(v) >= min_n]
    shown.sort(key=lambda kv: (-kv[1]["n"], kv[0][0], kv[0][1]))
    for (src, task), st in shown:
        print(f"| {src} | {task} | {st['n']} | {st['median_rounds']:.0f} | "
              f"{_pct(st['corr_rate'])} | {_pct(st['done'])} | {_pct(st['cont'])} | "
              f"{_pct(st['pivot'])} | {_pct(st['abandon'])} |")

    print("\n## 来源总览（跨任务类型合计，用于看来源整体档位）\n")
    by_src: Dict[str, List[dict]] = defaultdict(list)
    for r in records:
        by_src[r["src"]].append(r)
    print("| src | 会话数 | 中位轮次 | 修正率 | 收工 | 顺延 | 换方案 | 放弃 | 频道 |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    src_rows = sorted(((_cell_stats(v), k, v) for k, v in by_src.items()),
                      key=lambda kv: (-kv[0]["n"], kv[1]))
    for st, src, v in src_rows:
        n_channel = sum(1 for r in v if r.get("channel"))
        print(f"| {src} | {st['n']} | {st['median_rounds']:.0f} | "
              f"{_pct(st['corr_rate'])} | {_pct(st['done'])} | {_pct(st['cont'])} | "
              f"{_pct(st['pivot'])} | {_pct(st['abandon'])} | {n_channel} |")

    print("\n## 任务类型总览（跨来源合计，看哪类任务最费轮次）\n")
    by_task: Dict[str, List[dict]] = defaultdict(list)
    for r in records:
        by_task[r["task"]].append(r)
    print("| 任务类型 | 会话数 | 中位轮次 | 修正率 | 收工 | 顺延 | 换方案 | 放弃 |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|")
    task_rows = sorted(((_cell_stats(v), k) for k, v in by_task.items()),
                       key=lambda kv: (-kv[0]["n"], kv[1]))
    for st, task in task_rows:
        print(f"| {task} | {st['n']} | {st['median_rounds']:.0f} | "
              f"{_pct(st['corr_rate'])} | {_pct(st['done'])} | {_pct(st['cont'])} | "
              f"{_pct(st['pivot'])} | {_pct(st['abandon'])} |")

    # 模型维度：session_models.jsonl（ai_history_model_recover.py 产物）。
    # 空 model = 文件缺失 / tier 档位（无具体模型名）/ 未覆盖——统一归
    # "(未恢复/档位级)"，不给它算模型排名。
    print("\n## 模型总览（恢复口径：per_message=逐条恢复，session=会话级粗粒度"
          "可能漏会话中换模型）\n")
    by_model: Dict[str, List[dict]] = defaultdict(list)
    for r in records:
        by_model[r.get("model") or "(未恢复/档位级)"].append(r)
    model_src = {}
    for m, v in by_model.items():
        model_src[m] = Counter(r["src"] for r in v).most_common(1)[0][0]
    print("| 模型 | 会话数 | 中位轮次 | 修正率 | 收工 | 顺延 | 换方案 | 主要来源 |")
    print("|---|---:|---:|---:|---:|---:|---:|---|")
    model_rows = sorted(((_cell_stats(v), k) for k, v in by_model.items()),
                        key=lambda kv: (-kv[0]["n"], kv[1]))
    for st, model in model_rows:
        print(f"| {model} | {st['n']} | {st['median_rounds']:.0f} | "
              f"{_pct(st['corr_rate'])} | {_pct(st['done'])} | {_pct(st['cont'])} | "
              f"{_pct(st['pivot'])} | {model_src[model]} |")

    print("\n## 模型 × 任务类型（每格 <{} 条不展示）\n".format(min_n))
    mc: Dict[Tuple[str, str], List[dict]] = defaultdict(list)
    for r in records:
        mc[(r.get("model") or "(未恢复/档位级)", r["task"])].append(r)
    shown_m = [(k, _cell_stats(v)) for k, v in mc.items() if len(v) >= min_n]
    shown_m.sort(key=lambda kv: (-kv[1]["n"], kv[0][0], kv[0][1]))
    if shown_m:
        print("| 模型 | 任务类型 | 会话数 | 中位轮次 | 修正率 | 收工 | 顺延 |")
        print("|---|---|---:|---:|---:|---:|---:|")
        for (model, task), st in shown_m:
            print(f"| {model} | {task} | {st['n']} | {st['median_rounds']:.0f} | "
                  f"{_pct(st['corr_rate'])} | {_pct(st['done'])} | {_pct(st['cont'])} |")
    else:
        print("（无 ≥{} 会话的格子）".format(min_n))

    # 模型 × 回复长度：msg_lens.jsonl（lenscan 产物）按模型池化逐条长度。
    # 正文/思考是模型输出；user 永不归模型；tool 摘要由 build.py 生成，只计数。
    # 长度=clean 后字符数（正文 14000 截断上限，罕见触及）。
    if LENGTHS_PATH.exists():
        print("\n## 模型 × 回复长度（正文=模型输出字符数；换模型会话按时间戳"
              "最近邻归属，±120s 外回落主模型）\n")
        print("> 口径：只统计上表纳入的会话（子任务默认剔除）；条数受各客户端"
              "导出粒度影响（Copilot 增量日志把一条回复拆成多条小消息），跨客户端"
              "比条数无意义，**轮均输出**（未换模型会话的 AI 正文字符 ÷ 用户轮次）"
              "才是可比口径。\n")
        allow_ids = {r["id"] for r in records}
        pool: Dict[str, dict] = defaultdict(
            lambda: {"ai": [], "think": [], "tool": 0, "cap": 0})
        # 轮均输出的分子分母必须同口径：都只取「未换模型会话」——该会话全部
        # AI 输出确定归其主模型；换模型会话的归属是近似，混进来会虚增
        # （首版 bug：分子含换模型会话的归属消息、分母只有未换模型会话，
        # step-5-preview 被虚抬到 12116，实际 1413）
        turn_out: Dict[str, List[int]] = defaultdict(lambda: [0, 0])
        for r in records:
            if not r.get("model_switched") and r.get("model"):
                turn_out[r["model"]][0] += r.get("ai_len_total", 0)
                turn_out[r["model"]][1] += r["n_user_turns"]
        with LENGTHS_PATH.open(encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                o = json.loads(line)
                if not o.get("m") or o["id"] not in allow_ids:
                    continue  # user / 未恢复 / 档位级 / 子任务：不进模型长度统计
                d = pool[o["m"]]
                if o["r"] == "ai":
                    d["ai"].append(o["n"])
                    d["cap"] += o.get("cap", 0)
                elif o["r"] == "think":
                    d["think"].append(o["n"])
                elif o["r"] == "tool":
                    d["tool"] += 1
        print("| 模型 | 正文条数 | 正文中位 | 正文均值 | p25~p75 | 轮均输出 | "
              "思考条数 | 思考中位 | 工具调用 |")
        print("|---|---:|---:|---:|---|---:|---:|---:|---:|")
        len_rows = sorted(pool.items(), key=lambda kv: -len(kv[1]["ai"]))
        for model, d in len_rows:
            ai, th = d["ai"], d["think"]
            if not ai:
                continue
            try:
                q = statistics.quantiles(ai, n=4)
                rng = f"{q[0]:.0f}~{q[2]:.0f}"
            except statistics.StatisticsError:
                rng = "—"
            per_turn = (f"{turn_out[model][0] / turn_out[model][1]:.0f}"
                        if turn_out.get(model) and turn_out[model][1] else "—")
            print(f"| {model} | {len(ai)} | {statistics.median(ai):.0f} | "
                  f"{sum(ai) / len(ai):.0f} | {rng} | {per_turn} | {len(th)} | "
                  f"{statistics.median(th) if th else 0:.0f} | {d['tool']} |")
        capped_total = sum(d["cap"] for _, d in len_rows)
        if capped_total:
            detail = "、".join(f"{m}×{d['cap']}" for m, d in len_rows if d["cap"])
            print(f"> ⚠ 触顶截断 {capped_total} 条（build.py MAXMSG=14000，保留头72%+尾28%，"
                  f"真实长度未知）——这些消息按 14000 计，均值/总量被轻微低估：{detail}")
    else:
        print("\n（模型 × 回复长度：先跑 lenscan 生成 msg_lens.jsonl）")

    # 模型 × 任务量：segment_tasks 近似任务段按模型聚合。换模型会话整段归
    # 主模型（19/76 主会话换过模型，这部分是近似）；复杂度按任务轮次分桶。
    print("\n## 模型 × 任务量（近似任务段：收口词 / 间隔>2h / 话题切换切段；"
          "频道内快速连发的小问询会被并入相邻任务，任务数是**下限**）\n")
    agg_t = defaultdict(
        lambda: {"n": 0, "light": 0, "mid": 0, "heavy": 0, "rework": 0, "sizes": []})
    for r in records:
        m = r.get("model") or "(未恢复/档位级)"
        for size, corr in zip(r.get("task_sizes") or [], r.get("task_corrections") or []):
            a = agg_t[m]
            a["n"] += 1
            a["light" if size <= 2 else "mid" if size <= 8 else "heavy"] += 1
            a["rework"] += 1 if corr else 0
            a["sizes"].append(size)
    print("| 模型 | 任务数 | 轻(1-2轮) | 中(3-8轮) | 重(≥9轮) | 加权任务量 | "
          "含返工占比 | 中位任务轮次 |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|")
    task_rows = sorted(agg_t.items(), key=lambda kv: -kv[1]["n"])
    for m, a in task_rows:
        weighted = a["light"] + a["mid"] * 2 + a["heavy"] * 4
        rw = _pct(a["rework"] / a["n"]) if a["n"] else "—"
        med = f"{statistics.median(a['sizes']):.0f}" if a["sizes"] else "—"
        print(f"| {m} | {a['n']} | {a['light']} | {a['mid']} | {a['heavy']} | "
              f"{weighted} | {rw} | {med} |")
    print("\n## 来源 × 半年时段（会话数；跨时段直接排名会冤枉早期来源，"
          "比较只在同时段内做）\n")
    periods = sorted({r["period"] for r in records} - {"?"})
    header = "| src | " + " | ".join(periods) + " |"
    print(header)
    print("|---" * (len(periods) + 1) + "|")
    for src in sorted(by_src, key=lambda k: -len(by_src[k])):
        counts = [sum(1 for r in by_src[src] if r["period"] == p) for p in periods]
        print(f"| {src} | " + " | ".join(str(c) for c in counts) + " |")
    return 0


def cmd_extremes(args) -> int:
    """人工校验素材。指标先于结论：这些格子要跟用户记忆对得上，表才可信。"""
    records = _filter_metrics(args)
    cells: Dict[Tuple[str, str], List[dict]] = defaultdict(list)
    for r in records:
        cells[(r["src"], r["task"])].append(r)

    print("\n## 校验素材 A：修正率最高的格子（≥2 会话）\n")
    ranked = sorted(((_cell_stats(v), k, v) for k, v in cells.items() if len(v) >= 2),
                    key=lambda kv: (-kv[0]["corr_rate"], -kv[0]["n"]))
    for st, (src, task), v in ranked[:args.top]:
        print(f"### {src} × {task} — 修正率 {_pct(st['corr_rate'])}，"
              f"中位轮次 {st['median_rounds']:.0f}，{st['n']} 会话")
        for r in v:
            extra = f"，频道{r['span_hours']:.0f}h" if r.get("channel") else ""
            print(f"  - [{r['start'][:10]}] {r['title']}（{r['n_user_turns']} 轮，"
                  f"修正 {r['corrections']} 次，终态={r['terminal']}{extra}）")
        print()

    print(f"\n## 校验素材 B：修正事件最多的会话（≥{args.min_turns} 轮，带样本引文）\n")
    heavy = [r for r in records if r["n_user_turns"] >= args.min_turns and r["corrections"] > 0]
    heavy.sort(key=lambda r: (-r["corrections"], -r["correction_rate"]))
    for r in heavy[:args.top]:
        extra = f" / 频道{r['span_hours']:.0f}h" if r.get("channel") else ""
        if r.get("model_switched"):
            extra += " / 会话中换过模型"
        model_tag = f"（{r['model']}，逐条占比 {r['model_share']:.0%}）" if r.get("model") else "（模型未恢复）"
        print(f"### [{r['start'][:10]}] {r['src']} · {r['title']}")
        print(f"    {r['n_user_turns']} 轮 / 修正 {r['corrections']} 次"
              f"（{r['correction_rate'] * 100:.0f}%）/ 终态={r['terminal']}{extra} / "
              f"任务={r['task']}（头名占比 {r['task_share']:.0%}） / 模型={model_tag}")
        for s in r["samples"]:
            print(f"    > {s}")
        print()

    print("\n## 校验素材 C：终态=放弃 的会话\n")
    dropped = [r for r in records if r["terminal"] == "放弃"]
    for r in dropped:
        print(f"  - [{r['start'][:10]}] {r['src']} · {r['title']}（{r['n_user_turns']} 轮）")
    if not dropped:
        print("  （无）")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="AI 对话史行为分析：来源 × 任务类型过程指标（路由先验素材）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_scan = sub.add_parser("scan", help="全量重算指标写 sessions_metrics.jsonl")
    p_scan.set_defaults(func=cmd_scan)

    p_len = sub.add_parser("lenscan",
                           help="从上游 data.json 抽逐条消息长度+模型归属写 msg_lens.jsonl")
    p_len.set_defaults(func=cmd_lenscan)

    def _common_filters(p):
        p.add_argument("--include-subagent", action="store_true",
                       help="包含子任务会话（默认剔除：fan-out 探索任务无真实用户交互）")
        p.add_argument("--src", help="只看某来源")
        p.add_argument("--period", help="只看某半年时段（如 2026H2）")
        p.add_argument("--min-sessions", type=int, default=2,
                       help="表格每格最少会话数（默认 2，防小样本噪声）")

    p_table = sub.add_parser("table", help="src × 任务类型聚合表")
    _common_filters(p_table)
    p_table.set_defaults(func=cmd_table)

    p_ext = sub.add_parser("extremes", help="人工校验素材（极端格子/引文/放弃清单）")
    _common_filters(p_ext)
    p_ext.add_argument("--top", type=int, default=10, help="每类素材条数上限（默认 10）")
    p_ext.add_argument("--min-turns", type=int, default=3,
                       help="素材 B 的最少用户轮次（默认 3）")
    p_ext.set_defaults(func=cmd_extremes)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
