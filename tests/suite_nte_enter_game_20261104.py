"""异环「开始游戏 / 进入游戏」认错按钮 + 窗口句柄失效不重接管 回归（2026-11-04）。

背景（11-04 实锤，三次失败）：
- 10:10 / 14:26 / 20:35 三轮 NTE_MONTHLY 全卡在步骤2「等待开始游戏」，
  10:11 那次空转 60 分钟后失败（失败记录 #168）。
- **失败截图 `20261104_111109_NTE_MONTHLY_等待开始游戏.jpg` 就是异环游戏本体的
  登录页**，底部中间明明白白写着「进入游戏」——而流程在等**启动器**上的
  「开始游戏」。用户 2026-11-04 给的真实截图（data/artifacts/
  ref_nte_enter_game_20261104.png）也是同一屏。
- 同时窗口句柄在游戏重建窗口后失效：SetForegroundWindow 报「无效的窗口句柄
  (1400)」，_ocr_scan 拒绝回退全屏 → 「扫描了 0 个区域」→ 又空转 11 分钟。

实测（用用户真实截图）：
- 模板 `异环-进入游戏.png` 在真实截图上匹配分 **0.961 @scale1.0** → 模板没问题；
- `异环-开始游戏.png` 只有 0.430、`异环-确定.png` 只有 0.551 → 这两个是
  启动器上的元素，游戏本体里根本没有；
- RapidOCR 能读出「进入游戏」conf 0.72 @ (960, 931) → OCR 兜底可用。

修法：
  1. 步骤2「等待开始游戏」target_text 加「进入游戏」（模板命中启动器按钮，
     OCR 兜底命中游戏本体按钮，任一即可）；
  2. 步骤3「开始游戏」/步骤4「确定」改 optional_if_missing（当前版本没有这两个
     元素，原来白等 180s×4 + 45s×4 再失败）；
  3. 通用：窗口句柄**真死**时按流程声明的进程名重新接管窗口
     （`_window_is_dead` / `_recover_flow_window`）。

跑法：.venv\\Scripts\\python.exe tests\\suite_nte_enter_game_20261104.py
"""

import json
import sys
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from game_auto.runtime import foreground_flow_runner as ffr  # noqa: E402

FAILURES = []
REF_IMG = PROJECT_ROOT / "data" / "artifacts" / "ref_nte_enter_game_20261104.png"


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def test_step_config():
    print("[1] 异环步骤配置")
    for fname in ("flow_configs.json", "flow_configs.example.json"):
        path = PROJECT_ROOT / fname
        if not path.is_file():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        for flow_key in ("NTE", "NTE_MONTHLY"):
            flow = data.get(flow_key)
            if not isinstance(flow, dict):
                check(f"{fname}/{flow_key} 存在", False, flow_key)
                continue
            steps = {str(s.get("name", "")): s for s in flow.get("steps", []) if isinstance(s, dict)}
            wait = steps.get("等待开始游戏", {})
            check(
                f"{fname}/{flow_key} 等待开始游戏 目标含「进入游戏」",
                "进入游戏" in str(wait.get("target_text", "")),
                wait.get("target_text"),
            )
            check(
                f"{fname}/{flow_key} 等待开始游戏 仍保留「开始游戏」",
                "开始游戏" in str(wait.get("target_text", "")),
                wait.get("target_text"),
            )
            start = steps.get("开始游戏", {})
            check(
                f"{fname}/{flow_key} 开始游戏 改可选",
                start.get("optional_if_missing") is True,
                start.get("optional_if_missing"),
            )
            check(
                f"{fname}/{flow_key} 开始游戏 超时压到 60s 内",
                int(start.get("timeout_sec", 999)) <= 60,
                start.get("timeout_sec"),
            )
            ok_step = steps.get("确定", {})
            check(
                f"{fname}/{flow_key} 确定 改可选",
                ok_step.get("optional_if_missing") is True,
                ok_step.get("optional_if_missing"),
            )
            enter = steps.get("进入游戏", {})
            check(
                f"{fname}/{flow_key} 仍有「进入游戏」点击步",
                "异环-进入游戏.png" in str(enter.get("template", "")),
                enter.get("template"),
            )


def test_window_is_dead():
    print("[2] _window_is_dead 判定")
    with mock.patch.object(ffr.win32gui, "IsWindow", lambda h: False):
        check("失效句柄判为 dead", ffr._window_is_dead(1234) is True)
    with mock.patch.object(ffr.win32gui, "IsWindow", lambda h: True):
        check("有效句柄判为存活", ffr._window_is_dead(1234) is False)
    check("空句柄判为 dead", ffr._window_is_dead(None) is True)


def test_recover_flow_window():
    print("[3] _recover_flow_window 句柄失效后按进程名重接管")
    logger = mock.Mock()
    # 句柄还活着 → 不该动
    with mock.patch.object(ffr, "_window_is_dead", lambda h: False):
        out = ffr._recover_flow_window(1, 1234, ["neverness"], ["HTGame.exe"], logger, "NTE")
    check("句柄还活着时不恢复", out == (None, None), out)

    # 句柄死了 → 按进程名找到新窗口
    logger.reset_mock()
    with mock.patch.object(ffr, "_window_is_dead", lambda h: True), \
            mock.patch.object(ffr, "_find_window_by_filters",
                              lambda **kw: (9999, 4321)):
        out = ffr._recover_flow_window(1, 1234, ["neverness"], ["HTGame.exe"], logger, "NTE")
    check("按进程名重接管到新窗口", out == (9999, 4321), out)
    check("有日志留痕", logger.info.called)

    # 句柄死了但找不到别的窗口 → 不报错，返回空
    with mock.patch.object(ffr, "_window_is_dead", lambda h: True), \
            mock.patch.object(ffr, "_find_window_by_filters", lambda **kw: None):
        out = ffr._recover_flow_window(1, 1234, ["neverness"], ["HTGame.exe"], logger, "NTE")
    check("找不到就返回空", out == (None, None), out)

    # 没声明进程名/标题 → 不做恢复
    with mock.patch.object(ffr, "_window_is_dead", lambda h: True):
        out = ffr._recover_flow_window(1, 1234, [], [], logger, "NTE")
    check("无进程名线索时不恢复", out == (None, None), out)


def test_template_matches_real_screenshot():
    print("[4] 用真实截图验模板（缺参考图则跳过）")
    if not REF_IMG.is_file():
        print("  SKIP 缺参考图 data/artifacts/ref_nte_enter_game_20261104.png")
        return
    try:
        import cv2
        import numpy as np
        from PIL import Image
    except Exception as exc:  # noqa: BLE001 - 缺依赖就跳过，不误报失败
        print(f"  SKIP 依赖不可用: {exc}")
        return

    img = cv2.cvtColor(np.asarray(Image.open(REF_IMG).convert("RGB")), cv2.COLOR_RGB2BGR)

    def _best(template_name):
        tpl = Image.open(PROJECT_ROOT / "assets" / "templates" / template_name).convert("RGB")
        t = cv2.cvtColor(np.asarray(tpl), cv2.COLOR_RGB2BGR)
        best = 0.0
        for sc in (1.0, 1.25, 0.8, 1.5, 0.67):
            t2 = cv2.resize(t, None, fx=sc, fy=sc) if sc != 1.0 else t
            if t2.shape[0] > img.shape[0] or t2.shape[1] > img.shape[1]:
                continue
            best = max(best, float(cv2.matchTemplate(img, t2, cv2.TM_CCOEFF_NORMED).max()))
        return best

    enter = _best("异环-进入游戏.png")
    check("「进入游戏」模板在真实截图上匹配分 ≥0.9", enter >= 0.9, f"{enter:.3f}")
    start = _best("异环-开始游戏.png")
    check("「开始游戏」模板在游戏本体截图上不命中（<0.7）", start < 0.7, f"{start:.3f}")


def test_ocr_reads_enter_game():
    print("[5] 真实截图上 OCR 能读出「进入游戏」")
    if not REF_IMG.is_file():
        print("  SKIP 缺参考图")
        return
    try:
        import numpy as np
        from PIL import Image
        from game_auto.runtime.ocr_click_tester import _get_rapidocr_engine
    except Exception as exc:  # noqa: BLE001 - 缺依赖就跳过
        print(f"  SKIP 依赖不可用: {exc}")
        return
    engine = _get_rapidocr_engine()
    if not engine:
        print("  SKIP RapidOCR 不可用")
        return
    img = Image.open(REF_IMG).convert("RGB")
    result, _el = engine(np.asarray(img))
    texts = [(str(t).replace(" ", ""), float(c)) for _box, t, c in (result or [])]
    hit = [t for t in texts if "进入游戏" in t[0]]
    check("OCR 读到「进入游戏」", bool(hit), texts[:12])
    if hit:
        check("置信度 ≥0.6", hit[0][1] >= 0.6, hit)


def main():
    test_step_config()
    test_window_is_dead()
    test_recover_flow_window()
    test_template_matches_real_screenshot()
    test_ocr_reads_enter_game()

    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
