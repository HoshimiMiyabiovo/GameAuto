"""沙箱临时目录垫片回归（2026-11-22）。

背景：DSH workspace-write 沙箱下 tempfile.mkdtemp/TemporaryDirectory 用
mode=0700 建目录 → 受保护 DACL，受限令牌写不进自己建的目录（WinError 5），
约 40 个套件本机假红。tests/sandbox_temp.py 把 mkdtemp 换成默认权限版。

本套件锁：
  [1] install 幂等 / uninstall 还原
  [2] mkdtemp：建目录可写可读可删（str 与 bytes 参数）
  [3] TemporaryDirectory：上下文内可写、退出自动清理
  [4] 文件类 API 不受影响（mkstemp / NamedTemporaryFile）
  [5] 接线：conftest 与全部用 tempfile 的套件都装了垫片

跑法：.venv\\Scripts\\python.exe tests/suite_sandbox_temp_shim_20261122.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import io as _io

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import sandbox_temp

FAILURES = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'} {label}" + ("" if ok else f"  {detail}"))
    if not ok:
        FAILURES.append(label)


def test_install_idempotent():
    print("[1] install 幂等 / uninstall 还原")
    orig = tempfile.mkdtemp
    check("首次 install 生效", sandbox_temp.install() is True or tempfile.mkdtemp is sandbox_temp._sandbox_mkdtemp)
    check("重复 install 返回 False", sandbox_temp.install() is False)
    check("mkdtemp 已被替换", tempfile.mkdtemp is sandbox_temp._sandbox_mkdtemp)
    sandbox_temp.uninstall()
    check("uninstall 还原原函数", tempfile.mkdtemp is orig)
    sandbox_temp.install()  # 后续用例需要垫片在位


def test_mkdtemp_writable():
    print("[2] mkdtemp：可写可读可删（str / bytes）")
    d = tempfile.mkdtemp(prefix="shim_reg_")
    p = os.path.join(d, "a.txt")
    try:
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("ok")
        check("目录内可写", open(p, encoding="utf-8").read() == "ok")
        check("目录可列", os.listdir(d) == ["a.txt"], str(os.listdir(d)))
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)
    check("目录可删", not os.path.exists(d))
    db = tempfile.mkdtemp(prefix=b"shim_reg_b_", dir=os.fsencode(tempfile.gettempdir()))
    try:
        check("bytes 参数返回 bytes", isinstance(db, bytes), str(type(db)))
        with open(os.path.join(db, b"b"), "wb") as fh:
            fh.write(b"1")
        check("bytes 目录内可写", True)
    finally:
        import shutil
        shutil.rmtree(db, ignore_errors=True)


def test_temporary_directory():
    print("[3] TemporaryDirectory：可写 + 自动清理")
    with tempfile.TemporaryDirectory(prefix="shim_reg_td_") as td:
        with open(os.path.join(td, "c.txt"), "w", encoding="utf-8") as fh:
            fh.write("1")
        check("上下文内可写", os.path.isfile(os.path.join(td, "c.txt")))
    check("退出后自动清理", not os.path.exists(td), td)


def test_file_apis_untouched():
    print("[4] 文件类 API 不受影响")
    fd, p = tempfile.mkstemp(prefix="shim_reg_f_")
    try:
        os.write(fd, b"x")
        os.close(fd)
        check("mkstemp 可写", os.path.getsize(p) == 1)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        os.unlink(p)
    with tempfile.NamedTemporaryFile(prefix="shim_reg_n_", delete=False) as fh:
        fh.write(b"y")
        np = fh.name
    try:
        check("NamedTemporaryFile 可写", os.path.getsize(np) == 1)
    finally:
        os.unlink(np)


def test_wiring():
    print("[5] 接线：conftest + 所有用 tempfile 的套件")
    conftest = (PROJECT / "tests" / "conftest.py").read_text(encoding="utf-8")
    check("conftest 装了垫片", "sandbox_temp" in conftest and "_install_sandbox_temp()" in conftest)
    missing = []
    for f in sorted((PROJECT / "tests").glob("suite_*.py")):
        src = f.read_text(encoding="utf-8")
        uses = ("tempfile.mkdtemp" in src or "tempfile.TemporaryDirectory" in src
                or "from tempfile import" in src and "mkdtemp" in src)
        if uses and "sandbox_temp" not in src:
            missing.append(f.name)
    check("用 tempfile 目录 API 的套件全部接线", not missing, str(missing))
    ci = (PROJECT / "tests" / "ci_suites.txt").read_text(encoding="utf-8")
    check("ci_suites.txt 登记本套件", "suite_sandbox_temp_shim_20261122.py" in ci)


def main():
    for t in (test_install_idempotent, test_mkdtemp_writable,
              test_temporary_directory, test_file_apis_untouched, test_wiring):
        t()
    print()
    if FAILURES:
        print(f"FAIL: {len(FAILURES)} 项失败 -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
