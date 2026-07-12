import os
import time
import ctypes
import logging
import subprocess
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import pyautogui
import win32gui


Point = Tuple[int, int]
Rect = Tuple[int, int, int, int]


@dataclass
class ClickStep:
    """单次点击步骤。"""
    x: int
    y: int
    delay_after: float = 0.8
    clicks: int = 1
    interval: float = 0.1
    button: str = "left"


@dataclass
class TaskProfile:
    """页面自动化配置。"""
    name: str
    exe_path: str
    work_dir: Optional[str] = None
    args: List[str] = field(default_factory=list)
    window_title_keyword: str = ""
    wait_window_timeout: int = 60
    startup_delay: float = 2.0
    capture_before: bool = True
    capture_after: bool = True
    clicks: List[ClickStep] = field(default_factory=list)


class ScreenTaskRunner:
    """
    启动指定 EXE 后，基于屏幕坐标执行点击任务。
    适用于固定分辨率、固定窗口布局场景。
    """

    def __init__(self, logger: Optional[logging.Logger] = None):
        self.logger = logger or logging.getLogger("GameAutomation")
        pyautogui.FAILSAFE = True
        pyautogui.PAUSE = 0.1

    def run(self, profile: TaskProfile, screenshot_dir: str) -> bool:
        os.makedirs(screenshot_dir, exist_ok=True)
        self.logger.info(f"[{profile.name}] 启动自动页面任务")

        proc = self._launch_exe(profile)
        if not proc:
            return False

        hwnd = self._wait_window(profile.window_title_keyword, profile.wait_window_timeout)
        if not hwnd:
            self.logger.error(f"[{profile.name}] 未找到目标窗口: {profile.window_title_keyword}")
            return False

        self._activate_window(hwnd)
        time.sleep(profile.startup_delay)

        if profile.capture_before:
            self._capture(screenshot_dir, f"{profile.name}_before")

        for idx, step in enumerate(profile.clicks, start=1):
            self.logger.info(f"[{profile.name}] 执行点击步骤 {idx}: ({step.x}, {step.y})")
            pyautogui.click(
                x=step.x,
                y=step.y,
                clicks=step.clicks,
                interval=step.interval,
                button=step.button,
            )
            time.sleep(step.delay_after)

        if profile.capture_after:
            self._capture(screenshot_dir, f"{profile.name}_after")

        self.logger.info(f"[{profile.name}] 页面自动任务完成")
        return True

    def _launch_exe(self, profile: TaskProfile) -> Optional[subprocess.Popen]:
        if not os.path.exists(profile.exe_path):
            self.logger.error(f"[{profile.name}] EXE 不存在: {profile.exe_path}")
            return None

        work_dir = profile.work_dir or os.path.dirname(profile.exe_path)
        cmd = [profile.exe_path] + profile.args
        self.logger.info(f"[{profile.name}] 启动程序: {' '.join(cmd)}")
        try:
            return subprocess.Popen(
                cmd,
                cwd=work_dir,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        except Exception as e:
            self.logger.error(f"[{profile.name}] 启动失败: {str(e)}")
            return None

    def _wait_window(self, title_keyword: str, timeout: int) -> Optional[int]:
        if not title_keyword:
            self.logger.warning("未提供 window_title_keyword，将跳过窗口匹配")
            return win32gui.GetForegroundWindow()

        end_time = time.time() + timeout
        while time.time() < end_time:
            hwnd = self._find_window_contains(title_keyword)
            if hwnd:
                return hwnd
            time.sleep(0.5)
        return None

    def _find_window_contains(self, keyword: str) -> Optional[int]:
        matches = []
        kw = keyword.lower()

        def callback(hwnd, _):
            if not win32gui.IsWindowVisible(hwnd):
                return
            title = win32gui.GetWindowText(hwnd).strip()
            if title and kw in title.lower():
                matches.append(hwnd)

        win32gui.EnumWindows(callback, None)
        return matches[0] if matches else None

    def _activate_window(self, hwnd: int):
        try:
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, 9)  # SW_RESTORE
            ctypes.windll.user32.SetForegroundWindow(hwnd)
            self.logger.info("目标窗口已激活")
        except Exception as e:
            self.logger.warning(f"激活窗口失败，将继续尝试点击: {str(e)}")

    def _capture(self, screenshot_dir: str, name: str):
        ts = time.strftime("%Y%m%d_%H%M%S")
        file_path = os.path.join(screenshot_dir, f"{name}_{ts}.png")
        try:
            pyautogui.screenshot(file_path)
            self.logger.info(f"截图已保存: {file_path}")
        except Exception as e:
            self.logger.warning(f"截图失败: {str(e)}")


def build_basic_profile(
    name: str,
    exe_path: str,
    window_title_keyword: str,
    click_points: List[Point],
    args: Optional[List[str]] = None,
    work_dir: Optional[str] = None,
) -> TaskProfile:
    """
    快速构建配置的辅助函数。
    click_points: [(x1, y1), (x2, y2), ...]
    """
    steps = [ClickStep(x=x, y=y) for x, y in click_points]
    return TaskProfile(
        name=name,
        exe_path=exe_path,
        work_dir=work_dir,
        args=args or [],
        window_title_keyword=window_title_keyword,
        clicks=steps,
    )
