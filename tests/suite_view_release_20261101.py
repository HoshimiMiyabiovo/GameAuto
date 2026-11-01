# -*- coding: utf-8 -*-
"""重页面视图回收回归（2026-11-01）。

背景：QML 视图加载后一直常驻（`*EverLoaded` 一旦 true 就永远 active），
实测把 10 个页面切一遍 +142MB 且不回落。用户要求"动态加载/别太吃内存"，
于是给重页面加了「停止切页 N 秒后释放非当前页」的回收逻辑。

本套件锁定：
1. QML 能正常加载（改 Main.qml 后不报语法/引用错误）；
2. 延迟未到不释放、超时释放、切回能重建；
3. 任务运行中（bridge.isRunning）不释放，避免销毁正在轮询的页面；
4. viewReleaseDelayMs <= 0 时整体关闭回收（给频繁切页的人留退路）。

跑法：.venv\\Scripts\\python.exe tests\\suite_view_release_20261101.py
（脚本自己设 QT_QPA_PLATFORM=offscreen，不会弹窗）
"""

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT.parent))

from PySide6.QtCore import QObject, QUrl, qInstallMessageHandler  # noqa: E402
from PySide6.QtQml import QQmlApplicationEngine  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def build():
    """起一个离屏 GUI，返回 (app, engine, bridge, root)。"""
    qInstallMessageHandler(lambda mode, ctx, msg: None)  # 屏蔽字体等噪声
    app = QApplication.instance() or QApplication(sys.argv)
    from pyhuskarui.husapp import HusApp
    from game_auto.ui.bridge import UiBridge
    from game_auto.ui.theme import apply_tokens_to_engine

    engine = QQmlApplicationEngine()
    HusApp.initialize(engine)
    bridge = UiBridge()
    engine.rootContext().setContextProperty("bridge", bridge)
    apply_tokens_to_engine(engine)
    qml_dir = PROJECT_ROOT / "ui" / "qml"
    engine.addImportPath(str(qml_dir))
    engine.load(QUrl.fromLocalFile(str(qml_dir / "Main.qml")))
    roots = engine.rootObjects()
    return app, engine, bridge, (roots[0] if roots else None)


def pump(app, seconds):
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.005)


def main():
    app, engine, bridge, root = build()
    print("[1] Main.qml 加载")
    check("有根对象", root is not None, len(engine.rootObjects()))
    if root is None:
        print("FAILED: QML 没加载起来")
        return 1

    def ever():
        return bool(root.property("dailyEverLoaded"))

    print("[2] 延迟到了才释放")
    root.setProperty("viewReleaseDelayMs", 400)
    bridge.currentView = "daily"
    pump(app, 1.5)
    check("首次进入 daily 已加载", ever() is True, ever())

    bridge.currentView = "gacha"
    pump(app, 0.15)
    check("切走 0.15s（<400ms）仍保留", ever() is True, ever())

    pump(app, 0.6)
    check("切走 0.75s（>400ms）已释放", ever() is False, ever())

    print("[3] 切回能重建")
    bridge.currentView = "daily"
    pump(app, 1.5)
    check("重建后重新标记", ever() is True, ever())

    print("[4] 任务运行中不释放")
    bridge._is_running = True
    bridge.currentView = "gacha"
    pump(app, 1.0)
    check("isRunning=True 时保留", ever() is True, ever())
    bridge._is_running = False
    pump(app, 0.8)
    check("isRunning=False 后释放", ever() is False, ever())

    print("[5] delay<=0 关闭回收")
    root.setProperty("viewReleaseDelayMs", 0)
    bridge.currentView = "daily"
    pump(app, 3.0)   # 重建走异步加载，留足时间
    check("重新加载", ever() is True, ever())
    bridge.currentView = "gacha"
    pump(app, 1.5)
    check("关闭回收后不释放", ever() is True, ever())

    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} 项 → {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)   # Qt 退出时线程/引擎清理会卡住，直接走
