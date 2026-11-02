import win32gui
import win32process
import re
from typing import Optional

class WindowDetector:
    @staticmethod
    def find_window_process(title_pattern: str) -> Optional[int]:
        pids = []
        def callback(hwnd, _):
            if win32gui.IsWindowVisible(hwnd):
                title = win32gui.GetWindowText(hwnd)
                if re.match(title_pattern, title):
                    _, pid = win32process.GetWindowThreadProcessId(hwnd)
                    pids.append(pid)
        win32gui.EnumWindows(callback, None)
        return pids[0] if pids else None