"""pytest 全局配置：把 src 目录加入 sys.path。

src 布局：包在 `<repo>/src/game_auto`，导入时需要把 `<repo>/src` 放进 sys.path。
这里兜底，保证从任意工作目录执行 pytest 都能导入 game_auto 包。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))


def _pin_real_tempdir() -> None:
    """Git Bash 给子进程传 TEMP=/tmp，Windows Python 找不到 E:\tmp 时 tempfile
    一路回退到 cwd——pytest 主进程与所有套件子进程的 mkdtemp/tmp_path 全建进
    项目根（2026-11-16 实锤：一轮全量漏一个 tmp*/dump.log，17 个文件夹）。
    检测到回退就把 TEMP/TMP 钉回真实用户临时目录；套件子进程经 os.environ
    继承修正值，不需要各自再处理。
    """
    if os.name != "nt":
        return
    try:
        if Path(tempfile.gettempdir()).resolve() != Path.cwd().resolve():
            return
    except OSError:
        return
    for candidate in (
        os.path.expandvars(r"%LOCALAPPDATA%\Temp"),
        os.path.expandvars(r"%SYSTEMROOT%\Temp"),
        os.path.expandvars(r"%SYSTEMDRIVE%\Temp"),
    ):
        try:
            if candidate and Path(candidate).is_dir():
                os.environ["TEMP"] = os.environ["TMP"] = candidate
                tempfile.tempdir = candidate
                return
        except OSError:
            continue


_pin_real_tempdir()

# 沙箱兼容垫片（2026-11-22）：tempfile 目录创建改默认权限，否则 0700 受保护
# DACL 让受限令牌写不进自己建的临时目录（约 40 个套件本机假红）。
# 直跑 python tests/suite_x.py 不经 conftest，由各套件自己 install()。
from sandbox_temp import install as _install_sandbox_temp  # noqa: E402

_install_sandbox_temp()


def pytest_sessionfinish(session, exitstatus) -> None:
    """收工打印最慢 10 个套件，避免"回归突然变慢"只能靠猜。

    pytest 的 hook 只在 conftest/插件里注册，写在 test_suites.py 里不会执行；
    计时数据存在该模块级字典里，这里按模块名取回。
    """
    module = sys.modules.get("test_suites")
    durations = getattr(module, "SUITE_DURATIONS", {}) if module else {}
    if not durations:
        return
    slow = sorted(durations.items(), key=lambda kv: kv[1], reverse=True)[:10]
    total = sum(durations.values())
    lines = [f"\n[回归耗时] 合计 {total:.1f}s / {len(durations)} 套件，Top10："]
    lines.extend(f"  {seconds:7.1f}s  {name}" for name, seconds in slow)
    sys.stderr.write("\n".join(lines) + "\n")
