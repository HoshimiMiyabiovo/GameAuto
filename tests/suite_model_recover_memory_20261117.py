# -*- coding: utf-8 -*-
"""模型恢复「记忆」回归（2026-11-17）。

背景：用户反馈「每次新导入都不加模型字段？没有记忆吗？」——上游 build.py 重跑后
（新增会话/新抓来源）没人触发 tools/ai_history_model_recover.py，新会话在
session_models.jsonl 里就没有行，展示页自然没有模型。11-17 实测：上游 553 会话，
11-15 的产物里 13 个新会话缺失、17 个旧会话多余。

两条修复：
[链] build.py 在注入前自动调 `scan`（解决"跑不跑"）；
[记忆] scan 默认合并历史产物（解决"别丢"）：本次没扫到的会话沿用上次已还原的
结果，单个恢复器失败/原始日志被清时不会把已有模型降级成 level=none。

本套件锁纯函数部分（合并分支在真实 scan 里要制造恢复器失败才触发得到）：
[1] _pick_rec 优先级与门槛；[2] load_existing 往返；[3] _rec_from_row 折叠口径；
[4] 妙手会话级+逐轮；[5] Kilo 逐条真实 modelID 原值记录（有字段不许臆测映射）
    + Trae 来源固定模型（无字段才用用户拍板值 deepseek-v4.1-flash）。

跑法：.venv\\Scripts\\python.exe -m pytest tests\\suite_model_recover_memory_20261117.py -q
"""

import io
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "tools"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import ai_history_model_recover as rec  # noqa: E402

FAILURES = []


def check(label, ok, detail=None):
    mark = "PASS" if ok else "FAIL"
    print(f"  {mark} {label}" + (f"  ← {detail}" if not ok and detail is not None else ""))
    if not ok:
        FAILURES.append(label)


def _row(sid, level, models_raw, **extra):
    row = {
        "id": sid, "level": level, "source": "ZCode", "n_msgs": 3,
        "models_raw": models_raw, "models": {k: v for k, v in models_raw.items()},
        "n_records": 1, "n_modeled": 1, "usage_requests": 1, "fallback_model": "",
    }
    row.update(extra)
    return row


def test_pick_rec():
    print("[1] _pick_rec 优先级与门槛")
    merged = {"a": {"level": "per_message", "source": "ZCode", "models": {"gpt-5.5": 2},
                    "turns": [(1, "gpt-5.5")], "n_records": 1, "n_modeled": 1,
                    "usage_requests": 1, "fallback_model": ""}}
    existing = {"b": _row("b", "per_message", {"deepseek-v4.1-flash": 4}),
                "c": _row("c", "none", {}),
                "d": _row("d", "tier", {"deepseek:default": 9})}
    turns = {"b": [(10, "deepseek-v4.1-flash"), (20, "deepseek-v4.1-flash")]}

    r, reused = rec._pick_rec(merged, existing, turns, "a")
    check("本次扫到 → 用本次，不算沿用", r is merged["a"] and reused is False)

    r, reused = rec._pick_rec(merged, existing, turns, "b")
    check("本次没扫到 → 沿用记忆并标记 reused", reused is True and r is not None)
    check("沿用记录保留 level/source", r["level"] == "per_message" and r["source"] == "ZCode")
    check("沿用记录 models 取 models_raw", r["models"] == {"deepseek-v4.1-flash": 4})
    check("沿用记录 turns 来自逐条产物", r["turns"] == turns["b"])

    r, reused = rec._pick_rec(merged, existing, turns, "c")
    check("旧记录 level=none → 不沿用（不把无模型抄一遍）",
          r is None and reused is False)

    r, reused = rec._pick_rec(merged, existing, turns, "d")
    check("旧记录 tier 级也沿用（默认/档位标记也是有效线索）",
          reused is True and r["level"] == "tier")

    r, reused = rec._pick_rec(merged, existing, turns, "b", no_merge=True)
    check("--no-merge 时完全不沿用", r is None and reused is False)

    r, reused = rec._pick_rec(merged, {}, {}, "zzz")
    check("两边都没有 → None/False", r is None and reused is False)


def test_load_existing_roundtrip():
    print("[2] load_existing 往返")
    with tempfile.TemporaryDirectory() as td:
        sp = Path(td) / "session_models.jsonl"
        tp = Path(td) / "turn_models.jsonl"
        rows = [_row("x", "per_message", {"gpt-5.5": 2}),
                _row("y", "session", {"claude-sonnet-4.6": 1})]
        sp.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                      encoding="utf-8")
        tp.write_text("\n".join([
            json.dumps({"id": "x", "ts": 100, "model_raw": "gpt-5.5", "model": "gpt-5.5"}),
            json.dumps({"id": "x", "ts": 200, "model_raw": "gpt-5.5", "model": "gpt-5.5"}),
            json.dumps({"id": "y", "ts": 300, "model_raw": "claude-sonnet-4.6",
                        "model": "claude-sonnet-4.6"}),
        ]) + "\n", encoding="utf-8")
        got_rows, got_turns = rec.load_existing(session_path=sp, turn_path=tp)
        check("会话行按 id 读回", set(got_rows) == {"x", "y"}, sorted(got_rows))
        check("逐条按 id 分组且保序", [t[0] for t in got_turns["x"]] == [100, 200])
        check("逐条取 model_raw 优先", got_turns["x"][0][1] == "gpt-5.5")

        # 缺文件不应炸（首次跑就是两边都缺）
        got_rows, got_turns = rec.load_existing(session_path=Path(td) / "nope.jsonl",
                                                turn_path=Path(td) / "nope2.jsonl")
        check("文件缺失 → 空字典不抛异常", got_rows == {} and dict(got_turns) == {})


def test_rec_from_row():
    print("[3] _rec_from_row 折叠口径")
    row = _row("z", "per_message", {"copilot/claude-sonnet-4.6": 2}, n_records=7,
               n_modeled=6, usage_requests=9, fallback_model="gpt-5.4-mini")
    r = rec._rec_from_row(row, [(1, "copilot/claude-sonnet-4.6")])
    check("level/source 原样带过", r["level"] == "per_message" and r["source"] == "ZCode")
    check("models 用 models_raw（未经 canonical，交给输出阶段统一）",
          r["models"] == {"copilot/claude-sonnet-4.6": 2})
    check("计数与兜底字段带过",
          (r["n_records"], r["n_modeled"], r["usage_requests"], r["fallback_model"])
          == (7, 6, 9, "gpt-5.4-mini"))
    check("turns 浅拷贝（不外泄内部引用）", r["turns"] == [(1, "copilot/claude-sonnet-4.6")]
          and r["turns"] is not row.get("turns"))
    r2 = rec._rec_from_row({"id": "w"}, [])
    check("空行 → level=none、models 空、turns 空",
          r2["level"] == "none" and r2["models"] == {} and r2["turns"] == [])


def test_recover_catpaw_session_model():
    print("[4] 妙手：sessions.persistedModelId + CLI 日志逐轮模型")
    c1 = "11111111-1111-1111-1111-111111111111"
    c2 = "22222222-2222-2222-2222-222222222222"
    old_root = rec.CATPAW_ROOT
    old_log_root = rec.CATPAW_LOG_ROOT
    try:
        with tempfile.TemporaryDirectory() as td:
            rec.CATPAW_ROOT = Path(td)
            rec.CATPAW_LOG_ROOT = Path(td) / ".meituan-catpaw"
            db = Path(td) / "catpaw-memory-test.db"
            c = sqlite3.connect(db)
            c.execute("create table sessions (id text primary key, extra text)")
            c.execute("insert into sessions values (?,?)",
                      (c1, json.dumps({"persistedModelId": 107, "timestamp": 1794645124965})))
            c.execute("insert into sessions values (?,?)",
                      (c2, json.dumps({"persistedModelId": 10002544, "timestamp": 1794645124965})))
            c.commit()
            c.close()
            log_dir = rec.CATPAW_LOG_ROOT / "3704180016" / "logs"
            log_dir.mkdir(parents=True)
            (log_dir / "app.1.log").write_text(
                "2026-11-17 10:00:00.001 DESKTOP x #XMDT#{conversationId=%s turn=1 model=107}#XMDT# [LLM] start\n"
                "2026-11-17 10:00:05.001 DESKTOP x #XMDT#{conversationId=%s turn=2 model=107}#XMDT# [LLM] start\n"
                "2026-11-17 10:00:06.001 DESKTOP x #XMDT#{conversationId=%s turn=1 model=10002544}#XMDT# [LLM] start\n"
                % (c1, c1, c2),
                encoding="utf-8",
            )
            got = rec.recover_catpaw()
    finally:
        rec.CATPAW_ROOT = old_root
        rec.CATPAW_LOG_ROOT = old_log_root
    check("两个会话都恢复", set(got) == {f"catpaw:{c1}", f"catpaw:{c2}"}, got.keys())
    if got:
        r1 = got.get(f"catpaw:{c1}", {})
        r2 = got.get(f"catpaw:{c2}", {})
        check("107 → LongCat-2.5", r1.get("models") == {"LongCat-2.5": 2}, r1.get("models"))
        check("10002544 → Auto（用户确认的自动选择）",
              r2.get("models") == {"Auto": 1}, r2.get("models"))
        check("日志恢复为逐轮（不是会话级 1 条）",
              r1.get("level") == "per_message" and r1.get("n_modeled") == 2, r1.get("level"))
        check("原始会话级 model_id 仍保留", r1.get("model_id") == "107", r1.get("model_id"))


def test_recover_kilo_and_trae():
    print("[5] Kilo 逐条 modelID 归一 + Trae 来源固定模型（2026-11-17 用户拍板）")
    old_kilo_raw = rec.KILO_RAW
    old_trae_json = rec.TRAE_JSON
    try:
        with tempfile.TemporaryDirectory() as td:
            kilo_raw = Path(td) / "raw"
            (kilo_raw / "ses_abc").mkdir(parents=True)
            (kilo_raw / "ses_abc" / "messages.json").write_text(json.dumps([
                {"info": {"role": "user", "time": {"created": 1000}}},
                {"info": {"role": "assistant", "modelID": "particle-ai/particle-1:free",
                          "time": {"created": 2000}}},
                {"info": {"role": "assistant", "modelID": "particle-ai/particle-1:free",
                          "time": {"created": 3000}}},
            ]), encoding="utf-8")
            rec.KILO_RAW = kilo_raw
            kilo = rec.recover_kilo()
            check("kilo:ses_abc 命中（raw 目录名 + kilo: 前缀）",
                  "kilo:ses_abc" in kilo, kilo.keys())
            r = kilo.get("kilo:ses_abc") or {}
            check("逐条级 + 2 条建模", r.get("level") == "per_message" and r.get("n_modeled") == 2,
                  r.get("level"))
            check("原始名保留 particle-ai/particle-1:free（models_raw 可追溯）",
                  r.get("models") == {"particle-ai/particle-1:free": 2}, r.get("models"))
            check("Kilo 有真实字段 → 按原值记，不臆测映射（2026-11-17 用户纠正）",
                  rec.canonical_model("particle-ai/particle-1:free")
                  == "particle-ai/particle-1:free")
            check("turn ts 用毫秒（turn_models 口径）",
                  [t[0] for t in r.get("turns", [])] == ["2000", "3000"], r.get("turns"))

            trae = Path(td) / "trae.json"
            trae.write_text(json.dumps([
                {"id": "trae:AAA", "title": "t", "msgs": [{"k": 1793710069}, {"k": 1793711000}]},
                {"id": "zzz:not-trae", "msgs": [{"k": 1}]},
            ]), encoding="utf-8")
            rec.TRAE_JSON = trae
            got = rec.recover_trae()
            check("只认 trae: 前缀 id", set(got) == {"trae:AAA"}, got.keys())
            r = got.get("trae:AAA") or {}
            check("会话级固定模型 deepseek-v4.1-flash",
                  r.get("level") == "session"
                  and r.get("models") == {"deepseek-v4.1-flash": 2}
                  and r.get("primary_model") == "deepseek-v4.1-flash", r.get("models"))
            check("单条 turn ts=末条消息秒→毫秒对齐 turn_models 口径",
                  [t[0] for t in r.get("turns", [])] == ["1793711000000"], r.get("turns"))
    finally:
        rec.KILO_RAW = old_kilo_raw
        rec.TRAE_JSON = old_trae_json


def test_recover_opencode_raw_and_note_model():
    print("[6] TeleAgent/码道逐条 modelID（opencode 变体 raw）+ note_model 双格式"
          "（2026-11-19 补：新源接入漏恢复器，最终页模型空）")
    old_tele = rec.TELEAGENT_RAW
    old_code = rec.CODEARTS_RAW
    try:
        with tempfile.TemporaryDirectory() as td:
            def stub_raw(root: Path, ses: str, mid: str, no_time: bool = False):
                d = root / ses
                d.mkdir(parents=True)
                msg = {"role": "assistant", "modelID": mid}
                ts = {"created": 2000} if not no_time else {}
                (d / "messages.json").write_text(json.dumps({
                    "session": {"id": ses, "title": "t"},
                    "messages": [
                        {"msg": {"role": "user"}, "ts_ms": 1000, "parts": []},
                        {"msg": dict(msg, time=ts), "ts_ms": 2000, "parts": []},
                        {"msg": dict(msg, time=ts), "ts_ms": 3000, "parts": []},
                    ],
                }), encoding="utf-8")

            tele = Path(td) / "tele_raw"
            code = Path(td) / "code_raw"
            stub_raw(tele, "ses_ta", "chat-flash")
            stub_raw(code, "ses_ca", "deepseek-v4.1-flash-1017", no_time=True)
            rec.TELEAGENT_RAW = tele
            rec.CODEARTS_RAW = code
            got = rec.recover_teleagent()
            check("teleagent:ses_ta 命中（raw 目录名 + teleagent: 前缀）",
                  "teleagent:ses_ta" in got, got.keys())
            r = got.get("teleagent:ses_ta") or {}
            check("逐条级，user 不算建模（2 assistant）",
                  r.get("level") == "per_message" and r.get("n_modeled") == 2
                  and r.get("n_records") == 3, (r.get("level"), r.get("n_modeled")))
            check("TeleAgent chat-flash 是内部路由名 → 恢复器存原值，"
                  "canonical 走别名（2026-11-19 用户拍板 = deepseek-v4.1-flash-1017）",
                  r.get("models") == {"chat-flash": 2}
                  and rec.canonical_model("chat-flash") == "deepseek-v4.1-flash-1017",
                  (r.get("models"), rec.canonical_model("chat-flash")))
            check("turn ts 用 time.created 毫秒",
                  [t[0] for t in r.get("turns", [])] == ["2000", "2000"], r.get("turns"))

            got = rec.recover_codearts()
            r = got.get("codearts:ses_ca") or {}
            check("码道 modelID 按原值 + time 缺失退 ts_ms",
                  r.get("models") == {"deepseek-v4.1-flash-1017": 2}
                  and [t[0] for t in r.get("turns", [])] == ["2000", "3000"], r.get("turns"))
    finally:
        rec.TELEAGENT_RAW = old_tele
        rec.CODEARTS_RAW = old_code

    check("note_model 认「模型：xxx」冒号格式",
          rec.note_model("xx · 模型：Auto / 备") == "Auto", rec.note_model("xx · 模型：Auto"))
    check("note_model 兼容「· 模型 xxx」无冒号（Kilo/TeleAgent/码道 fetch 格式）",
          rec.note_model("381 条入库记录 · 模型 chat-flash") == "chat-flash",
          rec.note_model("381 条入库记录 · 模型 chat-flash"))
    check("note_model 不把散文词当模型（「模型字段/模型名」）",
          rec.note_model("只有模型字段说明") == "" and rec.note_model("模型名字段") == "",
          "prose words leaked")


def test_recover_qoder_auto_resolve():
    print("[7] Qoder 1.1.86 起 message.model=auto → 用 runtime-config 解析回真实代号"
          "（2026-11-20 补：新版把选择器档位写进逐条字段，最终页错显 Auto）")
    old_root = rec.QODER_ROOT
    old_db = rec.QODER_DB
    try:
        with tempfile.TemporaryDirectory() as td:
            proj = Path(td) / "projects" / "E--demo"
            proj.mkdir(parents=True)
            sid_new = "aaaaaaaa-0000-0000-0000-000000000001"
            sid_old = "bbbbbbbb-0000-0000-0000-000000000002"
            sid_bare = "cccccccc-0000-0000-0000-000000000003"

            def jl(rows):
                return "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n"

            # 新版：runtime-config=dfmodel，逐条全 auto；中途换 qfmodel 后 auto 跟随
            (proj / f"{sid_new}.jsonl").write_text(jl([
                {"type": "runtime-config", "model": "dfmodel", "timestamp": 1000},
                {"type": "assistant", "timestamp": "1100",
                 "message": {"role": "assistant", "model": "auto"}},
                {"type": "assistant", "timestamp": "1200",
                 "message": {"role": "assistant", "model": "auto"}},
                {"type": "runtime-config", "model": "qfmodel", "timestamp": 1300},
                {"type": "assistant", "timestamp": "1400",
                 "message": {"role": "assistant", "model": "auto"}},
            ]), encoding="utf-8")
            # 旧版：逐条真实代号，不受影响
            (proj / f"{sid_old}.jsonl").write_text(jl([
                {"type": "runtime-config", "model": "lmodel", "timestamp": 2000},
                {"type": "assistant", "timestamp": "2100",
                 "message": {"role": "assistant", "model": "lmodel"}},
            ]), encoding="utf-8")
            # 无 runtime-config：auto 原样保留（不臆测）
            (proj / f"{sid_bare}.jsonl").write_text(jl([
                {"type": "assistant", "timestamp": "3100",
                 "message": {"role": "assistant", "model": "auto"}},
            ]), encoding="utf-8")
            rec.QODER_ROOT = Path(td)
            rec.QODER_DB = Path(td) / "nope.sqlite"   # 不建库，隔离会话级兜底
            got = rec.recover_qoder()
    finally:
        rec.QODER_ROOT = old_root
        rec.QODER_DB = old_db

    r = got.get("qoder:" + sid_new[:8]) or {}
    check("auto 按当时生效 runtime-config 解析（dfmodel×2 + qfmodel×1）",
          r.get("models") == {"dfmodel": 2, "qfmodel": 1}, r.get("models"))
    check("解析计数 auto_resolved=3、逐条级",
          r.get("auto_resolved") == 3 and r.get("level") == "per_message",
          (r.get("auto_resolved"), r.get("level")))
    check("canonical 后是拍板映射而非 Auto",
          rec.canonical_model(r.get("primary_model", "")) == "deepseek-v4.1-flash-1017",
          rec.canonical_model(r.get("primary_model", "")))
    r2 = got.get("qoder:" + sid_old[:8]) or {}
    check("旧版真实代号原样保留", r2.get("models") == {"lmodel": 1}, r2.get("models"))
    r3 = got.get("qoder:" + sid_bare[:8]) or {}
    check("无 runtime-config → auto 原样保留不臆测",
          r3.get("models") == {"auto": 1} and r3.get("auto_resolved") == 0,
          (r3.get("models"), r3.get("auto_resolved")))


def main():
    test_pick_rec()
    test_load_existing_roundtrip()
    test_rec_from_row()
    test_recover_catpaw_session_model()
    test_recover_kilo_and_trae()
    test_recover_opencode_raw_and_note_model()
    test_recover_qoder_auto_resolve()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED {len(FAILURES)}: {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
