# -*- coding: utf-8 -*-
"""AI 对话史行为分析回归（2026-11-14）。

背景：tools/ai_history_behavior.py 从 data/ai_history 的 chunks/sessions 提取
「来源 × 任务类型」过程指标（路由先验素材）。首跑实机校验抓到两类噪声会
系统性歪曲结论，本套件锁住修复后的行为：

[1] 块重组去重叠：相邻块 150 字符重叠区里的关键词/消息标记只计一次
    （不剥重叠 → 跨块词数两遍，分类与轮次全歪）；
[2] 消息切分与轮次合并：`[时间戳] 问:/答:` 切分；harness 注入块
    （# Environment / <total_tokens> / 裸 TodoWrite 提醒）不计轮次；
    连续 问:（含 harness 夹杂）合并为一轮——<total_tokens> 不滤会把
    ZCode 轮次虚增到数倍（实测 201 → 48）；
[3] 任务分类：标题 ×3 加权、lowercase 大小写无关、无命中 → 其他/未分类；
[4] 修正检测与终态：修正引文来自命中词表的那条消息体（不是轮头部）；
    显式收口从新到旧扫且 换方案 > 放弃 > 收工；全不命中时最后一轮有纠偏
    → 未收口、无纠偏 → 顺延（2026-11-14 校验修正：用户真实行为是"做好了
    直接派下一个"，不说收口词——收工率只是下限）；零轮次 → 未标注；
    频道判定 span>48h（WorkBuddy/Hermes 日常频道 74~97h，ZCode 深夜连跑
    ~24-30h，48h 卡中间）；
[5] 聚合口径：修正率=池化（总修正/总轮次）而非会话均值；子任务会话
    默认剔除；中位数/比率计算；
[6] 命令级：scan→table→extremes 全链在临时目录跑通，产物落盘；模型维度
    （session_models.jsonl）接入后 metrics 带模型字段、table 出模型总览，
    文件缺失时优雅降级（模型空串，不阻塞）；tier 档位不冒充模型名。
[6b] lenscan：从 data.json 抽逐条长度按模型归属——单模型会话精确、换模型
    会话按时间戳最近邻（±120s 外回落主模型）、user 永不归模型、tool 摘要只
    计数；not_rag 会话过滤；scan 摘要与 table 长度段联动。
[6c] segment_tasks：任务段切分三边界（收口词 / 间隔>2h / 话题词不相交），
    纠偏轮默认续挂当前任务、话题切换可越过纠偏词（频道新事故≠返工）。
[7] 真实库环境依赖级验证：data/ai_history 存在时（本机），对首会话跑
    完整提取并断言指标结构。

跑法：.venv\\Scripts\\python.exe -m pytest tests\\suite_ai_history_behavior_20261114.py -q
"""

import io
import json
import sys
import tempfile
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "tools"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import ai_history_behavior as beh  # noqa: E402

FAILURES = []


def check(label, ok, detail=None):
    mark = "PASS" if ok else "FAIL"
    print(f"  {mark} {label}" + (f"  ← {detail}" if not ok and detail is not None else ""))
    if not ok:
        FAILURES.append(label)


def _mk_session(sid, src, title, start, end=None, n_msgs=3, score=31):
    return {"id": sid, "src": src, "title": title, "start": start,
            "end": end or start, "n_msgs": n_msgs, "score": score}


def _mk_chunk(sid, idx, text, src="ZCode", title="t"):
    return {"session": sid, "src": src, "title": title, "start": "2026-01-01 00:00",
            "end": "2026-01-01 01:00", "chunk": idx, "text": text}


def _write_lib(td_path, sessions, chunks):
    with (td_path / "sessions.jsonl").open("w", encoding="utf-8") as f:
        for s in sessions:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")
    with (td_path / "chunks.jsonl").open("w", encoding="utf-8") as f:
        for c in chunks:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")


@contextmanager
def _tmp_lib(td_path):
    """把工具的库路径全局量指到临时目录（命令级测试用）。

    MODELS_PATH / SOURCE_JSON / TURN_MODELS_PATH 默认指向不存在路径——顺带
    覆盖"模型/长度数据缺失优雅降级"分支；要测模型与长度维度时先在临时目录
    落 session_models.jsonl / turn_models.jsonl / data.json。
    """
    with patch.object(beh, "DATA_DIR", td_path), \
         patch.object(beh, "SESSIONS_PATH", td_path / "sessions.jsonl"), \
         patch.object(beh, "CHUNKS_PATH", td_path / "chunks.jsonl"), \
         patch.object(beh, "BEHAVIOR_DIR", td_path / "behavior"), \
         patch.object(beh, "METRICS_PATH", td_path / "behavior" / "sessions_metrics.jsonl"), \
         patch.object(beh, "MODELS_PATH", td_path / "model" / "session_models.jsonl"), \
         patch.object(beh, "TURN_MODELS_PATH", td_path / "model" / "turn_models.jsonl"), \
         patch.object(beh, "SOURCE_JSON", td_path / "data.json"), \
         patch.object(beh, "LENGTHS_PATH", td_path / "behavior" / "msg_lens.jsonl"):
        yield


def test_overlap_join():
    print("[1] 块重组去重叠")
    head = "A" * 240 + "OCR阈值"
    c1 = _mk_chunk("s1", 1, head)
    c2 = _mk_chunk("s1", 2, head[-150:] + "后续无关键词正文" * 5)
    joined = beh.join_chunks([c1, c2])
    check("重叠区关键词只计一次", joined.lower().count("ocr") == 1,
          f"count={joined.lower().count('ocr')}")
    check("重叠剥离后正文保留", "后续无关键词正文" in joined)
    # 消息标记在重叠区也不能复制成两条
    c3 = _mk_chunk("s2", 1, "x" * 240 + "[2026-01-01 10:00] 答: 回答甲")
    c4 = _mk_chunk("s2", 2, ("x" * 240 + "[2026-01-01 10:00] 答: 回答甲")[-150:] + "续段内容")
    msgs = beh.split_messages(beh.join_chunks([c3, c4]))
    check("重叠区消息标记只切一条", len(msgs) == 1 and msgs[0]["role"] == "答",
          f"n={len(msgs)}")


def test_split_and_turns():
    print("[2] 消息切分与轮次合并")
    text = (
        "[2026-11-01 09:00] 问: # Environment\nYou have been invoked in game_auto\n"
        "[2026-11-01 09:00] 问: 白荆 OCR 又认不出了\n"
        "[2026-11-01 09:01] 答: 我来看看\n"
        "[2026-11-01 09:02] 问: <total_tokens>14900000 tokens left</total_tokens>\n"
        "[2026-11-01 09:03] 问: 还是不行，换一天就认不出来了\n"
        "[2026-11-01 09:04] 答: 阈值调了\n"
        "[2026-11-01 09:05] 问: The TodoWrite tool hasn't been used recently. 忽略我\n"
        "[2026-11-01 09:06] 问: 好了，没问题了\n"
    )
    msgs = beh.split_messages(text)
    check("消息条数", len(msgs) == 8, f"n={len(msgs)}")
    turns = beh.merge_user_turns(msgs)
    # 轮 = [harness+白荆] [token噪声+还是不行] [TodoWrite+好了] → 3 轮，
    # harness 块不计入但不断开同一轮的真实消息
    check("连续问合并且harness不计轮次", len(turns) == 3, f"n={len(turns)}")
    turn_text = lambda t: "\n".join(t["bodies"])
    check("轮体无harness残留", all("# Environment" not in turn_text(t) for t in turns) and
          all("total_tokens" not in turn_text(t) for t in turns) and
          all("TodoWrite" not in turn_text(t) for t in turns))
    check("轮序保留真实消息", "白荆 OCR" in turn_text(turns[0]) and "好了" in turn_text(turns[2]))
    check("时段解析", beh.half_year("2026-11-01 09:00") == "2026H2" and
          beh.half_year("2026-03-15 20:00") == "2026H1" and beh.half_year("垃圾") == "?")


def test_classify():
    print("[3] 任务分类")
    cat, share, hits = beh.classify_task("Qoder 界面视图问题", "qml 桥接 WebUI 控件 超时")
    check("标题+正文加权归 UI", cat == "UI与桥接", f"got {cat}")
    cat2, _, _ = beh.classify_task("随便聊聊", "今天天气不错")
    check("无命中归其他", cat2 == "其他/未分类", f"got {cat2}")
    # 标题 ×3：单个标题命中（3 分）压过两个正文命中（2 分）
    cat3, _, _ = beh.classify_task("白荆回廊", "写点文档 changelog")
    check("标题强词压正文", cat3 == "游戏专项与接入", f"got {cat3}")
    cat4, _, _ = beh.classify_task("OCR 排查", "OCR OCR")
    check("大小写无关", cat4 == "OCR与视觉", f"got {cat4}")


def test_correction_and_terminal():
    print("[4] 修正检测与终态")
    msgs = beh.split_messages(
        "[2026-11-01 09:00] 问: 无关噪音开头\n"
        "[2026-11-01 09:01] 答: 收到\n"
        "[2026-11-01 09:02] 问: 还是不行\n"
        "[2026-11-01 09:03] 答: 改了\n"
        "[2026-11-01 09:04] 问: 好了\n"
        "[2026-11-01 09:05] 答: 完成\n"
        "[2026-11-01 09:06] 问: 算了，先放着吧\n"
    )
    turns = beh.merge_user_turns(msgs)
    check("从新到旧：放弃盖收工", beh._terminal_state(turns) == "放弃",
          f"got {beh._terminal_state(turns)}")
    sess = {"id": "t", "src": "T", "title": "t", "start": "2026-11-01 09:00",
            "end": "2026-11-01 09:06", "n_msgs": 7}
    m = beh.session_metrics(sess, [_mk_chunk("t", 1, "".join(
        "[2026-11-01 09:00] 问: 无关噪音开头\n"
        "[2026-11-01 09:01] 答: 收到\n"
        "[2026-11-01 09:02] 问: 还是不行\n"))])
    check("修正引文来自命中消息体", m["samples"] and "还是不行" in m["samples"][0],
          f"samples={m['samples']}")
    # 单轮内优先级：换方案词 > 收工词
    msgs2 = beh.split_messages(
        "[2026-11-01 09:00] 问: 好了但是不如换个方式\n"
        "[2026-11-01 09:01] 答: ok\n")
    check("轮内优先级换方案>收工", beh._terminal_state(beh.merge_user_turns(msgs2)) == "换方案")
    # 全无信号 → 顺延（做好了直接派下一个，2026-11-14 校验修正）
    msgs3 = beh.split_messages(
        "[2026-11-01 09:00] 问: 帮我看看这个\n"
        "[2026-11-01 09:01] 答: 好\n"
        "[2026-11-01 09:02] 问: 明白了，接下来把商店也接上\n")
    check("无信号归顺延", beh._terminal_state(beh.merge_user_turns(msgs3)) == "顺延",
          f"got {beh._terminal_state(beh.merge_user_turns(msgs3))}")
    # 最后一轮带纠偏 → 未收口（会话停在不满上，不能算顺延）
    msgs4 = beh.split_messages(
        "[2026-11-01 09:00] 问: 帮我看看这个\n"
        "[2026-11-01 09:01] 答: 好\n"
        "[2026-11-01 09:02] 问: 还是报错了\n")
    check("最后一轮纠偏归未收口", beh._terminal_state(beh.merge_user_turns(msgs4)) == "未收口",
          f"got {beh._terminal_state(beh.merge_user_turns(msgs4))}")
    # 零轮次（只有答）→ 未标注
    msgs5 = beh.split_messages("[2026-11-01 09:00] 答: 只有回答\n")
    check("零轮次归未标注", beh._terminal_state(beh.merge_user_turns(msgs5)) == "未标注")
    # 频道判定：>48h 判频道（WorkBuddy 实测 74h 级），同日会话不算，坏输入 None
    check("频道判定", beh.span_hours("2026-11-10 20:46", "2026-11-13 23:07") > 48
          and 0 < (beh.span_hours("2026-11-01 09:00", "2026-11-01 21:00") or 0) <= 24
          and beh.span_hours("垃圾", "2026-11-01 09:00") is None)


def test_aggregation():
    print("[5] 聚合口径")
    s1 = _mk_session("a", "ZCode", "OCR识别排查", "2026-11-01 09:00")
    s2 = _mk_session("b", "DeepSeek", "OCR阈值排查", "2026-03-20 10:00")
    s3 = _mk_session("c", "ZCode（子任务）", "In repo explore", "2026-11-02 10:00")
    m1 = {"id": "a", "src": "ZCode", "title": "t", "start": "2026-11-01 09:00",
          "end": "", "n_msgs": 5, "filter_score": 31, "period": "2026H2",
          "n_user_turns": 3, "n_ai_msgs": 2, "corrections": 2, "correction_rate": 0.6667,
          "terminal": "收工", "task": "OCR与视觉", "task_share": 0.8,
          "top_hits": [], "samples": []}
    m2 = {"id": "b", "src": "DeepSeek", "title": "t", "start": "2026-03-20 10:00",
          "end": "", "n_msgs": 3, "filter_score": 31, "period": "2026H1",
          "n_user_turns": 5, "n_ai_msgs": 2, "corrections": 0, "correction_rate": 0.0,
          "terminal": "未标注", "task": "OCR与视觉", "task_share": 0.9,
          "top_hits": [], "samples": []}
    st = beh._cell_stats([m1, m2])
    check("池化修正率=(2+0)/(3+5)", abs(st["corr_rate"] - 0.25) < 1e-9,
          f"got {st['corr_rate']}")
    check("中位轮次=(3+5)/2", st["median_rounds"] == 4, f"got {st['median_rounds']}")
    check("收工率=1/2", st["done"] == 0.5, f"got {st['done']}")
    # 子任务剔除在 [6] 命令级覆盖（table 默认输出不含"子任务"）


def test_commands_end_to_end():
    print("[6] 命令级 scan→table→extremes（含模型维度接入与缺失降级）")
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        sessions = [
            _mk_session("a", "ZCode", "OCR识别排查", "2026-11-01 09:00"),
            _mk_session("b", "DeepSeek", "异环接入", "2026-03-15 20:00"),
            _mk_session("c", "ZCode（子任务）", "In repo explore", "2026-11-02 10:00"),
        ]
        chunks = [
            _mk_chunk("a", 1, "[2026-11-01 09:00] 问: 白荆 OCR 报错了\n"
                              "[2026-11-01 09:01] 答: 看看\n"
                              "[2026-11-01 09:02] 问: 还是不行\n"
                              "[2026-11-01 09:03] 答: 调了阈值\n"
                              "[2026-11-01 09:04] 问: 好了，没问题了\n"),
            _mk_chunk("b", 1, "[2026-03-15 20:00] 问: 异环接入怎么弄\n"
                              "[2026-03-15 20:01] 答: 步骤如下\n"
                              "[2026-03-15 20:02] 问: 算了，先放着\n"),
            _mk_chunk("c", 1, "[2026-11-02 10:00] 问: explore how periodic works\n"
                              "[2026-11-02 10:01] 答: findings..."),
        ]
        _write_lib(td_path, sessions, chunks)
        # 先测"模型文件缺失"分支：scan 不炸、模型字段空串
        with _tmp_lib(td_path), redirect_stdout(io.StringIO()) as out_scan:
            rc = beh.main(["scan"])
        check("scan 退出码（无模型文件）", rc == 0)
        check("scan 产物落盘", (td_path / "behavior" / "sessions_metrics.jsonl").exists())
        m = [json.loads(l) for l in
             (td_path / "behavior" / "sessions_metrics.jsonl").open(encoding="utf-8")]
        a_rec = next(r for r in m if r["id"] == "a")
        check("模型缺失时降级为空串", a_rec["model"] == "" and a_rec["model_level"] == "")
        # 模型文件接入：a=named 逐条，b=tier 档位（不得冒充模型名）
        (td_path / "model").mkdir()
        with (td_path / "model" / "session_models.jsonl").open("w", encoding="utf-8") as f:
            f.write(json.dumps({"id": "a", "src": "ZCode", "in_rag": True,
                                "level": "per_message", "model_kind": "named",
                                "primary_model": "GLM-5.3-Flash",
                                "models": {"GLM-5.3-Flash": 5, "GLM-5.3": 1},
                                "n_models": 2, "switched": False}, ensure_ascii=False) + "\n")
            f.write(json.dumps({"id": "b", "src": "DeepSeek", "in_rag": True,
                                "level": "tier", "model_kind": "tier",
                                "primary_model": "expert",
                                "models": {"expert": 1}, "n_models": 1,
                                "switched": False}, ensure_ascii=False) + "\n")
        with _tmp_lib(td_path), redirect_stdout(io.StringIO()) as out_scan:
            rc = beh.main(["scan"])
        check("scan 退出码（有模型文件）", rc == 0)
        m = [json.loads(l) for l in
             (td_path / "behavior" / "sessions_metrics.jsonl").open(encoding="utf-8")]
        a_rec = next(r for r in m if r["id"] == "a")
        b_rec = next(r for r in m if r["id"] == "b")
        check("named 模型接入", a_rec["model"] == "GLM-5.3-Flash"
              and a_rec["model_level"] == "per_message" and a_rec["model_share"] > 0.5)
        check("tier 档位不冒充模型名", b_rec["model"] == "" and b_rec["model_level"] == "tier")
        with _tmp_lib(td_path), redirect_stdout(io.StringIO()) as out_table:
            rc = beh.main(["table", "--min-sessions", "1"])
        t = out_table.getvalue()
        check("table 含 ZCode×OCR", "ZCode | OCR与视觉" in t, t[:200])
        check("table 默认剔除子任务", "子任务" not in t)
        check("table 出模型总览", "GLM-5.3-Flash" in t and "模型总览" in t, t[-600:])
        check("模型总览含未恢复桶", "(未恢复/档位级)" in t)
        with _tmp_lib(td_path), redirect_stdout(io.StringIO()) as out_ext:
            rc = beh.main(["extremes", "--top", "5", "--min-turns", "1"])
        e = out_ext.getvalue()
        check("extremes 含引文样本", "还是不行" in e, e[:300])
        check("extremes 含放弃清单", "终态=放弃" in e and "异环接入" in e, e[-400:])
        check("extremes 带模型标注", "GLM-5.3-Flash" in e)


def test_lenscan_and_lengths():
    print("[6b] lenscan：逐条长度抽取 + 模型归属")
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        sessions = [
            _mk_session("a", "ZCode", "OCR识别排查", "2026-11-01 09:00"),
            # sw 不在 chunks 里没关系（load_inputs 容忍空 chunks），但要进
            # sessions.jsonl 才算 RAG 会话——lenscan 按 RAG id 过滤上游
            _mk_session("sw", "WorkBuddy", "换模型会话", "2026-11-05 10:00"),
        ]
        chunks = [_mk_chunk("a", 1, "[2026-11-01 09:00] 问: 白荆 OCR 报错了\n"
                                    "[2026-11-01 09:01] 答: 看看\n"
                                    "[2026-11-01 09:02] 问: 还是不行\n"
                                    "[2026-11-01 09:03] 答: 调了阈值\n"
                                    "[2026-11-01 09:04] 问: 好了，没问题了\n")]
        _write_lib(td_path, sessions, chunks)
        # 上游 data.json：会话 a 单模型；会话 sw 换模型（turns 按时间戳归属）
        # k 为 epoch 秒（build.py 口径）；turn_models.ts 为毫秒（恢复器口径）
        data_json = {
            "meta": {}, "sessions": [
                {"id": "a", "src": "ZCode", "n": 6, "msgs": [
                    {"r": "user", "k": 1794498000, "x": "白荆OCR报错了"},
                    {"r": "ai", "k": 1794498010, "x": "我来看看这个失败"},
                    {"r": "think", "k": 1794498010, "x": "思考内容"},
                    {"r": "tool", "k": 1794498011, "x": "工具摘要行"},
                    {"r": "ai", "k": 1794498020, "x": "阈值已修复完成"},
                    {"r": "ai", "k": 1794498030, "x": "A" * 14000},
                ]},
                {"id": "sw", "src": "WorkBuddy", "n": 3, "msgs": [
                    {"r": "ai", "k": 1000, "x": "甲模型答得长一些"},
                    {"r": "ai", "k": 2000, "x": "乙模型答"},
                    {"r": "ai", "k": 9999, "x": "远处消息回落主模型"},
                ]},
                {"id": "not_rag", "src": "Cursor", "n": 1, "msgs": [
                    {"r": "ai", "k": 1, "x": "不该出现在产物里"},
                ]},
            ]}
        (td_path / "data.json").write_text(
            json.dumps(data_json, ensure_ascii=False), encoding="utf-8")
        (td_path / "model").mkdir()
        with (td_path / "model" / "session_models.jsonl").open("w", encoding="utf-8") as f:
            f.write(json.dumps({"id": "a", "src": "ZCode", "in_rag": True,
                                "level": "per_message", "model_kind": "named",
                                "primary_model": "GLM-5.3-Flash",
                                "models": {"GLM-5.3-Flash": 2}, "n_models": 1,
                                "switched": False}, ensure_ascii=False) + "\n")
            f.write(json.dumps({"id": "sw", "src": "WorkBuddy", "in_rag": True,
                                "level": "per_message", "model_kind": "named",
                                "primary_model": "甲",
                                "models": {"甲": 2, "乙": 1}, "n_models": 2,
                                "switched": True}, ensure_ascii=False) + "\n")
        with (td_path / "model" / "turn_models.jsonl").open("w", encoding="utf-8") as f:
            f.write(json.dumps({"id": "sw", "i": 0, "ts": "1000500", "model": "甲"},
                               ensure_ascii=False) + "\n")
            f.write(json.dumps({"id": "sw", "i": 1, "ts": "2000500", "model": "乙"},
                               ensure_ascii=False) + "\n")
        with _tmp_lib(td_path), redirect_stdout(io.StringIO()) as out:
            rc = beh.main(["lenscan"])
        check("lenscan 退出码", rc == 0, out.getvalue()[:200])
        lens = [json.loads(l) for l in
                (td_path / "behavior" / "msg_lens.jsonl").open(encoding="utf-8")]
        check("not_rag 被过滤", all(r["id"] != "not_rag" for r in lens))
        a_rows = [r for r in lens if r["id"] == "a"]
        check("单模型会话精确归属", all(r["m"] == "GLM-5.3-Flash" and r["how"] == "single"
                                    for r in a_rows if r["r"] == "ai"))
        check("user 永不归模型", all(r["m"] == "" for r in a_rows if r["r"] == "user"))
        check("tool 保留行", sum(1 for r in a_rows if r["r"] == "tool") == 1)
        check("ai 长度按字符计", any(r["r"] == "ai" and r["n"] == len("我来看看这个失败")
                                    for r in a_rows))
        check("触顶行带 cap 标记", sum(1 for r in a_rows if r.get("cap") == 1) == 1,
              f"caps={[r.get('cap') for r in a_rows]}")
        sw_rows = {(r["r"], r["n"]): r for r in lens if r["id"] == "sw"}
        check("时间戳命中归甲", sw_rows[("ai", len("甲模型答得长一些"))]["m"] == "甲")
        check("时间戳命中归乙", sw_rows[("ai", len("乙模型答"))]["m"] == "乙")
        check("容差外回落主模型", sw_rows[("ai", len("远处消息回落主模型"))]["m"] == "甲"
              and sw_rows[("ai", len("远处消息回落主模型"))]["how"] == "primary")
        # scan 摘要 + table 长度段
        with _tmp_lib(td_path), redirect_stdout(io.StringIO()):
            beh.main(["scan"])
        m = [json.loads(l) for l in
             (td_path / "behavior" / "sessions_metrics.jsonl").open(encoding="utf-8")]
        a_m = next(r for r in m if r["id"] == "a")
        check("scan 摘要带长度字段", a_m["n_ai_true"] == 3 and a_m["n_tool"] == 1
              and a_m["ai_len_median"] > 0 and a_m["n_capped"] == 1)
        with _tmp_lib(td_path), redirect_stdout(io.StringIO()) as out_t:
            beh.main(["table", "--min-sessions", "1"])
        t = out_t.getvalue()
        check("table 出长度段", "模型 × 回复长度" in t and "正文中位" in t)
        check("长度段含两模型", "GLM-5.3-Flash" in t and "甲" in t)
        check("长度段标注触顶", "触顶截断" in t)
        check("思考不进正文列", "思考条数" in t)


def test_segment_tasks():
    print("[6c] 任务段切分：收口/间隔/话题三边界，纠偏默认续挂")
    T = lambda date, time, *bodies: {"date": date, "time": time, "bodies": list(bodies)}
    turns = [
        T("2026-11-01", "09:00", "白荆为什么失败"),
        T("2026-11-01", "09:05", "还是不行"),
        T("2026-11-01", "09:10", "好了，没问题了"),
        T("2026-11-01", "09:12", "异环接入怎么弄"),
        T("2026-11-01", "09:20", "那阈值呢"),
        T("2026-11-01", "14:00", "帮我看看暖暖的商店"),
        T("2026-11-01", "14:05", "白荆今天又失败了"),
    ]
    tasks = beh.segment_tasks(turns)
    check("4 个任务段", len(tasks) == 4, f"got {tasks}")
    check("开场报障+续挂纠偏都计任务1", tasks[0]["corrections"] == 2
          and tasks[0]["turns"] == 3, f"got {tasks[0]}")
    check("收口后开新任务", tasks[1]["turns"] == 2 and tasks[1]["corrections"] == 0,
          f"got {tasks[1]}")
    check("间隔>2h切分", tasks[2]["turns"] == 1, f"got {tasks[2]}")
    check("话题切换越过纠偏词", tasks[3]["corrections"] == 1 and tasks[3]["turns"] == 1,
          f"got {tasks[3]}")


def test_real_data_smoke():
    print("[7] 真实库环境依赖级验证（data/ai_history 不存在则跳过）")
    if not beh.CHUNKS_PATH.exists():
        print("  SKIP：本机无 data/ai_history")
        return
    sessions, chunks_by_session = beh.load_inputs()
    check("load_inputs 返回非空", len(sessions) > 0)
    m = beh.session_metrics(sessions[0], chunks_by_session.get(sessions[0]["id"], []))
    for key in ("src", "period", "n_user_turns", "corrections", "correction_rate",
                "terminal", "task", "task_share", "samples", "channel", "span_hours",
                "model", "model_level", "model_share", "model_switched"):
        check(f"指标含 {key}", key in m)
    check("n_user_turns 为非负整数", isinstance(m["n_user_turns"], int) and m["n_user_turns"] >= 0)
    check("task_share ∈ [0,1]", 0.0 <= m["task_share"] <= 1.0)


def main():
    test_overlap_join()
    test_split_and_turns()
    test_classify()
    test_correction_and_terminal()
    test_aggregation()
    test_commands_end_to_end()
    test_lenscan_and_lengths()
    test_segment_tasks()
    test_real_data_smoke()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED {len(FAILURES)}: {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
