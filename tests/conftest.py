"""pytest 全局配置：把 src 目录加入 sys.path。

src 布局：包在 `<repo>/src/game_auto`，导入时需要把 `<repo>/src` 放进 sys.path。
这里兜底，保证从任意工作目录执行 pytest 都能导入 game_auto 包。
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))
