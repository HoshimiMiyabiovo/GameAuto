"""UiBridge 的按视图 mixin。

ui/bridge.py 只保留核心状态、Qt 属性/信号与事件分发；各视图的 @Slot 实现
按 QML 页面拆到这里（PySide6 支持在普通 mixin 里声明 @Slot/@Property/@Signal，
最终 QObject 子类会统一注册进 metaobject）。
"""
