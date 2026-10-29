"""pytest 全局配置：把仓库父目录加入 sys.path。

各套件脚本自身也会做同样的插入（依赖目录名为 game_auto），
这里兜底，保证从任意工作目录执行 pytest 都能导入 game_auto 包。
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_PARENT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_PARENT) not in sys.path:
    sys.path.insert(0, str(_REPO_PARENT))
