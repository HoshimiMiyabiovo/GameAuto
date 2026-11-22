"""沙箱兼容的临时目录垫片（2026-11-22，只影响测试进程）。

背景：DSH workspace-write 沙箱下，Python `tempfile.mkdtemp()` /
`TemporaryDirectory()` 用 mode=0700 建目录 → 写出的是**受保护 DACL**（丢掉继承），
沙箱受限令牌拿不到任何授权 → 目录建完自己立刻写不进、读不了、删不掉
（WinError 5 / PermissionError，连 icacls 都 Access is denied）。
实测约 40 个套件在本机因此假红（suite_tool_manager / suite_sign_resilience /
suite_flow_diagnostics_annotate / suite_monthly_auto_schedule …）。

机制对照（受限令牌，工作区内实测）：
  os.mkdir(p) / os.mkdir(p, 0o755) / os.makedirs(p)  → 继承授权，可写可读可删
  os.mkdir(p, 0o700) / tempfile.mkdtemp()            → 受保护 DACL，全被拒
  tempfile.mkstemp / NamedTemporaryFile（文件）       → 正常（文件不受影响）

做法：整体替换 `tempfile.mkdtemp` 为「默认权限 os.mkdir + 随机名重试」版。
`TemporaryDirectory.__init__` 走模块全局 mkdtemp，一处替换两者全覆盖；
文件类 API（mkstemp/NamedTemporaryFile）不动。幂等：重复 install() 只装一次。
（3.12/3.13 的 stdlib 没有 _mkdtemp_inner 内部函数，别找它——直接换 mkdtemp。）

接线（两条路都要，缺一就有套件漏网）：
  ① tests/conftest.py 顶部 install() —— 覆盖 `pytest tests`；
  ② 每个用到 tempfile 的 suite_*.py 在 sys.path.insert 之后
     `from sandbox_temp import install; install()` —— 覆盖 `python tests/suite_x.py`
     直跑（sitecustomize 在 site 初始化阶段执行，早于脚本目录进 sys.path，
     实测不生效，别走那条路）。

回归：tests/suite_sandbox_temp_shim_20261122.py。
"""

from __future__ import annotations

import errno
import os
import sys
import tempfile

_installed = False
_orig_mkdtemp = None


def _sandbox_mkdtemp(suffix=None, prefix=None, dir=None):
    """默认权限建目录版 mkdtemp（签名与标准库一致，含 bytes 参数支持）。

    不用 0700：os.mkdir 不带 mode → 继承父目录授权，沙箱令牌可写。
    随机名 + 重名重试沿用标准库语义（_sanitize_params/_get_candidate_names/TMP_MAX）。
    """
    prefix, suffix, dir, output_type = tempfile._sanitize_params(prefix, suffix, dir)
    names = tempfile._get_candidate_names()
    if output_type is bytes:
        names = map(os.fsencode, names)
    for _ in range(tempfile.TMP_MAX):
        name = next(names)
        file = os.path.join(dir, prefix + name + suffix)
        sys.audit("tempfile.mkdtemp", file)
        try:
            os.mkdir(file)  # 不带 mode：继承授权，沙箱可写（0700 正是事故源）
        except FileExistsError:
            continue
        except PermissionError:
            # 与标准库同款：Windows 上重名目录也可能报 PermissionError；
            # 父目录可写就换名重试，不可写才抛出（别空转 TMP_MAX 次）
            if os.name == "nt" and os.path.isdir(dir) and os.access(dir, os.W_OK):
                continue
            raise
        return os.path.abspath(file)
    raise FileExistsError(errno.EEXIST, "No usable temporary directory name found")


def install() -> bool:
    """给 tempfile 装上沙箱兼容垫片；已装过返回 False。绝不抛异常。"""
    global _installed, _orig_mkdtemp
    if _installed:
        return False
    _orig_mkdtemp = tempfile.mkdtemp
    tempfile.mkdtemp = _sandbox_mkdtemp
    _installed = True
    return True


def uninstall() -> None:
    """还原（测试断言用；生产进程从不 install）。"""
    global _installed, _orig_mkdtemp
    if _installed and _orig_mkdtemp is not None:
        tempfile.mkdtemp = _orig_mkdtemp
    _installed = False
    _orig_mkdtemp = None
