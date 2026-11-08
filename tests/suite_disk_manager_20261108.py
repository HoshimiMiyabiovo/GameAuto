"""磁盘管理页回归（2026-11-08）。

三段式（照 suite_tool_manager_20261103）：
  [1-4] services/disk_usage.py 纯逻辑：上溯推导 / 扫描求和与取消 / 目标收集
        （monkeypatch registry+manager）/ 脚本目录两来源与去重
  [5]   bridge / QML 接线源码断言（mixin/信号/事件分支/白名单/菜单/Loader）
  [6]   磁盘管理页离屏加载（stub bridge + item 事件渐进刷新）

跑法：.venv\\Scripts\\python.exe -m pytest tests/suite_disk_manager_20261108.py -q
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

PROJECT_ROOT = Path(__file__).resolve().parent.parent

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}", flush=True)
    else:
        print(f"  FAIL {name} {detail}", flush=True)
        FAILURES.append(name)


def test_up_levels_and_inside():
    print("[1] 上溯推导与包含关系")
    from game_auto.services.disk_usage import _is_inside, _up_levels

    p = Path(r"E:\Game\Star Rail\Game\StarRail.exe").parent
    check("上溯 0 层 = exe 目录", str(_up_levels(p, 0)) == str(p))
    check("上溯 1 层 = 安装根", _up_levels(p, 1).name == "Star Rail")
    check("越过盘符根不再上溯", _up_levels(Path(r"E:\a"), 5).drive == "E:")
    check("负数层数安全", str(_up_levels(p, -1)) == str(p))
    check("包含：子目录在内", _is_inside(r"E:\a\b", r"E:\a"))
    check("包含：相等也算", _is_inside(r"E:\a", r"E:\a"))
    check("不包含：兄弟目录", not _is_inside(r"E:\a\x", r"E:\a\b"))
    check("不包含：不同盘符", not _is_inside(r"D:\a", r"E:\a"))
    check("不包含：空路径", not _is_inside("", r"E:\a"))


def test_scan_target_and_cancel():
    print("[2] 目录求和 / 缺失 / 取消 / 缓存")
    from game_auto.services import disk_usage as du

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "sub" / "deep").mkdir(parents=True)
        (root / "a.bin").write_bytes(b"0" * 10)
        (root / "sub" / "b.bin").write_bytes(b"0" * 20)
        (root / "sub" / "deep" / "c.bin").write_bytes(b"0" * 5)
        du.CACHE_FILE = root / "cache" / "disk_usage_cache.json"

        check("嵌套求和 35", du.scan_target(str(root)) == 35)
        check("路径不存在 = 0", du.scan_target(str(root / "nope")) == 0)
        check("文件路径 = 0", du.scan_target(str(root / "a.bin")) == 0)
        # 2026-11-08 首跑实锤：Path("") == 当前目录，空 path 会把整个项目扫进去
        check("空路径 = 0（不退化为扫当前目录）", du.scan_target("") == 0)
        check("纯空白路径 = 0", du.scan_target("   ") == 0)
        try:
            du.scan_target(str(root), stop_check=lambda: True)
            check("stop_check 命中抛 ScanCancelled", False)
        except du.ScanCancelled:
            check("stop_check 命中抛 ScanCancelled", True)

        done_log = []
        items = du.scan_all(
            [
                {"key": "x", "group": "games", "name": "X", "path": str(root), "exists": True},
                {"key": "y", "group": "data", "name": "Y", "path": str(root / "sub"), "exists": True},
            ],
            on_item_done=lambda item, done, total: done_log.append((item["key"], done, total)),
        )
        check("scan_all 求和", [i["size"] for i in items] == [35, 25], [i["size"] for i in items])
        check("on_item_done 顺序", done_log == [("x", 1, 2), ("y", 2, 2)], done_log)
        cache = du.load_cache()
        check("缓存落盘且可回读", cache and [i["size"] for i in cache["items"]] == [35, 25]
              and bool(cache.get("ts")))

        # path 为空的行（data 根散文件）：保留收集期 size，不扫描（2026-11-08 首跑实锤）
        loose = du.scan_all([
            {"key": "data:<root>", "group": "data", "name": "（data 根散文件）",
             "path": "", "exists": True, "size": 15404288},
        ])
        check("空 path 行保留收集期求和", loose[0]["size"] == 15404288, loose[0]["size"])

        # 取消路径不写缓存（保留上次完整结果）
        cache_before = du.load_cache()
        try:
            du.scan_all([{"key": "z", "group": "data", "name": "Z", "path": str(root), "exists": True}],
                        stop_check=lambda: True)
            check("取消抛 ScanCancelled", False)
        except du.ScanCancelled:
            check("取消抛 ScanCancelled", True)
        check("取消后缓存保持上次结果", du.load_cache() == cache_before)


def test_collect_targets(monkeypatch_like=None):
    print("[3] 目标收集：上溯字段 / launcher 回落 / 覆盖目录")
    from types import SimpleNamespace as NS

    from game_auto.services import disk_usage as du

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "MyGame" / "bin").mkdir(parents=True)
        (root / "override").mkdir()
        exe = root / "MyGame" / "bin" / "game.exe"
        exe.write_bytes(b"MZ")

        fake_profiles = [
            # 有第三方脚本的游戏：launcher 不回落（可能是 BetterGI 之类）
            NS(game_key="alpha", app_key="Alpha", display_name="阿尔法",
               disk_root_up_levels=0, script_tools=(NS(),)),
            # 无第三方脚本的游戏：_discovery 缺 game_exe_path 时回落 launcher（白荆模式）
            NS(game_key="beta", app_key="Beta", display_name="贝塔",
               disk_root_up_levels=1, script_tools=()),
        ]
        fake_apps = {
            "Alpha": {"_discovery": {"game_exe_path": str(exe)}},
            "Beta": {"launcher": str(exe), "_discovery": {"script_exe_path": str(exe)}},
        }

        from game_auto.games import registry
        from game_auto.settings import manager
        orig_iter, orig_get, orig_get_app = registry.iter_profiles, manager.get, manager.get_app
        try:
            registry.iter_profiles = lambda: fake_profiles
            manager.get = lambda key, default=None: ({"beta": str(root / "override")} if key == "disk_scan_overrides" else default)
            manager.get_app = lambda key: fake_apps.get(key)
            targets = du._game_targets()
        finally:
            registry.iter_profiles = orig_iter
            manager.get = orig_get
            manager.get_app = orig_get_app

        by_key = {t["key"]: t for t in targets}
        check("上溯字段生效（alpha exe 目录）",
              by_key["alpha"]["path"] == str(root / "MyGame" / "bin"), by_key["alpha"]["path"])
        check("script_tools 非空不回落且覆盖优先（beta → override）",
              by_key["beta"]["path"] == str(root / "override"), by_key["beta"]["path"])
        check("exists 判定", all(t["exists"] for t in targets))


def test_script_targets():
    print("[4] 脚本目录：collect_specs + flow_exe_paths 通配 + 去重")
    from types import SimpleNamespace as NS

    from game_auto.services import disk_usage as du

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        game_root = root / "GameX"
        (game_root / "bin").mkdir(parents=True)
        script_dir = root / "scripts" / "ok-xx"
        script_dir.mkdir(parents=True)
        (script_dir / "ok-xx.exe").write_bytes(b"MZ")
        # flow_exe_paths 通配形态（XM/CJ：exe 名带版本号）
        (root / "xm" ).mkdir()
        (root / "xm" / "XM 脱机 ver 2.6.5.exe").write_bytes(b"MZ")
        (root / "xm" / "XM 脱机 ver 2.6.9.exe").write_bytes(b"MZ")

        from game_auto.services import script_version_check as svc
        from game_auto.settings import manager
        orig_specs, orig_load = svc.collect_specs, manager.load
        try:
            svc.collect_specs = lambda: [
                {"tool_key": "ok-xx", "path": str(script_dir / "ok-xx.exe")},
                {"tool_key": "inside", "path": str(game_root / "bin" / "launcher.exe")},  # 游戏根内 → 跳过
            ]
            manager.load = lambda force=False: {"flow_exe_paths": {"XM": str(root / "xm" / "XM 脱机 ver *.exe")}}
            targets = du._script_targets([str(game_root)])
        finally:
            svc.collect_specs = orig_specs
            manager.load = orig_load

        paths = {t["path"] for t in targets}
        check("collect_specs 工具目录收录", str(script_dir) in paths, paths)
        check("通配 exe 解析到所在目录", str(root / "xm") in paths, paths)
        check("游戏根内目录去重", not any(p.startswith(str(game_root)) for p in paths), paths)
        by_path = {t["path"]: t for t in targets}
        check("key 用归一化目录（稳定不漂移）",
              by_path[str(root / "xm")]["key"].startswith("script:"), by_path[str(root / "xm")]["key"])


def test_bridge_and_qml_wiring():
    print("[5] bridge / QML 接线源码检查")
    bridge_src = (PROJECT_ROOT / "src" / "game_auto" / "ui" / "bridge.py").read_text(encoding="utf-8")
    check("mixin 已挂载", "DiskManagerView" in bridge_src
          and "from .bridge_views.disk_manager import" in bridge_src)
    check("信号声明", "diskScanChanged = Signal(str)" in bridge_src)
    check("事件分支", 'if kind == "disk_scan":' in bridge_src)
    check("白名单含 disk_manager", '"disk_manager"' in bridge_src)
    check("扫描状态字段初始化", "self._disk_scan_running = False" in bridge_src)

    mixin_src = (PROJECT_ROOT / "src" / "game_auto" / "ui" / "bridge_views" / "disk_manager.py").read_text(
        encoding="utf-8")
    check("getDiskManagerData 声明 result",
          "@Slot(result=str)\n    def getDiskManagerData" in mixin_src)
    check("扫描/取消/覆盖槽", all(s in mixin_src for s in
          ("def startDiskScan", "def cancelDiskScan", "def setDiskScanOverride")))

    main_src = (PROJECT_ROOT / "src" / "game_auto" / "ui" / "qml" / "Main.qml").read_text(encoding="utf-8")
    check("侧边栏菜单", '{ key: "disk_manager"' in main_src)
    check("Loader 注册", 'source: "views/DiskManagerView.qml"' in main_src)
    check("懒加载标志", "diskManagerEverLoaded" in main_src)

    qml_src = (PROJECT_ROOT / "src" / "game_auto" / "ui" / "qml" / "views" / "DiskManagerView.qml").read_text(
        encoding="utf-8")
    check("QML 订阅扫描事件", "onDiskScanChanged" in qml_src)
    check("QML 改目录对话框", "setDiskScanOverride" in qml_src)

    manager_src = (PROJECT_ROOT / "src" / "game_auto" / "settings" / "manager.py").read_text(encoding="utf-8")
    check("覆盖持久化 helper", "def update_disk_scan_overrides" in manager_src)

    base_src = (PROJECT_ROOT / "src" / "game_auto" / "games" / "base.py").read_text(encoding="utf-8")
    check("GameProfile 上溯字段", "disk_root_up_levels: int = 0" in base_src)


def test_qml_load():
    print("[6] 磁盘管理页离屏加载（stub bridge + item 事件渐进刷新）")
    from PySide6.QtCore import QObject, QUrl, Signal, Slot
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQml import QQmlApplicationEngine, QQmlComponent
    from pyhuskarui.husapp import HusApp

    items = [
        {"key": "zzz", "group": "games", "name": "绝区零", "path": "E:/Game/ZZZ", "exists": True, "size": 0},
        {"key": "script:x", "group": "scripts", "name": "ok-ww", "path": "D:/ok-ww", "exists": True, "size": 4096},
        {"key": "data:logs", "group": "data", "name": "logs", "path": "E:/data/logs", "exists": True, "size": 1024},
    ]
    data_json = json.dumps({"items": items, "scanned_at": "2026-11-08 09:00:00",
                            "overview": [{"drive": "E:", "total": 100, "free": 40, "used": 60}]},
                           ensure_ascii=False)
    clicks = {"scan": 0, "override": []}

    class _Bridge(QObject):
        diskScanChanged = Signal(str)

        @Slot(result=str)
        def getDiskManagerData(self):
            return data_json

        @Slot()
        def startDiskScan(self):
            clicks["scan"] += 1

        @Slot()
        def cancelDiskScan(self):
            pass

        @Slot(str, str, result=bool)
        def setDiskScanOverride(self, game_key, path):
            clicks["override"].append((game_key, path))
            return True

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
""" % (PROJECT_ROOT / "src" / "game_auto" / "ui" / "qml" / "views" / "DiskManagerView.qml").as_posix()
    comp = QQmlComponent(engine)
    comp.setData(qml.encode("utf-8"), QUrl.fromLocalFile(
        str(PROJECT_ROOT / "src" / "game_auto" / "ui" / "qml" / "_dm_test.qml")))
    obj = comp.create()
    check("QML 无语法错误", not comp.isError(),
          [e.toString() for e in comp.errors()][:3])
    if obj is None:
        return

    root = obj.findChild(QObject, "diskManagerRoot")
    check("根节点可寻址", root is not None)
    if root is None:
        return

    def snapshot():
        val = root.property("scanData")
        try:
            return val.toVariant() if hasattr(val, "toVariant") else (val or {})
        except Exception:
            return {}

    n_items = len(items)
    data0 = snapshot()
    check("三组清单渲染", len(data0.get("items") or []) == n_items,
          len(data0.get("items") or []))
    check("盘符总览渲染", len(data0.get("overview") or []) == 1)
    check("上次扫描时间", data0.get("scanned_at") == "2026-11-08 09:00:00")

    # item 事件渐进刷新：模拟扫描器扫完 zzz
    bridge.diskScanChanged.emit(json.dumps({
        "phase": "item", "item": {"key": "zzz", "size": 25, "exists": True},
        "done": 1, "total": n_items,
    }, ensure_ascii=False))
    updated = [i for i in snapshot().get("items", []) if i["key"] == "zzz"]
    check("item 事件行级更新", updated and updated[0]["size"] == 25, updated)
    check("扫描中标志", bool(root.property("scanning")))

    bridge.diskScanChanged.emit(json.dumps({
        "phase": "done", "items": items, "overview": [],
        "scanned_at": "2026-11-08 10:00:00",
    }, ensure_ascii=False))
    check("done 收尾回空闲", not bool(root.property("scanning")))
    check("done 更新扫描时间", snapshot().get("scanned_at") == "2026-11-08 10:00:00")

    obj.deleteLater()
    engine.deleteLater()


def main():
    test_up_levels_and_inside()
    test_scan_target_and_cancel()
    test_collect_targets()
    test_script_targets()
    test_bridge_and_qml_wiring()
    test_qml_load()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
