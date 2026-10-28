"""
固定点击序列「录制 / 回放」模块。

用途：机械性地按"用户点击顺序"重放固定流程。
- ClickRecorder: 全局监听鼠标点击与键盘（pynput），记录点击坐标 + 按键，
  小键盘0 停止（小键盘+ 打标记），保存为 JSON。
- ClickSequencePlayer: 加载 JSON，按序机械回放；回放时按窗口标题重新定位窗口，
  用窗口相对坐标重放（窗口移动也不怕），找不到窗口则回退绝对坐标。
  键盘事件回放时重按，标记事件跳过不点击。

坐标策略：录制时同时保存绝对坐标(x, y)与窗口相对坐标(rel_x, rel_y)。
回放优先使用窗口相对坐标（当前窗口左上角 + rel），窗口找不到 / 无相对坐标时回退绝对坐标。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pyautogui
import win32gui

try:
    from pynput import mouse as _pynput_mouse
    from pynput import keyboard as _pynput_keyboard
    _HAS_PYNPUT = True
except Exception:  # pragma: no cover - 录制功能依赖 pynput
    _pynput_mouse = None
    _pynput_keyboard = None
    _HAS_PYNPUT = False

from ..settings.paths import CLICK_SEQUENCES_DIR


def sequence_path(name: str, directory: Optional[str] = None) -> Path:
    """校验序列名并返回文件路径；拒绝路径分隔符、盘符与上级目录。"""
    safe = str(name or "").strip()
    if (
        not safe
        or safe in {".", ".."}
        or "\0" in safe
        or ":" in safe
        or Path(safe).name != safe
    ):
        raise ValueError(f"非法序列名: {name!r}")
    return Path(directory or CLICK_SEQUENCES_DIR) / f"{safe}.json"


def _resolve_window(
    keyword: Optional[str] = None,
    hwnd: Optional[int] = None,
) -> Tuple[Optional[int], Optional[str], Optional[Tuple[int, int, int, int]]]:
    """定位窗口，返回 (hwnd, title, rect)。rect = (l, t, r, b)。

    优先按 hwnd 精确定位（UI 下拉选中窗口时锁定句柄，避免同名窗口取错）；
    句柄失效时再退回标题关键字模糊匹配。
    """
    if hwnd:
        try:
            if win32gui.IsWindow(hwnd):
                title = win32gui.GetWindowText(hwnd).strip()
                rect = win32gui.GetWindowRect(hwnd)
                return int(hwnd), title, rect
        except Exception:
            pass
    if not keyword:
        return None, None, None
    kw = keyword.lower()
    found: List[Tuple[int, str]] = []

    def cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd).strip()
        if title and kw in title.lower():
            found.append((hwnd, title))

    win32gui.EnumWindows(cb, None)
    if not found:
        return None, None, None
    hwnd, title = found[0]
    try:
        rect = win32gui.GetWindowRect(hwnd)
    except Exception:
        rect = None
    return hwnd, title, rect


def _is_cloaked(hwnd: int) -> bool:
    """UWP / 系统后台窗口会被 DWM 标记为 cloak，虽"可见"但用户看不到，需剔除。"""
    try:
        import ctypes
        from ctypes import wintypes

        DWMWA_CLOAKED = 14
        cloaked = wintypes.DWORD(0)
        res = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            wintypes.HWND(hwnd), DWMWA_CLOAKED,
            ctypes.byref(cloaked), ctypes.sizeof(cloaked),
        )
        return res == 0 and cloaked.value != 0
    except Exception:
        return False


_EXE_CACHE: Dict[int, str] = {}


def _query_process_exe(pid: Optional[int]) -> str:
    """取进程可执行文件基名（如 Game.exe），失败返回空串。带 pid 级缓存。"""
    if not pid:
        return ""
    if pid in _EXE_CACHE:
        return _EXE_CACHE[pid]
    exe = ""
    try:
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid)
        )
        if handle:
            try:
                buf = ctypes.create_unicode_buffer(1024)
                size = wintypes.DWORD(1024)
                ok = ctypes.windll.kernel32.QueryFullProcessImageNameW(
                    handle, 0, buf, ctypes.byref(size)
                )
                if ok:
                    exe = os.path.basename(buf.value or "")
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
    except Exception:
        exe = ""
    _EXE_CACHE[pid] = exe
    return exe


def list_windows(
    *,
    min_width: int = 200,
    min_height: int = 150,
    exclude_own_process: bool = True,
    limit: int = 80,
) -> List[dict]:
    """枚举当前可见窗口，供 UI 下拉选择目标窗口。

    返回字段：hwnd / title / exe / pid / rect(l,t,r,b) / width / height /
    minimized / foreground。按 EnumWindows 的 Z 序返回（前台窗口在前）。
    """
    try:
        from win32process import GetWindowThreadProcessId
    except Exception:  # pragma: no cover - pywin32 正常情况下一定有
        GetWindowThreadProcessId = None

    own_pid = os.getpid()
    fg_hwnd = 0
    try:
        fg_hwnd = win32gui.GetForegroundWindow()
    except Exception:
        pass

    items: List[dict] = []

    def cb(hwnd, _):
        if len(items) >= limit:
            return
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return
            title = win32gui.GetWindowText(hwnd).strip()
            if not title:
                return
            rect = win32gui.GetWindowRect(hwnd)
        except Exception:
            return
        w, h = rect[2] - rect[0], rect[3] - rect[1]
        if w < min_width or h < min_height:
            return
        pid = None
        exe = ""
        if GetWindowThreadProcessId is not None:
            try:
                _, pid = GetWindowThreadProcessId(hwnd)
                if exclude_own_process and pid == own_pid:
                    return  # 排除本程序的窗口（避免选到自己）
                exe = _query_process_exe(pid)
            except Exception:
                pass
        if _is_cloaked(hwnd):
            return
        try:
            minimized = bool(win32gui.IsIconic(hwnd))
        except Exception:
            minimized = False
        items.append({
            "hwnd": int(hwnd),
            "title": title,
            "exe": exe,
            "pid": pid,
            "rect": [int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3])],
            "width": int(w),
            "height": int(h),
            "minimized": minimized,
            "foreground": int(hwnd) == int(fg_hwnd),
        })

    try:
        win32gui.EnumWindows(cb, None)
    except Exception:
        return []
    # 最小化窗口沉底，其余保持 Z 序
    items.sort(key=lambda it: (it["minimized"],))
    return items


@dataclass
class ClickEvent:
    x: int
    y: int
    button: str = "left"                 # "left" / "right"（kind=key/mark 时无意义）
    ts: float = 0.0                      # 录制时绝对时间戳
    window_title: str = ""
    rel_x: Optional[int] = None          # 相对目标窗口左上角
    rel_y: Optional[int] = None
    gap_after: Optional[float] = None    # 到下一步间隔（秒），用于忠实重放节奏
    kind: str = "click"                  # "click" / "key" / "mark"
    key: str = ""                        # kind="key" 时的键名（pynput 命名）


# pynput 键名/vk → pyautogui 键名（仅两者命名不同的需要映射）
_PYNPUT_TO_PYAUTOGUI_KEYS = {
    "pad_0": "num0", "pad_1": "num1", "pad_2": "num2", "pad_3": "num3",
    "pad_4": "num4", "pad_5": "num5", "pad_6": "num6", "pad_7": "num7",
    "pad_8": "num8", "pad_9": "num9",
    "pad_add": "add", "pad_decimal": "decimal",
    "cmd": "win", "cmd_l": "winleft", "cmd_r": "winright",
}

# Windows VK 码 → 小键盘键名（pynput 在 Windows 上不提供 pad_* 命名键，
# 小键盘按键表现为带 vk 的 KeyCode，需按 vk 区分；13 与主回车共用无法区分）
_VK_TO_PAD_NAME = {
    106: "pad_mult", 107: "pad_add", 109: "pad_sub",
    110: "pad_decimal", 111: "pad_div",
}


def _key_display_name(key) -> Optional[str]:
    """把 pynput 按键对象转成可存储/可重放的键名；无法识别返回 None。

    小键盘数字/符号按 vk 码判定（96-105 = pad_0..pad_9），优先于 char，
    否则无法和主键盘数字区分。
    """
    if key is None or _pynput_keyboard is None:
        return None
    if isinstance(key, _pynput_keyboard.KeyCode):
        vk = getattr(key, "vk", None)
        if vk is not None:
            if 96 <= vk <= 105:
                return f"pad_{vk - 96}"
            pad_name = _VK_TO_PAD_NAME.get(vk)
            if pad_name:
                return pad_name
        char = getattr(key, "char", None)
        if char:
            return char
        return None
    name = getattr(key, "name", None)
    return name or None


def _press_recorded_key(name: str) -> bool:
    """回放一个录制下来的按键；键名不可识别返回 False。"""
    if not name:
        return False
    pyautogui.press(_PYNPUT_TO_PYAUTOGUI_KEYS.get(name, name))
    return True


@dataclass
class ClickSequence:
    name: str
    events: List[ClickEvent] = field(default_factory=list)
    window_title_keyword: Optional[str] = None
    created_at: str = ""
    note: str = ""


class ClickRecorder:
    """全局监听鼠标点击，记录点击顺序。"""

    def __init__(
        self,
        logger: Optional[logging.Logger] = None,
        window_title_keyword: Optional[str] = None,
        only_in_target_window: bool = True,
        window_hwnd: Optional[int] = None,
    ):
        self.logger = logger or logging.getLogger("GameAutomation")
        self.window_title_keyword = window_title_keyword
        # UI 下拉选中窗口时锁定句柄，避免同名窗口 / 多开时取错
        self.window_hwnd = int(window_hwnd) if window_hwnd else None
        # 仅在指定窗口（关键字或句柄）时才过滤窗口外点击
        self.only_in_target_window = (
            bool(window_title_keyword) or window_hwnd is not None
        ) and only_in_target_window
        self.events: List[ClickEvent] = []
        self._mouse_listener = None
        self._key_listener = None
        self._recording = False
        self._last_ts = 0.0

    def start(self):
        if not _HAS_PYNPUT:
            raise RuntimeError("pynput 未安装，无法录制（pip install pynput）")
        self.events = []
        self._recording = True
        self._last_ts = time.perf_counter()
        self._mouse_listener = _pynput_mouse.Listener(on_click=self._on_click)
        self._key_listener = _pynput_keyboard.Listener(on_press=self._on_key)
        self._mouse_listener.start()
        self._key_listener.start()
        if self.window_hwnd:
            _h, live_title, _r = _resolve_window(self.window_title_keyword, self.window_hwnd)
            tip = f"目标窗口='{live_title or self.window_title_keyword or '?'}'(句柄 {self.window_hwnd})"
        elif self.window_title_keyword:
            tip = f"目标窗口='{self.window_title_keyword}'"
        else:
            tip = "无窗口约束(记录所有点击)"
        self.logger.info(
            f"[录制] 已开始，点击/按键均会记录（{tip}）；"
            "小键盘0 停止，小键盘+ 打标记（回放不点击）"
        )

    def _on_key(self, key):
        """按键处理（pynput 线程）：
        - 小键盘 0 → 停止（仅置标志，listener.stop 由收尾统一调用）
        - 小键盘 + → 插入标记事件（回放时跳过不点击，仅作节奏/分段锚点）
        - 其他按键（含 ESC / q）→ 记录为键盘事件（回放时重按）
        """
        try:
            name = _key_display_name(key)
            if name == "pad_0":
                self.logger.info("[录制] 收到停止热键（小键盘0）")
                self._recording = False
                return
            if not self._recording:
                return
            now = time.perf_counter()
            if name == "pad_add":
                # 标记：记录当前鼠标位置仅作参考，回放时不点击
                try:
                    pos = _pynput_mouse.Controller().position
                    mx, my = int(pos[0]), int(pos[1])
                except Exception:
                    mx, my = 0, 0
                if self.events:
                    self.events[-1].gap_after = round(now - self._last_ts, 3)
                self.events.append(ClickEvent(
                    x=mx, y=my, button="mark", ts=time.time(),
                    kind="mark", key="",
                ))
                self._last_ts = now
                self.logger.info(f"[录制] 第{len(self.events)}次: ✚ 标记 @({mx},{my})")
                return
            if name:
                # 键盘事件：记录键名，回放时重按
                if self.events:
                    self.events[-1].gap_after = round(now - self._last_ts, 3)
                self.events.append(ClickEvent(
                    x=0, y=0, button="key", ts=time.time(), kind="key", key=name,
                ))
                self._last_ts = now
                self.logger.info(f"[录制] 第{len(self.events)}次: ⌨ 按键 {name}")
        except Exception:
            pass

    def _on_click(self, x, y, button, pressed):
        if not pressed or not self._recording:
            return
        now = time.perf_counter()
        hwnd, title, rect = (None, "", None)
        if self.window_title_keyword or self.window_hwnd:
            hwnd, title, rect = _resolve_window(self.window_title_keyword, self.window_hwnd)
            if self.only_in_target_window:
                if rect is None or not (rect[0] <= x <= rect[2] and rect[1] <= y <= rect[3]):
                    self.logger.debug(f"[录制] 跳过窗口外点击 ({int(x)},{int(y)})")
                    return
        btn = "right" if button == _pynput_mouse.Button.right else "left"
        rel_x = rel_y = None
        if rect is not None:
            rel_x = int(x - rect[0])
            rel_y = int(y - rect[1])
        # 计算「上一次点击 → 本次」的真实间隔，回填给上一次事件作为 gap_after
        # （即上一次点击执行后、到本次点击之间的等待时长），保证回放节奏归位
        interval = round(now - self._last_ts, 3) if self.events else 0.0
        if self.events:
            self.events[-1].gap_after = interval
        ev = ClickEvent(
            x=int(x), y=int(y), button=btn, ts=time.time(),
            window_title=title or "", rel_x=rel_x, rel_y=rel_y, gap_after=None,
        )
        self.events.append(ev)
        self._last_ts = now
        self.logger.info(
            f"[录制] 第{len(self.events)}次: ({int(x)},{int(y)}) rel=({rel_x},{rel_y}) btn={btn} 距上步{interval}s"
        )

    def stop(self):
        if not self._recording and self._mouse_listener is None:
            return
        self._recording = False
        for lst in (self._mouse_listener, self._key_listener):
            if lst is not None:
                try:
                    lst.stop()
                except Exception:
                    pass
        self._mouse_listener = None
        self._key_listener = None
        if self.events:
            self.events[-1].gap_after = 0.0
        self.logger.info(f"[录制] 已停止，共记录 {len(self.events)} 次点击")

    def to_sequence(self, name: str, note: str = "") -> ClickSequence:
        return ClickSequence(
            name=name, events=list(self.events),
            window_title_keyword=self.window_title_keyword,
            created_at=time.strftime("%Y-%m-%d %H:%M:%S"), note=note,
        )

    def save(self, name: str, note: str = "", directory: Optional[str] = None) -> str:
        safe = str(name or "").strip()
        path = sequence_path(safe, directory)
        seq = self.to_sequence(safe, note)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(_seq_to_dict(seq), f, ensure_ascii=False, indent=2)
        self.logger.info(f"[录制] 已保存序列: {path}")
        return str(path)


def _seq_to_dict(seq: ClickSequence) -> dict:
    return {
        "name": seq.name,
        "window_title_keyword": seq.window_title_keyword,
        "created_at": seq.created_at,
        "note": seq.note,
        "events": [
            {
                "x": e.x, "y": e.y, "button": e.button, "ts": e.ts,
                "window_title": e.window_title, "rel_x": e.rel_x,
                "rel_y": e.rel_y, "gap_after": e.gap_after,
                "kind": e.kind, "key": e.key,
            }
            for e in seq.events
        ],
    }


def _dict_to_seq(d: dict) -> ClickSequence:
    seq = ClickSequence(
        name=d.get("name", "unnamed"),
        window_title_keyword=d.get("window_title_keyword"),
        created_at=d.get("created_at", ""),
        note=d.get("note", ""),
    )
    for e in d.get("events", []):
        seq.events.append(ClickEvent(
            x=int(e["x"]), y=int(e["y"]), button=e.get("button", "left"),
            ts=float(e.get("ts", 0.0)), window_title=e.get("window_title", ""),
            rel_x=e.get("rel_x"), rel_y=e.get("rel_y"), gap_after=e.get("gap_after"),
            kind=e.get("kind", "click"), key=e.get("key", ""),
        ))
    return seq


class ClickSequencePlayer:
    """加载 JSON，按序机械回放固定点击序列。"""

    def __init__(self, logger: Optional[logging.Logger] = None, failsafe: bool = True):
        self.logger = logger or logging.getLogger("GameAutomation")
        pyautogui.FAILSAFE = failsafe
        pyautogui.PAUSE = 0.05

    def load(self, name: str, directory: Optional[str] = None) -> ClickSequence:
        path = sequence_path(name, directory)
        if not path.exists():
            raise FileNotFoundError(f"序列不存在: {path}")
        with open(path, "r", encoding="utf-8") as f:
            return _dict_to_seq(json.load(f))

    def list_sequences(self, directory: Optional[str] = None) -> List[str]:
        directory = directory or str(CLICK_SEQUENCES_DIR)
        if not os.path.isdir(directory):
            return []
        return sorted(p[:-5] for p in os.listdir(directory) if p.endswith(".json"))

    def play(
        self,
        seq: ClickSequence,
        *,
        loop: int = 1,
        window_title_keyword: Optional[str] = None,
        min_gap: Optional[float] = None,
        max_gap: Optional[float] = None,
        default_gap: float = 0.5,
        dry_run: bool = False,
        stop_event: Optional[threading.Event] = None,
    ) -> bool:
        kw = window_title_keyword or seq.window_title_keyword
        total = len(seq.events)
        if total == 0:
            self.logger.warning("[回放] 序列为空，无操作")
            return False
        loop = max(1, int(loop))
        self.logger.info(
            f"[回放] 开始: {seq.name} 共 {total} 步, 循环 {loop} 次"
            + (f", 目标窗口='{kw}'" if kw else ", 无窗口约束(绝对坐标)")
            + (" [dry_run]" if dry_run else "")
        )
        for lap in range(1, loop + 1):
            if stop_event and stop_event.is_set():
                self.logger.info("[回放] 收到停止信号，退出")
                return False
            if loop > 1:
                self.logger.info(f"[回放] 第 {lap}/{loop} 轮")
            _hwnd, _title, rect = _resolve_window(kw)
            if kw and rect is None:
                self.logger.warning(f"[回放] 未找到窗口 '{kw}'，将回退绝对坐标")
            for idx, ev in enumerate(seq.events, start=1):
                if stop_event and stop_event.is_set():
                    self.logger.info("[回放] 收到停止信号，退出")
                    return False
                gap = ev.gap_after if ev.gap_after is not None else default_gap
                if min_gap is not None:
                    gap = max(min_gap, gap)
                if max_gap is not None:
                    gap = min(max_gap, gap)
                if ev.kind == "mark":
                    # 标记事件：仅作分段/节奏锚点，回放不点击
                    self.logger.info(f"[回放] 步骤{idx}/{total}: ✚ 标记 @({ev.x},{ev.y})（跳过）")
                elif ev.kind == "key":
                    if dry_run:
                        self.logger.info(f"[回放][dry] 步骤{idx}: 按键 {ev.key}")
                    elif _press_recorded_key(ev.key):
                        self.logger.info(f"[回放] 步骤{idx}/{total}: 按键 {ev.key}")
                    else:
                        self.logger.warning(f"[回放] 步骤{idx}/{total}: 按键 {ev.key!r} 无法识别，跳过")
                else:
                    sx, sy = self._resolve_coord(ev, rect)
                    if dry_run:
                        self.logger.info(f"[回放][dry] 步骤{idx}: 点击 ({sx},{sy}) btn={ev.button}")
                    else:
                        pyautogui.click(sx, sy, button=ev.button)
                        self.logger.info(f"[回放] 步骤{idx}/{total}: 点击 ({sx},{sy}) btn={ev.button}")
                if gap and gap > 0 and idx < total:
                    time.sleep(gap)
        self.logger.info(f"[回放] 完成: {seq.name}")
        return True

    @staticmethod
    def _resolve_coord(ev: ClickEvent, rect) -> Tuple[int, int]:
        if ev.rel_x is not None and ev.rel_y is not None and rect is not None:
            return int(rect[0] + ev.rel_x), int(rect[1] + ev.rel_y)
        return int(ev.x), int(ev.y)


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="固定点击序列录制/回放")
    sub = p.add_subparsers(dest="cmd", required=True)

    rp = sub.add_parser("record", help="录制鼠标点击")
    rp.add_argument("name", help="序列名称")
    rp.add_argument("--win", default=None, help="目标窗口标题关键字（只录该窗口内点击并算相对坐标）")
    rp.add_argument("--note", default="", help="备注")
    rp.add_argument("--dir", default=None, help="存储目录（默认 click_sequences/）")

    pp = sub.add_parser("play", help="回放序列")
    pp.add_argument("name", help="序列名称")
    pp.add_argument("--loop", type=int, default=1, help="循环次数，默认 1")
    pp.add_argument("--win", default=None, help="覆盖目标窗口标题关键字")
    pp.add_argument("--min-gap", type=float, default=None, help="最小步骤间隔(秒)")
    pp.add_argument("--max-gap", type=float, default=None, help="最大步骤间隔(秒)")
    pp.add_argument("--gap", type=float, default=0.5, help="无记录间隔时的默认间隔(秒)")
    pp.add_argument("--dry", action="store_true", help="只打印坐标不真点击")
    pp.add_argument("--dir", default=None, help="序列目录")

    lp = sub.add_parser("list", help="列出已存序列")
    lp.add_argument("--dir", default=None)
    return p


def main(argv=None):
    args = _build_arg_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logger = logging.getLogger("ClickSequence")
    if args.cmd == "record":
        rec = ClickRecorder(logger, window_title_keyword=args.win)
        rec.start()
        try:
            while rec._recording:
                time.sleep(0.2)
        except KeyboardInterrupt:
            pass
        rec.stop()
        if rec.events:
            path = rec.save(args.name, note=args.note, directory=args.dir)
            print(f"已保存 {len(rec.events)} 步 -> {path}")
        else:
            print("未记录到任何点击")
    elif args.cmd == "play":
        player = ClickSequencePlayer(logger)
        seq = player.load(args.name, directory=args.dir)
        ok = player.play(
            seq, loop=args.loop, window_title_keyword=args.win,
            min_gap=args.min_gap, max_gap=args.max_gap,
            default_gap=args.gap, dry_run=args.dry,
        )
        print("回放结果:", "成功" if ok else "失败/中止")
    elif args.cmd == "list":
        player = ClickSequencePlayer(logger)
        names = player.list_sequences(directory=args.dir)
        print("已存序列:" if names else "无序列")
        for n in names:
            print(" -", n)


if __name__ == "__main__":
    main()
