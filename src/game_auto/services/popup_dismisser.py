"""任务启动前清理挡住游戏窗口的第三方弹窗（如 MiniMax Code 更新提示）。

背景（2026-11-16）：一条龙跑到鸣潮前置流程时，MiniMax Code 弹「更新已来」
模态窗抢前台，OCR 把弹窗文字当成游戏界面、点击落空，任务卡住。该弹窗是
Electron 自绘模态框，没有独立顶层 HWND，WM_CLOSE 会误关整个应用。

设计：纯配置驱动、按窗口标题关键词命中，绝不按进程名杀进程。两种动作：
  - minimize（默认、零误伤）：最小化整个窗口，弹窗随之不可见。X 坐标不确定时
    这是最稳的消除遮挡方式。
  - click_close：点窗口矩形内固定相对位置（右上角 X）；点偏可能触发「立即
    体验」，故点完若窗口仍在会最小化兜底。
任何异常都吞掉并 debug 记录——清理弹窗是锦上添花，失败绝不阻断任务。

配置形状（app_config.json 顶层 `popup_dismiss` 段）：
    {
      "enabled": true,
      "rules": [
        {"title_contains": ["MiniMax"], "action": "minimize"}
      ]
    }
  - title_contains：窗口标题命中任一关键词（不区分大小写）才算目标
  - action：minimize（默认）或 click_close
  - close_offset：click_close 时点击坐标 = 窗口左上 + (宽*fx, 高*fy)
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

try:
    import win32api
    import win32con
    import win32gui
except ImportError:  # pragma: no cover - 非 Windows 环境导入不炸
    win32api = win32con = win32gui = None  # type: ignore[assignment]


DEFAULT_CLOSE_OFFSET = (0.93, 0.24)


def _title_hits(title: str, keywords: Sequence[str]) -> bool:
    low = (title or "").lower()
    return any(str(k).strip().lower() in low for k in keywords if str(k).strip())


def find_popup_windows(rules: Iterable[Dict[str, Any]]) -> List[Tuple[int, str, Tuple[int, int, int, int]]]:
    """枚举可见顶层窗口，返回命中任一规则的 (hwnd, title, rect)。"""
    if win32gui is None:
        return []
    compiled: List[Tuple[Sequence[str], Tuple[float, float]]] = []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        kws = rule.get("title_contains") or []
        if isinstance(kws, str):
            kws = [kws]
        off = rule.get("close_offset") or DEFAULT_CLOSE_OFFSET
        try:
            off = (float(off[0]), float(off[1]))
        except (TypeError, IndexError, ValueError):
            off = DEFAULT_CLOSE_OFFSET
        compiled.append((kws, off))

    hits: List[Tuple[int, str, Tuple[int, int, int, int]]] = []
    if not compiled:
        return hits

    def _cb(hwnd: int, _: Any) -> None:
        if not win32gui.IsWindowVisible(hwnd):
            return
        try:
            title = win32gui.GetWindowText(hwnd)
            rect = win32gui.GetWindowRect(hwnd)
        except Exception:
            return
        for kws, _off in compiled:
            if _title_hits(title, kws):
                hits.append((hwnd, title, rect))
                return

    try:
        win32gui.EnumWindows(_cb, None)
    except Exception:
        pass
    return hits


def click_popup_close(hwnd: int, rect: Tuple[int, int, int, int], offset: Sequence[float]) -> bool:
    """点击弹窗主窗右上角 X（先置前再点），返回是否点击成功。"""
    if win32api is None or win32gui is None:
        return False
    left, top, right, bottom = rect
    x = int(left + (right - left) * float(offset[0]))
    y = int(top + (bottom - top) * float(offset[1]))
    try:
        try:
            win32gui.SetForegroundWindow(hwnd)
            time.sleep(0.2)
        except Exception:
            pass
        win32api.SetCursorPos((x, y))
        time.sleep(0.05)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        return True
    except Exception:
        return False


def minimize_window(hwnd: int) -> bool:
    """最小化窗口（弹窗随之不可见），返回是否成功。"""
    if win32gui is None:
        return False
    try:
        win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
        return True
    except Exception:
        return False


def _matched_rule(title: str, rules: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    for rule in rules:
        if isinstance(rule, dict) and _title_hits(title, rule.get("title_contains") or []):
            return rule
    return None


def dismiss_blocking_popups(
    config: Optional[Dict[str, Any]],
    logger: Optional[logging.Logger] = None,
    *,
    dry_run: bool = False,
) -> int:
    """按配置清理挡路弹窗，返回实际处理的窗口数。

    只在任务启动前调用；dry_run 只报命中不动窗口（供测试/诊断）。
    """
    if not isinstance(config, dict) or not config.get("enabled"):
        return 0
    rules = config.get("rules") or []
    if not isinstance(rules, list) or not rules:
        return 0
    hits = find_popup_windows(rules)
    if not hits:
        return 0
    handled = 0
    for hwnd, title, rect in hits:
        if logger:
            logger.info(f"[弹窗清理] 命中挡路窗口: {title!r} rect={rect} (hwnd={hwnd})")
        if dry_run:
            continue
        rule = _matched_rule(title, rules) or {}
        action = str(rule.get("action") or "minimize").lower()
        if action == "click_close":
            cand = rule.get("close_offset") or DEFAULT_CLOSE_OFFSET
            try:
                off = (float(cand[0]), float(cand[1]))
            except (TypeError, IndexError, ValueError):
                off = DEFAULT_CLOSE_OFFSET
            if click_popup_close(hwnd, rect, off):
                handled += 1
                if logger:
                    logger.info(f"[弹窗清理] 已点击关闭 X: {title!r}")
                # 点偏没命中 X 时窗口仍在 → 最小化兜底，避免挡住游戏
                time.sleep(0.3)
                if win32gui is not None and win32gui.IsWindow(hwnd) and win32gui.IsWindowVisible(hwnd):
                    if minimize_window(hwnd) and logger:
                        logger.info(f"[弹窗清理] 点击未关闭，已最小化兜底: {title!r}")
        else:
            # 默认 minimize：零误伤，弹窗随主窗最小化一起消失
            if minimize_window(hwnd):
                handled += 1
                if logger:
                    logger.info(f"[弹窗清理] 已最小化挡路窗口: {title!r}")
    return handled
