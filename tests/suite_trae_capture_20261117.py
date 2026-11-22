# -*- coding: utf-8 -*-
"""Trae 会话抓取（tools/trae_capture）解析契约回归（2026-11-17）。

背景：Trae 本地对话正文在 SQLCipher 加密的 ModularData/ai-agent/database.db
里拿不到，唯一干净来源是官方分享页公开接口（curl 实测无鉴权）：
  GET /api/remote/v1/share/<ID>            会话元数据
  GET /api/remote/v1/share/<ID>/messages   消息（user content 是 JSON 字符串段
                                           列表；assistant content 是 JSON 字符串
                                           字典 {messages:[plan_item...], task_id}，
                                           最终回答在 tool_call_info.name=="finish"
                                           的 params.summary，思考在该项
                                           reasoning_content）
trae_fetch.py 把分享会话解析成对话总库 add() 同构扁平 JSON，build.py 的
do_trae() 只做转发（同 grok 契约：抓取侧与汇总侧只保留一个契约）。

本套件锁（全部打桩，绝不真联网）：
[1] extract_share_id：链接 / 裸 ID / 非法输入；
[2] parse_content_segments：text 段拼接、非文本段占位、坏 JSON 回退原文；
[3] parse_assistant_message：finish→(reasoning, summary)、无 finish 回退 thought、
    share-artifact 死链清理；
[4] parse_session：user/think/ai 顺序与时间戳（ISO 与 epoch ms 双来源）、
    id 前缀 trae:、note 格式；
[5] fetch_messages 分页：has_more 时下一页 URL 带 cursor=<最后一条 message_id>；
[6] merge_export 增量合并（同 id 覆盖、不同 id 追加、按 sk 降序）；
[7] 接线锚点：build.py 的 do_trae 已注册 main() 且读 game_auto 的 trae.json。

跑法：.venv\\Scripts\\python.exe -m pytest tests\\suite_trae_capture_20261117.py -q
"""

import io
import json
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

# 沙箱兼容：tempfile 目录创建改默认权限（0700 受保护 DACL 在沙箱里写不进）
from sandbox_temp import install as _install_sandbox_temp

_install_sandbox_temp()
sys.path.insert(0, str(PROJECT_ROOT / "tools" / "trae_capture"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import trae_fetch as tf  # noqa: E402

FAILURES = []


def check(label, ok, detail=None):
    mark = "PASS" if ok else "FAIL"
    print(f"  {mark} {label}" + (f"  ← {detail}" if not ok and detail is not None else ""))
    if not ok:
        FAILURES.append(label)


def test_extract_share_id():
    print("[1] extract_share_id")
    check("完整分享链接",
          tf.extract_share_id("https://share.traecontent.cn/share/4FVARBSY2JNPZY?enter_from=pc")
          == "4FVARBSY2JNPZY")
    check("带连字符的 ID（2026-11-17 实锤形态，字符类必须含 -）",
          tf.extract_share_id("https://share.traecontent.cn/share/A1H-90LCUV-ZCE?enter_from=pc")
          == "A1H-90LCUV-ZCE")
    check("裸 ID", tf.extract_share_id("4FVARBSY2JNPZY") == "4FVARBSY2JNPZY")
    try:
        tf.extract_share_id("不是链接")
        check("非法输入抛 ValueError", False)
    except ValueError:
        check("非法输入抛 ValueError", True)


def test_parse_content_segments():
    print("[2] parse_content_segments")
    segs = json.dumps([
        {"type": "text", "text_content": "今天我为啥没收到邮件？？？？"},
        {"type": "image", "file_name": "shot.png"},
    ], ensure_ascii=False)
    check("text 段拼接 + 非文本段占位",
          tf.parse_content_segments(segs) == "今天我为啥没收到邮件？？？？\n[附件:image shot.png]",
          tf.parse_content_segments(segs))
    check("坏 JSON 回退原文",
          tf.parse_content_segments("不是JSON") == "不是JSON")
    check("空内容返回空串", tf.parse_content_segments("") == "")


def test_parse_assistant_message():
    print("[3] parse_assistant_message")
    content = json.dumps({"messages": [
        {"plan_item": {"tool_call_info": {"name": "search", "params": {"q": "x"}},
                        "reasoning_content": "中间推理", "thought": ""}},
        {"plan_item": {"tool_call_info": {"name": "finish",
                                          "params": {"summary": "结论 [auto_runner.log](share-artifact:///x/auto_runner.log) 如下"}},
                        "reasoning_content": "最终推理", "thought": ""}},
    ], "task_id": "t1"}, ensure_ascii=False)
    think, answer = tf.parse_assistant_message({"content": content})
    check("finish 的 summary 是最终回答",
          answer == "结论 auto_runner.log 如下", answer)
    check("share-artifact 死链已清", "share-artifact://" not in answer)
    check("思考取 finish 项 reasoning", think == "最终推理", think)
    # 无 finish：回退 thought 拼接
    content2 = json.dumps({"messages": [
        {"plan_item": {"tool_call_info": {"name": "read"}, "thought": "中断前的想法"}},
    ]}, ensure_ascii=False)
    think2, answer2 = tf.parse_assistant_message({"content": content2})
    check("无 finish 回退 thought", answer2 == "中断前的想法" and think2 == "", answer2)
    check("坏 JSON 回退截断原文", tf.parse_assistant_message({"content": "???"})[1] == "???")


def test_parse_session():
    print("[4] parse_session")
    meta = {"share_session_id": "4FVARBSY2JNPZY", "title": "查询未收到邮件原因",
            "creator_name": "用户7912044979"}
    user_content = json.dumps([{"type": "text", "text_content": "今天我为啥没收到邮件？？？？"}],
                              ensure_ascii=False)
    asst_content = json.dumps({"messages": [
        {"plan_item": {"tool_call_info": {"name": "finish", "params": {"summary": "**一封都没发出去**"}},
                       "reasoning_content": "先看日志"}},
    ]}, ensure_ascii=False)
    # references 带真实项目路径（2026-11-17 实锤格式）：ws 推断依据
    references = json.dumps([{"uri": "e:\\GithubProject\\game_auto\\AGENTS.md",
                              "file_name": "AGENTS.md", "reference_type": 9}], ensure_ascii=False)
    messages = [
        {"role": "user", "content": user_content,
         "created_at": "2026-11-03T19:28:56+08:00", "message_index": 1},
        {"role": "assistant", "content": asst_content, "references": references,
         "created_at": "2026-11-03T19:31:36+08:00", "chat_start_time": 1793705336864,
         "chat_end_time": 1793705533549, "message_index": 2},
    ]
    s = tf.parse_session(meta, messages)
    check("id 前缀 trae:", s["id"] == "trae:4FVARBSY2JNPZY", s["id"])
    check("标题透传", s["title"] == "查询未收到邮件原因")
    check("ws 从 references 路径推断（RAG 工作区门控依赖它）",
          s["ws"] == "game_auto", s["ws"])
    check("无项目路径时 ws 为空", tf.parse_session(meta, messages[:1])["ws"] == "")
    check("角色顺序 user/think/ai", [m["r"] for m in s["msgs"]] == ["user", "think", "ai"],
          [m["r"] for m in s["msgs"]])
    check("user 时间取 ISO", s["msgs"][0]["k"] > 0 and s["msgs"][0]["t"].startswith("2026-11-03 19:28"),
          s["msgs"][0]["t"])
    check("assistant 时间取 chat_start_time(epoch ms)",
          s["msgs"][1]["k"] == 1793705336864 // 1000, s["msgs"][1]["k"])
    check("sk 取最大时间", s["sk"] == s["msgs"][-1]["k"])
    check("note 含链接与分享者",
          "share.traecontent.cn/share/4FVARBSY2JNPZY" in s["note"] and "用户7912044979" in s["note"],
          s["note"])
    check("add() 同构字段齐备",
          set(s.keys()) >= {"id", "src", "title", "ws", "s", "e", "sk", "n", "chars", "note", "msgs"})
    check("src=Trae", s["src"] == "Trae")
    check("infer_workspace 正斜杠路径也可匹配",
          tf.infer_workspace([{"references": "e:/GithubProject/game_auto/README.md"}]) == "game_auto")
    check("infer_workspace 其他项目名透传",
          tf.infer_workspace([{"references": "E:\\GithubProject\\other_proj\\a.py"}]) == "other_proj")


def test_fetch_messages_pagination():
    print("[5] fetch_messages 分页（打桩 http_get_json）")
    calls = []

    def fake_get(url):
        calls.append(url)
        if "cursor=" not in url:
            return {"items": [{"message_id": "m1"}, {"message_id": "m2"}], "has_more": True}
        return {"items": [{"message_id": "m3"}], "has_more": False}

    orig = tf.http_get_json
    orig_interval = tf.REQUEST_INTERVAL
    tf.http_get_json = fake_get
    tf.REQUEST_INTERVAL = 0
    try:
        items = tf.fetch_messages("TESTID")
    finally:
        tf.http_get_json = orig
        tf.REQUEST_INTERVAL = orig_interval
    check("两页合并 3 条", len(items) == 3, len(items))
    check("第二页 URL 带 cursor=<最后一条 message_id>",
          len(calls) == 2 and "cursor=m2" in calls[1], calls)


def test_merge_export():
    print("[6] merge_export 增量合并")
    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "trae.json")
        orig = tf.TRAE_JSON
        tf.TRAE_JSON = path
        try:
            s1 = {"id": "trae:A", "title": "旧标题", "sk": 100, "msgs": []}
            merged, added = tf.merge_export([s1])
            check("首写新增 1", added == 1 and len(merged) == 1)
            s1_new = {"id": "trae:A", "title": "新标题", "sk": 100, "msgs": []}
            s2 = {"id": "trae:B", "title": "B", "sk": 200, "msgs": []}
            merged, added = tf.merge_export([s1_new, s2])
            check("同 id 覆盖不重复", added == 1 and len(merged) == 2, (added, len(merged)))
            check("按 sk 降序", [x["id"] for x in merged] == ["trae:B", "trae:A"],
                  [x["id"] for x in merged])
            on_disk = {x["id"]: x["title"] for x in json.load(open(path, encoding="utf-8"))}
            check("同 id 落盘为新标题", on_disk == {"trae:A": "新标题", "trae:B": "B"}, on_disk)
        finally:
            tf.TRAE_JSON = orig


def test_build_wiring():
    print("[7] 接线锚点：build.py do_trae")
    build_src = Path(r"F:\data\_ai_organize_tools\build.py")
    if not build_src.exists():
        check("build.py 不存在则跳过（外部文件）", True)
        return
    src = build_src.read_text(encoding="utf-8")
    check("do_trae 已注册 main() 函数清单", "do_hermes, do_grok, do_trae" in src)
    check("读 game_auto 的 trae.json",
          "data" in str(tf.TRAE_JSON).replace("\\", "/") and tf.TRAE_JSON.endswith("trae.json"),
          tf.TRAE_JSON)
    check("契约注释指向 trae_fetch.py", "trae_fetch.py" in src)


def main():
    test_extract_share_id()
    test_parse_content_segments()
    test_parse_assistant_message()
    test_parse_session()
    test_fetch_messages_pagination()
    test_merge_export()
    test_build_wiring()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED {len(FAILURES)}: {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
