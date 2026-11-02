"""ui/bridge.py 与各视图 mixin 共用的常量与小工具。"""

import logging
from typing import Dict

logger = logging.getLogger("GameAutomation.Bridge")

TONE_INFO = "info"
TONE_RUNNING = "running"
TONE_SUCCESS = "success"
TONE_WARNING = "warning"
TONE_DANGER = "danger"

TONE_MAP: Dict[str, tuple] = {
    TONE_INFO:    ("待命中", "#dff1e4", "#2f7d4a"),
    TONE_RUNNING: ("进行中", "#e9f3fb", "#295d86"),
    TONE_SUCCESS: ("已完成", "#dff1e4", "#2f7d4a"),
    TONE_WARNING: ("请留意", "#faebd7", "#b66f1f"),
    TONE_DANGER:  ("已中断", "#f7dfdd", "#b04b44"),
}

def _mask_address(addr: str) -> str:
    """日志中脱敏邮箱地址：a*c@example.com。"""
    if not addr or "@" not in addr:
        return "***"
    name, domain = addr.split("@", 1)
    if len(name) <= 2:
        masked = "*" * len(name)
    else:
        masked = name[0] + "*" * (len(name) - 2) + name[-1]
    return f"{masked}@{domain}"


def _make_ui_click_recorder_class():
    """懒加载 ClickRecorder，返回带 UI 回调的子类。

    子类覆写 _on_click：先走基类记录逻辑，再把最新一次点击推给 bridge，
    由 bridge 发信号实时刷新 QML 的计数与"最近点击"面板。
    注意 _on_click 运行在 pynput 监听线程，Qt 信号跨线程会自动排队投递。
    """
    from ...runtime.click_sequence import ClickRecorder

    class _UiClickRecorder(ClickRecorder):
        def __init__(self, bridge, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._bridge = bridge

        def _on_click(self, x, y, button, pressed):
            super()._on_click(x, y, button, pressed)
            try:
                self._bridge._on_click_recorded(self)
            except Exception:
                logger.debug("[Bridge] 推送录制点击到 UI 失败", exc_info=True)

        def _on_key(self, key):
            super()._on_key(key)
            # 键盘事件/标记也要实时推 UI（停止热键时 events 无新增，推送无害）
            try:
                if self._recording:
                    self._bridge._on_click_recorded(self)
            except Exception:
                logger.debug("[Bridge] 推送录制按键到 UI 失败", exc_info=True)

    return _UiClickRecorder
