"""失败取证「测试进程守卫」回归（2026-11-22）。

事故：用户 11-22 贴出 GUI「最近失败记录」，三条 BaiJingJointTraining/防区页
全是「点击入口后未见防区页……轮询期间见过：「hub帧签名」」且没有截图，问
"这是什么情况，也不留证据"。查生产库发现：

- 60 条 hwnd=1234（suite_baijing_joint_training 的假句柄）+ 78 条 hwnd=1
  （其他白荆套件的假句柄）全都是套件写进来的；真机 11-17~11-21 联合训练场
  天天 success。
- 假句柄 GetWindowRect 必然失败 → 截图恒为空 → 卡片上只剩"没有截图的失败"。

本套件锁住的口径：
  [1] 测试进程识别（argv 跑套件 / GAMEAUTO_TEST_MODE / pytest 用例内）
  [2] 测试进程里 record_failure 不截图、不建库、返回 None
  [3] 正式进程（非测试）照旧取证：写库 + image_path 用标注图 + 原图进 extra
  [4] 同族补漏（2026-11-22 第二轮）：每日进度 record_task、月度/周常门控
      set_task_meta 在"测试进程 + 生产路径"时也拒写，写临时库照常放行
  [5] 接线静态断言

跑法：`.venv\\Scripts\\python.exe tests/suite_flow_failure_test_guard_20261122.py`
"""
from __future__ import annotations

import contextlib
import logging
import os
import sys
import time
import unittest.mock as mock
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

import io as _io

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from game_auto.runtime import flow_diagnostics as fd
import game_auto.storage.workflow_db as wdb

FAILURES = []
LOG = logging.getLogger("suite")
_NO_TEST_ENV = {"PYTEST_CURRENT_TEST": "", "GAMEAUTO_TEST_MODE": "", "GAME_AUTO_TEST_MODE": ""}


@contextlib.contextmanager
def _tmp_db(name="guard"):
    """临时库文件放 .temp 下（.temp 已 gitignore）。

    不建子目录：本机沙箱下**新建目录**不可写也不可删（WinError 5），
    直接落文件才通得过。清理失败一律忽略——环境问题不该让套件红。
    """
    base = PROJECT / ".temp"
    base.mkdir(parents=True, exist_ok=True)
    p = base / f"{name}_{os.getpid()}_{int(time.time() * 1000)}.sqlite3"
    try:
        yield p
    finally:
        for sfx in ("", "-wal", "-shm"):
            try:
                Path(str(p) + sfx).unlink()
            except Exception:
                pass


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'} {label}" + ("" if ok else f"  {detail}"))
    if not ok:
        FAILURES.append(label)


def test_detect():
    print("[1] 测试进程识别三路")
    with mock.patch.dict(os.environ, dict(_NO_TEST_ENV)), \
            mock.patch.object(sys, "argv", [str(Path(__file__).resolve())]):
        check("直接跑 tests/suite_*.py → 测试进程", fd.is_test_process() is True)
    with mock.patch.dict(os.environ, dict(_NO_TEST_ENV, GAMEAUTO_TEST_MODE="1")), \
            mock.patch.object(sys, "argv", [r"E:\GithubProject\game_auto\main.py"]):
        check("GAMEAUTO_TEST_MODE=1 → 测试进程", fd.is_test_process() is True)
    with mock.patch.dict(os.environ, dict(_NO_TEST_ENV, PYTEST_CURRENT_TEST="pytest::x")), \
            mock.patch.object(sys, "argv", ["pytest"]):
        check("pytest 用例内 → 测试进程", fd.is_test_process() is True)
    with mock.patch.dict(os.environ, dict(_NO_TEST_ENV)), \
            mock.patch.object(sys, "argv", [str(PROJECT / "main.py")]):
        check("正式启动脚本不算测试进程", fd.is_test_process() is False)
    with mock.patch.dict(os.environ, dict(_NO_TEST_ENV, GAMEAUTO_TEST_MODE="0")), \
            mock.patch.object(sys, "argv", [str(PROJECT / "scripts" / "run.py")]):
        check("GAMEAUTO_TEST_MODE=0 不算测试进程", fd.is_test_process() is False)


def test_guard_skips():
    print("[2] 测试进程：不截图 / 不建库 / 返回 None")
    calls = {"capture": 0, "db": 0}

    def _capture(*a, **k):
        calls["capture"] += 1
        return ("stub.jpg", "stub_ocr.jpg")

    with mock.patch.object(fd, "capture_failure_image", side_effect=_capture), \
            mock.patch.object(wdb, "WorkflowDB", side_effect=lambda *a, **k: calls.__setitem__("db", calls["db"] + 1)):
        rid = fd.record_failure(LOG, "BaiJingJointTraining", "防区页", "桩失败", hwnd=1234)
    check("返回 None（没有记录 id）", rid is None, str(rid))
    check("没有抓截图", calls["capture"] == 0, str(calls))
    check("没有打开生产库", calls["db"] == 0, str(calls))


def test_prod_still_records():
    print("[3] 正式进程：照旧取证（写库 + 标注图 + 原图留 extra）")
    recorded = {}

    class FakeDB:
        def __init__(self, path):
            recorded["db_path"] = path

        def record_flow_failure(self, record):
            recorded.update(record)
            return 42

    with mock.patch.object(fd, "is_test_process", return_value=False), \
            mock.patch.object(fd, "capture_failure_image", return_value=("raw.jpg", "raw_ocr.jpg")), \
            mock.patch.object(fd, "prune_old_failure_images", return_value=0), \
            mock.patch.object(wdb, "WorkflowDB", FakeDB):
        rid = fd.record_failure(
            LOG, "BaiJingJointTraining", "防区页", "真机失败",
            hwnd=0, game_key="baijing", ocr_sample=["物资筹备(1.00)@126,108"],
        )
    check("返回写库 id", rid == 42, str(rid))
    check("image_path 用 OCR 标注图（打开就能看到识别框）",
          recorded.get("image_path") == "raw_ocr.jpg", str(recorded.get("image_path")))
    check("原图留在 extra.raw_image", (recorded.get("extra") or {}).get("raw_image") == "raw.jpg",
          str(recorded.get("extra")))
    check("当帧 OCR 样本落库", recorded.get("ocr_texts") == ["物资筹备(1.00)@126,108"],
          str(recorded.get("ocr_texts")))
    check("flow_key / step 原样落库",
          recorded.get("flow_key") == "BaiJingJointTraining" and recorded.get("step") == "防区页",
          str((recorded.get("flow_key"), recorded.get("step"))))


def test_paths_helpers():
    print("[4a] 路径识别：生产 data/ 与临时目录要分得开")
    from game_auto.settings import paths as P

    check("生产库路径算生产数据", P.is_production_data_path(P.WORKFLOW_DB_PATH) is True,
          str(P.WORKFLOW_DB_PATH))
    check("证据目录算生产数据", P.is_production_data_path(P.FLOW_FAILURE_DIR) is True,
          str(P.FLOW_FAILURE_DIR))
    with _tmp_db("paths") as p:
        check("临时库不算生产数据", P.is_production_data_path(p) is False, str(p))


def test_progress_guard():
    print("[4b] 每日进度：测试进程写生产库拦住、写临时库放行")
    import game_auto.services.daily_progress_tracker as dpt
    from game_auto.services.daily_progress_tracker import DailyProgressTracker
    from game_auto.storage.workflow_db import WorkflowDB

    def _count(db):
        with db._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM daily_progress").fetchone()[0]

    with _tmp_db("progress") as p:
        db = WorkflowDB(str(p))
        tracker = DailyProgressTracker(db)
        # 生产路径（用桩把判定拉成 True）→ 拒写：这是 wuwa/script_script 那种假进度
        with mock.patch.object(dpt, "is_production_data_path", return_value=True):
            tracker.record_task("wuwa", "鸣潮", "script_script", "script",
                                "failed", message="ok（任务线程心跳超时，看门狗止损）")
        check("生产路径：假进度不落库", _count(db) == 0, str(_count(db)))
        # 临时库 → 照写（套件自建库测 tracker 的既有写法不能被拦）
        with mock.patch.object(dpt, "is_production_data_path", return_value=False):
            tracker.record_task("wuwa", "鸣潮", "script_script", "script", "failed", message="ok")
        check("临时库：正常落库", _count(db) == 1, str(_count(db)))
        # 正式进程（非测试）写生产库也不拦——由 is_test_process 决定
        with mock.patch.object(dpt, "is_test_process", return_value=False), \
                mock.patch.object(dpt, "is_production_data_path", return_value=True):
            tracker.record_task("wuwa", "鸣潮", "script_ok-ww", "ok-ww", "success", message="成功")
        check("正式进程：照常落库", _count(db) == 2, str(_count(db)))


def test_meta_guard():
    print("[4c] 月度/周常门控：测试进程不写生产 task_meta")
    from game_auto.storage.workflow_db import WorkflowDB

    with _tmp_db("meta") as p:
        db = WorkflowDB(str(p))
        with mock.patch.object(wdb, "is_production_data_path", return_value=True):
            db.set_task_meta("monthly_flow:GENSHIN_MONTHLY", "2026-11")
        check("生产路径：门控不写入", db.get_task_meta("monthly_flow:GENSHIN_MONTHLY") is None,
              str(db.get_task_meta("monthly_flow:GENSHIN_MONTHLY")))
        with mock.patch.object(wdb, "is_production_data_path", return_value=False):
            db.set_task_meta("monthly_flow:GENSHIN_MONTHLY", "2026-11")
        check("临时库：门控正常写入",
              db.get_task_meta("monthly_flow:GENSHIN_MONTHLY") == "2026-11",
              str(db.get_task_meta("monthly_flow:GENSHIN_MONTHLY")))


def test_wiring():
    print("[5] 接线静态断言")
    fd_src = (PROJECT / "src/game_auto/runtime/flow_diagnostics.py").read_text(encoding="utf-8")
    body = fd_src[fd_src.index("def record_failure("):]
    check("record_failure 判「测试进程 + 生产路径」",
          "if is_test_process() and is_production_data_path(WORKFLOW_DB_PATH):" in body)
    guard_at = body.index("if is_test_process()")
    check("测试分支在抓图之前 return",
          guard_at < body.index("capture_failure_image(") and guard_at < body.index("WorkflowDB("),
          "guard 必须排在截图/写库之前")
    tracker_src = (PROJECT / "src/game_auto/services/daily_progress_tracker.py").read_text(encoding="utf-8")
    check("每日进度 record_task 有同款守卫",
          "is_test_process() and is_production_data_path(" in tracker_src)
    db_src = (PROJECT / "src/game_auto/storage/workflow_db.py").read_text(encoding="utf-8")
    check("task_meta 门控有同款守卫",
          "is_test_process() and is_production_data_path(self.db_path)" in db_src)
    paths_src = (PROJECT / "src/game_auto/settings/paths.py").read_text(encoding="utf-8")
    check("守卫工具在 paths（runtime/services/storage 共用）",
          "def is_test_process()" in paths_src and "def is_production_data_path(" in paths_src)
    suite_src = (PROJECT / "tests/suite_baijing_joint_training_20261115.py").read_text(encoding="utf-8")
    check("联合训练场套件已打桩 _record_failure",
          'mock.patch.object(bjt, "_record_failure"' in suite_src)
    ci = (PROJECT / "tests/ci_suites.txt").read_text(encoding="utf-8")
    check("ci_suites.txt 登记本套件", "suite_flow_failure_test_guard_20261122.py" in ci)


def main():
    for t in (test_detect, test_guard_skips, test_prod_still_records,
              test_paths_helpers, test_progress_guard, test_meta_guard, test_wiring):
        t()
    print()
    if FAILURES:
        print(f"FAIL: {len(FAILURES)} 项失败 -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
