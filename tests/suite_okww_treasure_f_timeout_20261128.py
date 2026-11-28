# -*- coding: utf-8 -*-
"""内置 ok-ww working 树补丁：宝箱 F 提示等待放宽（2026-11-28）。

现场：11-28 13:40 `TacetTask.farm_tacet` → `walk_to_treasure` → `walk_until_f`
只等 2 秒、且 `backward_time=0`（不后退补等），宝箱前 F 提示偶发漏识别一次就
`CannotFindException: cant find the f to enter`，整轮 DailyTask 中止、ok-ww 关游戏，
波片没花出去（#531，停在 155/240）。原版同一段 11-27 是成功的，属偶发漏识别被
fail-fast 放大。补丁只把等待放宽为 4s + 后退补等 2s，仍找不到才抛（保持 fail-fast）。

补丁只活在 data/okww 的 working 树（gitignore 内），重装 ok-ww 会丢，所以用锚点锁住。
"""

import ast
import builtins
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WORKING = (
    PROJECT_ROOT
    / "data"
    / "okww"
    / "ok-ww-v3.8.3"
    / "data"
    / "apps"
    / "ok-ww"
    / "working"
)
BASE_WW_TASK = "src/task/BaseWWTask.py"


def _read(rel: str) -> str:
    path = WORKING / rel
    if not path.is_file():
        pytest.skip(f"内置 ok-ww working 树不在本机：{path}")
    return path.read_text(encoding="utf-8", errors="replace")


class _Stub:
    """walk_to_treasure 只依赖 self 的少数方法，用桩替掉。"""

    def __init__(self, box_result=True):
        self.box_result = box_result
        self.f_calls = []
        self.logs = []

    def tr(self, text):
        return text

    def log_info(self, text):
        self.logs.append(text)

    def walk_to_box(self, *args, **kwargs):
        return self.box_result

    def find_treasure_icon(self):
        return None

    def find_f_with_claim_text(self):
        return None

    def find_f_with_text(self):
        return None

    def walk_until_f(self, **kwargs):
        self.f_calls.append(kwargs)
        return True

    def sleep(self, *args, **kwargs):
        return None


def _load_walk_to_treasure():
    tree = ast.parse(_read(BASE_WW_TASK))
    cls = next(node for node in tree.body
               if isinstance(node, ast.ClassDef) and node.name == "BaseWWTask")
    fn = next(node for node in cls.body
              if isinstance(node, ast.FunctionDef) and node.name == "walk_to_treasure")
    module = ast.Module(body=[fn], type_ignores=[])
    ast.fix_missing_locations(module)
    code = compile(module, "<BaseWWTask.walk_to_treasure>", "exec")
    namespace = {"__builtins__": dict(vars(builtins))}
    exec(code, namespace)
    return namespace["walk_to_treasure"]


def test_patch_anchors():
    """源码锚点：放宽后的参数在位、旧的 2s 行已消失、备份存在。"""
    src = _read(BASE_WW_TASK)
    assert "self.walk_until_f(time_out=4, backward_time=2, raise_if_not_found=raise_if_not_found)" in src
    assert "self.walk_until_f(time_out=2, backward_time=0" not in src
    assert (WORKING / (BASE_WW_TASK + ".bak_20261128")).is_file()


def test_default_waits_four_seconds_and_steps_back():
    stub = _Stub()
    _load_walk_to_treasure()(stub)
    assert stub.f_calls == [
        {"time_out": 4, "backward_time": 2, "raise_if_not_found": True}
    ], stub.f_calls


def test_raise_flag_still_honoured():
    stub = _Stub()
    _load_walk_to_treasure()(stub, raise_if_not_found=False)
    assert stub.f_calls[0]["raise_if_not_found"] is False
    assert stub.f_calls[0]["time_out"] == 4


def test_send_f_false_skips_wait():
    stub = _Stub()
    _load_walk_to_treasure()(stub, send_f=False)
    assert stub.f_calls == []


def test_cannot_find_icon_still_raises():
    stub = _Stub(box_result=False)
    with pytest.raises(Exception, match="can not walk to treasure"):
        _load_walk_to_treasure()(stub)
    assert stub.f_calls == []
