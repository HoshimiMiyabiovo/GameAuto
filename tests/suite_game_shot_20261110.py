"""全局热键游戏截图回归（2026-11-10）。

纯逻辑：打桩抓屏 / 前台窗口 / 落盘目录，不碰真实桌面、不响提示音、不注册热键。
跑法：.venv\\Scripts\\python.exe tests\\suite_game_shot_20261110.py
"""

import sys
import tempfile
from pathlib import Path
from unittest import mock

from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from game_auto.services import game_shot_service as gss  # noqa: E402
from game_auto.settings import manager  # noqa: E402
from game_auto.settings.defaults import DEFAULT_GAME_SHOT  # noqa: E402
from game_auto.settings.paths import GAME_SHOT_DIR  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def _service(tmp_dir, **overrides):
    cfg = {
        "enabled": True,
        "capture": "window",
        "format": "png",
        "sound": False,
        "toast": True,
        "min_interval_sec": 0.0,
    }
    cfg.update(overrides)
    events = []
    svc = gss.GameShotService(
        notify=lambda message, color="#2f7d4a": events.append((message, color)),
        config=cfg,
        shot_dir=tmp_dir,
    )
    return svc, events


def _patched_capture(svc, img=None, front=(123, "原神", "GenshinImpact.exe"), region=(0, 0, 320, 180)):
    """用假帧驱动一次真实 capture() 落盘流程。"""
    img = img or Image.new("RGB", (320, 180), (12, 34, 56))
    with mock.patch.object(gss, "_front_window_info", return_value=front), \
         mock.patch.object(gss, "_window_region", return_value=region), \
         mock.patch("game_auto.runtime.ocr_click_tester.grab_screen", return_value=img) as grab:
        path = svc.capture()
        return path, grab


def test_hotkey_parsing():
    print("[1] 热键写法 → pynput 组合")
    check("ctrl+alt+s", gss.to_pynput_hotkey("ctrl+alt+s") == "<ctrl>+<alt>+s")
    check("win+shift+f12", gss.to_pynput_hotkey("win+shift+f12") == "<cmd>+<shift>+<f12>")
    check("已是 pynput 形态", gss.to_pynput_hotkey("<ctrl>+<alt>+p") == "<ctrl>+<alt>+p")
    check("control/super 别名", gss.to_pynput_hotkey("control+super+k") == "<ctrl>+<cmd>+k")
    for bad in ("", "ctrl+alt", "ctrl+alt+abc", "ctrl+alt+?", "ctrl+alt+a+b"):
        try:
            gss.to_pynput_hotkey(bad)
            check(f"非法 {bad!r} 应报错", False, gss.to_pynput_hotkey(bad))
        except ValueError:
            check(f"非法 {bad!r} 应报错", True)


def test_window_region():
    print("[2] GetWindowRect 单位换算与越界退回全屏")
    with mock.patch.object(gss.win32gui, "GetWindowRect", return_value=(100, 200, 900, 700)):
        check("(l,t,r,b) → (l,t,w,h)", gss._window_region(1) == (100, 200, 800, 500))
        check("超出主屏 → None", gss._window_region(1, screen_size=(800, 600)) is None)
        check("主屏内保留", gss._window_region(1, screen_size=(1920, 1080)) == (100, 200, 800, 500))
    with mock.patch.object(gss.win32gui, "GetWindowRect", return_value=(-100, 0, 500, 400)):
        check("副屏负坐标 → None", gss._window_region(1, screen_size=(1920, 1080)) is None)
    with mock.patch.object(gss.win32gui, "GetWindowRect", return_value=(0, 0, 0, 0)):
        check("最小化零尺寸 → None", gss._window_region(1) is None)


def test_capture_and_save():
    print("[3] 截图落盘 / 命名 / toast / 缩放")
    with tempfile.TemporaryDirectory() as td:
        svc, events = _service(td)
        path, grab = _patched_capture(svc)
        check("返回存在的图片路径", path is not None and Path(path).exists(), path)
        check("文件名带进程名", path is not None and "GenshinImpact" in path.name, path)
        check("PNG 扩展名", path is not None and path.suffix == ".png", path)
        check("图片尺寸正确", path is not None and Image.open(path).size == (320, 180))
        check("grab_screen 收到窗口 region",
              grab.call_args.kwargs.get("region") == (0, 0, 320, 180), grab.call_args)
        check("toast 绿色且带文件名",
              any("截图已保存" in m and c == "#2f7d4a" for m, c in events), events)

    with tempfile.TemporaryDirectory() as td:
        svc, events = _service(td, capture="screen")
        path, grab = _patched_capture(svc)
        check("整屏模式不传 region", grab.call_args.kwargs.get("region") is None, grab.call_args)

    with tempfile.TemporaryDirectory() as td:
        svc, _ = _service(td, format="jpg", jpeg_quality=80)
        path, _ = _patched_capture(svc)
        check("JPEG 分支", path is not None and path.suffix == ".jpg" and Image.open(path).format == "JPEG")

    with tempfile.TemporaryDirectory() as td:
        svc, _ = _service(td, max_width=160)
        path, _ = _patched_capture(svc)
        check("max_width 等比缩放", path is not None and Image.open(path).width == 160,
              Image.open(path).size if path else None)


def test_debounce_and_errors():
    print("[4] 防抖 / 失败不吞异常 / 不抢前台")
    with tempfile.TemporaryDirectory() as td:
        svc, _ = _service(td, min_interval_sec=30.0)
        p1, _ = _patched_capture(svc)
        p2, _ = _patched_capture(svc)
        check("第一次成功", p1 is not None)
        check("30s 内连按被防抖", p2 is None and len(list(Path(td).glob("*.png"))) == 1,
              list(Path(td).glob("*.png")))

    with tempfile.TemporaryDirectory() as td:
        svc, events = _service(td)
        with mock.patch("game_auto.runtime.ocr_click_tester.grab_screen", side_effect=RuntimeError("WGC 挂了")):
            path = svc.capture()
        check("抓屏异常不抛出", path is None)
        check("失败发红色 toast", any(c == "#b04b44" for _, c in events), events)

    svc = gss.GameShotService(config={"enabled": False}, shot_dir="unused")
    check("disabled 不启动", svc.start() is False)
    check("未启动 stop 幂等", svc.stop() is None)


def test_static_wiring():
    print("[5] 接线与约束静态断言")
    import ast

    src = (PROJECT_ROOT / "src/game_auto/services/game_shot_service.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    # 用 AST 查"有没有真的调用"，别拿注释/文档字符串里的反例说明当违规
    called = {
        (n.func.id if isinstance(n.func, ast.Name) else n.func.attr)
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and (isinstance(n.func, ast.Name) or isinstance(n.func, ast.Attribute))
    }
    imported = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    for forbidden in ("SetForegroundWindow", "BringWindowToTop", "SetWindowPos", "ShowWindow"):
        check(f"不调用 {forbidden}", forbidden not in called)
    check("不 import PySide6", not any(m.startswith("PySide6") for m in imported))
    check("启动即预热 WGC", "_warmup" in src and "GameShotWarmup" in src)
    check("回调只发 notify", "self._notify(" in src)
    check("走 manager 读配置", "cfg_manager.get(" in src)

    gui = (PROJECT_ROOT / "src/game_auto/app/gui.py").read_text(encoding="utf-8")
    check("GUI 创建服务", "GameShotService(" in gui)
    check("GUI 延迟启动", "_shot_starter.start(1500)" in gui)
    check("退出注销热键", "game_shot.stop()" in gui)
    check("notify 走 emit_event", 'bridge.emit_event(' in gui)
    check("保存目录在 data/artifacts", GAME_SHOT_DIR.parent.name == "artifacts")
    check("默认段热键", DEFAULT_GAME_SHOT["hotkey"] == "ctrl+alt+s")
    check("manager 合并默认段", "game_shot" in manager._default_config())


def main():
    test_hotkey_parsing()
    test_window_region()
    test_capture_and_save()
    test_debounce_and_errors()
    test_static_wiring()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
