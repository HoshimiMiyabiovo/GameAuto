"""历史回归套件的 pytest 包装。

套件本体是独立脚本（tests/suite_*.py）：内部打印 PASS/FAIL，
失败时以非零码退出。这里逐个子进程执行，原因：
- 套件会 monkeypatch 模块级状态（pyautogui / OCR / DB），同进程连跑会互相污染；
- 子进程隔离后，单个套件崩溃不影响其余用例。

改名规则：原 artifacts/test_*.py → tests/suite_*.py，
避免 pytest 把套件内部的辅助函数误当用例收集。
"""

from __future__ import annotations

import os
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
    # 套件里有 ✅ 等 GBK 不可编码字符；子进程被管道捕获时 stdout 默认 cp936，
    # 打 PASS/FAIL 行会 UnicodeEncodeError（2026-11-02 全套跑失败实锤）。
    # 统一让子进程用 UTF-8，包装层按 UTF-8 解码，中文输出也不再乱码。
    env = {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        # src 布局：套件脚本即使没自己插 sys.path，也能导入 game_auto
        "PYTHONPATH": str(SUITE_DIR.parent / "src") + os.pathsep + os.environ.get("PYTHONPATH", ""),
    }
    proc = subprocess.run(
        [sys.executable, str(path)],
        cwd=str(SUITE_DIR.parent),
        capture_output=True,
        timeout=300,
        env=env,
    )
    output = (proc.stdout + proc.stderr).decode("utf-8", errors="replace")
    assert proc.returncode == 0, f"{suite} 退出码 {proc.returncode}\n{output[-4000:]}"
