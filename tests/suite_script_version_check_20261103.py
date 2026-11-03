# -*- coding: utf-8 -*-
"""第三方脚本版本检测回归（2026-11-03）。

覆盖 services/script_version_check.py 的核心逻辑与配置向导"脚本版本"卡片：
1. 版本解析/比较（前导 v、位数补零、非数字 tag）；
2. 本地版本按 spec 策略检测（pyappify 清单 / 版本文件 / exe 元数据 / 目录名）；
3. 安装路径解析（顶层 launcher / launcher_options / _discovery 回落）；
4. 自更新误报回归：原地自更新后目录名过期，绝不能拿目录名报"有更新"
   （2026-11-03 实盘：4 个"有更新"里 3 个是目录名误报）；
5. check_all 汇总：TTL 缓存、失败回退 stale、notified 同版本去重、
   本地未知不误报"有更新"；
6. QML 离屏冒烟 + bridge/QML 接线（@Slot result= 约定）源码检查。

跑法：.venv\\Scripts\\python.exe tests\\suite_script_version_check_20261103.py
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from game_auto.games.base import ScriptToolSpec  # noqa: E402
from game_auto.services import script_version_check as svc  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def test_version_parse():
    print("[1] 版本解析与比较")
    check("前导 v", svc.parse_tag_version("v2.2.0.3") == (2, 2, 0, 3))
    check("无前缀", svc.parse_tag_version("1.4.81") == (1, 4, 81))
    check("非数字 tag", svc.parse_tag_version("nightly-build") is None)
    check("空 tag", svc.parse_tag_version("") is None)
    check("新版本判定", svc.is_newer("v1.4.82", "1.4.81"))
    check("相同版本", not svc.is_newer("v1.4.81", "1.4.81"))
    check("位数补零", not svc.is_newer("v2.2.0", "2.2.0.0"))
    check("本地较新", not svc.is_newer("v1.4.80", "1.4.81"))
    check("本地未知不判新", not svc.is_newer("v1.4.82", ""))
    check("远端无数字版本不判新", not svc.is_newer("nightly", "1.0.0"))
    check("文本提取统一去 v", svc._extract_version("v2026.10.31\n") == "2026.10.31")


def test_detect_local_version():
    print("[2] 本地版本按 spec 策略检测")
    # ── 目录名策略（仅无自更新、按版本号 zip 分发的工具：ScriptChainer）──
    spec_dir = ScriptToolSpec(label="t", trust_dir_version=True)
    check("目录名版本（trust_dir）",
          svc.detect_local_version(r"D:\Tools\ScriptChainer-1.1.0 (1)\runner.exe", spec_dir)
          == "1.1.0")
    check("目录名默认不可信",
          svc.detect_local_version(r"D:\Tools\ScriptChainer-1.1.0\runner.exe",
                                   ScriptToolSpec(label="t")) == "")

    # ── pyappify 清单策略（ok 系）──
    with tempfile.TemporaryDirectory() as tmp:
        # 真实布局：安装目录名带初次安装版本，exe 就叫 ok-ww.exe，
        # 清单在 data/apps/<exe名>/app.json
        root = Path(tmp) / "ok-ww-v1.4.81"
        manifest = root / "data" / "apps" / "ok-ww" / "app.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"current_version": "v3.7.13"}), encoding="utf-8")
        spec_py = ScriptToolSpec(label="t", pyappify=True)
        check("pyappify 清单优先于过期目录名",
              svc.detect_local_version(str(root / "ok-ww.exe"), spec_py) == "3.7.13")
    check("pyappify 清单缺失 → 本地未知（不回落目录名）",
          svc.detect_local_version(r"D:\Tools\ok-ww-v1.4.81\ok-ww.exe",
                                   ScriptToolSpec(label="t", pyappify=True)) == "")

    # ── 版本文件策略（March7th）──
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "March7thAssistant_v2.2.0.3_full"
        vf = root / "assets" / "config" / "version.txt"
        vf.parent.mkdir(parents=True)
        vf.write_text("v2026.10.31\n", encoding="utf-8")
        spec_vf = ScriptToolSpec(label="t", version_file="assets/config/version.txt")
        check("版本文件优先于过期目录名",
              svc.detect_local_version(str(root / "March7th Assistant.exe"), spec_vf)
              == "2026.10.31")
    check("版本文件缺失 → 本地未知",
          svc.detect_local_version(r"Q:\no_such_place\March7thAssistant_v9.9.9_full\M7.exe",
                                   ScriptToolSpec(label="t",
                                                  version_file="assets/config/version.txt"))
          == "")

    # ── exe 元数据策略（BetterGI）──
    spec_exe = ScriptToolSpec(label="t", trust_exe_version=True)
    check("exe 不可读 → 本地未知",
          svc.detect_local_version(r"D:\Tools\BetterGI\BetterGI.exe", spec_exe) == "")
    check("非 exe 路径不读元数据",
          svc.detect_local_version(r"D:\Tools\Whimbox\run_one_dragon.bat", spec_exe) == "")

    # ── spec=None 兜底（旧行为：目录 → exe）──
    check("无 spec 走目录正则",
          svc.detect_local_version(r"D:\Tools\ok-ww-v1.4.81\ok-ww.exe") == "1.4.81")
    check("空路径", svc.detect_local_version("") == "")


def test_resolve_install_path():
    print("[3] app 配置 → 安装路径")
    check("顶层 launcher",
          svc.resolve_install_path({"launcher": r"D:\a\b.exe"}, "") == r"D:\a\b.exe")
    check("launcher_options 命中",
          svc.resolve_install_path(
              {"launcher_options": [{"name": "March7th", "launcher": r"D:\m7.exe"}]},
              "March7th") == r"D:\m7.exe")
    check("launcher_options 未命中返回空",
          svc.resolve_install_path(
              {"launcher_options": [{"name": "March7th", "launcher": r"D:\m7.exe"}]},
              "StarRailOneDragon") == "")
    check("discovery options 回落",
          svc.resolve_install_path(
              {"_discovery": {"launcher_options": {"March7th": {"launcher": r"D:\d7.exe"}}}},
              "March7th") == r"D:\d7.exe")
    check("discovery launcher 回落",
          svc.resolve_install_path({"_discovery": {"launcher": r"D:\bg.exe"}}, "")
          == r"D:\bg.exe")
    check("script_exe_path 兜底",
          svc.resolve_install_path({"_discovery": {"script_exe_path": r"D:\s.exe"}}, "")
          == r"D:\s.exe")
    check("空配置", svc.resolve_install_path({}, "") == "")


class _TmpCache:
    """把 CACHE_FILE / CACHE_DIR 指到临时目录的上下文。"""

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self._old = (svc.CACHE_DIR, svc.CACHE_FILE)
        svc.CACHE_DIR = self.dir
        svc.CACHE_FILE = self.dir / "script_versions.json"
        return self

    def __exit__(self, *exc):
        svc.CACHE_DIR, svc.CACHE_FILE = self._old
        self._tmp.cleanup()
        return False


def _patch_check(specs, fetch_results):
    """替换 collect_specs / fetch_latest_release，返回还原函数。"""
    old = (svc.collect_specs, svc.fetch_latest_release)
    svc.collect_specs = lambda: specs
    results = list(fetch_results)
    svc.fetch_latest_release = lambda repo: results.pop(0) if results else None
    return lambda: old


def _make_install(version_txt: str = "") -> str:
    """造一个 March7th 式安装目录：过期目录名 + 自更新后的 version.txt。"""
    tmp = tempfile.mkdtemp()
    root = Path(tmp) / "March7thAssistant_v2.2.0.3_full"
    if version_txt:
        vf = root / "assets" / "config" / "version.txt"
        vf.parent.mkdir(parents=True)
        vf.write_text(version_txt, encoding="utf-8")
    return str(root / "March7th Assistant.exe")


def test_cache_fresh():
    print("[4] TTL 缓存判定")
    now = datetime.now()
    check("新鲜条目", svc._cache_fresh({"checked_at": now.isoformat(timespec="seconds")}))
    check("过期条目", not svc._cache_fresh(
        {"checked_at": (now - timedelta(hours=25)).isoformat(timespec="seconds")}))
    check("缺失时间戳", not svc._cache_fresh({}))
    check("坏时间戳", not svc._cache_fresh({"checked_at": "not-a-date"}))


def test_check_all_offline():
    print("[5] check_all：离线读缓存")
    specs = [
        {"app_key": "WutheringWaves", "display_name": "鸣潮", "label": "ok-ww",
         "repo": "ok-oldking/ok-wuthering-waves",
         "path": r"D:\Tools\ok-ww-v1.4.81\ok-ww.exe",
         "spec": ScriptToolSpec(label="ok-ww", trust_dir_version=True)},
        {"app_key": "ZenlessZoneZero", "display_name": "绝区零",
         "label": "OneDragon ScriptChainer", "repo": "",
         "path": r"D:\Tools\ScriptChainer-1.1.0\runner.exe",
         "spec": ScriptToolSpec(label="OneDragon ScriptChainer", trust_dir_version=True)},
    ]
    with _TmpCache():
        svc.CACHE_FILE.write_text(json.dumps({
            "repos": {"ok-oldking/ok-wuthering-waves": {
                "tag": "v1.4.82", "checked_at": datetime.now().isoformat(timespec="seconds"),
                "stale": False}},
            "notified": {"ok-oldking/ok-wuthering-waves": "v1.4.82"},
        }, ensure_ascii=False), encoding="utf-8")
        restore = _patch_check(specs, [])
        try:
            rows, toasts = svc.check_all(allow_network=False)
        finally:
            restore()
        check("离线也返回全部行", len(rows) == 2, len(rows))
        ok_ww = next(r for r in rows if r["label"] == "ok-ww")
        check("缓存 tag 用于比较", ok_ww["latest"] == "v1.4.82" and ok_ww["has_update"] is True,
              ok_ww)
        check("有更新状态", ok_ww["status"] == "update", ok_ww["status"])
        check("离线不产生提醒", toasts == [], toasts)
        check("无仓库脚本标记 no_repo",
              next(r for r in rows if r["label"].startswith("OneDragon ScriptChainer"))["status"]
              == "no_repo")
        check("离线不改缓存文件", svc.CACHE_FILE.exists())


def test_check_all_update_and_dedup():
    print("[6] check_all：联网检查 + 提醒去重")
    specs = [
        {"app_key": "WutheringWaves", "display_name": "鸣潮", "label": "ok-ww",
         "repo": "ok-oldking/ok-wuthering-waves",
         "path": r"D:\Tools\ok-ww-v1.4.81\ok-ww.exe",
         "spec": ScriptToolSpec(label="ok-ww", trust_dir_version=True)},
    ]
    with _TmpCache():
        restore = _patch_check(specs, ["v1.4.82"])
        try:
            rows, toasts = svc.check_all(force=True)
        finally:
            restore()
        check("发现更新", rows[0]["has_update"] is True and rows[0]["status"] == "update",
              rows[0])
        check("产生一次提醒", len(toasts) == 1 and "1.4.82" in toasts[0], toasts)
        check("提醒落盘 notified",
              json.loads(svc.CACHE_FILE.read_text(encoding="utf-8"))["notified"]
              .get("ok-oldking/ok-wuthering-waves") == "v1.4.82")

        # 第二次检查同版本：不再重复提醒
        restore = _patch_check(specs, ["v1.4.82"])
        try:
            rows2, toasts2 = svc.check_all(force=True)
        finally:
            restore()
        check("同版本不重复提醒", toasts2 == [], toasts2)
        check("二次检查仍是更新状态", rows2[0]["has_update"] is True)


def test_self_update_no_false_positive():
    print("[7] 自更新误报回归（2026-11-03 实盘 bug）")
    # 场景一：March7th 原地自更新——目录名 v2.2.0.3 过期，version.txt 已是最新版。
    # 旧检测拿目录名比较 → 永久误报"有更新"；现在必须判"已是最新"。
    path = _make_install("v2026.10.31")
    specs = [
        {"app_key": "HonkaiStarRail", "display_name": "崩坏：星穹铁道",
         "label": "March7th Assistant", "repo": "moesnow/March7thAssistant",
         "path": path,
         "spec": ScriptToolSpec(label="March7th Assistant",
                                version_file="assets/config/version.txt")},
    ]
    with _TmpCache():
        restore = _patch_check(specs, ["v2026.10.31"])
        try:
            rows, toasts = svc.check_all(force=True)
        finally:
            restore()
        check("本地取自 version.txt 而非目录名", rows[0]["local"] == "2026.10.31", rows[0])
        check("已自更新到最新不误报", rows[0]["status"] == "latest", rows[0]["status"])
        check("不产生提醒", toasts == [], toasts)

    # 场景二：pyappify 自更新——目录名 v1.4.81 过期，app.json 已是最新版。
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "ok-ww"
        manifest = root / "data" / "apps" / "ok-ww" / "app.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"current_version": "v3.7.13"}), encoding="utf-8")
        specs2 = [
            {"app_key": "WutheringWaves", "display_name": "鸣潮", "label": "ok-ww",
             "repo": "ok-oldking/ok-wuthering-waves",
             "path": str(root / "ok-ww.exe"),
             "spec": ScriptToolSpec(label="ok-ww", pyappify=True)},
        ]
        with _TmpCache():
            restore = _patch_check(specs2, ["v3.7.13"])
            try:
                rows2, toasts2 = svc.check_all(force=True)
            finally:
                restore()
            check("pyappify 本地取自 app.json", rows2[0]["local"] == "3.7.13", rows2[0])
            check("已自更新到最新不误报", rows2[0]["status"] == "latest", rows2[0]["status"])
            check("不产生提醒", toasts2 == [], toasts2)


def test_check_all_failure_fallback():
    print("[8] check_all：网络失败回退旧值")
    specs = [
        {"app_key": "WutheringWaves", "display_name": "鸣潮", "label": "ok-ww",
         "repo": "ok-oldking/ok-wuthering-waves",
         "path": r"D:\Tools\ok-ww-v1.4.81\ok-ww.exe",
         "spec": ScriptToolSpec(label="ok-ww", trust_dir_version=True)},
    ]
    with _TmpCache():
        svc.CACHE_FILE.write_text(json.dumps({
            "repos": {"ok-oldking/ok-wuthering-waves": {
                "tag": "v1.4.82",
                "checked_at": (datetime.now() - timedelta(hours=30)).isoformat(
                    timespec="seconds"),
                "stale": False}},
            "notified": {"ok-oldking/ok-wuthering-waves": "v1.4.82"},
        }, ensure_ascii=False), encoding="utf-8")
        # fetch 返回 None = 查询失败（超时/限速）
        restore = _patch_check(specs, [None])
        try:
            rows, toasts = svc.check_all(force=True)
        finally:
            restore()
        check("沿用旧 tag", rows[0]["latest"] == "v1.4.82", rows[0])
        check("标记 stale", rows[0]["stale"] is True, rows[0]["stale"])
        check("失败不产生提醒", toasts == [], toasts)


def test_check_all_local_unknown():
    print("[9] check_all：本地未知不误报")
    # BetterGI 策略：只信 exe 元数据；exe 不可读 → 本地未知，绝不拿目录名凑数
    specs = [
        {"app_key": "GenshinImpact", "display_name": "原神", "label": "BetterGI",
         "repo": "babalae/better-genshin-impact",
         "path": r"D:\Tools\BetterGI\BetterGI.exe",
         "spec": ScriptToolSpec(label="BetterGI", trust_exe_version=True)},
    ]
    with _TmpCache():
        restore = _patch_check(specs, ["v0.5.2"])
        try:
            rows, toasts = svc.check_all(force=True)
        finally:
            restore()
        check("本地未知状态", rows[0]["status"] == "local_unknown", rows[0]["status"])
        check("本地未知不判 has_update", rows[0]["has_update"] is False)
        check("本地未知仍展示远端版本", rows[0]["latest"] == "v0.5.2")
        check("本地未知不提醒", toasts == [], toasts)
        # 无 release（404 → ""）
        restore = _patch_check(specs, [""])
        try:
            rows2, _ = svc.check_all(force=True)
        finally:
            restore()
        check("无 release 状态", rows2[0]["status"] == "no_release", rows2[0]["status"])
        # 未配置路径优先级最高
        specs2 = [dict(specs[0], path="")]
        restore = _patch_check(specs2, [""])
        try:
            rows3, _ = svc.check_all(force=True)
        finally:
            restore()
        check("未配置路径状态", rows3[0]["status"] == "no_path", rows3[0]["status"])


def test_bridge_and_qml_wiring():
    print("[10] bridge / QML 接线源码检查")
    bridge_src = (PROJECT_ROOT / "src" / "game_auto" / "ui" / "bridge.py").read_text(
        encoding="utf-8")
    check("信号声明", "scriptVersionsChanged = Signal(str)" in bridge_src)
    check("running 信号", "scriptCheckRunningChanged = Signal()" in bridge_src)
    check("事件分支", 'if kind == "script_versions":' in bridge_src)
    check("属性暴露", "def scriptVersionsJson" in bridge_src and "def scriptCheckRunning" in bridge_src)
    check("启动定时检查", "QTimer.singleShot(5_000, self._auto_check_script_versions)" in bridge_src)
    setup_src = (PROJECT_ROOT / "src" / "game_auto" / "ui" / "bridge_views" / "setup.py").read_text(
        encoding="utf-8")
    # 03-ui-qml 约定：QML 使用返回值的 @Slot 必须声明 result=
    check("getScriptVersions 声明 result",
          "@Slot(result=str)\n    def getScriptVersions" in setup_src)
    check("手动检查槽", "def checkScriptUpdates" in setup_src)
    check("后台 worker", "def _script_check_worker" in setup_src)
    qml_src = (PROJECT_ROOT / "src" / "game_auto" / "ui" / "qml" / "views" / "SetupWizardView.qml").read_text(
        encoding="utf-8")
    check("版本卡片存在", "脚本版本 / 更新" in qml_src)
    check("数据推送连接", "onScriptVersionsChanged" in qml_src)
    check("手动检查按钮", "bridge.checkScriptUpdates()" in qml_src)
    check("检查中禁用按钮", "!bridge.scriptCheckRunning" in qml_src)


def test_qml_load():
    print("[11] 配置向导离屏加载（stub bridge 灌版本数据）")
    from PySide6.QtCore import QObject, QUrl, QTimer, Signal, Slot, Property
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQml import QQmlApplicationEngine, QQmlComponent, QQmlExpression
    from pyhuskarui.husapp import HusApp

    sample_rows = [
        {"app_key": "WutheringWaves", "display_name": "鸣潮", "label": "ok-ww",
         "path": r"D:\Tools\ok-ww\ok-ww.exe", "local": "3.7.13",
         "latest": "v3.7.13", "has_update": False,
         "repo": "ok-oldking/ok-wuthering-waves",
         "repo_url": "https://github.com/ok-oldking/ok-wuthering-waves/releases",
         "status": "latest", "stale": False, "checked_at": "2026-11-03T12:00:00"},
        {"app_key": "ZenlessZoneZero", "display_name": "绝区零",
         "label": "OneDragon ScriptChainer", "path": r"D:\Tools\sc\runner.exe",
         "local": "1.1.0", "latest": "", "has_update": False, "repo": "", "repo_url": "",
         "status": "no_repo", "stale": False, "checked_at": ""},
        {"app_key": "GenshinImpact", "display_name": "原神", "label": "BetterGI",
         "path": r"D:\Tools\BetterGI\BetterGI.exe", "local": "0.66.0",
         "latest": "v0.66.0", "has_update": False,
         "repo": "babalae/better-genshin-impact",
         "repo_url": "https://github.com/babalae/better-genshin-impact/releases",
         "status": "latest", "stale": False, "checked_at": "2026-11-03T12:00:00"},
    ]
    sample_json = json.dumps(sample_rows, ensure_ascii=False)

    class _Bridge(QObject):
        scriptVersionsChanged = Signal(str)
        toastRequested = Signal(str, str)

        def __init__(self):
            super().__init__()
            self._running = False

        @Property(bool, notify=scriptVersionsChanged)
        def scriptCheckRunning(self):
            return self._running

        @Property(str, notify=scriptVersionsChanged)
        def scriptVersionsJson(self):
            return sample_json

        @Slot(result=str)
        def getSetupItems(self):
            return "[]"

        @Slot(result=str)
        def getScriptVersions(self):
            return sample_json

        @Slot()
        def checkScriptUpdates(self):
            pass

        @Slot(str, result=bool)
        def checkFileExists(self, path):
            return bool(path)

        @Slot(str, result=bool)
        def saveSetupPaths(self, json_paths):
            return True

        @Slot(str)
        def switchView(self, key):
            pass

    app = QGuiApplication.instance() or QGuiApplication([])
    engine = QQmlApplicationEngine()
    HusApp.initialize(engine)
    bridge = _Bridge()
    # setContextProperty 不接管所有权：不挂 parent 的话 Python 侧引用在
    # 测试函数结束后释放，销毁期绑定重估会看到 null（报 TypeError 噪音）
    bridge.setParent(engine)
    engine.rootContext().setContextProperty("bridge", bridge)
    qml = """
import QtQuick
import QtQuick.Controls.Basic
Window {
  width: 1200; height: 900; visible: true
  Loader { anchors.fill: parent; source: "file:///%s" }
}
""" % (PROJECT_ROOT / "src" / "game_auto" / "ui" / "qml" / "views" / "SetupWizardView.qml").as_posix()
    comp = QQmlComponent(engine)
    comp.setData(qml.encode("utf-8"), QUrl.fromLocalFile(
        str(PROJECT_ROOT / "src" / "game_auto" / "ui" / "qml" / "_sv_test.qml")))
    obj = comp.create()
    check("QML 无语法错误", not comp.isError(),
          [e.toString() for e in comp.errors()][:3])
    if obj is None:
        return

    def inspect():
        try:
            root = obj.findChild(QObject, "setupWizardRoot")
            check("根节点可寻址", root is not None)
            if root is None:
                return
            ctx = engine.rootContext()
            n = QQmlExpression(ctx, root, "scriptVersions.length").evaluate()
            if isinstance(n, tuple):
                n = n[0]
            check("版本行已灌入", n == len(sample_rows), n)
            meta = QQmlExpression(
                ctx, root, 'statusMeta({status: "update"}).text').evaluate()
            if isinstance(meta, tuple):
                meta = meta[0]
            check("状态映射 update", meta == "有更新", meta)
            stale_txt = QQmlExpression(
                ctx, root, 'statusMeta({status: "latest"}).text').evaluate()
            if isinstance(stale_txt, tuple):
                stale_txt = stale_txt[0]
            check("状态映射 latest", stale_txt == "已是最新", stale_txt)
        finally:
            app.quit()

    QTimer.singleShot(900, inspect)
    app.exec()


def main():
    test_version_parse()
    test_detect_local_version()
    test_resolve_install_path()
    test_cache_fresh()
    test_check_all_offline()
    test_check_all_update_and_dedup()
    test_self_update_no_false_positive()
    test_check_all_failure_fallback()
    test_check_all_local_unknown()
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
