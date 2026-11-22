# -*- coding: utf-8 -*-
"""AI 对话史：Code2Api 网关执行器副本过滤回归（2026-11-21）。

背景：DSH 经 Code2Api 调 opencode 时多数请求不带会话 ID，网关按 oneshot
处理；正常完成会 DELETE 上游 opencode 会话，但中断/崩溃时会残留（实测 12 个：
9 个 `code2api` 标题 + 3 个仅目录命中，其中 2 个约 35 万字符、命中 game_auto
关键词），与 DSH 本地会话重复。
本套件锁三件事：

[1] RAG 判定：标题 `code2api ` 前缀只对 OpenCode 源生效（防误伤用户自起名的
    DSH 会话）；网关 workspace 目录对任意源生效；Code2Api 仓库根不算 workspace。
[2] RAG cmd_build 端到端：命中即跳过并计数，正常会话照常入库。
[3] tools/build.py do_opencode：真读临时 opencode.db，网关副本不入 SESSIONS。

build.py 在本机 F:（不在仓库），缺失时 SKIP。
跑法：.venv\\Scripts\\python.exe tests\\suite_ai_history_gateway_filter_20261121.py
"""

import argparse
import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import time
import types
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

# 沙箱兼容：tempfile 目录创建改默认权限（0700 受保护 DACL 在沙箱里写不进）
from sandbox_temp import install as _install_sandbox_temp

_install_sandbox_temp()
sys.path.insert(0, str(PROJECT_ROOT / "tools"))

import ai_history_rag as rag  # noqa: E402

GATEWAY_WS = r"E:\GithubProject\Code2Api\workspace"
FAILURES = []


def check(label, ok, detail=None):
    mark = "PASS" if ok else "FAIL"
    print(f"  {mark} {label}" + (f"  ← {detail}" if not ok and detail is not None else ""))
    if not ok:
        FAILURES.append(label)


def _sess(sid, src, title, ws, text):
    return {
        "id": sid,
        "src": src,
        "title": title,
        "ws": ws,
        "msgs": [
            {"r": "user", "t": "2026-11-21 20:00", "x": text},
            {"r": "ai", "t": "2026-11-21 20:01", "x": "答：" + text},
        ],
    }


def test_rag_gateway_predicate():
    print("[1] RAG 网关执行器副本判定")
    check("OpenCode + code2api 标题", rag.is_gateway_exec_session(
        {"src": "OpenCode", "title": "code2api oneshot-x", "ws": ""}))
    check("标题大小写不敏感", rag.is_gateway_exec_session(
        {"src": "OpenCode", "title": "Code2API ws-live-1", "ws": ""}))
    check("网关 workspace 精确目录", rag.is_gateway_exec_session(
        {"src": "DSH", "title": "正常", "ws": GATEWAY_WS}))
    check("网关 workspace 子目录", rag.is_gateway_exec_session(
        {"src": "DSH", "title": "正常", "ws": GATEWAY_WS + r"\sub"}))
    check("普通 OpenCode 会话不误伤", not rag.is_gateway_exec_session(
        {"src": "OpenCode", "title": "正常会话", "ws": r"E:\GithubProject\game_auto"}))
    check("Code2Api 仓库根不算 workspace", not rag.is_gateway_exec_session(
        {"src": "OpenCode", "title": "正常会话", "ws": r"E:\GithubProject\Code2Api"}))
    check("同名前缀的其它目录不算", not rag.is_gateway_exec_session(
        {"src": "OpenCode", "title": "正常会话", "ws": GATEWAY_WS + r"-other"}))
    check("DSH 自起 code2api 标题不误伤", not rag.is_gateway_exec_session(
        {"src": "DSH", "title": "code2api 网关排查", "ws": r"E:\GithubProject\game_auto"}))


def test_cmd_build_filters_gateway_copies(work_dir: Path):
    print("[2] RAG cmd_build 端到端")
    work_dir.mkdir(parents=True, exist_ok=True)
    data = {"sessions": [
        _sess("gw-title", "OpenCode", "code2api oneshot-a", "", "game_auto ok-ww 签到"),
        _sess("gw-ws", "OpenCode", "正常标题", GATEWAY_WS, "game_auto ok-ww 签到"),
        _sess("keep-oc", "OpenCode", "正常会话", r"E:\GithubProject\game_auto", "game_auto ok-ww 签到"),
        _sess("keep-dsh", "DSH", "正常会话", r"E:\GithubProject\game_auto", "game_auto ok-ww 签到"),
        _sess("noise", "OpenCode", "无关会话", r"E:\GithubProject\other", "今天天气不错，随便聊聊"),
    ]}
    src = work_dir / "data.json"
    src.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    out = work_dir / "ai_history"
    old_src, old_out = rag.SOURCE_JSON, rag.OUT_DIR
    buf = io.StringIO()
    try:
        rag.SOURCE_JSON, rag.OUT_DIR = src, out
        with contextlib.redirect_stdout(buf):
            rc = rag.cmd_build(argparse.Namespace())
    finally:
        rag.SOURCE_JSON, rag.OUT_DIR = old_src, old_out
    check("cmd_build 返回 0", rc == 0, rc)
    got = [json.loads(line)["id"]
           for line in (out / "sessions.jsonl").read_text(encoding="utf-8").splitlines()
           if line.strip()]
    check("只保留两条正常会话", set(got) == {"keep-oc", "keep-dsh"}, got)
    printed = buf.getvalue()
    check("输出过滤计数 2", "过滤网关执行器副本：2" in printed, printed[-300:])


def test_build_py_do_opencode_skips_gateway(work_dir: Path):
    print("[3] build.py do_opencode 桩测")
    build_py = Path(os.environ.get("AI_ORGANIZE_TOOLS") or r"F:\data\_ai_organize_tools") / "build.py"
    if not build_py.exists():
        print(f"  SKIP 本机工具链不存在：{build_py}")
        return
    work_dir.mkdir(parents=True, exist_ok=True)

    # 直接 exec 源码，避免 import 机制往被拦截的 F: 写 __pycache__。
    build = types.ModuleType("ai_organize_build_gateway_filter")
    build.__file__ = str(build_py)
    exec(compile(build_py.read_text(encoding="utf-8"), str(build_py), "exec"), build.__dict__)

    check("标题前缀判定", build.is_gateway_exec_session("code2api oneshot-x", ""))
    check("网关 workspace 判定", build.is_gateway_exec_session("正常", GATEWAY_WS))
    check("仓库根不误伤", not build.is_gateway_exec_session("正常", r"E:\GithubProject\Code2Api"))

    db_path = work_dir / "opencode.db"
    db = sqlite3.connect(db_path)
    try:
        db.execute("CREATE TABLE session_v2 "
                   "(id TEXT PRIMARY KEY, title TEXT, directory TEXT, time_created INTEGER)")
        db.execute("CREATE TABLE session_message "
                   "(session_id TEXT, seq INTEGER, time_created INTEGER, type TEXT, data TEXT)")
        now = int(time.time() * 1000)
        for sid, title, directory in (
            ("gw", "code2api oneshot-x", GATEWAY_WS),
            ("normal", "正常会话", r"E:\GithubProject\game_auto"),
        ):
            db.execute("INSERT INTO session_v2 VALUES (?,?,?,?)", (sid, title, directory, now))
            db.execute(
                "INSERT INTO session_message VALUES (?,?,?,?,?)",
                (sid, 1, now, "user",
                 json.dumps({"role": "user", "text": "game_auto ok-ww 签到"}, ensure_ascii=False)),
            )
        db.commit()
    finally:
        db.close()

    old_db, old_old = build.OC_DB, build.OC_OLD
    try:
        build.OC_DB, build.OC_OLD = str(db_path), str(work_dir / "missing.db")
        build.SESSIONS.clear()
        build.do_opencode()
    finally:
        build.OC_DB, build.OC_OLD = old_db, old_old
    titles = [s["title"] for s in build.SESSIONS]
    check("网关副本不入库", titles == ["正常会话"], titles)


def main():
    base = PROJECT_ROOT / "data" / "cache"
    base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="aihist_gw_", dir=base) as td:
        td = Path(td)
        test_rag_gateway_predicate()
        test_cmd_build_filters_gateway_copies(td / "rag")
        test_build_py_do_opencode_skips_gateway(td / "build")
    print("-" * 60)
    if FAILURES:
        print(f"FAILED {len(FAILURES)}: {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
