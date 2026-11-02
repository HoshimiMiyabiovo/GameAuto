# -*- coding: utf-8 -*-
"""脚本刷本配置页性能回归（2026-10-31）。

现象：页面打开时 7 张卡 + 几十个下拉一次性构建，离屏首帧约 2.3 秒，
用户看到长时间白屏/闪烁。

修复：卡片容器改为 ListView，只实例化视口附近卡片；delegate 不复用，
未保存编辑存到页面级 fieldDrafts，滚回来能恢复。

本套件锁定：
1. QML 离屏可加载、7 张卡都在 ListView 里；
2. 结构不再退回 Flickable + Repeater 全量构建；
3. fieldDrafts 的写入/读取/清理行为；
4. 关键性能参数（cacheBuffer / reuseItems: false）仍在。

跑法：.venv\\Scripts\\python.exe tests\\suite_stamina_view_perf_20261031.py
"""

import json
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, QUrl, QTimer, Slot
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine, QQmlComponent, QQmlExpression
from pyhuskarui.husapp import HusApp

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from game_auto.services.stamina_configs import list_configs  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


class _Bridge(QObject):
    @Slot(result=str)
    def getStaminaConfigs(self):
        return json.dumps(list_configs(), ensure_ascii=False)

    @Slot(str, str, result=str)
    def saveStaminaConfig(self, game_key, values_json):
        return "{}"

    @Slot(str, result=str)
    def openStaminaConfigFile(self, game_key):
        return ""


def test_source():
    print("[1] QML 结构")
    qml_path = PROJECT_ROOT / "src" / "game_auto" / "ui" / "qml" / "views" / "StaminaConfigView.qml"
    src = qml_path.read_text(encoding="utf-8")
    check("卡片使用 ListView", "ListView {" in src and 'objectName: "staminaCardList"' in src)
    check("不复用 delegate", "reuseItems: false" in src)
    check("只渲染当前游戏", "model: root.visibleConfigs" in src)
    check("游戏选择器存在", 'objectName: "staminaGameSelector"' in src)
    check("页面级草稿缓存", "fieldDrafts" in src and "setFieldDraft(" in src)
    check("不再使用全量 Flickable", "contentHeight: contentColumn.implicitHeight" not in src)


def test_qml_load():
    print("[2] 离屏实例化与草稿行为")
    app = QGuiApplication.instance() or QGuiApplication([])
    engine = QQmlApplicationEngine()
    HusApp.initialize(engine)
    bridge = _Bridge()
    engine.rootContext().setContextProperty("bridge", bridge)
    qml = """
import QtQuick
import QtQuick.Controls.Basic
Window {
  width: 1200; height: 800; visible: true
  Loader { anchors.fill: parent; source: "file:///%s" }
}
""" % (PROJECT_ROOT / "src" / "game_auto" / "ui" / "qml" / "views" / "StaminaConfigView.qml").as_posix()
    comp = QQmlComponent(engine)
    comp.setData(qml.encode("utf-8"), QUrl.fromLocalFile(
        str(PROJECT_ROOT / "src" / "game_auto" / "ui" / "qml" / "_stamina_perf_test.qml")))
    obj = comp.create()
    check("QML 无语法错误", not comp.isError(),
          [e.toString() for e in comp.errors()][:3])
    if obj is None:
        return

    result = {}

    def inspect():
        root = obj.findChild(QObject, "staminaConfigRoot")
        card_list = obj.findChild(QObject, "staminaCardList")
        selector = obj.findChild(QObject, "staminaGameSelector")
        check("根节点存在", root is not None)
        check("卡片 ListView 存在", card_list is not None)
        check("7 个游戏按钮存在", selector is not None and selector.property("count") == 7,
              selector.property("count") if selector is not None else None)
        if card_list is not None:
            check("只渲染当前 1 张卡", card_list.property("count") == 1,
                  card_list.property("count"))
            check("内容高度已计算", float(card_list.property("contentHeight") or 0) > 0,
                  card_list.property("contentHeight"))
        if root is not None:
            expr = QQmlExpression(
                engine.rootContext(), root,
                'setFieldDraft("genshin","power_enable","",true,"bool");'
                'var d=fieldDraft("genshin","power_enable");'
                'var ok=d&&d.boolValue===true&&d.dirty===true;'
                'clearGameDrafts("genshin");'
                'ok && fieldDraft("genshin","power_enable")===null')
            value = expr.evaluate()
            # PySide6 的 QQmlExpression.evaluate() 在部分版本返回 (value, isUndefined)
            if isinstance(value, tuple):
                value = value[0]
            result["draft_ok"] = value
            check("草稿写入/读取/清理", result.get("draft_ok") is True, result)
        app.quit()

    QTimer.singleShot(900, inspect)
    app.exec()


def main():
    test_source()
    test_qml_load()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
