"""白荆回廊启动器「开始游戏」定位范围回归（2026-11-02）。

背景（2026-11-02 两次失败 #145/#146）：
    跑日志的 cmd 控制台把白荆启动器整个盖住 → 模板「白荆回廊-开始游戏.png」
    在屏幕上根本不存在（模板连续 3 次定位失败，这本身是对的）→ 兜底的全屏 OCR
    读到了**控制台里自己的日志行**「[白荆回廊] 启动: 点击启动器「开始游戏」按钮」
    （RapidOCR conf 0.80，坐标 (918, 976) / (827, 976)）→ 点击落在控制台日志上
    → 启动器没被点到 → 游戏本体进程 GateMK-Win64-Shipping.exe 300s 起不来。
    防护网也漏了：[OCR防护] 硬拦截只剔「带日志特征串」的条目（GameAutomation:xxx
    / Traceback），那行「开始游戏」不含特征串，反而被保留下来。

修法：点「开始游戏」前先定位启动器窗口（Seed_OB_Prod.exe）并置顶，之后模板匹配
    与 OCR 全部限定在该窗口矩形内；命中点二次校验必须落在窗口内；窗口几乎被遮挡
    时不识别；拿不到目标窗口就**拒绝全屏识别**直接失败。

覆盖：
    1. _point_in_window：窗内/窗外/无窗口/窗口失效
    2. _find_and_click：拒绝全屏（hwnd=None 直接失败，不点不识别）
    3. _find_and_click：识别被限定在窗口矩形内（hwnd + region 都传下去）
    4. _find_and_click：窗外命中被丢弃（控制台里的同名文字点不到）
    5. _find_and_click：窗口几乎被遮挡 → 置顶且本轮不识别
    6. _find_and_click：窗口不可用（已关闭/最小化）→ 跳过识别
    7. _wait_process：超时前触发一次诊断回调（on_wait）
    8. _diagnose_launcher_state：按钮仍在原位 → 判定"点击未生效"
    9. run_baijing_ingame_flow：定位不到启动器窗口 → 不识别不点击，直接失败取证
    10. 静态断言：调用点确实把启动器窗口句柄传了进去

跑法：.venv\\Scripts\\python.exe tests\\suite_baijing_launcher_scope_20261102.py
"""

import contextlib
import sys
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT.parent))

import game_auto.runtime.baijing_flow as bf  # noqa: E402

LAUNCHER_HWND = 1234
# 启动器窗口矩形（left, top, width, height）
RECT = (100, 200, 400, 300)
# 窗口内一点
INSIDE = (250, 300)
# 控制台里那行「开始游戏」日志的位置（窗外，正是 2026-11-02 事故里的落点）
OUTSIDE = (918, 976)

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def _logger():
    import logging

    logger = logging.getLogger("test_baijing_launcher_scope")
    logger.setLevel(logging.CRITICAL)
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


class _Recorder:
    """记录调用参数的替身；结果用完后重复最后一个（模拟"一直命中同一个点"）。"""

    def __init__(self, results=None):
        self.calls = []
        self.results = list(results or [])

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if not self.results:
            return None
        if len(self.results) > 1:
            return self.results.pop(0)
        return self.results[0]


@contextlib.contextmanager
def _patched(**overrides):
    """给 _find_and_click 套上的一组 mock，yield 出各记录器供断言用。"""
    recorders = {
        "_focus_game_window": _Recorder(),
        "ocr_and_template_match": _Recorder(),
        "_pyautogui_click_guarded": _Recorder(),
    }
    base = {
        "_window_region": lambda hwnd: RECT,
        "_window_obscured_ratio": lambda hwnd: 0.0,
        "_point_covered_by_other_window": lambda point, hwnd: False,
        "_focus_game_window": recorders["_focus_game_window"],
        "ocr_and_template_match": recorders["ocr_and_template_match"],
        "_pyautogui_click_guarded": recorders["_pyautogui_click_guarded"],
    }
    base.update(overrides)
    with contextlib.ExitStack() as stack:
        for name, value in base.items():
            stack.enter_context(mock.patch.object(bf, name, value))
        stack.enter_context(mock.patch.object(bf.time, "sleep", lambda *_: None))
        yield recorders


def test_point_in_window():
    print("[1] _point_in_window 命中点归属判定")
    with mock.patch.object(bf, "_window_region", lambda hwnd: RECT):
        check("窗内点判定为 True", bf._point_in_window(INSIDE, LAUNCHER_HWND) is True)
        check("窗外点判定为 False", bf._point_in_window(OUTSIDE, LAUNCHER_HWND) is False)
        check("无窗口时不限制", bf._point_in_window(OUTSIDE, None) is True)
    with mock.patch.object(bf, "_window_region", lambda hwnd: None):
        check("窗口失效时判定为 False", bf._point_in_window(INSIDE, LAUNCHER_HWND) is False)


def test_refuse_fullscreen():
    print("[2] 没有目标窗口时拒绝全屏识别")
    ocr = _Recorder([(INSIDE, "ocr")])
    click = _Recorder()
    with _patched(**{"ocr_and_template_match": ocr, "_pyautogui_click_guarded": click}):
        ok = bf._find_and_click(_logger(), "launcher_start", "开始游戏", timeout=5, hwnd=None)
    check("hwnd=None 时直接失败", ok is False)
    check("hwnd=None 时不做任何识别", not ocr.calls, ocr.calls)
    check("hwnd=None 时不点任何位置", not click.calls, click.calls)


def test_scope_to_window():
    print("[3] 识别被限定在启动器窗口矩形内")
    ocr = _Recorder([(INSIDE, "template")])
    click = _Recorder()
    with _patched(**{"ocr_and_template_match": ocr, "_pyautogui_click_guarded": click}):
        ok = bf._find_and_click(
            _logger(), "launcher_start", "开始游戏", timeout=5, hwnd=LAUNCHER_HWND
        )
    check("识别成功", ok is True)
    check("识别被调用", len(ocr.calls) == 1, ocr.calls)
    if ocr.calls:
        _args, kwargs = ocr.calls[0]
        check("传入目标窗口句柄", kwargs.get("hwnd") == LAUNCHER_HWND, kwargs)
        check("传入窗口矩形作为识别区域", kwargs.get("region") == RECT, kwargs)
    check("点击落在窗内按钮位置", click.calls and click.calls[0][0][:2] == INSIDE, click.calls)


def test_discard_outside_hit():
    print("[4] 窗外命中（控制台同名文字）被丢弃")
    ocr = _Recorder([(OUTSIDE, "ocr"), (INSIDE, "ocr")])
    click = _Recorder()
    with _patched(**{"ocr_and_template_match": ocr, "_pyautogui_click_guarded": click}):
        ok = bf._find_and_click(
            _logger(), "launcher_start", "开始游戏", timeout=5, hwnd=LAUNCHER_HWND
        )
    check("第二次命中窗内才算成功", ok is True)
    check("只点了一次（窗外那次被丢弃）", len(click.calls) == 1, click.calls)
    if click.calls:
        check("点击的是窗内坐标", click.calls[0][0][:2] == INSIDE, click.calls[0])


def test_covered_point_dropped():
    print("[5] 命中点被别的窗口压住 → 抬高一轮再判，仍压住就丢弃")
    ocr = _Recorder([(INSIDE, "ocr"), (INSIDE, "ocr")])
    click = _Recorder()
    focus = _Recorder()
    covered = {"value": True}

    def _covered(point, hwnd):
        return covered["value"]

    with _patched(**{
        "ocr_and_template_match": ocr,
        "_pyautogui_click_guarded": click,
        "_focus_game_window": focus,
        "_point_covered_by_other_window": _covered,
    }):
        ok = bf._find_and_click(
            _logger(), "launcher_start", "开始游戏", timeout=1, hwnd=LAUNCHER_HWND
        )
    check("命中点一直被压住 → 判定失败", ok is False)
    check("被压住期间没有点击", not click.calls, click.calls)
    check("被压住期间尝试抬高窗口", len(focus.calls) >= 1, focus.calls)

    # 抬高成功（不再被压住）时应当正常点下去
    ocr2 = _Recorder([(INSIDE, "ocr")])
    click2 = _Recorder()
    with _patched(**{
        "ocr_and_template_match": ocr2,
        "_pyautogui_click_guarded": click2,
        "_point_covered_by_other_window": lambda point, hwnd: False,
    }):
        ok2 = bf._find_and_click(
            _logger(), "launcher_start", "开始游戏", timeout=5, hwnd=LAUNCHER_HWND
        )
    check("不再被压住时正常点击", ok2 is True and len(click2.calls) == 1, (ok2, click2.calls))


def test_covered_point_helper():
    print("[5.1] _point_covered_by_other_window 判定")
    with mock.patch.object(bf.win32gui, "WindowFromPoint", lambda pt: 9999), \
            mock.patch.object(bf.win32gui, "GetParent", lambda h: None):
        check("点处是别的窗口 → 判为被盖住",
              bf._point_covered_by_other_window(INSIDE, LAUNCHER_HWND) is True)
    with mock.patch.object(bf.win32gui, "WindowFromPoint", lambda pt: LAUNCHER_HWND):
        check("点处就是目标窗口 → 不算被盖住",
              bf._point_covered_by_other_window(INSIDE, LAUNCHER_HWND) is False)
    with mock.patch.object(bf.win32gui, "WindowFromPoint", lambda pt: 8888), \
            mock.patch.object(bf.win32gui, "GetParent", lambda h: LAUNCHER_HWND):
        check("点处是目标窗口的子窗口 → 不算被盖住",
              bf._point_covered_by_other_window(INSIDE, LAUNCHER_HWND) is False)


def test_occluded_window():
    print("[5] 窗口几乎被遮挡 → 置顶且本轮不识别")
    ocr = _Recorder([(INSIDE, "ocr")])
    focus = _Recorder()
    with _patched(**{
        "ocr_and_template_match": ocr,
        "_focus_game_window": focus,
        "_window_obscured_ratio": lambda hwnd: 0.95,
    }):
        ok = bf._find_and_click(
            _logger(), "launcher_start", "开始游戏", timeout=1, hwnd=LAUNCHER_HWND
        )
    check("被完全遮挡时判定失败", ok is False)
    check("被遮挡期间不识别", not ocr.calls, ocr.calls)
    check("被遮挡期间尝试置顶", len(focus.calls) >= 1, focus.calls)


def test_window_unavailable():
    print("[6] 窗口不可用（已关闭/最小化）→ 跳过识别")
    ocr = _Recorder([(INSIDE, "ocr")])
    with _patched(**{
        "ocr_and_template_match": ocr,
        "_window_region": lambda hwnd: None,
    }):
        ok = bf._find_and_click(
            _logger(), "launcher_start", "开始游戏", timeout=1, hwnd=LAUNCHER_HWND
        )
    check("窗口不可用时判定失败", ok is False)
    check("窗口不可用时不识别", not ocr.calls, ocr.calls)


def test_wait_process_diagnosis():
    print("[7] _wait_process 超时前触发诊断回调")
    calls = []

    class _Proc:
        def __init__(self, name, pid):
            self.info = {"pid": pid, "name": name}

    iter_results = [[], [_Proc(bf.GAME_PROCESS, 999)]]

    def _fake_iter(_attrs):
        return iter_results.pop(0) if iter_results else []

    with mock.patch.object(bf.psutil, "process_iter", _fake_iter), \
            mock.patch.object(bf, "PROCESS_WAIT_DIAGNOSIS_SEC", 0.0), \
            mock.patch.object(bf.time, "sleep", lambda *_: None):
        pid = bf._wait_process(
            _logger(), bf.GAME_PROCESS, timeout=30, on_wait=lambda: calls.append(1)
        )
    check("进程出现后返回 PID", pid == 999, pid)
    check("等待期间触发过一次诊断", len(calls) == 1, calls)


def test_diagnose_launcher_state():
    print("[8] _diagnose_launcher_state 点击后诊断")
    logger = _logger()
    with mock.patch.object(bf.win32gui, "IsWindow", lambda h: True), \
            mock.patch.object(bf, "_window_region", lambda hwnd: RECT), \
            mock.patch.object(bf, "_window_obscured_ratio", lambda hwnd: 0.0):
        with mock.patch.object(bf, "_find_by_template", lambda *a, **k: (200, 250)):
            text = bf._diagnose_launcher_state(logger, LAUNCHER_HWND)
        check("按钮仍在原位 → 判定点击未生效", "点击未生效" in text, text)
        check("诊断里带出遮挡比例", "被遮挡 0%" in text, text)
        with mock.patch.object(bf, "_find_by_template", lambda *a, **k: None):
            text2 = bf._diagnose_launcher_state(logger, LAUNCHER_HWND)
        check("按钮消失 → 判定点击已生效", "点击已生效" in text2, text2)
    with mock.patch.object(bf.win32gui, "IsWindow", lambda h: False):
        text3 = bf._diagnose_launcher_state(logger, LAUNCHER_HWND)
    check("窗口消失有专门描述", "启动器窗口已消失" in text3, text3)


def test_flow_refuses_without_launcher_window():
    print("[9] 流程层：定位不到启动器窗口 → 不识别不点击")
    logger = _logger()
    ocr = _Recorder([(OUTSIDE, "ocr")])
    failures = []
    with mock.patch.object(bf, "_wait_launcher_window", lambda logger, timeout=30: None), \
            mock.patch.object(bf, "ocr_and_template_match", ocr), \
            mock.patch.object(bf, "_record_baijing_flow_failure",
                              lambda logger, step, error, hwnd=None: failures.append((step, error))):
        ok = bf.run_baijing_ingame_flow({}, logger)
    check("流程判定失败", ok is False)
    check("没有做任何全屏识别", not ocr.calls, ocr.calls)
    check("写入了失败取证", len(failures) == 1, failures)
    if failures:
        check("失败原因点明拒绝全屏识别", "拒绝全屏识别" in failures[0][1], failures[0])


def test_static_wiring():
    print("[10] 静态断言：调用点把启动器窗口传了进去")
    src = (PROJECT_ROOT / "runtime" / "baijing_flow.py").read_text(encoding="utf-8")
    check("流程里定位启动器窗口", "_wait_launcher_window(logger" in src)
    check("点击时传入 hwnd=launcher_hwnd", "hwnd=launcher_hwnd" in src)
    check("点击前调用了置顶", "_focus_game_window(logger, launcher_hwnd)" in src)
    check("失败取证带上启动器窗口", "hwnd=launcher_hwnd" in src)
    check("等待进程带诊断回调", "on_wait=_diagnose" in src)


def main():
    test_point_in_window()
    test_refuse_fullscreen()
    test_scope_to_window()
    test_discard_outside_hit()
    test_covered_point_dropped()
    test_covered_point_helper()
    test_occluded_window()
    test_window_unavailable()
    test_wait_process_diagnosis()
    test_diagnose_launcher_state()
    test_flow_refuses_without_launcher_window()
    test_static_wiring()

    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
