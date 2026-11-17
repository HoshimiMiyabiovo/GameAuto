"""Theme tokens — Codex-style neutral palette (2026-11-17 restyle).

QML 侧唯一真值是 components/Theme.qml（单例）与 Main.qml 的 HusTheme.install*
基色引导；本文件保留同名 token 作为 Python 侧文档与 PaperTheme 兼容层
（apply_tokens_to_engine 当前未接线，留作 WebUI/Flet 回退侧参考）。
两侧改主题必须同步。

Reference: https://pyhuskarui.deepwiki.com
"""

PALETTE = {
    "page": "#f7f8fa",
    "panel": "#ffffff",
    "panelAlt": "#f6f6f7",
    "card": "#ffffff",
    "cardActive": "#ecfdf5",
    "line": "#e4e4e7",
    "accent": "#059669",
    "accentSoft": "#ecfdf5",
    "text": "#18181b",
    "muted": "#71717a",
    "warning": "#d97706",
    "warningSoft": "#fffbeb",
    "danger": "#dc2626",
    "dangerSoft": "#fef2f2",
    "info": "#2563eb",
    "infoSoft": "#eff6ff",
    "logBg": "#18181b",
    "logFg": "#f4f4f5",
}

# HusTheme-style colour tokens
THEME_TOKENS = {
    "colorPrimary": PALETTE["accent"],
    "colorPrimaryBg": PALETTE["accentSoft"],
    "colorBgBase": PALETTE["page"],
    "colorBgContainer": PALETTE["panel"],
    "colorBgElevated": PALETTE["card"],
    "colorBgLayout": PALETTE["panelAlt"],
    "colorTextBase": PALETTE["text"],
    "colorTextSecondary": PALETTE["muted"],
    "colorBorder": PALETTE["line"],
    "colorWarning": PALETTE["warning"],
    "colorWarningBg": PALETTE["warningSoft"],
    "colorError": PALETTE["danger"],
    "colorErrorBg": PALETTE["dangerSoft"],
    "colorInfo": PALETTE["info"],
    "colorInfoBg": PALETTE["infoSoft"],
    "borderRadius": 8,
    "fontFamily": "Microsoft YaHei UI",
}


def apply_tokens_to_engine(engine) -> None:
    """Push theme tokens into the QML engine context as 'PaperTheme'.

    QML views can bind to these values via:
        PaperTheme.colorPrimary, PaperTheme.colorBgBase, etc.
    """
    from PySide6.QtCore import QObject, Property, Signal

    class _ThemeObj(QObject):
        def __init__(self, tokens: dict, parent=None):
            super().__init__(parent)
            self._tokens = tokens

        def get(self, key: str) -> str:
            return str(self._tokens.get(key, ""))

    theme_obj = _ThemeObj(THEME_TOKENS)
    engine.rootContext().setContextProperty("PaperTheme", theme_obj)
