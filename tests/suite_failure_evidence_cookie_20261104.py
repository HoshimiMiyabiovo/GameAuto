"""2026-11-04 晚间四项修复回归。

覆盖：
  1. confirm 失败重试耗尽也会落取证（绝区零「元素 10s 未消失」无图事故）；
  2. 异环启动器窗口连续不可用止损（12 分钟空转），被遮挡不判失败；
  3. 米游社便笺 Cookie 过期自动刷新重试 + stuid_v2 误报告警消除；
  4. 测试套件日志与生产 auto_runner.log 隔离（生产路径行为不变）。

跑法：.venv\\Scripts\\python.exe tests\\suite_failure_evidence_cookie_20261104.py
"""

from __future__ import annotations

import ast
import io
import json
import logging
import os
import sys
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import game_auto.clients.daily_checker as dc  # noqa: E402
import game_auto.clients.mihoyo_sign as ms  # noqa: E402
import game_auto.runtime.automation_controller as ac  # noqa: E402
import game_auto.runtime.foreground_flow_runner as ff  # noqa: E402
import game_auto.runtime.ocr_click_tester as oc  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} :: {detail}")
        FAILURES.append(name)


def _run_flow_source() -> str:
    return ast.get_source_segment(
        Path(ff.__file__).read_text(encoding="utf-8"),
        next(
            n for n in ast.parse(Path(ff.__file__).read_text(encoding="utf-8")).body
            if isinstance(n, ast.FunctionDef) and n.name == "run_flow"
        ),
    ) or ""


def test_confirm_failure_evidence():
    print("[1] confirm 失败重试耗尽 → 落取证")
    tree = ast.parse(Path(ff.__file__).read_text(encoding="utf-8"))
    fn = next(
        n for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "run_flow"
    )
    # 找 `if not ok:` 节点，断言其 body 里真的调了取证函数
    branches = [
        node for node in ast.walk(fn)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.UnaryOp)
        and isinstance(node.test.op, ast.Not)
        and isinstance(node.test.operand, ast.Name)
        and node.test.operand.id == "ok"
    ]
    check("run_flow 里有 `if not ok:` 确认失败分支", len(branches) == 1, len(branches))
    if branches:
        called = {
            n.func.id
            for n in ast.walk(branches[0])
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }
        check("该分支调用 _collect_failure_evidence",
              "_collect_failure_evidence" in called, sorted(called))
        seg = ast.get_source_segment(Path(ff.__file__).read_text(encoding="utf-8"), branches[0]) or ""
        check("error 里带上真实 reason", "确认失败: {reason}" in seg, seg[:120])
    src = Path(ff.__file__).read_text(encoding="utf-8")
    # 4 = 1 处 def + 3 处调用（未匹配 / 分发失败 / 确认失败）
    check("全模块共 3 处取证调用（未匹配/分发失败/确认失败）",
          src.count("_collect_failure_evidence(") == 4,
          src.count("_collect_failure_evidence("))


def test_window_dead_stop():
    print("[2] 异环窗口连续不可用止损")
    with mock.patch.object(oc, "_ensure_window_region", return_value=None):
        check("最小化/句柄失效 → 不可用", ff._window_available_for_ocr(123) is False)
    with mock.patch.object(oc, "_ensure_window_region", return_value=(0, 0, 1920, 1080)):
        check("窗口有效 → 可用", ff._window_available_for_ocr(123) is True)
    helper = ast.get_source_segment(
        Path(ff.__file__).read_text(encoding="utf-8"),
        next(
            n for n in ast.parse(Path(ff.__file__).read_text(encoding="utf-8")).body
            if isinstance(n, ast.FunctionDef) and n.name == "_window_available_for_ocr"
        ),
    ) or ""
    check("只判窗口可用性，不碰遮挡判据",
          "_window_obscured_ratio" not in helper and "_ensure_window_region" in helper)
    src = _run_flow_source()
    check("run_flow 有连续不可用计时", "window_dead_since" in src and "dead_window_timeout" in src)
    check("提前 break 而不是空转到 timeout", "提前结束本轮匹配" in src)
    check("止损条件只针对窗口可用性 + 非全屏步骤（不判遮挡）",
          "not _window_available_for_ocr(hwnd)" in src
          and "search_scope != \"screen\"" in src
          and "obscured" not in src)

    cfgs = {}
    for fname in ("flow_configs.json", "flow_configs.example.json"):
        cfgs[fname] = json.loads((PROJECT_ROOT / fname).read_text(encoding="utf-8"))
    a, b = cfgs["flow_configs.json"], cfgs["flow_configs.example.json"]
    check("两份 NTE_MONTHLY 配置一致", a["NTE_MONTHLY"] == b["NTE_MONTHLY"])
    nte = a["NTE_MONTHLY"]
    check("止损阈值 45s", nte.get("window_dead_timeout_sec") == 45, nte.get("window_dead_timeout_sec"))
    check("真更新等待未被一刀切缩短（step2 仍 1200s）",
          nte["steps"][1]["timeout_sec"] == 1200, nte["steps"][1]["timeout_sec"])
    others = [
        k for k, v in a.items()
        if k != "NTE_MONTHLY" and isinstance(v, dict)
        and "window_dead_timeout_sec" in v
    ]
    check("止损键只写在 NTE_MONTHLY，其它流程不受影响", others == [], others)


def test_cookie_refresh():
    print("[3] 米游社便笺 Cookie 过期自动刷新")
    calls = []

    def once(cookie, uid, server, game, dh):
        calls.append(cookie)
        if len(calls) == 1:
            raise RuntimeError("Cookie 已过期，请重新登录")
        return {"current_train_score": 500}

    with mock.patch.object(dc, "_query_hoyo_daily_note_once", once), \
            mock.patch.object(ms, "_ensure_fresh_cookie_token",
                              return_value="account_id=1;cookie_token=NEW"):
        res = dc._query_hoyo_daily_note("account_id=1;cookie_token=OLD", 1,
                                        "prod_gf_cn", "honkai_sr")
    check("过期后刷新重试成功", res == {"current_train_score": 500}, res)
    check("只多一次请求", len(calls) == 2, len(calls))
    check("重试用的是新 cookie_token", calls[-1].endswith("NEW"), calls)

    calls.clear()

    def once_other(cookie, *args):
        calls.append(cookie)
        raise RuntimeError("API 错误 (retcode=1034)")

    with mock.patch.object(dc, "_query_hoyo_daily_note_once", once_other), \
            mock.patch.object(ms, "_ensure_fresh_cookie_token") as refresh:
        try:
            dc._query_hoyo_daily_note("x", 1, "s", "g")
        except RuntimeError:
            pass
    check("非过期错误不重试、不刷新", len(calls) == 1 and not refresh.called, len(calls))

    calls.clear()

    def once_expired(cookie, *args):
        calls.append(cookie)
        raise RuntimeError("Cookie 已过期，请重新登录")

    with mock.patch.object(dc, "_query_hoyo_daily_note_once", once_expired), \
            mock.patch.object(ms, "_ensure_fresh_cookie_token", return_value="x"):
        try:
            dc._query_hoyo_daily_note("x", 1, "s", "g")
            raised = False
        except RuntimeError:
            raised = True
    check("刷新未生效时原样抛错、不重复请求", raised and len(calls) == 1, (raised, len(calls)))

    logs = []

    class Collector(logging.Handler):
        def emit(self, record):
            logs.append((record.levelname, record.getMessage()))

    handler = Collector()
    dc.LOGGER.addHandler(handler)
    old_level = dc.LOGGER.level
    dc.LOGGER.setLevel(logging.DEBUG)
    try:
        with mock.patch.object(dc, "get_bound_accounts",
                               return_value=[{"uid": "1", "region": "x"}]):
            dc._check_hoyo_accounts("account_id=123;cookie_token=abc;stoken=v2_x", "honkai_sr")
    finally:
        dc.LOGGER.removeHandler(handler)
        dc.LOGGER.setLevel(old_level)
    warns = [m for level, m in logs if level == "WARNING"]
    check("不再误报「缺少 stuid_v2」", warns == [], warns)


def test_log_isolation():
    print("[4] 测试日志与生产 auto_runner.log 隔离")
    suites_src = (PROJECT_ROOT / "tests" / "test_suites.py").read_text(encoding="utf-8")
    check("test_suites.py 给所有子进程套件设开关",
          '"GAMEAUTO_LOG_FILE_DISABLED": "1"' in suites_src)
    okww_src = (PROJECT_ROOT / "tests" / "suite_okww_module_20261104.py").read_text(encoding="utf-8")
    check("手动单跑套件也有兜底 setdefault",
          'os.environ.setdefault("GAMEAUTO_LOG_FILE_DISABLED", "1")' in okww_src)
    init_src = ast.get_source_segment(
        Path(ac.__file__).read_text(encoding="utf-8"),
        next(
            n for n in ast.walk(ast.parse(Path(ac.__file__).read_text(encoding="utf-8")))
            if isinstance(n, ast.FunctionDef) and n.name == "_init_logger"
        ),
    ) or ""
    check("开关与 PYTEST_CURRENT_TEST 双判据",
          "GAMEAUTO_LOG_FILE_DISABLED" in init_src and "PYTEST_CURRENT_TEST" in init_src)
    check("Console/UI handler 仍在（只是不挂文件）",
          "listener_handlers" in init_src and "console_handler" in init_src)

    # 两条路径的动态验证：mock 掉文件 handler 与 listener，避免真写日志/起线程
    for label, flag, expected in (("生产(默认)", None, True), ("测试(开关=1)", "1", False)):
        os.environ.pop("GAMEAUTO_LOG_FILE_DISABLED", None)
        os.environ.pop("PYTEST_CURRENT_TEST", None)
        if flag:
            os.environ["GAMEAUTO_LOG_FILE_DISABLED"] = flag
        inst = object.__new__(ac.AutomationController)
        with mock.patch.object(ac, "TimedRotatingFileHandler") as file_handler, \
                mock.patch("logging.handlers.QueueListener"):
            inst._init_logger()
            check(f"{label} → 挂生产文件 handler = {expected}",
                  file_handler.called is expected, file_handler.called)
    os.environ.pop("GAMEAUTO_LOG_FILE_DISABLED", None)


def main():
    test_confirm_failure_evidence()
    test_window_dead_stop()
    test_cookie_refresh()
    test_log_isolation()


if __name__ == "__main__":
    main()
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILED: {FAILURES}")
        sys.exit(1)
    print("\nALL PASS")
