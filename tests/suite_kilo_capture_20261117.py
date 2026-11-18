# -*- coding: utf-8 -*-
"""Kilo Desktop 会话抓取回归（2026-11-17）。

背景：Kilo Desktop 对话正文只在本地内核 HTTP API（kilo.exe serve，
Basic kilo:KILO_SERVER_PASSWORD）。本套件锁 kilo_fetch/kilo_env_dump 的纯函数
与发现逻辑，全部打桩不碰网络、不写真实 data/kilo_capture：

[1] find_kernel 双路径：cmdline 含 serve 的主路径 + 提权进程 cmdline 被
    AccessDenied 吞成空时的监听反查兜底（kilo.exe 精确名 ≠ kilo-desktop.exe）；
[2] msg_to_records 映射：user.text→user / assistant.text→ai /
    reasoning→think / tool→tool 摘要 / patch→tool / step-* 跳过 /
    时间戳 epoch 毫秒→秒；
[3] tool/patch 摘要：输出截断 600、文件列表超 10 个折叠；
[4] convert_session：add() 同构字段（id 前缀 kilo:、ws 从 cwd 提取、
    s/e/sk/n/chars/note 含模型名）；
[5] merge_export：按 id 合并去重（存在即整条替换）；
[6] load_password 优先级：--password > 环境变量 > kernel_env.json（旧 pid 拒用）；
[7] fetch_session_messages：瞬时错误重试、limit 截断告警；
[8] 接线：gitignore 覆盖 data/kilo_capture/、build.py do_kilo 已注册。

跑法：.venv\\Scripts\\python.exe -m pytest tests\\suite_kilo_capture_20261117.py -q
（经 tests/test_suites.py 子进程跑；直接文件调用有 stdout 包装 capture 老毛病）
"""

import io
import json
import os
import sys
import tempfile
import types
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

FAILURES = []


def check(label, ok, detail=None):
    mark = "PASS" if ok else "FAIL"
    print(f"  {mark} {label}" + (f"  ← {detail}" if not ok and detail is not None else ""))
    if not ok:
        FAILURES.append(label)


def load_fetch():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "kilo_fetch", PROJECT_ROOT / "tools" / "kilo_capture" / "kilo_fetch.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakePsutil(types.ModuleType):
    """可编程 psutil 桩：process_iter / net_connections / Process 由测试注入。"""

    CONN_LISTEN = "LISTEN"

    def __init__(self, processes, connections):
        super().__init__("psutil")
        self._processes = processes
        self._connections = connections

    class _Proc:
        def __init__(self, pid, name):
            self._pid, self._name = pid, name

        def pid(self):
            return self._pid

        def name(self):
            return self._name

    def process_iter(self, attrs=None):
        for pid, name, cmdline in self._processes:
            # 模拟提权进程：name/cmdline 查询 AccessDenied 被 psutil 吞成 None/[]
            yield types.SimpleNamespace(
                info={"pid": pid,
                      "name": None if name == "DENIED" else name,
                      "cmdline": [] if cmdline == "DENIED" else cmdline})

    def net_connections(self, kind="inet"):
        return self._connections

    def Process(self, pid):
        for cpid, name, _cmd in self._processes:
            if cpid == pid and name != "DENIED":
                return self._Proc(pid, name)
        raise RuntimeError("no such process")


class Monkey:
    def __init__(self):
        self._saved = []

    def setitem(self, d, k, v):
        self._saved.append((d, k, d.get(k), k in d))
        d[k] = v

    def setattr(self, obj, k, v):
        self._saved.append((obj, k, getattr(obj, k, None), True))
        setattr(obj, k, v)

    def undo(self):
        for obj, k, old, existed in reversed(self._saved):
            if isinstance(obj, dict):
                if existed:
                    obj[k] = old
                else:
                    obj.pop(k, None)
            elif existed:
                setattr(obj, k, old)
            else:
                try:
                    delattr(obj, k)
                except AttributeError:
                    pass
        self._saved.clear()


def test_find_kernel():
    print("[1] find_kernel 双路径（cmdline 主路径 + 监听反查兜底）")
    kf = load_fetch()

    # 路径 A：cmdline 可读（普通权限内核场景）
    mk = Monkey()
    procs = [(21612, "kilo.exe", ["kilo.exe", "serve", "--port=0"]),
             (28504, "kilo-desktop.exe", ["kilo-desktop.exe"])]
    conns = [types.SimpleNamespace(status="LISTEN", pid=28504,
                                   laddr=types.SimpleNamespace(ip="127.0.0.1", port=52240)),
             types.SimpleNamespace(status="LISTEN", pid=21612,
                                   laddr=types.SimpleNamespace(ip="127.0.0.1", port=4096))]
    mk.setitem(sys.modules, "psutil", FakePsutil(procs, conns))
    check("cmdline 路径: kilo.exe serve → (21612, 4096)", kf.find_kernel() == (21612, 4096))
    mk.undo()

    # 路径 B：提权进程 cmdline 被吞成空（2026-11-17 实坑），监听反查兜底；
    # kilo-desktop.exe 的 52240 监听不得误报（名字精确相等区分）
    mk = Monkey()
    procs = [(21612, "kilo.exe", "DENIED"), (28504, "kilo-desktop.exe", ["x"])]
    conns = [types.SimpleNamespace(status="LISTEN", pid=28504,
                                   laddr=types.SimpleNamespace(ip="127.0.0.1", port=52240)),
             types.SimpleNamespace(status="LISTEN", pid=21612,
                                   laddr=types.SimpleNamespace(ip="127.0.0.1", port=4096))]
    mk.setitem(sys.modules, "psutil", FakePsutil(procs, conns))
    check("监听反查兜底: cmdline 被吞仍命中 (21612, 4096)",
          kf.find_kernel() == (21612, 4096))
    check("监听反查不会误认 kilo-desktop.exe 的 52240", kf.find_kernel() != (28504, 52240))
    mk.undo()

    mk = Monkey()
    mk.setitem(sys.modules, "psutil", FakePsutil([(1, "other.exe", ["x"])], []))
    check("无内核 → (None, None)", kf.find_kernel() == (None, None))
    mk.undo()


def test_msg_to_records():
    print("[2] msg_to_records 映射")
    kf = load_fetch()
    recs = kf.msg_to_records({
        "info": {"role": "user", "time": {"created": 1794654648225}},
        "parts": [{"type": "text", "text": "评价一下这个项目"}],
    })
    check("user 文本 → (user, epoch秒, 正文)",
          recs == [("user", 1794654648, "评价一下这个项目")], recs)

    recs = kf.msg_to_records({
        "info": {"role": "assistant", "time": {"created": 1000}},
        "parts": [
            {"type": "step-start"},
            {"type": "reasoning", "text": "先看 README"},
            {"type": "tool", "tool": "bash", "state": {"status": "completed",
             "input": {"command": "ls"}, "output": "src\n"}},
            {"type": "patch", "files": ["E:/x/a.py"]},
            {"type": "text", "text": "结论：质量不错"},
            {"type": "step-finish", "reason": "tool-calls"},
        ],
    })
    roles = [r for r, _k, _x in recs]
    check("assistant 顺序映射 think→tool→output→tool→ai（step-* 跳过）",
          roles == ["think", "tool", "output", "tool", "ai"], roles)
    check("工具返回走 output 角色（2026-11-17 与①口径对齐）",
          recs[2][2] == "src" and recs[2][0] == "output", recs[2])
    check("一条消息拆多条共享同一时间戳（Trae 同款口径）",
          all(k == 1 for _r, k, _x in recs), [k for _r, k, _x in recs])
    check("patch 记录带 [patch] 前缀与文件名",
          "[patch]" in recs[3][2] and "a.py" in recs[3][2], recs[3][2])
    check("空 part 全丢弃", all(x.strip() for _r, _k, x in recs))

    check("user 无 text part（纯附件）→ 零记录",
          kf.msg_to_records({"info": {"role": "user", "time": {"created": 1}},
                             "parts": [{"type": "file", "path": "x"}]}) == [])
    check("空消息体 → 零记录", kf.msg_to_records({"info": {}, "parts": []}) == [])


def test_tool_patch_summary():
    print("[3] tool/patch 摘要截断与折叠")
    kf = load_fetch()
    long_out = "x" * 2000
    part = {"tool": "bash", "state": {"status": "completed",
            "input": {"command": "dir", "description": "列目录"}, "output": long_out}}
    s = kf._tool_summary(part)
    check("输出截到 600 上下（TOOL_RESULT_LIMIT）",
          len(s) < kf.TOOL_RESULT_LIMIT + 200 and "x" * 500 in s, len(s))
    check("含工具名/状态/输入描述", "[tool] bash (completed)" in s and "列目录" in s, s[:80])

    part = {"tool": "grep", "state": {"status": "completed",
            "input": {"pattern": "TODO", "command": None}, "output": None}}
    got = kf._tool_summary(part)
    check("无描述时回退 pattern，无输出不拼『输出:』",
          "[tool] grep (completed)" in got and "TODO" in got and "输出:" not in got, got)

    files = [f"E:/p/{i}.py" for i in range(15)]
    s = kf._patch_summary({"files": files})
    check("patch 文件列表 >10 折叠『另 N 个文件』", "另 5 个文件" in s, s)

    part = {"tool": "bash", "state": {"status": "completed",
            "input": {"command": {"nested": "json"}}, "output": {"a": 1}}}
    check("input/output 为 dict/list 时 JSON 化不炸", "nested" in kf._tool_summary(part))


def test_convert_session():
    print("[4] convert_session add() 同构字段")
    kf = load_fetch()
    ws_entry = {"title": "某排版咨询会话", "chatIds": ["ses_abc"]}
    msgs = [
        {"info": {"role": "user", "time": {"created": 1794916232525},
                  "path": {"cwd": "E:\\GithubProject\\game_auto"}},
         "parts": [{"type": "text", "text": "这是个什么排版"}]},
        {"info": {"role": "assistant", "modelID": "particle-ai/particle-1:free",
                  "time": {"created": 1794916240000, "completed": 1794916300000}},
         "parts": [{"type": "reasoning", "text": "想想"},
                   {"type": "text", "text": "这是排版说明"}]},
    ]
    s = kf.convert_session(ws_entry, msgs)
    check("id 前缀 kilo: + chat id", s["id"] == "kilo:ses_abc", s["id"])
    check("src=Kilo / 标题来自 work-sessions", s["src"] == "Kilo" and s["title"] == "某排版咨询会话")
    check("ws 从 path.cwd 提取", s["ws"] == "game_auto", s["ws"])
    check("sk 取末条记录时间（记录带 created，不含 completed）", s["sk"] == 1794916240, s["sk"])
    check("n=3 条记录 / chars=各记录 x 长度和", s["n"] == 3 and s["chars"] > 0, (s["n"], s["chars"]))
    check("note 含来源说明与模型名",
          "Kilo Desktop" in s["note"] and "particle-ai/particle-1:free" in s["note"], s["note"])
    check("msgs 记录含 r/t/k/x 四键", all(set(m) == {"r", "t", "k", "x"} for m in s["msgs"]))


def test_merge_export(tmp_path):
    print("[5] merge_export 按 id 合并去重")
    kf = load_fetch()
    mk = Monkey()
    mk.setattr(kf, "KILO_JSON", str(tmp_path / "kilo.json"))
    mk.setattr(kf, "EXPORT", str(tmp_path))
    base = {"id": "kilo:ses_a", "title": "旧标题", "n": 1, "msgs": []}
    check("首次合并 → 1 会话", kf.merge_export(base) == 1)
    fresh = {"id": "kilo:ses_a", "title": "新标题", "n": 5, "msgs": []}
    check("同 id 重抓 → 整条替换仍 1 会话", kf.merge_export(fresh) == 1)
    data = json.load(open(tmp_path / "kilo.json", encoding="utf-8"))
    check("替换后读到的是新内容", data[0]["title"] == "新标题", data[0]["title"])
    check("异 id 追加 → 2 会话",
          kf.merge_export({"id": "kilo:ses_b", "title": "b", "msgs": []}) == 2)
    (tmp_path / "kilo.json").write_text("{broken", encoding="utf-8")
    check("上游聚合损坏 → 重建不炸", kf.merge_export(fresh) == 1)
    mk.undo()


def test_load_password(tmp_path):
    print("[6] load_password 优先级与旧 pid 拒用")
    kf = load_fetch()
    check("--password 最高优先", kf.load_password(" explicit ") == "explicit")

    mk = Monkey()
    mk.setattr(kf, "KERNEL_ENV_JSON", str(tmp_path / "nope.json"))
    mk.setattr(kf, "find_kernel", lambda: (21612, 4096))
    saved_env = dict(os.environ)
    os.environ["KILO_SERVER_PASSWORD"] = "envpw"
    try:
        check("次选环境变量", kf.load_password("") == "envpw")
    finally:
        os.environ.clear()
        os.environ.update(saved_env)
    mk.undo()

    mk = Monkey()
    env_file = tmp_path / "kernel_env.json"
    env_file.write_text(json.dumps({"pid": 999, "port": 1,
                                    "env": {"KILO_SERVER_PASSWORD": "filepw"}}), encoding="utf-8")
    mk.setattr(kf, "KERNEL_ENV_JSON", str(env_file))
    mk.setattr(kf, "find_kernel", lambda: (21612, 4096))
    saved_env = dict(os.environ)
    os.environ.pop("KILO_SERVER_PASSWORD", None)
    try:
        code = None
        try:
            kf.load_password("")
        except SystemExit as exc:
            code = str(exc)
        check("kernel_env.json pid 过期 → 拒用并提示重跑 dump",
              bool(code) and "重启会轮换密码" in code, (code or "")[:80])
        env_file.write_text(json.dumps({"pid": 21612, "port": 4096,
                                        "env": {"KILO_SERVER_PASSWORD": "filepw"}}),
                            encoding="utf-8")
        check("pid 匹配 → 取用文件密码", kf.load_password("") == "filepw")
    finally:
        os.environ.clear()
        os.environ.update(saved_env)
    mk.undo()


def test_fetch_messages():
    print("[7] fetch_session_messages 重试与截断告警")
    kf = load_fetch()
    calls = []

    def flaky(url):
        calls.append(url)
        if len(calls) < 3:
            raise IOError("transient")
        return [{"info": {}}]

    msgs = kf.fetch_session_messages(flaky, "http://127.0.0.1:4096", "ses_x", limit=10)
    check("瞬时错误自动重试后成功", len(msgs) == 1 and len(calls) == 3, len(calls))
    check("重试 URL 正确", calls[0].endswith("/session/ses_x/message?limit=10"), calls[0])

    full = [{"info": {}} for _ in range(10)]
    check("返回条数==limit 正常返回（截断告警走日志不抛）",
          kf.fetch_session_messages(lambda url: full, "http://x", "s", limit=10) == full)

    code = None

    def always_down(url):
        raise IOError("down")

    try:
        kf.fetch_session_messages(always_down, "http://x", "s", limit=5)
    except SystemExit as exc:
        code = str(exc)
    check("重试耗尽 → SystemExit 带原因", bool(code) and "重试 3 次" in code, (code or "")[:60])


def test_wiring():
    print("[8] 接线：gitignore / build.py do_kilo 注册")
    check("data/kilo_capture/ 在 gitignore（含正文与密码）",
          "/data/kilo_capture/" in (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8"))
    build_src = Path("F:/data/_ai_organize_tools/build.py")
    if build_src.exists():
        text = build_src.read_text(encoding="utf-8")
        check("build.py 已注册 do_kilo（来源循环，注意循环在 def main() 内部）",
              "def do_kilo" in text and "do_kilo" in
              text.split("for fn in (", 1)[-1].split(")", 1)[0], "do_kilo")
        check("build.py KILO_JSON 指向 game_auto data 聚合",
              "kilo_capture" in text and "kilo.json" in text)
    else:
        check("build.py 存在则查注册（外部文件缺失则跳过）", True)
    dump_src = PROJECT_ROOT / "tools" / "kilo_capture" / "kilo_env_dump.py"
    check("提权 dump 助手存在且含 KILO_SERVER_PASSWORD 校验",
          dump_src.exists() and "KILO_SERVER_PASSWORD" in dump_src.read_text(encoding="utf-8"))


def main():
    test_find_kernel()
    test_msg_to_records()
    test_tool_patch_summary()
    test_convert_session()
    _pin = os.path.join(PROJECT_ROOT, "data", "cache")
    os.makedirs(_pin, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=_pin) as td:
        test_merge_export(Path(td))
        test_load_password(Path(td))
    test_fetch_messages()
    test_wiring()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED {len(FAILURES)}: {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
