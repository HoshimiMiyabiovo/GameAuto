# -*- coding: utf-8 -*-
"""脚本管理页（自动下载安装）回归（2026-11-03）。

覆盖 services/script_tool_installer.py 与脚本管理视图：
1. 资产正则：ok-nte 的 full/body/setup 全部不命中；March7th 7z 优先于 zip；
2. zip-slip 防护：恶意成员路径拒绝解压；7z 成员名检查；
3. 7z 往返：py7zr 写微型包 → 解压 → 定位启动器（真格式，不造大文件）；
4. 安装编排（mock 下载/fetch）：全新安装与换血更新全流程、路径注册、
   临时目录清理；
5. 守卫：进程运行中拒绝、托管目录外安装拒绝、取消清理；
6. bridge/QML 接线源码检查 + ToolManagerView stub-bridge 离屏冒烟（进度渲染）。

跑法：.venv\\Scripts\\python.exe tests\\suite_tool_manager_20261103.py
"""

import hashlib
import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from game_auto.games.base import ScriptToolSpec  # noqa: E402
from game_auto.services import script_tool_installer as inst  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def test_pick_asset():
    print("[1] 资产正则")
    ok_nte_assets = [
        {"name": "ok-nte-win-x86_64-v1.4.23-full.zip", "size": 1, "url": "u1"},
        {"name": "ok-nte-win-x86_64-v1.4.23-body.zip", "size": 1, "url": "u2"},
        {"name": "ok-nte-win32-China-setup.exe", "size": 1, "url": "u3"},
        {"name": "ok-nte-win32.zip", "size": 2, "url": "u4"},
    ]
    picked = inst.pick_asset(ok_nte_assets, [r"^ok-nte-win32\.zip$"])
    check("ok-nte 精确命中在线包", picked and picked["url"] == "u4", picked)
    check("full/body/setup 不被命中",
          inst.pick_asset(ok_nte_assets, [r"^ok-nte-win32\.zip$"]) is not None
          and inst.pick_asset(ok_nte_assets, [r"^ok-nte-win32-v"]) is None)
    m7 = [
        {"name": "March7thAssistant_full.zip", "size": 1, "url": "z"},
        {"name": "March7thAssistant_full.7z", "size": 1, "url": "s"},
    ]
    check("March7th 7z 优先于 zip",
          inst.pick_asset(m7, [r"^March7thAssistant_full\.7z$",
                               r"^March7thAssistant_full\.zip$"])["url"] == "s")
    ef = [{"name": "ok-ef-win32-v2.0.10.zip", "size": 1, "url": "e"},
          {"name": "ok-ef-win32-China-setup-v2.0.10.exe", "size": 1, "url": "e2"}]
    check("ok-ef 带版本 zip 命中",
          inst.pick_asset(ef, [r"^ok-ef-win32-v[\d.]+\.zip$"])["name"]
          == "ok-ef-win32-v2.0.10.zip")
    check("无命中返回 None", inst.pick_asset(ok_nte_assets, [r"^nope$"]) is None)


def _make_zip(zip_path: Path, files: dict, evil_member: str = ""):
    with zipfile.ZipFile(zip_path, "w") as zf:
        for arcname, content in files.items():
            zf.writestr(arcname, content)
        if evil_member:
            zf.writestr(evil_member, "evil")


def test_extract_safety():
    print("[2] zip-slip 防护与 7z 往返")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        evil_zip = tmp / "evil.zip"
        _make_zip(evil_zip, {"ok-ww/ok-ww.exe": "x"}, evil_member="../evil.txt")
        dest = tmp / "dest"
        try:
            inst.extract_archive(evil_zip, dest)
            check("zip-slip 成员被拒绝", False, "未抛异常")
        except RuntimeError as exc:
            check("zip-slip 成员被拒绝", "可疑" in str(exc), str(exc))
        except Exception as exc:  # zipfile 自身也可能直接拒绝相对路径成员
            check("zip-slip 成员被拒绝", True, f"(zipfile 拒绝: {type(exc).__name__})")

        good_zip = tmp / "good.zip"
        _make_zip(good_zip, {"ok-ww/data/apps/ok-ww/app.json": "{}",
                             "ok-ww/ok-ww.exe": "fake-exe"})
        dest2 = tmp / "dest2"
        inst.extract_archive(good_zip, dest2)
        launcher = inst.locate_launcher(dest2, "ok-ww.exe")
        check("zip 解压 + 定位启动器", launcher.name == "ok-ww.exe", launcher)

        # 7z 往返（py7zr 写微型包，真格式）
        import py7zr
        src7 = tmp / "7zsrc"
        (src7 / "BetterGI").mkdir(parents=True)
        (src7 / "BetterGI" / "BetterGI.exe").write_text("fake-exe", encoding="utf-8")
        archive7 = tmp / "bettergi.7z"
        with py7zr.SevenZipFile(archive7, "w") as z:
            z.write(src7 / "BetterGI" / "BetterGI.exe", arcname="BetterGI/BetterGI.exe")
        dest3 = tmp / "dest3"
        inst.extract_archive(archive7, dest3)
        launcher7 = inst.locate_launcher(dest3, "BetterGI.exe")
        check("7z 解压 + 定位启动器", launcher7.name == "BetterGI.exe", launcher7)

        # 7z 成员名逃逸检查（构造一个名字可疑的清单直接喂 _assert_member_safe）
        try:
            inst._assert_member_safe("../evil.exe")
            check("成员名逃逸检查", False, "未抛异常")
        except RuntimeError:
            check("成员名逃逸检查", True)

        check("限深边界：深度 2 可命中（默认限 3）",
              inst.locate_launcher(dest2, "ok-ww.exe", max_depth=2) is not None)
        deep = tmp / "deep"
        (deep / "a" / "b" / "c" / "d").mkdir(parents=True)
        (deep / "a" / "b" / "c" / "d" / "x.exe").write_text("x", encoding="utf-8")
        try:
            inst.locate_launcher(deep, "x.exe", max_depth=3)
            check("超深启动器拒定位", False, "未抛异常")
        except RuntimeError:
            check("超深启动器拒定位", True)


class _Patches:
    """按模块属性打补丁的极简上下文。"""

    def __init__(self, target, **replacements):
        self._target = target
        self._replacements = replacements
        self._saved = {}

    def __enter__(self):
        for name, value in self._replacements.items():
            self._saved[name] = getattr(self._target, name)
            setattr(self._target, name, value)
        return self

    def __exit__(self, *exc):
        for name, value in self._saved.items():
            setattr(self._target, name, value)
        return False


def _fake_release(tag, zip_path, sha_hex=""):
    assets = [{"name": "ok-ww-win32.zip", "size": zip_path.stat().st_size,
               "url": zip_path.as_uri()}]
    if sha_hex:
        sha_file = zip_path.parent / "win32_sha256.txt"
        sha_file.write_text(f"{sha_hex}  ok-ww-win32.zip\n", encoding="utf-8")
        assets.append({"name": "win32_sha256.txt", "size": sha_file.stat().st_size,
                       "url": sha_file.as_uri()})
    return {"tag": tag, "assets": assets}


def _fake_tool(tmp: Path, path: str = ""):
    """collect_specs 条目的假货：ok-ww 形态（pyappify + 顶层 launcher）。"""
    spec = ScriptToolSpec(
        label="ok-ww", repo="ok-oldking/ok-wuthering-waves", pyappify=True,
        tool_key="ok-ww", asset_patterns=(r"^ok-ww-win32\.zip$",),
        launcher_name="ok-ww.exe")
    return {"app_key": "WutheringWaves", "display_name": "鸣潮", "label": "ok-ww",
            "repo": "ok-oldking/ok-wuthering-waves", "path": path, "spec": spec}


def _use_tmp_script_tools(tmp: Path):
    """把 settings.SCRIPT_TOOLS 指向临时目录（测试结束还原）。"""
    from game_auto.settings import SCRIPT_TOOLS
    saved = (SCRIPT_TOOLS.get("install_root", ""), dict(SCRIPT_TOOLS.get("overrides") or {}))
    SCRIPT_TOOLS["install_root"] = str(tmp)
    SCRIPT_TOOLS["overrides"] = {}
    return saved


def _restore_script_tools(saved):
    from game_auto.settings import SCRIPT_TOOLS
    SCRIPT_TOOLS["install_root"] = saved[0]
    SCRIPT_TOOLS["overrides"] = saved[1]


def test_install_fresh():
    print("[3] 安装编排：全新安装全流程")
    from game_auto.settings import SCRIPT_TOOLS
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        saved = _use_tmp_script_tools(tmp)
        try:
            fake_zip = tmp / "asset" / "ok-ww-win32.zip"
            fake_zip.parent.mkdir(parents=True)
            _make_zip(fake_zip, {
                "ok-ww/ok-ww.exe": "fake-exe",
                "ok-ww/data/apps/ok-ww/app.json": json.dumps({"current_version": "v1.0.0"}),
            })
            registered = {}
            phases = []
            with _Patches(inst, find_tool=lambda key: _fake_tool(tmp),
                          fetch_release_with_assets=lambda repo: _fake_release("v1.0.0", fake_zip),
                          describe_running_processes=lambda cfg: "",
                          register_launcher_path=lambda app, opt, path: registered.update(
                              {"app": app, "opt": opt, "path": path})):
                result = inst.install_tool(
                    "ok-ww", phase=lambda p: phases.append(p),
                    stop_check=lambda: False)
            check("返回安装结果", result["tag"] == "v1.0.0", result)
            check("阶段齐全", set(phases) >= {"downloading", "extracting", "installing", "registering"},
                  phases)
            launcher = Path(result["launcher"])
            check("启动器落位托管目录", launcher.is_file() and launcher.parent == Path(
                result["install_dir"]), launcher)
            check("pyappify 清单在位（版本检测可用）",
                  (launcher.parent / "data" / "apps" / "ok-ww" / "app.json").is_file())
            check("路径已注册", registered == {"app": "WutheringWaves", "opt": "", "path": str(launcher)},
                  registered)
            check("临时目录已清理",
                  not any(p.name.startswith(".ok-ww_install_") for p in tmp.iterdir()),
                  [p.name for p in tmp.iterdir()])
        finally:
            _restore_script_tools(saved)


def test_install_update_and_guards():
    print("[4] 安装编排：换血更新 / 运行中拒绝 / 外部目录拒绝 / 取消")
    from game_auto.settings import SCRIPT_TOOLS
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        saved = _use_tmp_script_tools(tmp)
        try:
            fake_zip = tmp / "asset" / "ok-ww-win32.zip"
            fake_zip.parent.mkdir(parents=True)
            _make_zip(fake_zip, {"ok-ww/ok-ww.exe": "new-exe"})

            install_dir = tmp / "ok-ww"
            install_dir.mkdir()
            (install_dir / "ok-ww.exe").write_text("old-exe", encoding="utf-8")
            (install_dir / "stale.txt").write_text("old", encoding="utf-8")
            old_path = str(install_dir / "ok-ww.exe")

            registered = {}
            with _Patches(inst, find_tool=lambda key: _fake_tool(tmp, path=old_path),
                          fetch_release_with_assets=lambda repo: _fake_release("v2.0.0", fake_zip),
                          describe_running_processes=lambda cfg: "",
                          register_launcher_path=lambda app, opt, path: registered.update({"path": path})):
                result = inst.install_tool("ok-ww", stop_check=lambda: False)
            check("换血后新 exe 就位", Path(result["launcher"]).read_text(encoding="utf-8") == "new-exe")
            check("旧文件随换血移除", not (install_dir / "stale.txt").exists())
            check("无 .old 残留", not (tmp / "ok-ww.old").exists())

            # 运行中拒绝
            with _Patches(inst, find_tool=lambda key: _fake_tool(tmp, path=old_path),
                          describe_running_processes=lambda cfg: "ok-ww.exe(PID 123)"):
                try:
                    inst.install_tool("ok-ww", stop_check=lambda: False)
                    check("运行中拒绝安装", False, "未抛异常")
                except RuntimeError as exc:
                    check("运行中拒绝安装", "正在运行" in str(exc), str(exc))

            # 托管目录外的已有安装拒绝
            outside = tmp / "elsewhere" / "ok-ww.exe"
            with _Patches(inst, find_tool=lambda key: _fake_tool(tmp, path=str(outside)),
                          describe_running_processes=lambda cfg: ""):
                try:
                    inst.install_tool("ok-ww", stop_check=lambda: False)
                    check("外部安装拒绝改动", False, "未抛异常")
                except RuntimeError as exc:
                    check("外部安装拒绝改动", "托管" in str(exc) or "自动安装只管理" in str(exc), str(exc))

            # 取消：独立重置一个旧安装，下载前就置位 stop_check → 抛取消，
            # 目标目录不被触碰
            (install_dir / "ok-ww.exe").write_text("old-exe", encoding="utf-8")
            (install_dir / "stale.txt").write_text("old", encoding="utf-8")
            with _Patches(inst, find_tool=lambda key: _fake_tool(tmp, path=old_path),
                          fetch_release_with_assets=lambda repo: _fake_release("v3.0.0", fake_zip),
                          describe_running_processes=lambda cfg: ""):
                try:
                    inst.install_tool("ok-ww", stop_check=lambda: True)
                    check("取消触发 InstallCancelled", False, "未抛异常")
                except inst.InstallCancelled:
                    check("取消触发 InstallCancelled", True)
            check("取消后旧安装完好",
                  Path(old_path).read_text(encoding="utf-8") == "old-exe"
                  and (install_dir / "stale.txt").exists())
        finally:
            _restore_script_tools(saved)


def test_download_and_sha():
    print("[5] 下载：进度/取消/sha256 对账")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        src = tmp / "payload.bin"
        src.write_bytes(b"x" * (256 * 1024 * 2 + 123))  # 2 片 + 尾巴
        dest = tmp / "out" / "payload.bin"

        received = []
        inst.download_file(src.as_uri(), dest, expected_size=src.stat().st_size,
                           progress=lambda r, t: received.append((r, t)))
        check("下载内容一致", dest.read_bytes() == src.read_bytes())
        check("进度回调已触发", len(received) >= 2, len(received))
        check("无 .part 残留", not list(dest.parent.glob("*.part")))

        # 取消
        try:
            inst.download_file(src.as_uri(), tmp / "out2" / "p.bin",
                               stop_check=lambda: True)
            check("取消抛 InstallCancelled", False, "未抛异常")
        except inst.InstallCancelled:
            check("取消抛 InstallCancelled", True)

        # sha 对账：_fetch_sha256_map 用 urlopen 拉清单，url 指向本地文件即可
        sha = hashlib.sha256(b"archive-bytes").hexdigest()
        sha_txt = tmp / "win32_sha256.txt"
        sha_txt.write_text(f"{sha}  ok.zip\n deadbeef  other.zip\n", encoding="utf-8")
        assets = [{"name": "win32_sha256.txt", "size": sha_txt.stat().st_size,
                   "url": sha_txt.as_uri()}]
        mapping = inst._fetch_sha256_map(assets)
        check("sha 清单解析", mapping.get("ok.zip") == sha, mapping)
        check("sha 不匹配可检出", mapping.get("ok.zip") != hashlib.sha256(b"tampered").hexdigest())


def test_version_check_rows():
    print("[6] 版本报告带下载元数据")
    from game_auto.services.script_version_check import collect_specs
    specs = collect_specs()
    downloadable = {s["label"] for s in specs if s.get("spec")
                    and getattr(s["spec"], "asset_patterns", ())
                    and getattr(s["spec"], "launcher_name", "")}
    check("五个可下载工具", downloadable == {"BetterGI", "March7th Assistant", "ok-ww", "ok-ef", "ok-nte"},
          downloadable)
    keys = {str(getattr(s.get("spec"), "tool_key", "")) for s in specs}
    check("tool_key 全部就位", "" not in keys and len(keys) == len(specs), keys)


def test_bridge_and_qml_wiring():
    print("[7] bridge / QML 接线源码检查")
    bridge_src = (PROJECT_ROOT / "src" / "game_auto" / "ui" / "bridge.py").read_text(encoding="utf-8")
    check("mixin 已挂载", "ToolManagerView" in bridge_src and "from .bridge_views.tool_manager import" in bridge_src)
    check("信号声明", "toolInstallChanged = Signal(str)" in bridge_src)
    check("事件分支", 'if kind == "tool_install":' in bridge_src)
    check("白名单含 tool_manager", '"tool_manager"' in bridge_src)
    setup_src = (PROJECT_ROOT / "src" / "game_auto" / "ui" / "bridge_views" / "tool_manager.py").read_text(
        encoding="utf-8")
    check("getToolManagerData 声明 result",
          "@Slot(result=str)\n    def getToolManagerData" in setup_src)
    check("安装/取消槽", "def installTool" in setup_src and "def cancelInstall" in setup_src)
    qml_src = (PROJECT_ROOT / "src" / "game_auto" / "ui" / "qml" / "views" / "ToolManagerView.qml").read_text(
        encoding="utf-8")
    check("进度条订阅", "onToolInstallChanged" in qml_src)
    check("版本刷新联动", "onScriptVersionsChanged" in qml_src)
    main_src = (PROJECT_ROOT / "src" / "game_auto" / "ui" / "qml" / "Main.qml").read_text(encoding="utf-8")
    check("侧边栏菜单", '{ key: "tool_manager"' in main_src)
    check("Loader 注册", 'source: "views/ToolManagerView.qml"' in main_src)
    wizard_src = (PROJECT_ROOT / "src" / "game_auto" / "ui" / "qml" / "views" / "SetupWizardView.qml").read_text(
        encoding="utf-8")
    check("向导页一键安装", "bridge.installTool(" in wizard_src)


def test_qml_load():
    print("[8] 脚本管理页离屏加载（stub bridge + 进度事件）")
    from PySide6.QtCore import QObject, QUrl, QTimer, Signal, Slot, Property
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQml import QQmlApplicationEngine, QQmlComponent, QQmlExpression
    from pyhuskarui.husapp import HusApp

    rows = [
        {"app_key": "WutheringWaves", "display_name": "鸣潮", "label": "ok-ww",
         "path": "", "local": "", "latest": "v3.7.13", "has_update": False,
         "repo": "ok-oldking/ok-wuthering-waves",
         "repo_url": "https://github.com/ok-oldking/ok-wuthering-waves/releases",
         "status": "no_path", "stale": False, "checked_at": "",
         "tool_key": "ok-ww", "downloadable": True,
         "install_root": "D:/data/script_tools",
         "managed_dir": "D:/data/script_tools/ok-ww", "managed": True},
        {"app_key": "HonkaiStarRail", "display_name": "崩坏：星穹铁道", "label": "星铁一条龙",
         "path": "", "local": "", "latest": "v4.0.3", "has_update": False,
         "repo": "OneDragon-Anything/StarRailOneDragon",
         "repo_url": "https://github.com/OneDragon-Anything/StarRailOneDragon/releases",
         "status": "no_path", "stale": False, "checked_at": "",
         "tool_key": "sr-one-dragon", "downloadable": False,
         "install_root": "D:/data/script_tools",
         "managed_dir": "D:/data/script_tools/sr-one-dragon", "managed": True},
    ]
    rows_json = json.dumps(rows, ensure_ascii=False)
    clicks = {"install": 0}

    class _Bridge(QObject):
        toolInstallChanged = Signal(str)
        scriptVersionsChanged = Signal(str)

        def __init__(self):
            super().__init__()
            self._install_json = "{}"

        @Property(str, notify=toolInstallChanged)
        def toolInstallJson(self):
            return self._install_json

        @Slot(result=str)
        def getToolManagerData(self):
            return rows_json

        @Slot(str)
        def installTool(self, tool_key):
            clicks["install"] += 1

        @Slot(str)
        def cancelInstall(self, tool_key):
            pass

        @Slot(str, result=bool)
        def setToolInstallRoot(self, path):
            return True

        @Slot(str, str, result=bool)
        def setToolOverridePath(self, tool_key, path):
            return True

        @Slot(str, result=bool)
        def checkDirExists(self, path):
            return bool(path)

    app = QGuiApplication.instance() or QGuiApplication([])
    engine = QQmlApplicationEngine()
    HusApp.initialize(engine)
    bridge = _Bridge()
    bridge.setParent(engine)
    engine.rootContext().setContextProperty("bridge", bridge)
    qml = """
import QtQuick
import QtQuick.Controls.Basic
Window {
  width: 1200; height: 900; visible: true
  Loader { anchors.fill: parent; source: "file:///%s" }
}
""" % (PROJECT_ROOT / "src" / "game_auto" / "ui" / "qml" / "views" / "ToolManagerView.qml").as_posix()
    comp = QQmlComponent(engine)
    comp.setData(qml.encode("utf-8"), QUrl.fromLocalFile(
        str(PROJECT_ROOT / "src" / "game_auto" / "ui" / "qml" / "_tm_test.qml")))
    obj = comp.create()
    check("QML 无语法错误", not comp.isError(),
          [e.toString() for e in comp.errors()][:3])
    if obj is None:
        return

    def inspect():
        try:
            root = obj.findChild(QObject, "toolManagerRoot")
            check("根节点可寻址", root is not None)
            if root is None:
                return
            ctx = engine.rootContext()
            n = QQmlExpression(ctx, root, "tools.length").evaluate()
            if isinstance(n, tuple):
                n = n[0]
            check("工具行已灌入", n == 2, n)
            busy = QQmlExpression(ctx, root, "installBusy()").evaluate()
            if isinstance(busy, tuple):
                busy = busy[0]
            check("空闲时不在安装态", busy is False, busy)
            # 模拟后台安装进度回包
            bridge._install_json = json.dumps(
                {"tool_key": "ok-ww", "phase": "downloading",
                 "received": 3145728, "total": 5242880, "message": ""},
                ensure_ascii=False)
            bridge.toolInstallChanged.emit(bridge._install_json)
            prog = QQmlExpression(ctx, root, "isInstalling('ok-ww')").evaluate()
            if isinstance(prog, tuple):
                prog = prog[0]
            check("进度事件进入安装态", prog is True, prog)
            txt = QQmlExpression(ctx, root, "phaseText(installState)").evaluate()
            if isinstance(txt, tuple):
                txt = txt[0]
            check("下载进度文案", "3.0MB" in str(txt) and "5.0MB" in str(txt), txt)
        finally:
            app.quit()

    QTimer.singleShot(900, inspect)
    app.exec()


def main():
    test_pick_asset()
    test_extract_safety()
    test_install_fresh()
    test_install_update_and_guards()
    test_download_and_sha()
    test_version_check_rows()
    test_bridge_and_qml_wiring()
    test_qml_load()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
