import ctypes
import logging
import os
import sys
from pathlib import Path

from ..services.dependency_manager import run_full_dependency_check
from ..services.instance_lock import acquire_instance_lock
from ..services.mem_reduct_detect import warn_once_at_startup


def main():
    bootstrap_logger = logging.getLogger("GameAutoBootstrap")
    if not bootstrap_logger.handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="[%(asctime)s.%(msecs)03d] [%(levelname)-8s] %(name)s:%(funcName)s:%(lineno)d - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    logging.getLogger("GameAutomation").setLevel(logging.DEBUG)

    existing_pid = acquire_instance_lock()
    if existing_pid:
        msg = (
            f"已有 game_auto 实例在运行 (PID {existing_pid})，请先关闭它再启动新的窗口。\n\n"
            "避免多个窗口同时跑 OK 监控导致任务竞争和日志混乱。"
        )
        bootstrap_logger.error("[启动] 拒绝启动，检测到重复实例 PID=%d", existing_pid)
        ctypes.windll.user32.MessageBoxW(0, msg, "启动失败 - 已有实例", 0x10)
        return

    warn_once_at_startup(bootstrap_logger)

    dep_report = run_full_dependency_check(bootstrap_logger, auto_install=True)

    try:
        _launch_qml_ui(dep_report)
    except Exception as e:
        bootstrap_logger.exception("[启动] QML UI 启动失败")
        ctypes.windll.user32.MessageBoxW(
            0,
            f"GUI 启动失败:\n{str(e)}",
            "启动失败",
            0x10,
        )
        raise


def _launch_qml_ui(dep_report=None) -> None:
    """内联 QML 初始化，避免 python -m 跨模块导入链导致资源加载异常。"""
    ui_logger = logging.getLogger("GameAutomation.UI")

    from PySide6.QtWidgets import QApplication
    from PySide6.QtQml import QQmlApplicationEngine
    from PySide6.QtCore import QUrl
    from pyhuskarui.husapp import HusApp

    app = QApplication(sys.argv)
    app.setOrganizationName("GameAuto")
    app.setApplicationName("游戏自动化助手")
    # 明确启用：最后一个窗口关闭时退出事件循环
    app.setQuitOnLastWindowClosed(True)

    engine = QQmlApplicationEngine()
    HusApp.initialize(engine)

    from ..ui.bridge import UiBridge
    from ..ui.shared import UILogHandler
    from ..ui.theme import apply_tokens_to_engine

    bridge = UiBridge()
    engine.rootContext().setContextProperty("bridge", bridge)
    UILogHandler.set_bridge(bridge)

    # 将 UILogHandler 挂到 GameAutomation logger，后端日志自动转发到 QML
    game_logger = logging.getLogger("GameAutomation")
    ui_log_handler = UILogHandler()
    ui_log_handler.setLevel(logging.INFO)
    game_logger.addHandler(ui_log_handler)

    apply_tokens_to_engine(engine)

    from ..settings.manager import is_setup_completed
    if not is_setup_completed():
        bridge.currentView = "setup_wizard"
        ui_logger.info("[启动] 首次启动检测：未完成路径配置，进入向导视图")

    qml_dir = Path(__file__).resolve().parent.parent / "ui" / "qml"
    main_qml = qml_dir / "Main.qml"
    if not main_qml.exists():
        ui_logger.error("Main.qml not found at %s", main_qml)
        sys.exit(1)

    engine.addImportPath(str(qml_dir))
    engine.load(QUrl.fromLocalFile(str(main_qml)))

    if not engine.rootObjects():
        ui_logger.error("Failed to load QML — no root objects created.")
        sys.exit(-1)

    ui_logger.info("PyHuskarUI window launched with %d root object(s).",
                   len(engine.rootObjects()))

    # ── 退出清理：停止后台自动化任务，防止子进程残留导致终端不释放 ──
    def _cleanup_on_quit():
        ui_logger.info("[退出] 开始清理资源...")
        try:
            if bridge._controller is not None:
                bridge._controller.stop_current_task = True
                ui_logger.info("[退出] 已请求停止自动化任务")
        except Exception as exc:
            ui_logger.warning("[退出] 停止自动化任务时异常: %s", exc)
        try:
            # 移除日志 handler 防止 Qt 对象被回收后回调崩溃
            game_logger.removeHandler(ui_log_handler)
        except Exception:
            pass
        # 强制刷新所有日志 handler，确保退出前日志落盘
        for h in logging.getLogger().handlers:
            try:
                h.flush()
            except Exception:
                pass
        # 手动释放实例锁：os._exit 会跳过 atexit 钩子，必须先释放
        # 否则下次启动会误判为"已有实例在运行"
        try:
            from ..services.instance_lock import get_lock_path, _release_instance_lock
            _release_instance_lock(get_lock_path())
            ui_logger.info("[退出] 已释放实例锁")
        except Exception as exc:
            ui_logger.warning("[退出] 释放实例锁失败: %s", exc)

    app.aboutToQuit.connect(_cleanup_on_quit)

    exit_code = app.exec()
    _cleanup_on_quit()
    ui_logger.info("[退出] 事件循环结束，exit_code=%s", exit_code)
    # 用 os._exit 强制终止解释器，避免 daemon 线程（requests/psutil/sqlite）
    # 和 subprocess 子进程持有 console handle 导致终端不返回提示符。
    # atexit 钩子已在 _cleanup_on_quit 中手动执行（实例锁已释放）。
    os._exit(exit_code)


if __name__ == "__main__":
    main()
