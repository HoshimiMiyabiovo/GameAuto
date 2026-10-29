"""重新冻结 requirements.lock（当前虚拟环境的精确快照）。

用法（在仓库根目录、用项目 venv 执行）：
    .venv\\Scripts\\python.exe tools\\refresh_requirements_lock.py

为什么是 freeze 而不是 uv lock / pip-compile：
- 本项目是单机 Windows 应用，锁的目的只是“复现已验证可用的环境”；
- RapidOCR 依赖 onnxruntime，而实际必须跑 GPU 版 onnxruntime-gpu。在线解析
  会把 CPU 版 onnxruntime 带进来，导致 OCR 静默退回 CPU（验证过的事故）；
- 因此锁文件记录全部 99 个已安装包，setup_deps.bat 用 --no-deps 精确回放。

改动依赖的正确顺序：改 requirements.txt → 在本机验证 → 执行本脚本刷新锁。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCK_FILE = ROOT / "requirements.lock"

# 开发工具及其私有依赖不进运行时锁
DEV_ONLY = {"pytest", "iniconfig", "pluggy", "ruff", "pygments"}

HEADER = """\
# 自动生成，请勿手改。
# 由 tools/refresh_requirements_lock.py 从已验证环境冻结；安装时使用 --no-deps：
#   uv pip install --python .venv/Scripts/python.exe --no-deps -r requirements.lock
# 必须保留 onnxruntime-gpu、且不得出现 CPU 版 onnxruntime（否则 RapidOCR 静默降级）。
"""


def main() -> int:
    frozen = subprocess.run(
        [sys.executable, "-m", "pip", "freeze"],
        capture_output=True, text=True, check=True,
    ).stdout
    lines = []
    for line in frozen.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name = re.split(r"[=<>!~ ]", line, maxsplit=1)[0].strip().lower()
        if name in DEV_ONLY:
            continue
        lines.append(line)
    lines.sort(key=str.lower)
    LOCK_FILE.write_text(HEADER + "\n".join(lines) + "\n", encoding="utf-8")
    print(f"已写入 {LOCK_FILE}（{len(lines)} 个包）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
