# -*- coding: utf-8 -*-
"""AI 对话史全链刷新编排回归（2026-11-18）。

背景：用户拍板「不做定时自动化，弄个开关在 gameauto 上」——DailyView 新增
「AI 对话史」卡片手动触发全链（妙手/Kilo 抓取 → build.py 重导 → 拷回 F: →
RAG 重建），编排逻辑在 services/ai_history_refresh.py。

本套件锁纯逻辑（subprocess 层 _py 全打桩，不跑真链、不写 F:）：
[1] 状态快照与单飞：运行中重复触发被拒（msg 提示、不执行任何步骤）；
[2] 失败收口：任一步 RuntimeError → last_ok=False、running 复位、
    msg 带「步骤[名]」、暂存目录被清理；
[3] Kilo 降级：②失败其余成功 → 整链仍成功、msg 注明「Kilo 沿用旧数据」；
[3b] TeleAgent/码道/DSH 并入抓取链（2026-11-20）：调用顺序含五抓取器、脚本路径
    指向 tools/<src>_capture/<src>_fetch.py、单源失败降级不阻链；
[4] 成功路径：build stdout 的 sessions 数与 RAG stdout 的切块/索引数解析进
    msg；COPY_FILES 逐个经 _copy_to_f（shutil 直写——管理员 GUI 直写 F:，
    2026-11-18 提权探针实锤，非管理员全被拦）；
[5] 超时收口：TimeoutExpired → last_ok=False 不崩。

跑法：.venv\\Scripts\\python.exe -m pytest tests\\suite_ai_history_refresh_20261118.py -q
"""

import io
import json
import logging
import os
import sys
import tempfile
import threading
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

# 沙箱兼容：tempfile 目录创建改默认权限（0700 受保护 DACL 在沙箱里写不进）
from sandbox_temp import install as _install_sandbox_temp

_install_sandbox_temp()
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import game_auto.services.ai_history_refresh as ahr  # noqa: E402

FAILURES = []


def check(label, ok, detail=None):
    mark = "PASS" if ok else "FAIL"
    print(f"  {mark} {label}" + (f"  ← {detail}" if not ok and detail is not None else ""))
    if not ok:
        FAILURES.append(label)


class Monkey:
    def __init__(self):
        self._saved = []

    def setattr(self, obj, k, v):
        self._saved.append((obj, k, getattr(obj, k, None)))
        setattr(obj, k, v)

    def undo(self):
        for obj, k, old in reversed(self._saved):
            setattr(obj, k, old)
        self._saved.clear()


def _reset():
    """恢复初始态（各用例 run_refresh 的 finally 已自释放闸，这里只重置状态）。"""
    ahr._set({"running": False, "last_ok": None, "last_ts": 0,
              "last_msg": "", "step": ""})


def _hold_flight():
    """跨线程占住单飞闸（模拟另一线程正在刷新），返回释放函数。"""
    held = threading.Event()
    release = threading.Event()

    def holder():
        ahr._FLIGHT.acquire()
        held.set()
        release.wait(30)
        ahr._FLIGHT.release()

    threading.Thread(target=holder, daemon=True).start()
    assert held.wait(5), "占闸线程未启动"
    return release.set


def test_state_and_singleflight():
    print("[1] 状态快照与单飞")
    _reset()
    snap = json.loads(ahr.status_json())
    check("初始态字段齐全", all(k in snap for k in
          ("running", "last_ok", "last_ts", "last_msg", "step")), sorted(snap))
    check("初始未跑过", snap["running"] is False and snap["last_ok"] is None)

    # 跨线程占住单飞闸模拟另一线程在跑（同线程 RLock 可重入，模拟不了拒绝）
    release_flight = _hold_flight()
    calls = []
    mk = Monkey()
    mk.setattr(ahr, "_py", lambda *a, **kw: calls.append(a) or "")
    out = ahr.run_refresh(logging.getLogger("suite"), lambda s: None)
    release_flight()
    check("运行中触发 → 拒绝且不执行任何步骤", calls == [], calls)
    check("拒绝提示写进 msg", "已有刷新在跑" in out.get("msg", ""), out.get("msg"))
    check("拒绝路径不阻塞状态读取（status_json 秒回）",
          "running" in json.loads(ahr.status_json()), "ok")
    mk.undo()
    _reset()


def test_failure_collect(tmp_path):
    print("[2] 失败收口")
    _reset()
    mk = Monkey()
    mk.setattr(ahr, "STAGE_DIR", str(tmp_path / "stage"))
    os.makedirs(ahr.STAGE_DIR, exist_ok=True)
    (Path(ahr.STAGE_DIR) / "AI对话全览.html").write_text("x", encoding="utf-8")

    def fake_py(args, timeout, logger, label, emit, env_extra=None):
        if label == "重导":
            raise RuntimeError("boom 重导失败")
        return ""

    mk.setattr(ahr, "_py", fake_py)
    copied = []
    mk.setattr(ahr, "_copy_to_f", lambda s, d: copied.append(os.path.basename(s)))
    out = ahr.run_refresh(logging.getLogger("suite"), lambda s: None)
    check("核心步骤失败 → last_ok=False", out["last_ok"] is False, out)
    check("running 复位", out["running"] is False)
    check("msg 带步骤名与原因", "步骤[重导总库]" in out["last_msg"] and "boom" in out["last_msg"],
          out["last_msg"])
    check("失败时不拷回 F:", copied == [], copied)
    check("暂存目录已清理", not os.path.isdir(ahr.STAGE_DIR))
    mk.undo()
    _reset()


def test_kilo_degrade(tmp_path):
    print("[3] Kilo 降级（②失败整链仍成功）")
    _reset()
    mk = Monkey()
    mk.setattr(ahr, "STAGE_DIR", str(tmp_path / "stage"))

    def fake_py(args, timeout, logger, label, emit, env_extra=None):
        if label == "Kilo":
            raise RuntimeError("内核没开")
        if label == "重导":
            # 模拟 build.py 产物：run_refresh 开头会清空暂存目录，产物必须在
            # 这一步（清空之后）落盘才符合真实时序
            for name in ahr.COPY_FILES:
                open(os.path.join(ahr.STAGE_DIR, name), "w").close()
            return '"sessions": 42'
        if label == "RAG":
            return "切块：1,234 块 / 约 9 千万字\n索引：1000000 词项 / index.bin 225MB（77s）"
        return ""

    mk.setattr(ahr, "_py", fake_py)
    copied = []
    mk.setattr(ahr, "_copy_to_f", lambda s, d: copied.append(os.path.basename(s)))
    out = ahr.run_refresh(logging.getLogger("suite"), lambda s: None)
    check("Kilo 失败但整链成功", out["last_ok"] is True, out)
    check("msg 注明 Kilo 沿用旧数据", "Kilo 沿用旧数据" in out["last_msg"], out["last_msg"])
    check("build stdout 的 sessions 数进 msg", "42 会话" in out["last_msg"], out["last_msg"])
    check("RAG 切块/索引数进 msg", "1234 块" in out["last_msg"] and "225MB" in out["last_msg"],
          out["last_msg"])
    check("三件套逐个拷回 F:", sorted(copied) == sorted(ahr.COPY_FILES), copied)
    mk.undo()
    _reset()


def test_teleagent_codearts_in_chain(tmp_path):
    print("[3b] TeleAgent/码道/DSH/小浣熊/千问办公 并入抓取链（路径 + 顺序 + 降级）")
    _reset()
    mk = Monkey()
    mk.setattr(ahr, "STAGE_DIR", str(tmp_path / "stage"))
    labels, paths = [], {}

    def fake_py(args, timeout, logger, label, emit, env_extra=None):
        labels.append(label)
        if label in ("TeleAgent", "码道", "DSH", "小浣熊", "千问办公"):
            paths[label] = args[-1]
        if label in ("TeleAgent", "DSH", "小浣熊", "千问办公"):
            raise RuntimeError(f"{label} 抓取失败")
        if label == "重导":
            for name in ahr.COPY_FILES:
                open(os.path.join(ahr.STAGE_DIR, name), "w").close()
            return '"sessions": 7'
        if label == "RAG":
            return "切块：10 块 / 约 1 万字\n索引：100 词项 / index.bin 1MB（1s）"
        return ""

    mk.setattr(ahr, "_py", fake_py)
    mk.setattr(ahr, "_copy_to_f", lambda s, d: None)
    out = ahr.run_refresh(logging.getLogger("suite"), lambda s: None)
    check("TeleAgent/DSH 失败整链仍成功", out["last_ok"] is True, out)
    check("msg 注明 TeleAgent 沿用旧数据", "TeleAgent 沿用旧数据" in out["last_msg"],
          out["last_msg"])
    check("msg 注明 DSH 沿用旧数据", "DSH 沿用旧数据" in out["last_msg"],
          out["last_msg"])
    check("msg 注明 小浣熊 沿用旧数据", "小浣熊 沿用旧数据" in out["last_msg"],
          out["last_msg"])
    check("msg 注明 千问办公 沿用旧数据", "千问办公 沿用旧数据" in out["last_msg"],
          out["last_msg"])
    check("七抓取器都在链上且顺序正确",
          labels == ["妙手", "Kilo", "TeleAgent", "码道", "DSH", "小浣熊", "千问办公",
                     "重导", "RAG"], labels)
    check("TeleAgent 抓取脚本路径正确",
          paths.get("TeleAgent", "").replace("\\", "/").endswith(
              "tools/teleagent_capture/teleagent_fetch.py"), paths)
    check("码道抓取脚本路径正确",
          paths.get("码道", "").replace("\\", "/").endswith(
              "tools/codearts_capture/codearts_fetch.py"), paths)
    check("DSH 抓取脚本路径正确",
          paths.get("DSH", "").replace("\\", "/").endswith(
              "tools/dsh_capture/dsh_fetch.py"), paths)
    check("小浣熊抓取脚本路径正确",
          paths.get("小浣熊", "").replace("\\", "/").endswith(
              "tools/raccoon_capture/raccoon_fetch.py"), paths)
    check("千问办公抓取脚本路径正确",
          paths.get("千问办公", "").replace("\\", "/").endswith(
              "tools/qwenwork_capture/qwenwork_fetch.py"), paths)
    mk.undo()
    _reset()


def test_timeout_collect(tmp_path):
    print("[4] 超时收口")
    _reset()
    mk = Monkey()
    mk.setattr(ahr, "STAGE_DIR", str(tmp_path / "stage"))

    def fake_py(args, timeout, logger, label, emit, env_extra=None):
        raise __import__("subprocess").TimeoutExpired(cmd=args, timeout=timeout)

    mk.setattr(ahr, "_py", fake_py)
    out = ahr.run_refresh(logging.getLogger("suite"), lambda s: None)
    check("TimeoutExpired → last_ok=False 不崩", out["last_ok"] is False and out["running"] is False, out)
    check("msg 标明超时步骤", "超时" in out["last_msg"], out["last_msg"])
    mk.undo()
    _reset()


def test_copy_helper(tmp_path):
    print("[5] _copy_to_f 直写（管理员 GUI 场景）")
    src = tmp_path / "a.html"
    src.write_text("hello", encoding="utf-8")
    dst_dir = tmp_path / "dst"
    dst_dir.mkdir()
    ahr._copy_to_f(str(src), str(dst_dir))
    check("shutil 直写落盘", (dst_dir / "a.html").read_text(encoding="utf-8") == "hello")


def main():
    test_state_and_singleflight()
    with tempfile.TemporaryDirectory() as td:
        test_failure_collect(Path(td))
        test_kilo_degrade(Path(td))
        test_teleagent_codearts_in_chain(Path(td))
        test_timeout_collect(Path(td))
        test_copy_helper(Path(td))
    print("-" * 60)
    if FAILURES:
        print(f"FAILED {len(FAILURES)}: {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
