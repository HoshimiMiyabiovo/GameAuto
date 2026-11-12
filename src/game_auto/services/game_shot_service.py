"""全局热键游戏截图（类似 Xbox Game Bar，2026-11-10）。

数据流：GUI 启动 → ``start()`` 注册 pynput 全局热键 + 后台预热 WGC → 用户按键
→ pynput 监听线程回调 → ``grab_screen(前台窗口 region)`` → 落盘
``data/artifacts/game_shots`` → 系统提示音 + 经 notify 回调发 Qt toast →
写 GameAutomation 日志。

不变量与坑：
- **与 Qt 完全解耦**：本模块不 import PySide6。热键回调运行在 pynput 监听线程，
  只能调线程安全的 ``notify``；调用方用 ``bridge.emit_event`` 包装，Qt 会以
  QueuedConnection 投递到主线程（ui/bridge_views/common.py 的线程模型）。
- **绝不抢前台**：截图是被动读取，不调 SetForegroundWindow / BringWindowToTop /
  SetWindowPos，否则会打断正在跑的自动化批次（01-workflow.md:34 实操作互斥口径）。
- **启动即预热**：``grab_screen`` 首次调用要建 WGC 会话（最多 2.5s）。若等用户
  第一次按键才建，就不是"实时"了，所以 start() 立刻在后台线程预热一次。
- **坐标单位**：``grab_screen`` 吃 (left, top, width, height)，而
  ``win32gui.GetWindowRect`` 返回 (left, top, right, bottom)；多屏负坐标/越界
  一律退回全屏，避免 WGC 的 crop 截偏（monthly_shop_evidence._window_region 同口径）。
- **独占全屏**：游戏全屏时 GUI 窗口在后台，应用内 toast 看不见，所以默认发一个
  系统提示音作为即时反馈；不做 topmost 覆盖窗，那会与"不抢前台"冲突。
- **UIPI**：低完整性进程收不到发给高完整性窗口的键盘事件，GUI 需与游戏同级权限
  （管理员启动脚本），与点击自动化的口径一致。
"""

from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

import win32gui
import win32process

from ..settings import manager as cfg_manager
from ..settings.defaults import DEFAULT_GAME_SHOT
from ..settings.paths import GAME_SHOT_DIR

try:  # Windows 标准库；非 Windows 环境（含纯逻辑回归）允许缺席
    import winsound
except ImportError:  # pragma: no cover - 本项目只在 Windows 运行
    winsound = None
try:  # 进程名仅用于文件名，拿不到就退回窗口标题
    import psutil
except ImportError:  # pragma: no cover
    psutil = None

LOGGER = logging.getLogger("GameAutomation")

# 用户可读的热键写法 → pynput GlobalHotKeys 写法。<win>/<cmd> 走 pynput 的 cmd 键。
_MODIFIER_MAP = {
    "ctrl": "<ctrl>", "control": "<ctrl>",
    "alt": "<alt>", "shift": "<shift>",
    "win": "<cmd>", "cmd": "<cmd>", "super": "<cmd>", "meta": "<cmd>",
}
_PLAIN_KEY_RE = re.compile(r"[a-z0-9]")
_FKEY_RE = re.compile(r"f(?:1[0-2]|[1-9])")
_TAG_INVALID_RE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff._-]+")


def to_pynput_hotkey(spec: str) -> str:
    """把 ``ctrl+alt+s`` 这类写法转成 pynput 的 ``<ctrl>+<alt>+s``。

    支持普通字符键、F1~F12、以及已经是 pynput 形态的 ``<ctrl>`` 片段。
    :raises ValueError: 空串或含无法识别的键名（此时服务不启动，不静默降级）。
    """
    parts = [p.strip().lower() for p in str(spec or "").split("+") if p.strip()]
    if not parts:
        raise ValueError("热键不能为空")
    mods: list[str] = []
    plain: list[str] = []
    specials: list[str] = []
    for part in parts:
        if part in _MODIFIER_MAP:
            mods.append(_MODIFIER_MAP[part])
        elif part.startswith("<") and part.endswith(">"):
            specials.append(part)
        elif _PLAIN_KEY_RE.fullmatch(part):
            plain.append(part)
        elif _FKEY_RE.fullmatch(part):
            specials.append(f"<{part}>")
        else:
            raise ValueError(f"不支持的按键 {part!r}")
    if len(plain) > 1 or (not plain and not specials):
        raise ValueError("必须且只能有一个主键（字母/数字/F1~F12）")
    return "+".join(mods + specials + plain)


def _safe_tag(text: str, limit: int = 24) -> str:
    """窗口标题/进程名 → 可放进文件名的短标签（去掉 .exe 与非法字符）。"""
    name = str(text or "").strip()
    if name.lower().endswith(".exe"):
        name = name[:-4]
    cleaned = _TAG_INVALID_RE.sub("_", name).strip("._-")
    return (cleaned[:limit] or "screen")


def _window_region(hwnd, screen_size: Optional[Tuple[int, int]] = None):
    """GetWindowRect 的 (l,t,r,b) → grab_screen 的 (l,t,w,h)。

    返回 None 表示退回全屏：窗口最小化/尺寸非法、或落在主屏之外（WGC 固定
    monitor_index=1，多屏上负坐标会让 crop 拿到空白）。
    """
    try:
        left, top, right, bottom = win32gui.GetWindowRect(int(hwnd))
    except Exception:
        return None
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        return None
    if screen_size is not None:
        sw, sh = screen_size
        if left < 0 or top < 0 or left + width > sw or top + height > sh:
            return None
    return (left, top, width, height)


def _front_window_info() -> Tuple[Optional[int], str, str]:
    """前台窗口句柄 / 标题 / 进程名。任何一步失败都退回全屏，不让截图挂掉。"""
    try:
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return None, "", ""
        title = ""
        try:
            title = win32gui.GetWindowText(hwnd) or ""
        except Exception:
            pass
        proc = ""
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if pid and psutil is not None:
                proc = psutil.Process(pid).name()
        except Exception:
            pass
        return hwnd, title, proc
    except Exception:
        return None, "", ""


class GameShotService:
    """随 GUI 启动的全局热键截图服务。

    ``notify(message, color)`` 由调用方提供，必须是线程安全的（内部只发 Qt 信号）。
    ``config`` / ``shot_dir`` 可注入，便于纯逻辑回归，不碰真实桌面与磁盘。
    """

    def __init__(
        self,
        notify: Optional[Callable[..., None]] = None,
        config: Optional[Dict] = None,
        shot_dir=None,
    ) -> None:
        self._notify = notify or (lambda message, color="#2f7d4a": None)
        cfg = dict(DEFAULT_GAME_SHOT)
        cfg.update(config if config is not None else self._load_config())
        self._config: Dict = cfg
        self._shot_dir = Path(shot_dir) if shot_dir is not None else GAME_SHOT_DIR
        self._listener = None
        self._lock = threading.Lock()
        self._last_shot = 0.0
        self._started = False

    @staticmethod
    def _load_config() -> Dict:
        """读 app_config.game_shot（manager.load 已合并默认段，返回深拷贝）。"""
        try:
            return dict(cfg_manager.get("game_shot", {}) or {})
        except Exception as exc:
            LOGGER.warning("[游戏截图] 读取配置失败，使用默认值: %s", exc)
            return {}

    @property
    def hotkey(self) -> str:
        return str(self._config.get("hotkey") or DEFAULT_GAME_SHOT["hotkey"])

    def start(self) -> bool:
        """注册全局热键并后台预热 WGC。失败时明确报错 + toast，不静默假装启用。"""
        if self._started:
            return True
        if not self._config.get("enabled", True):
            LOGGER.info("[游戏截图] 配置已禁用，跳过启动")
            return False
        try:
            combo = to_pynput_hotkey(self.hotkey)
        except ValueError as exc:
            LOGGER.error("[游戏截图] 热键 %r 非法，服务未启动: %s", self.hotkey, exc)
            self._notify(f"截图热键配置无效：{exc}", "#b04b44")
            return False
        try:
            from pynput import keyboard

            self._listener = keyboard.GlobalHotKeys({combo: self._on_hotkey})
            self._listener.start()
        except Exception as exc:
            LOGGER.error("[游戏截图] 注册全局热键失败: %s", exc)
            self._listener = None
            self._notify("截图热键注册失败（组合被占用或权限不足）", "#b04b44")
            return False
        self._started = True
        LOGGER.info("[游戏截图] 已启动：热键 %s → %s", self.hotkey, self._shot_dir)
        # WGC 首次建会话最多 2.5s；启动即预热，保证第一次按键就是实时截图
        threading.Thread(target=self._warmup, daemon=True, name="GameShotWarmup").start()
        return True

    def _warmup(self) -> None:
        try:
            from ..runtime.ocr_click_tester import grab_screen

            grab_screen()
            LOGGER.info("[游戏截图] WGC 预热完成，首次按键可即时截图")
        except Exception as exc:
            LOGGER.warning("[游戏截图] WGC 预热失败，首次按键可能延迟: %s", exc)

    def _on_hotkey(self) -> None:
        try:
            self.capture()
        except Exception as exc:
            # 回调在 pynput 线程，未捕获异常会静默杀死监听，必须留痕
            LOGGER.exception("[游戏截图] 截图回调异常: %s", exc)
            self._notify("截图失败，详见日志", "#b04b44")

    def capture(self):
        """截当前前台窗口并落盘，返回图片路径；防抖/失败时返回 None。

        只读取画面，不改窗口层级、不聚焦、不置顶；异常一律记日志并通知，
        不向上抛（热键线程不能崩）。
        """
        now = time.monotonic()
        interval = float(self._config.get("min_interval_sec", 0.8))
        with self._lock:
            if now - self._last_shot < interval:
                return None
            self._last_shot = now
        try:
            from ..runtime.ocr_click_tester import grab_screen

            hwnd, title, proc = _front_window_info()
            region = None
            if self._config.get("capture") == "window" and hwnd:
                screen_size = None
                try:
                    import win32api

                    screen_size = (win32api.GetSystemMetrics(0), win32api.GetSystemMetrics(1))
                except Exception:
                    pass
                region = _window_region(hwnd, screen_size=screen_size)
            img = grab_screen(region=region)

            max_width = int(self._config.get("max_width", 0) or 0)
            if max_width > 0 and img.width > max_width:
                from PIL import Image

                img.thumbnail((max_width, max_width), resample=Image.Resampling.LANCZOS)
            self._shot_dir.mkdir(parents=True, exist_ok=True)
            fmt = str(self._config.get("format", "png")).lower()
            ext = "jpg" if fmt in ("jpg", "jpeg") else "png"
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
            path = self._shot_dir / f"{stamp}_{_safe_tag(proc or title)}.{ext}"
            if ext == "jpg":
                img.convert("RGB").save(
                    path, "JPEG", quality=int(self._config.get("jpeg_quality", 90)), optimize=True
                )
            else:
                # optimize=True 在 1920x1080 上要 1s+，会把"按键即截"拖成卡顿。
                # 低压缩级别换速度，本地留存的图片大一点无所谓。
                img.save(path, "PNG", compress_level=1)

            if self._config.get("sound", True) and winsound is not None:
                try:
                    winsound.PlaySound("SystemAsterisk", winsound.SND_ALIAS | winsound.SND_ASYNC)
                except Exception as exc:
                    LOGGER.debug("[游戏截图] 提示音播放失败: %s", exc)
            if self._config.get("toast", True):
                self._notify(f"截图已保存：{path.name}", "#2f7d4a")
            LOGGER.info(
                "[游戏截图] 已保存 %s（%dx%d，来源=%s，范围=%s）",
                path, img.width, img.height, proc or title or "全屏",
                "窗口" if region else "全屏",
            )
            return path
        except Exception as exc:
            LOGGER.exception("[游戏截图] 截图失败: %s", exc)
            self._notify("截图失败，详见日志", "#b04b44")
            return None

    def stop(self) -> None:
        """注销热键监听。GUI 用 os._exit 强退跳过 atexit，必须显式调用。"""
        if not self._started:
            return
        self._started = False
        try:
            if self._listener is not None:
                self._listener.stop()
        except Exception as exc:
            LOGGER.debug("[游戏截图] 注销热键失败: %s", exc)
        finally:
            self._listener = None
