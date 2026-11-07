# -*- coding: utf-8 -*-
"""脚本任务清单同步（`script_checklist_sync` 动作）。

适用：XM/CJ 这类第三方脚本带「勾选任务列表」的流程（Unity 标签下的
限时活动清单等）。游戏/脚本每次更新会在清单里追加新条目，新条目默认
未勾选，不勾就不会被执行——本动作把「新出现的条目」自动勾上。

口径（2026-11-07 凌晨与用户实机交互验证后拍板）：
- **只勾「本轮第一次见到的行」**：已见过的行绝不反向取消——用户手工取消
  的勾选必须保留。状态落 `data/cache/script_checklist_seen.json`。
- **首次运行（状态文件无本清单的键）= 引导轮**：只记录不点击。历史行
  全部处于已勾选状态，但万一有用户刻意未勾的行，引导轮直接点会把它们
  全部反勾——宁可多跑一轮引导。
- 行「是否已勾」用复选框中心取色判定：已勾=蓝色填充（还有白色对勾笔画），
  未勾=浅色空腔。行被选中高亮时背景变蓝，靠中心 8x8 内白色笔画区分。
- 勾选点击必须点**方块内部**（文字无效，用户实测强调）；方块中心 x 取
  行文本 box 左缘减约 3% 窗宽（901 宽实测偏移 26px），y 取行文本垂直中心。
- 滚动：鼠标必须先移到列表上（pyautogui.scroll 在当前鼠标位置生效，
  停在标签栏上等于白滚）；Qt 列表**不响应内容拖动**（会变成选中行），
  滚轮一格 ≈ 1/3 行，用大步长多轮。
- 到底判定：一轮滚动没有引入任何「本轮未见过的行」→ 已到底。
"""
import json
import logging
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import pyautogui
import win32gui

from ..settings.paths import CACHE_DIR
from .ocr_click_tester import _ocr_scan

LOGGER = logging.getLogger("GameAutomation")

STATE_FILE = CACHE_DIR / "script_checklist_seen.json"


def _state_key(flow_key: str, row_prefix: str) -> str:
    return f"{flow_key}:{row_prefix}"


def _load_seen(key: str) -> Optional[List[str]]:
    """读已见行清单；键不存在返回 None（= 引导轮），读失败也按引导轮处理。"""
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception as exc:  # noqa: BLE001 - 状态损坏按引导轮兜底，不阻断流程
        LOGGER.warning("[清单同步] 状态文件读取失败，按引导轮处理: %s", exc)
        return None
    rows = state.get(key)
    return [str(x) for x in rows] if isinstance(rows, list) else None


def _save_seen(key: str, rows: List[str]) -> None:
    try:
        state: Dict[str, Any] = {}
        if STATE_FILE.exists():
            try:
                state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001 - 损坏就重建
                state = {}
        state[key] = rows
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 - 状态写失败只影响下轮去重，不阻断
        LOGGER.warning("[清单同步] 状态文件写入失败: %s", exc)


def _checkbox_checked(screenshot, local_x: int, local_y: int) -> bool:
    """复选框中心取色：蓝色填充占比高 = 已勾；浅色空腔 = 未勾。"""
    crop = screenshot.crop((local_x - 4, local_y - 4, local_x + 4, local_y + 4))
    pixels = list(crop.convert("RGB").getdata())
    blue = sum(1 for r, g, b in pixels if b > 140 and b - r > 40)
    return blue >= 3


def _click_tab(logger: logging.Logger, hwnd: int, tab_name: str) -> bool:
    """点击窗口顶部的标签页：取所有匹配里 y 最小的（标签栏在窗口顶部）。"""
    from .foreground_flow_runner import _pyautogui_click_guarded

    results, _shot, _region = _ocr_scan(logger, hwnd=hwnd)
    candidates = [
        r for r in results
        if tab_name.lower() in str(r.get("text", "")).lower()
    ]
    if not candidates:
        logger.error("[清单同步] 未找到标签页 %s（OCR 无匹配）", tab_name)
        return False
    target = min(candidates, key=lambda r: r["center"][1])
    _pyautogui_click_guarded(int(target["center"][0]), int(target["center"][1]))
    logger.info("[清单同步] 已点击标签 %s @ %s", target["text"], target["center"])
    time.sleep(1.5)
    return True


def sync_checklist(
    *,
    logger: logging.Logger,
    flow_key: str,
    step: Dict[str, Any],
    hwnd: int,
    stop_check: Optional[Callable[[], bool]],
) -> Tuple[bool, str]:
    """动作实现：点标签页 → 滚动扫描 → 勾选新出现行 → 状态落盘。"""
    tab_name = str(step.get("checklist_tab") or "")
    row_prefix = str(step.get("row_prefix") or "限时活动")
    scroll_delta = int(step.get("scroll_delta", -150))
    max_rounds = int(step.get("max_rounds", 40))
    timeout_sec = int(step.get("timeout_sec", 420))

    if not tab_name:
        return False, "步骤缺少 checklist_tab 配置"
    key = _state_key(flow_key, row_prefix)
    known = _load_seen(key)
    bootstrap = known is None
    seen = set(known or [])

    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception:  # noqa: BLE001 - 前台抢占失败由后续点击的实际效果兜底
        pass
    if not _click_tab(logger, hwnd, tab_name):
        return False, f"未找到标签页 {tab_name}"

    deadline = time.time() + max(60, timeout_sec)
    clicked: List[str] = []
    seen_this_run: set = set()
    rounds = 0
    reached_bottom = False
    while rounds < max_rounds and time.time() < deadline:
        if stop_check and stop_check():
            return False, "用户停止"
        rounds += 1
        results, screenshot, region = _ocr_scan(logger, hwnd=hwnd)
        if not results or screenshot is None or not region:
            logger.warning("[清单同步] 第 %d 轮 OCR 无结果（窗口不可用？）", rounds)
            time.sleep(1.0)
            continue
        rows = [r for r in results if str(r.get("text", "")).startswith(row_prefix)]
        new_rows = [r for r in rows if _norm_text(r["text"]) not in seen_this_run]
        for r in new_rows:
            text = _norm_text(r["text"])
            seen_this_run.add(text)
        if new_rows:
            # 本轮出现了没见过的行：逐行判定勾选状态（引导轮除外）
            for r in new_rows:
                text = _norm_text(r["text"])
                if bootstrap or text in seen:
                    continue  # 引导轮 / 已见过的行不动
                box = r.get("box")
                if not box:
                    continue
                delta_x = int(0.03 * (region[2] - region[0]))
                local_x = int(box[0]) - delta_x
                local_y = (int(box[1]) + int(box[3])) // 2
                if _checkbox_checked(screenshot, local_x, local_y):
                    logger.info("[清单同步] %s 已勾选，跳过", text)
                    continue
                screen_x = region[0] + local_x
                screen_y = region[1] + local_y
                from .foreground_flow_runner import _pyautogui_click_guarded

                _pyautogui_click_guarded(screen_x, screen_y)
                clicked.append(text)
                logger.info("[清单同步] ✅ 已勾选新条目: %s @ screen(%d,%d)",
                            text, screen_x, screen_y)
                time.sleep(0.4)
        if not new_rows:
            # 本轮没有引入新行：两种情况——到底了，或列表只有一屏。都算完成。
            reached_bottom = True
            logger.info("[清单同步] 第 %d 轮无新行，判定已到列表底部（共 %d 行）",
                        rounds, len(seen_this_run))
            break
        # 滚动：鼠标移到列表中部再滚（滚轮在当前鼠标位置生效）
        import win32api
        center_x = region[0] + int((region[2] - region[0]) * 0.45)
        center_y = region[1] + int((region[3] - region[1]) * 0.5)
        win32api.SetCursorPos((center_x, center_y))
        pyautogui.scroll(scroll_delta)
        time.sleep(0.8)

    all_rows = sorted(seen_this_run)
    _save_seen(key, all_rows)
    if clicked:
        detail = "、".join(clicked)
        if len(clicked) > 4:
            detail = "、".join(clicked[:4]) + f" 等 {len(clicked)} 项"
        return True, f"本轮勾选 {len(clicked)} 项新条目：{detail}（扫描 {rounds} 轮，共 {len(all_rows)} 行）"
    if bootstrap:
        return True, f"引导轮：已记录 {len(all_rows)} 行基线（未点击），后续新条目将自动勾选"
    if reached_bottom:
        return True, f"清单已同步（{len(all_rows)} 行，无新增需要勾选）"
    return False, f"扫描未完成（{rounds} 轮，未到列表底部）——请检查窗口状态后重试"


def _norm_text(text: str) -> str:
    """行文本归一化（去空白与截断省略号），作为去重键。"""
    return "".join(str(text or "").split()).rstrip(".…")
