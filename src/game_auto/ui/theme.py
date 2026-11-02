"""Theme tokens derived from the original Flet PALETTE.

Maps the warm paper-like palette to HusTheme-compatible token names.
Reference: https://pyhuskarui.deepwiki.com
"""

PALETTE = {
    "page": "#f6f1e8",
    "panel": "#fffaf2",
    "panelAlt": "#f4ecdf",
    "card": "#fffdf9",
    "cardActive": "#eef8f1",
    "line": "#dbcdbd",
    "accent": "#18a05f",
    "accentSoft": "#dff1e4",
    "text": "#2c241d",
    "muted": "#6d6257",
    "warning": "#b66f1f",
    "warningSoft": "#faebd7",
    "danger": "#b04b44",
    "dangerSoft": "#f7dfdd",
    "info": "#295d86",
    "infoSoft": "#e9f3fb",
    "logBg": "#1f1d1a",
    "logFg": "#f5efe6",
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
    "borderRadius": 16,
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
