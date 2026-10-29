"""历史回归套件的 pytest 包装。

套件本体是独立脚本（tests/suite_*.py）：内部打印 PASS/FAIL，
失败时以非零码退出。这里逐个子进程执行，原因：
- 套件会 monkeypatch 模块级状态（pyautogui / OCR / DB），同进程连跑会互相污染；
- 子进程隔离后，单个套件崩溃不影响其余用例。

改名规则：原 artifacts/test_*.py → tests/suite_*.py，
避免 pytest 把套件内部的辅助函数误当用例收集。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SUITE_DIR = Path(__file__).resolve().parent
SUITES = sorted(p.name for p in SUITE_DIR.glob("suite_*.py"))

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="本项目仅支持 Windows")


@pytest.mark.parametrize("suite", SUITES)
def test_legacy_suite(suite: str) -> None:
    path = SUITE_DIR / suite
    proc = subprocess.run(
        [sys.executable, str(path)],
        cwd=str(SUITE_DIR.parent),
        capture_output=True,
        timeout=300,
    )
    output = (proc.stdout + proc.stderr).decode("utf-8", errors="replace")
    assert proc.returncode == 0, f"{suite} 退出码 {proc.returncode}\n{output[-4000:]}"
