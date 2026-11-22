# -*- coding: utf-8 -*-
"""内置 ok-ww GUI 开关回归（2026-11-19）。

鸣潮专项页新增「内置 ok-ww」开关：直接拉起 data/okww 内置副本的
pythonw main.py 引擎（跳过 ok-ww.exe 管理器，避免 UAC/自更新），关闭即结束。
本套件覆盖：
1. 安装根解析：只认项目 data/ 下的副本，配置 > 扫描，目录名带点号不误判；
2. 进程识别：只认该安装自带 pythonw + main.py，避开一条龙 python.exe runner；
3. PYTHONPATH 剥离：项目 src 不进 ok-ww 内嵌 python 环境；
4. bridge/QML 接线与离屏实例化（开关状态同步、槽调用）。
"""

import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

# 沙箱兼容：tempfile 目录创建改默认权限（0700 受保护 DACL 在沙箱里写不进）
from sandbox_temp import install as _install_sandbox_temp

_install_sandbox_temp()

from game_auto.ui.bridge_views.tool_manager import (  # noqa: E402
    _is_builtin_okww_process,
    _strip_project_src,
    resolve_builtin_okww_paths,
)

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def _make_layout(root: Path) -> Path:
    app = root / "data" / "apps" / "ok-ww"
    (app / "python").mkdir(parents=True)
    (app / "working").mkdir(parents=True)
    (app / "python" / "pythonw.exe").write_bytes(b"x")
    (app / "working" / "main.py").write_text("print('x')\n", encoding="utf-8")
    return root


def test_layout_and_resolution():
    print("[1] 安装根解析")
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp) / "data"
        builtin = _make_layout(data_dir / "okww" / "ok-ww-v9.9.9")
        src_tree = data_dir / "okww" / "gui-3.8.3-ws"
        src_tree.mkdir(parents=True)  # 没有 pyappify 布局 → 不应命中

        # 目录名带点号：解析必须返回自身，不能退到 data/okww
        paths = resolve_builtin_okww_paths({}, data_dir)
        check("扫描命中 ok-ww-v9.9.9", paths and paths["root"] == builtin.resolve(), paths)
        check("pythonw/main.py 指对",
              paths and paths["pythonw"].is_file() and paths["main_py"].is_file())

        # 无布局 → None
        check("无安装返回 None",
              resolve_builtin_okww_paths({}, Path(tmp) / "nowhere") is None)

        # 配置优先于扫描
        preferred = _make_layout(data_dir / "okww" / "custom-copy")
        cfg = {"_discovery": {"script_exe_path": str(preferred / "ok-ww.exe")}}
        paths = resolve_builtin_okww_paths(cfg, data_dir)
        check("配置指向的副本优先", paths and paths["root"] == preferred.resolve(), paths)

        # 项目 data/ 之外的配置路径不认（外置安装不是"内置"）
        outside = _make_layout(Path(tmp) / "outside" / "ok-ww")
        paths = resolve_builtin_okww_paths(
            {"_discovery": {"script_exe_path": str(outside / "ok-ww.exe")}}, data_dir)
        check("外置安装被忽略", paths and paths["root"] == builtin.resolve(), paths)

        # module_mode.runtime_root → parents[3] 推回安装根
        cfg = {"module_mode": {"runtime_root": str(builtin / "data" / "apps" / "ok-ww" / "working")}}
        check("runtime_root 可回推", resolve_builtin_okww_paths(cfg, data_dir) is not None)


def test_process_filter():
    print("[2] 进程识别")
    pythonw = Path(r"E:\proj\data\okww\ok-ww-v3.8.3\data\apps\ok-ww\python\pythonw.exe")
    check("pythonw + main.py 命中",
          _is_builtin_okww_process("pythonw.exe", str(pythonw), "pythonw.exe main.py", pythonw))
    check("python.exe runner 不命中",
          not _is_builtin_okww_process("python.exe", str(pythonw), "python.exe main.py", pythonw))
    check("runner.py 不命中",
          not _is_builtin_okww_process("pythonw.exe", str(pythonw), "pythonw.exe runner.py", pythonw))
    check("别的 pythonw 不命中",
          not _is_builtin_okww_process("pythonw.exe", r"D:\other\pythonw.exe", "main.py", pythonw))
    check("空 exe 不命中",
          not _is_builtin_okww_process("pythonw.exe", "", "pythonw.exe main.py", pythonw))


def test_pythonpath_strip():
    print("[3] PYTHONPATH 剥离")
    src = r"E:\GithubProject\game_auto\src"
    check("单独项目 src 清空", _strip_project_src(src, src) == "")
    check("保留无关路径", _strip_project_src(src + os.pathsep + r"D:\libs", src) == r"D:\libs")
    check("正斜杠也认",
          _strip_project_src("E:/GithubProject/game_auto/src;D:/libs", src) == "D:/libs")


def test_wiring():
    print("[4] bridge / QML 接线")
    tm_src = (PROJECT_ROOT / "src" / "game_auto" / "ui" / "bridge_views" / "tool_manager.py").read_text(
        encoding="utf-8")
    check("状态槽声明 result",
          "@Slot(result=str)\n    def getBuiltinOkwwState" in tm_src)
    check("开关槽声明 result",
          "@Slot(bool, result=bool)\n    def setBuiltinOkwwEnabled" in tm_src)
    check("一条龙运行中拦截", "_is_running" in tm_src and "暂不能启停内置 ok-ww" in tm_src)
    bridge_src = (PROJECT_ROOT / "src" / "game_auto" / "ui" / "bridge.py").read_text(encoding="utf-8")
    check("busy 状态已初始化", "self._okww_gui_busy = False" in bridge_src)
    qml_src = (PROJECT_ROOT / "src" / "game_auto" / "ui" / "qml" / "views" / "WuwaView.qml").read_text(
        encoding="utf-8")
    check("QML 使用 HusSwitch", "HusSwitch {" in qml_src)
    check("QML 调状态槽", "bridge.getBuiltinOkwwState()" in qml_src)
    check("QML 调启停槽", "bridge.setBuiltinOkwwEnabled(" in qml_src)
    check("QML 轮询定时器", "builtinOkwwTimer" in qml_src)


def test_qml_load():
    print("[5] 鸣潮专项页离屏加载（stub bridge）")
    from PySide6.QtCore import Property, QObject, QTimer, QUrl, Signal, Slot
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQml import QQmlApplicationEngine, QQmlComponent, QQmlEngine, QQmlExpression
    from pyhuskarui.husapp import HusApp

    state = {"json": json.dumps({"installed": True, "running": False, "pid": 0,
                                 "root": r"E:/x/ok-ww-v3.8.3", "busy": False})}
    calls = {"set": []}

    class _Bridge(QObject):
        currentViewChanged = Signal(str)
        isRunningChanged = Signal(bool)
        wuwaDailyChanged = Signal(str)
        wuwaGachaChanged = Signal(str)
        credentialsChanged = Signal(str)

        @Property(str, notify=currentViewChanged)
        def currentView(self):
            return "wuwa"

        @Property(bool, notify=isRunningChanged)
        def isRunning(self):
            return False

        @Slot()
        def loadWuwaGacha(self):
            pass

        @Slot()
        def loadCredentials(self):
            pass

        @Slot()
        def loadWuwaDailyCache(self):
            pass

        @Slot()
        def refreshWuwaDaily(self):
            pass

        @Slot(str, str, bool)
        def runWuwaKujiequCodeLogin(self, a, b, c):
            pass

        @Slot()
        def runWuwaKujiequAdbGrab(self):
            pass

        @Slot()
        def runWuwaKujiequSign(self):
            pass

        @Slot(str, str)
        def saveCredential(self, a, b):
            pass

        @Slot(str, str, bool)
        def saveCredentialBool(self, a, b, c):
            pass

        @Slot(str)
        def importWuwaGachaFromJson(self, a):
            pass

        @Slot(result=str)
        def getBuiltinOkwwState(self):
            return state["json"]

        @Slot(bool, result=bool)
        def setBuiltinOkwwEnabled(self, on):
            calls["set"].append(on)
            return True

    app = QGuiApplication.instance() or QGuiApplication([])
    engine = QQmlApplicationEngine()
    HusApp.initialize(engine)
    bridge = _Bridge()
    bridge.setParent(engine)
    engine.rootContext().setContextProperty("bridge", bridge)
    comp = QQmlComponent(engine)
    comp.loadUrl(QUrl.fromLocalFile(
        str(PROJECT_ROOT / "src" / "game_auto" / "ui" / "qml" / "views" / "WuwaView.qml")))
    obj = comp.create()
    errors = [e.toString() for e in comp.errors()]
    check("QML 无语法错误", obj is not None and not errors, errors[:3])
    if obj is None:
        return

    ctx = QQmlEngine.contextForObject(obj)

    def ev(expr):
        value = QQmlExpression(ctx, obj, expr).evaluate()
        return value[0] if isinstance(value, tuple) else value

    def inspect():
        try:
            check("初始未运行", ev("okwwGuiInstalled") is True and ev("okwwGuiRunning") is False)
            check("开关初始关闭", ev("builtinOkwwSwitch.checked") is False)
            state["json"] = json.dumps({"installed": True, "running": True, "pid": 4242,
                                        "root": r"E:/x/ok-ww-v3.8.3", "busy": False})
            ev("refreshBuiltinOkww()")
            check("状态同步到开关", ev("builtinOkwwSwitch.checked") is True)
            ev("toggleBuiltinOkww(false)")
            check("开关下发到桥接", calls["set"] == [False], calls["set"])
        finally:
            app.quit()

    QTimer.singleShot(700, inspect)
    app.exec()


def main():
    test_layout_and_resolution()
    test_process_filter()
    test_pythonpath_strip()
    test_wiring()
    test_qml_load()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
