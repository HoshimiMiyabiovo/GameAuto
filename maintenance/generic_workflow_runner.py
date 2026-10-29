import json
import os
import time
import ctypes
import logging
import subprocess
from typing import Any, Dict, List, Optional, Tuple

import pyautogui
import win32gui

from game_auto.storage.workflow_db import WorkflowDB


class GenericWorkflowRunner:
    """
    数据驱动的自动化执行器：
    1) 从 DB 读取流程定义
    2) 启动 exe 并等待窗口
    3) 按步骤执行截图识别/点击/等待
    """

    def __init__(self, db_path: str, logger: Optional[logging.Logger] = None):
        self.db = WorkflowDB(db_path)
        self.logger = logger or logging.getLogger("GameAutomation")
        pyautogui.FAILSAFE = True
        pyautogui.PAUSE = 0.1

    def run_workflow(self, workflow_name: str, screenshot_dir: str) -> bool:
        wf = self.db.get_workflow_by_name(workflow_name)
        if not wf:
            self.logger.error(f"未找到可用流程: {workflow_name}")
            return False

        steps = self.db.get_steps(int(wf["id"]))
        if not steps:
            self.logger.error(f"流程无步骤: {workflow_name}")
            return False

        if not self._launch(wf):
            return False

        hwnd = self._wait_window(wf.get("window_title_keyword", ""), int(wf.get("wait_window_timeout", 60)))
        if not hwnd:
            self.logger.error(f"[{workflow_name}] 未找到窗口: {wf.get('window_title_keyword', '')}")
            return False

        self._activate_window(hwnd)
        time.sleep(float(wf.get("startup_delay", 2.0)))
        os.makedirs(screenshot_dir, exist_ok=True)

        self.logger.info(f"[{workflow_name}] 开始执行步骤，共 {len(steps)} 步")
        for step in steps:
            ok = self._run_step(workflow_name, step, screenshot_dir)
            if ok:
                continue
            on_fail = step.get("on_fail", "stop")
            if on_fail == "continue":
                self.logger.warning(f"[{workflow_name}] 步骤失败但继续: {step.get('name') or step['step_type']}")
                continue
            self.logger.error(f"[{workflow_name}] 步骤失败并停止: {step.get('name') or step['step_type']}")
            return False

        self.logger.info(f"[{workflow_name}] 流程执行完成")
        return True

    def _launch(self, wf: Dict[str, Any]) -> bool:
        exe_path = wf["exe_path"]
        if not os.path.exists(exe_path):
            self.logger.error(f"EXE 不存在: {exe_path}")
            return False
        work_dir = wf.get("work_dir") or os.path.dirname(exe_path)
        args = self._safe_json_load(wf.get("args_json", "[]"), default=[])
        cmd = [exe_path] + args
        self.logger.info(f"启动流程程序: {' '.join(cmd)}")
        try:
            subprocess.Popen(
                cmd,
                cwd=work_dir,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            )
            return True
        except Exception as e:
            self.logger.error(f"启动失败: {str(e)}")
            return False

    def _run_step(self, workflow_name: str, step: Dict[str, Any], screenshot_dir: str) -> bool:
        step_type = step["step_type"]
        params = self._safe_json_load(step.get("params_json", "{}"), default={})
        step_name = step.get("name") or step_type
        self.logger.info(f"[{workflow_name}] 步骤: {step_name}")

        if step_type == "sleep":
            time.sleep(float(params.get("seconds", 1.0)))
            return True

        if step_type == "screenshot":
            file_name = params.get("name", f"{workflow_name}_shot")
            return self._capture(screenshot_dir, file_name)

        if step_type == "click_point":
            return self._click_point(params)

        if step_type in ("wait_image", "click_image"):
            image_path = params.get("image_path", "")
            timeout = int(params.get("timeout", 20))
            confidence = params.get("confidence", None)
            region = self._parse_region(params.get("region"))
            center = self._wait_image(image_path, timeout, confidence, region)
            if not center:
                return False
            if step_type == "click_image":
                x_offset = int(params.get("x_offset", 0))
                y_offset = int(params.get("y_offset", 0))
                clicks = int(params.get("clicks", 1))
                interval = float(params.get("interval", 0.1))
                button = params.get("button", "left")
                pyautogui.click(
                    x=center[0] + x_offset,
                    y=center[1] + y_offset,
                    clicks=clicks,
                    interval=interval,
                    button=button,
                )
                time.sleep(float(params.get("delay_after", 0.8)))
            return True

        self.logger.error(f"未知步骤类型: {step_type}")
        return False

    def _click_point(self, params: Dict[str, Any]) -> bool:
        try:
            x = int(params["x"])
            y = int(params["y"])
            clicks = int(params.get("clicks", 1))
            interval = float(params.get("interval", 0.1))
            button = params.get("button", "left")
            delay_after = float(params.get("delay_after", 0.8))
            pyautogui.click(x=x, y=y, clicks=clicks, interval=interval, button=button)
            time.sleep(delay_after)
            return True
        except Exception as e:
            self.logger.error(f"click_point 失败: {str(e)}")
            return False

    def _wait_image(
        self,
        image_path: str,
        timeout: int,
        confidence: Optional[float],
        region: Optional[Tuple[int, int, int, int]],
    ) -> Optional[Tuple[int, int]]:
        if not image_path or not os.path.exists(image_path):
            self.logger.error(f"图片不存在: {image_path}")
            return None
        end_time = time.time() + timeout
        while time.time() < end_time:
            try:
                locate_kwargs: Dict[str, Any] = {"region": region} if region else {}
                if confidence is not None:
                    locate_kwargs["confidence"] = float(confidence)
                pt = pyautogui.locateCenterOnScreen(image_path, **locate_kwargs)
                if pt:
                    return (pt.x, pt.y)
            except Exception:
                # 未安装 opencv 时 confidence 可能不可用，自动降级重试
                try:
                    pt = pyautogui.locateCenterOnScreen(image_path, region=region) if region else pyautogui.locateCenterOnScreen(image_path)
                    if pt:
                        return (pt.x, pt.y)
                except Exception as exc:
                    self.logger.debug("[等待图片] 定位失败 %s: %s", image_path, exc)
            time.sleep(0.4)
        self.logger.warning(f"等待图片超时: {image_path}")
        return None

    def _wait_window(self, title_keyword: str, timeout: int) -> Optional[int]:
        if not title_keyword:
            return win32gui.GetForegroundWindow()
        end = time.time() + timeout
        keyword = title_keyword.lower()
        while time.time() < end:
            hwnd = self._find_window_contains(keyword)
            if hwnd:
                return hwnd
            time.sleep(0.4)
        return None

    def _find_window_contains(self, keyword_lower: str) -> Optional[int]:
        result: List[int] = []

        def callback(hwnd, _):
            if not win32gui.IsWindowVisible(hwnd):
                return
            title = win32gui.GetWindowText(hwnd).strip().lower()
            if title and keyword_lower in title:
                result.append(hwnd)

        win32gui.EnumWindows(callback, None)
        return result[0] if result else None

    def _activate_window(self, hwnd: int):
        try:
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, 9)
            ctypes.windll.user32.SetForegroundWindow(hwnd)
        except Exception as e:
            self.logger.warning(f"激活窗口失败: {str(e)}")

    def _capture(self, screenshot_dir: str, prefix: str) -> bool:
        ts = time.strftime("%Y%m%d_%H%M%S")
        out = os.path.join(screenshot_dir, f"{prefix}_{ts}.png")
        try:
            pyautogui.screenshot(out)
            self.logger.info(f"截图保存: {out}")
            return True
        except Exception as e:
            self.logger.error(f"截图失败: {str(e)}")
            return False

    @staticmethod
    def _safe_json_load(text: str, default: Any) -> Any:
        try:
            return json.loads(text)
        except Exception:
            return default

    @staticmethod
    def _parse_region(value: Any) -> Optional[Tuple[int, int, int, int]]:
        if not value:
            return None
        if isinstance(value, (list, tuple)) and len(value) == 4:
            return int(value[0]), int(value[1]), int(value[2]), int(value[3])
        return None

