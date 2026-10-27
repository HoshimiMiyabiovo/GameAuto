"""
QML 布局几何 smoke 测试（offscreen 真实渲染 + 几何测量）

用途
----
PySide6/QML 的布局 bug（卡片塌缩、内容溢出父容器、被下方卡片遮挡）靠肉眼/猜极难定位，
因为 offscreen 下若没有真正的 Scene/窗口，锚定和布局都算不出高度（量到的全是 0）。

本脚本用 `QQuickView` + `HusApp.initialize()` 提供真实 Scene，加载指定视图、灌入样例数据，
然后遍历 `QQuickItem` 子树打印每个元素的 x/y/width/height，用于定位"哪张卡 bottom 超出父容器"。

用法
----
    .venv/Scripts/python.exe tools/qml_geometry_smoke.py <视图qml绝对路径> [empty|populated]

环境变量：QT_QPA_PLATFORM=offscreen（脚本内已设）

样例数据默认是一份终末地森空岛 skland 查询结果；若你的视图用别的 property 名，
改 __SAMPLE__ 即可。
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QUrl, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtQuick import QQuickView, QQuickItem
from pyhuskarui.husapp import HusApp
import pyhuskarui.qml_rc
import pyhuskarui.resources_rc
import pyhuskarui.shaders_rc


# ── 样例数据：按你自己视图的 property 名修改 ──
__SAMPLE__ = {
    "query_type": "skland",
    "success": True,
    "message": "",
    "data": {
        "role_name": "测试干员",
        "server_name": "森空岛-国产服",
        "uid": "123456789",
        "daily": {"current": 80, "total": 100},
        "extras": {
            "level": 45,
            "world_level": 8,
            "stamina": {"cur": 120, "max": 240},
            "radio": {"cur": 30, "max": 60},
            "achieve": {"count": 128},
            "char_num": 21,
            "main_mission": {"description": "第三章·深入"},
        },
        "raw": {"detail": {"base": {"name": "测试干员", "level": 45}}},
    },
}


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else None
    mode = sys.argv[2] if len(sys.argv) > 2 else "populated"
    if not target:
        print("用法: qml_geometry_smoke.py <视图qml路径> [empty|populated]")
        sys.exit(1)

    app = QGuiApplication(sys.argv)
    view = QQuickView()
    HusApp.initialize(view.engine())  # 注册 HusRadius 等 HuskarUI 类型，否则编译失败
    view.engine().addImportPath(os.path.join(os.path.dirname(target), ".."))
    view.engine().addImportPath(os.path.join(os.path.dirname(target), "..", "components"))
    view.setResizeMode(QQuickView.SizeRootObjectToView)
    view.setSource(QUrl.fromLocalFile(target))
    for e in view.errors():
        print("LOAD ERR:", e.toString())
    view.setWidth(1100)
    view.setHeight(850)
    view.show()
    app.processEvents()

    root = view.rootObject()
    if root is None:
        print("rootObject is None —— 视图未加载")
        sys.exit(1)

    if mode == "populated":
        root.setProperty("queryResult", __SAMPLE__)
        app.processEvents()
        QTimer.singleShot(150, lambda: None)
        app.processEvents()

    # 1) 顶层卡片 / 滚动容器总览
    print("=== 卡片与滚动容器总览 ===")

    def focus(o):
        if not isinstance(o, QQuickItem):
            return
        cn = o.metaObject().className()
        if "Flickable" in cn or "ScrollView" in cn:
            ch = o.property("contentHeight")
            print(f"  SCROLL h={o.height():.0f} contentH={ch} y={o.y():.0f}")
        if "Card" in cn:
            print(f"  CARD y={o.y():.0f} h={o.height():.0f} bottom={o.y()+o.height():.0f}")
        for c in o.children():
            focus(c)

    focus(root)

    # 2) 全量子树几何（用于人工定位溢出）
    print("\n=== 全量子树几何 ===")
    lines = []

    def dump(o, depth):
        if not isinstance(o, QQuickItem):
            return
        cn = o.metaObject().className()
        tag = ""
        if "Card" in cn or "ScrollView" in cn or "Flickable" in cn:
            tag = f"[{cn}]"
        lines.append(
            f"{'  '*depth}{cn} x={o.x():.0f} y={o.y():.0f} "
            f"w={o.width():.0f} h={o.height():.0f} {tag}"
        )
        for c in o.children():
            dump(c, depth + 1)

    dump(root, 0)
    print("\n".join(lines))


if __name__ == "__main__":
    main()
