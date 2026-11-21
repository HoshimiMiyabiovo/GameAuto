# -*- coding: utf-8 -*-
"""DSH NEXT 会话抓取回归（2026-11-20）。

背景：DSH NEXT 把会话正文落在 `~/.dsh/sessions/**/session.v4.jsonl.zstd`，
格式是「每行 JSON 单独压成一个 zstd 帧、多帧顺序拼接」——单帧 API 只能读出第一行，
必须跨帧流式读（tools/dsh_capture/dsh_fetch.py 用 backports.zstd.open）。

本套件锁纯逻辑，全部用**编造的中性文案**在临时目录里现造会话文件，不读真实
~/.dsh、不镜像真实会话（隐私口径见 01-workflow）：
[1] 跨帧读取：逐行压缩拼接的 3 帧全部读出；
[2] 映射：user/think/ai/tool/output 五类落位，系统/审批/步骤等噪声不进库；
[3] team 模式 inbox 去重：与 user/message 同 id 的 inbox 不重复，
    只有 inbox 的会话靠 inbox 兜底；
[4] 标题/工作区/模型：session/title 优先、首条用户文案兜底；
    GithubProject 路径取仓库名、其余取目录末段；模型按原值收集；
[5] 工具摘要截断到 600；
[6] 端到端：--sessions-root/--out-root 造会话 → dsh.json/raw/md，重跑按 id 合并。

跑法：.venv\\Scripts\\python.exe -m pytest tests\\suite_dsh_capture_20261120.py -q
"""

import importlib.util
import io
import json
import os
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# 只在直接跑脚本时改 stdout 编码；pytest 下换掉 sys.stdout 会顶掉它的 capture
# 包装，整套在 teardown 时炸「I/O operation on closed file」。
if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

try:
    import backports.zstd as zstd
except ImportError:
    zstd = None

_spec = importlib.util.spec_from_file_location(
    "dsh_fetch", PROJECT_ROOT / "tools" / "dsh_capture" / "dsh_fetch.py")
dsh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dsh)

FAILURES = []
DATA_CACHE = PROJECT_ROOT / "data" / "cache"


def check(label, ok, detail=None):
    mark = "PASS" if ok else "FAIL"
    print(f"  {mark} {label}" + (f"  ← {detail}" if not ok and detail is not None else ""))
    if not ok:
        FAILURES.append(label)


def _write_session(root: Path, ws_slug: str, sid: str, records: list) -> Path:
    """把记录序列写成真实的逐行 zstd 帧拼接文件（模拟 DSH 落盘格式）。"""
    d = root / ws_slug / sid
    d.mkdir(parents=True, exist_ok=True)
    # 真实格式：每帧是一行 JSON + 换行（缺换行会让相邻两帧粘成一行解析失败）
    buf = b"".join(zstd.compress((json.dumps(r, ensure_ascii=False) + "\n").encode("utf-8"))
                   for r in records)
    p = d / "session.v4.jsonl.zstd"
    p.write_bytes(buf)
    return p


def _rec(rtype, data=None, ts=1700000000000, **extra):
    r = {"type": rtype, "time": ts}
    if data is not None:
        r["data"] = data
    r.update(extra)
    return r


def _session(sid, cwd, ts=1700000000000):
    """会话头：id/cwd/createdAt 在**顶层**（实测真实格式如此，不在 data 里）。"""
    return {"type": "session", "version": 4, "id": sid, "createdAt": ts,
            "cwd": cwd, "isSeeded": False}


def test_multiframe_read():
    print("[1] 跨帧读取")
    with tempfile.TemporaryDirectory(dir=DATA_CACHE) as td:
        recs = [_session("s1", r"E:\GithubProject\demo"),
                _rec("user/message", {"content": [{"type": "text", "text": "示例问题"}]}),
                _rec("assistant/message", {"message": {"content": [
                    {"type": "text", "text": "示例回答"}]}})]
        p = _write_session(Path(td), "--demo--", "s1", recs)
        got = dsh.load_records(str(p))
        check("逐行压缩拼接的 3 帧全部读出", [r["type"] for r in got] ==
              ["session", "user/message", "assistant/message"], [r["type"] for r in got])
        # 单帧（整段一次压缩）也要能读——ZstdFile 两种形态通吃
        single = Path(td) / "--demo--" / "s2"
        single.mkdir(parents=True, exist_ok=True)
        (single / "session.v4.jsonl.zstd").write_bytes(
            zstd.compress(b"\n".join(json.dumps(r, ensure_ascii=False).encode("utf-8")
                                    for r in recs)))
        got2 = dsh.load_records(str(single / "session.v4.jsonl.zstd"))
        check("整段单帧压缩也能读", len(got2) == 3, len(got2))


def test_mapping():
    print("[2] 记录映射")
    recs = [
        _session("s1", r"E:\GithubProject\demo"),
        _rec("model/selection", {"model": "demo-model/a"}),
        _rec("user/message", {"id": "u1", "content": [
            {"type": "text", "text": "示例问题"}]}),
        _rec("assistant/message", {"message": {"source": {"model": "demo-model/a"},
            "content": [{"type": "reasoning", "text": "示例思考"},
                        {"type": "text", "text": "示例回答"},
                        {"type": "tool-call", "name": "pwsh",
                         "arguments": "{\"command\":\"Get-ChildItem\",\"description\":\"列目录\"}"}]}}),
        _rec("tool/result", {"message": {"content": [{"type": "text", "text": "示例工具输出"}]}}),
        _rec("system/message", {"message": {"content": [{"type": "text", "text": "系统噪声"}]}}),
        _rec("approval/asked", {"id": "a1"}),
        _rec("step/start", {"turn": 1, "step": 1}),
    ]
    s = dsh.convert_session("s1", "--demo--", recs)
    roles = [m["r"] for m in s["msgs"]]
    check("五类角色落位且顺序正确",
          roles == ["user", "think", "ai", "tool", "output"], roles)
    check("系统/审批/步骤噪声不进库", len(s["msgs"]) == 5, s["n"])
    check("工具摘要带名称与参数", s["msgs"][3]["x"].startswith("🔧 pwsh"), s["msgs"][3]["x"])
    check("标题缺失时用首条用户文案", s["title"] == "示例问题", s["title"])
    check("ws 取仓库名", s["ws"] == "demo", s["ws"])
    check("note 含模型名", "demo-model/a" in s["note"], s["note"])


def test_inbox_dedupe():
    print("[3] team inbox 去重")
    recs = [
        _session("s1", r"E:\GithubProject\demo"),
        _rec("agent/inbox/spliced", {"inserted": [
            {"role": "user", "id": "u1",
             "content": [{"type": "text", "text": "示例问题"}]}]}),
        _rec("user/message", {"id": "u1", "content": [
            {"type": "text", "text": "示例问题"}]}),
    ]
    s = dsh.convert_session("s1", "--demo--", recs)
    users = [m for m in s["msgs"] if m["r"] == "user"]
    check("inbox 与 user/message 同 id 只入库一次", len(users) == 1, len(users))

    recs2 = [
        _session("s2", r"E:\GithubProject\demo"),
        _rec("agent/inbox/spliced", {"inserted": [
            {"role": "user", "id": "u9",
             "content": [{"type": "text", "text": "仅 inbox 的问题"}]}]}),
    ]
    s2 = dsh.convert_session("s2", "--demo--", recs2)
    check("只有 inbox 的会话靠 inbox 兜底", s2["n"] == 1 and s2["msgs"][0]["x"] == "仅 inbox 的问题",
          s2["msgs"])


def test_title_and_ws():
    print("[4] 标题 / 工作区")
    recs = [_session("s1", r"C:\Users\demo\Documents\default-workspace"),
            _rec("session/title", {"title": "示例标题"}),
            _rec("user/message", {"content": [{"type": "text", "text": "示例问题"}]})]
    s = dsh.convert_session("s1", "--demo--", recs)
    check("session/title 优先", s["title"] == "示例标题", s["title"])
    check("非 GithubProject 路径取目录末段", s["ws"] == "default-workspace", s["ws"])


def test_tool_truncate():
    print("[5] 工具摘要截断")
    long_cmd = "x" * 900
    recs = [_session("s1", r"E:\GithubProject\demo"),
            _rec("assistant/message", {"message": {"content": [
                {"type": "tool-call", "name": "pwsh",
                 "arguments": json.dumps({"command": long_cmd})}]}})]
    s = dsh.convert_session("s1", "--demo--", recs)
    check("摘要截到 600 级别", len(s["msgs"][0]["x"]) <= dsh.TOOL_RESULT_LIMIT + 50,
          len(s["msgs"][0]["x"]))


def test_end_to_end():
    print("[6] 端到端导出 + 重跑合并")
    with tempfile.TemporaryDirectory(dir=DATA_CACHE) as td:
        td = Path(td)
        sessions_root = td / "sessions"
        out_root = td / "out"
        recs = [_session("s1", r"E:\GithubProject\demo"),
                _rec("session/title", {"title": "示例标题"}),
                _rec("user/message", {"content": [{"type": "text", "text": "示例问题"}]}),
                _rec("assistant/message", {"message": {"content": [
                    {"type": "text", "text": "示例回答"}]}})]
        _write_session(sessions_root, "--demo--", "s1", recs)
        rc = dsh.main(["--sessions-root", str(sessions_root), "--out-root", str(out_root)])
        check("main 返回 0", rc == 0, rc)
        data = json.loads((out_root / "export" / "dsh.json").read_text(encoding="utf-8"))
        check("dsh.json 落盘且 1 个会话", len(data) == 1, len(data))
        check("id 带 dsh: 前缀", data[0]["id"] == "dsh:s1", data[0]["id"])
        check("raw 原始记录落盘",
              (out_root / "raw" / "s1" / "messages.json").is_file())
        check("逐会话 md 落盘", (out_root / "export" / "s1.md").is_file())
        dsh.main(["--sessions-root", str(sessions_root), "--out-root", str(out_root)])
        data2 = json.loads((out_root / "export" / "dsh.json").read_text(encoding="utf-8"))
        check("重跑按 id 合并不重复", len(data2) == 1, len(data2))


def main():
    if zstd is None:
        print("SKIP：缺 backports.zstd")
        return 0
    test_multiframe_read()
    test_mapping()
    test_inbox_dedupe()
    test_title_and_ws()
    test_tool_truncate()
    test_end_to_end()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED {len(FAILURES)}: {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
