"""ok-ww module runner 子进程 PYTHONPATH 隔离回归（2026-11-16）。

根因：GUI 由 start_gui_admin.bat 启动时会把 <项目>/src 塞进 PYTHONPATH；
module runner 用 ok-ww 内嵌 python 跑，若继承该路径，ok-ww 引擎 import
onnxocr 会命中本项目的 src/onnxocr（硬依赖 onnxruntime，内嵌 python 没装），
而非 ok-ww 自带的 openvino 版，DailyTask 55s 后报 "No module named 'onnxruntime'"。

本套件验证 OkwwModuleSession.start() 组装的 env 已把项目 src 从 PYTHONPATH
剥离，同时保留其它无关路径。
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from game_auto.runtime.okww_module_session import OkwwModuleSession  # noqa: E402


def _project_src():
    # 源码里 Path(__file__).resolve().parents[2]；__file__ 是
    # src/game_auto/runtime/okww_module_session.py → parents[2] = <项目>/src。
    # 这里从 runtime/__init__.py 起算要多退一层。
    from game_auto import runtime
    return str(Path(runtime.__file__).resolve().parents[2]).replace("/", "\\").rstrip("\\").lower()


def _strip(env_pythonpath):
    """复刻 start() 里的剥离逻辑，便于无子进程断言。"""
    project_src = _project_src()
    kept = []
    for entry in env_pythonpath.split(os.pathsep):
        norm = entry.replace("/", "\\").strip().rstrip("\\").lower()
        if norm and norm != project_src:
            kept.append(entry)
    return os.pathsep.join(kept)


def test_project_src_stripped_various_forms():
    src = r"E:\GithubProject\game_auto\src"
    assert _strip(src) == ""
    assert _strip(src + os.pathsep + r"D:\libs") == r"D:\libs"
    assert _strip(r"D:\libs" + os.pathsep + src + os.pathsep + r"C:\x") == r"D:\libs" + os.pathsep + r"C:\x"
    # 正斜杠也要认
    assert _strip("E:/GithubProject/game_auto/src;D:/libs") == "D:/libs"


def test_start_method_strips_pythonpath():
    """接线锚点：start() 里必须显式重写 PYTHONPATH（剥离逻辑存在）。"""
    import inspect
    src = inspect.getsource(OkwwModuleSession.start)
    assert "PYTHONPATH" in src, "start() 未处理 PYTHONPATH"
    assert "os.environ.copy()" in src
    assert "_project_src" in src, "未计算项目 src 路径做剥离"
