"""挡路弹窗清理逻辑回归（2026-11-16 MiniMax 更新提示遮挡一条龙）。

只测纯逻辑：开关、标题命中、规则/offset 解析。真鼠标点击/窗口枚举不在这里
测（依赖 GUI 环境），由 find_popup_windows / click_popup_close 自己吞异常。

配置：app_config.popup_dismiss.{enabled, rules[].title_contains/close_offset}
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from game_auto.services import popup_dismisser as pd  # noqa: E402


def test_title_hits_case_insensitive():
    assert pd._title_hits("MiniMax Code", ["minimax"])
    assert pd._title_hits("更新已来 - MiniMax", ["MiniMax"])
    assert pd._title_hits("foo", ["MiniMax"]) is False
    assert pd._title_hits("MiniMax", ["  "]) is False
    assert pd._title_hits("anything", []) is False


def test_disabled_config_does_nothing(monkeypatch):
    called = []
    monkeypatch.setattr(pd, "find_popup_windows", lambda rules: called.append(rules) or [])
    assert pd.dismiss_blocking_popups({"enabled": False, "rules": [{"title_contains": ["MiniMax"]}]}) == 0
    assert not called, "关闭时不应枚举窗口"
    assert pd.dismiss_blocking_popups({}) == 0
    assert pd.dismiss_blocking_popups(None) == 0


def test_dry_run_reports_without_clicking(monkeypatch):
    monkeypatch.setattr(
        pd, "find_popup_windows",
        lambda rules: [(123, "MiniMax 更新", (0, 0, 100, 100))],
    )
    clicked = []
    monkeypatch.setattr(pd, "click_popup_close", lambda *a: clicked.append(a) or True)
    n = pd.dismiss_blocking_popups(
        {"enabled": True, "rules": [{"title_contains": ["MiniMax"]}]}, dry_run=True
    )
    assert n == 0 and not clicked, "dry_run 不应点击"


def test_default_action_minimizes(monkeypatch):
    minimized = []
    monkeypatch.setattr(
        pd, "find_popup_windows",
        lambda rules: [(123, "MiniMax", (10, 20, 1010, 820))],
    )
    monkeypatch.setattr(pd, "minimize_window", lambda h: minimized.append(h) or True)
    n = pd.dismiss_blocking_popups({
        "enabled": True,
        "rules": [{"title_contains": ["MiniMax"]}],
    })
    assert n == 1 and minimized == [123], "默认动作应是最小化（零误伤）"


def test_click_close_action_uses_rule_offset(monkeypatch):
    monkeypatch.setattr(
        pd, "find_popup_windows",
        lambda rules: [(123, "MiniMax", (10, 20, 1010, 820))],
    )
    clicked = []
    monkeypatch.setattr(pd, "click_popup_close", lambda hwnd, rect, off: clicked.append((hwnd, rect, off)) or True)
    monkeypatch.setattr(pd.time, "sleep", lambda _: None)
    if pd.win32gui is not None:
        monkeypatch.setattr(pd.win32gui, "IsWindow", lambda h: False)  # 跳过兜底分支
    n = pd.dismiss_blocking_popups({
        "enabled": True,
        "rules": [{"title_contains": ["MiniMax"], "action": "click_close", "close_offset": [0.9, 0.2]}],
    })
    assert n == 1
    assert clicked[0][0] == 123
    assert clicked[0][2] == (0.9, 0.2), "应用了规则里的 close_offset"


def test_default_config_has_minimax_rule():
    from game_auto.settings.manager import _default_config
    cfg = _default_config()["popup_dismiss"]
    assert cfg["enabled"] is True
    rr = [r for r in cfg["rules"] if "MiniMax" in r.get("title_contains", [])]
    assert rr and rr[0].get("action") == "minimize"
