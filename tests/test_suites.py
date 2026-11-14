"""历史回归套件的 pytest 包装。

套件本体是独立脚本（tests/suite_*.py）：内部打印 PASS/FAIL，
失败时以非零码退出。这里逐个子进程执行，原因：
- 套件会 monkeypatch 模块级状态（pyautogui / OCR / DB），同进程连跑会互相污染；
- 子进程隔离后，单个套件崩溃不影响其余用例。

改名规则：原 artifacts/test_*.py → tests/suite_*.py，
避免 pytest 把套件内部的辅助函数误当用例收集。

## CI 分层（2026-11-14 起）

本机跑全量（`pytest tests`，README 的老口径不变）；CI 只跑"不依赖真机/GPU/本地
配置"的那部分，清单在 `tests/ci_suites.txt`：

    suite_xxx.py                      # 进 CI
    !suite_yyy.py  # 需要 CUDA 真机    # 不进 CI（前缀 ! + 原因）

- `GAMEAUTO_TEST_TIER=ci`：只跑清单内套件（workflow 设置）；其余套件以 skip 呈现，
  不会假装通过。
- `GAMEAUTO_TEST_SHARD=1/3`：按序号取模分片，CI 三片并行压总时长。
- `GAMEAUTO_TEST_SUITE_TIMEOUT_SEC`：覆盖单个套件超时（默认 300s）。
- `test_manifest_covers_all_suites` 是漂移闸门：新增套件不同步清单会直接红，
  逼一次显式决策（"进 CI"还是"真机专用"），而不是让它悄悄不被执行。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

SUITE_DIR = Path(__file__).resolve().parent
MANIFEST_PATH = SUITE_DIR / "ci_suites.txt"
ALL_SUITES = sorted(p.name for p in SUITE_DIR.glob("suite_*.py"))
DEFAULT_SUITE_TIMEOUT = 300.0

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="本项目仅支持 Windows")


def _parse_manifest() -> tuple[list[str], dict[str, str]]:
    """读 ci_suites.txt：返回（进 CI 的套件顺序表, {排除套件: 原因}）。

    行格式：`suite_x.py` = 进 CI；`!suite_y.py  # 原因` = 不进 CI。
    `#` 之后为注释；空行忽略；重复行视为写错，直接抛错让 CI 红。
    """
    if not MANIFEST_PATH.exists():
        return [], {}
    include: list[str] = []
    exclude: dict[str, str] = {}
    for raw in MANIFEST_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, _, reason = line.partition("#")
        name = name.strip()
        reason = reason.strip()
        if not name:
            continue
        excluded = name.startswith("!")
        if excluded:
            name = name[1:].strip()
        if (name in exclude) or (name in include):
            raise ValueError(f"ci_suites.txt 重复条目: {name}")
        if excluded:
            exclude[name] = reason or "未写原因"
        else:
            include.append(name)
    return include, exclude


def _selected_suites() -> tuple[list[str], list[tuple[str, str]]]:
    """按 tier/shard 选套件：返回（要跑的套件, [(被 skip 的套件, 原因)]）。"""
    include, exclude = _parse_manifest()
    tier = os.environ.get("GAMEAUTO_TEST_TIER", "").strip().lower()
    if tier != "ci":
        # 本机默认全量：被排除的套件也照跑（真机机器才能验证它们）
        return ALL_SUITES, []

    runnable = [name for name in ALL_SUITES if name in include]
    skipped = [(name, exclude.get(name, "不在 tests/ci_suites.txt")) for name in ALL_SUITES if name not in include]

    spec = os.environ.get("GAMEAUTO_TEST_SHARD", "").strip()
    if spec:
        parts = spec.split("/")
        if len(parts) != 2 or not all(p.strip().isdigit() for p in parts):
            raise ValueError(f"GAMEAUTO_TEST_SHARD 期望 'i/n'，实际 {spec!r}")
        index, total = int(parts[0]), int(parts[1])
        if not 1 <= index <= total:
            raise ValueError(f"GAMEAUTO_TEST_SHARD 片号越界: {spec!r}")
        runnable = [name for i, name in enumerate(runnable) if i % total == index - 1]
        # 分片只切"要跑的"，skip 列表每片都全量呈现，避免误读成"这片少跑了"
    return runnable, skipped


def _suite_timeout() -> float:
    raw = os.environ.get("GAMEAUTO_TEST_SUITE_TIMEOUT_SEC", "").strip()
    if not raw:
        return DEFAULT_SUITE_TIMEOUT
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_SUITE_TIMEOUT
    return value if value > 0 else DEFAULT_SUITE_TIMEOUT


RUNNABLE, TIER_SKIPPED = _selected_suites()
PARAMS = [pytest.param(name, id=name) for name in RUNNABLE]
PARAMS += [
    pytest.param(name, marks=pytest.mark.skip(reason=f"CI 分层未选中：{reason}"), id=name)
    for name, reason in TIER_SKIPPED
]


def test_manifest_covers_all_suites() -> None:
    """漂移闸门：tests/suite_*.py 与 ci_suites.txt 必须一一对应。"""
    include, exclude = _parse_manifest()
    listed = set(include) | set(exclude)
    missing = sorted(set(ALL_SUITES) - listed)
    stale = sorted(listed - set(ALL_SUITES))
    assert not missing, f"新增/未登记套件，请在 tests/ci_suites.txt 里显式选择是否进 CI: {missing}"
    assert not stale, f"ci_suites.txt 里有已不存在的套件，请删掉这些行: {stale}"


@pytest.mark.parametrize("suite", PARAMS)
def test_legacy_suite(suite: str) -> None:
    path = SUITE_DIR / suite
    # 套件里有 ✅ 等 GBK 不可编码字符；子进程被管道捕获时 stdout 默认 cp936，
    # 打 PASS/FAIL 行会 UnicodeEncodeError（2026-11-02 全套跑失败实锤）。
    # 统一让子进程用 UTF-8，包装层按 UTF-8 解码，中文输出也不再乱码。
    env = {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        # 2026-11-04：套件里实例化 AutomationController 会挂生产文件 handler，
        # FakeSession 等测试日志会写进 data/logs/auto_runner.log。统一关掉，
        # 生产 GUI/CLI 不受影响（默认不开这个变量）。
        "GAMEAUTO_LOG_FILE_DISABLED": "1",
        # src 布局：套件脚本即使没自己插 sys.path，也能导入 game_auto
        "PYTHONPATH": str(SUITE_DIR.parent / "src") + os.pathsep + os.environ.get("PYTHONPATH", ""),
    }
    proc = subprocess.run(
        [sys.executable, str(path)],
        cwd=str(SUITE_DIR.parent),
        capture_output=True,
        timeout=_suite_timeout(),
        env=env,
    )
    output = (proc.stdout + proc.stderr).decode("utf-8", errors="replace")
    assert proc.returncode == 0, f"{suite} 退出码 {proc.returncode}\n{output[-4000:]}"
