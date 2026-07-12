"""PyHuskarUI main window launcher (参考，不再被直接调用)。

QML 初始化已内联到 app/gui.py 的 _launch_qml_ui() 中，
避免 python -m 模式下跨模块导入链导致的 QML 资源加载异常。

此文件保留作为参考和独立测试入口。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

logger = logging.getLogger("GameAutomation.UI")


def run_huskar_ui(dep_report=None) -> None:
    """Launch the game automation app with PyHuskarUI (参考实现)。

    生产环境请使用 app/gui.py -> _launch_qml_ui()。
    """
    from PySide6.QtWidgets import QApplication
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtCore import QUrl
    from pyhuskarui.husapp import HusApp

    app = QApplication(sys.argv)
    app.setOrganizationName("GameAuto")
    app.setApplicationName("游戏自动化助手")

    engine = QQmlApplicationEngine()
    HusApp.initialize(engine)

    from .bridge import UiBridge
    from .shared import UILogHandler
    from .theme import apply_tokens_to_engine

    bridge = UiBridge()
    engine.rootContext().setContextProperty("bridge", bridge)
    UILogHandler.set_bridge(bridge)
    apply_tokens_to_engine(engine)

    qml_dir = Path(__file__).resolve().parent / "qml"
    main_qml = qml_dir / "Main.qml"
    if not main_qml.exists():
        logger.error("Main.qml not found at %s", main_qml)
        sys.exit(1)

    engine.addImportPath(str(qml_dir))
    engine.load(QUrl.fromLocalFile(str(main_qml)))

    if not engine.rootObjects():
        logger.error("Failed to load QML — no root objects created.")
        sys.exit(-1)

    logger.info("PyHuskarUI window launched with %d root object(s).",
                len(engine.rootObjects()))
    sys.exit(app.exec())
