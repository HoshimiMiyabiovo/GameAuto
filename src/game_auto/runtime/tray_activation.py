"""Windows 托盘图标激活（2026-11-26）。

某些 Electron 应用关闭主窗口后仍驻留托盘，普通 ``EnumWindows`` 看不到窗口，
再次启动 exe 只会单实例交接并退出。这里用系统 UI Automation 找到托盘图标并
调用 InvokePattern，让应用自己恢复主窗口；不发送鼠标坐标、不结束进程。

PowerShell 仅作为 UIAutomationClient 的宿主，目标应用名通过环境变量传入，
避免把配置文本拼进命令造成注入。脚本使用 ``-EncodedCommand``，规避中文与
引号在命令行里的编码问题。
"""

from __future__ import annotations

import base64
import logging
import os
import subprocess
import time
from typing import Callable, Optional

from ..services.task_heartbeat_watchdog import beat_thread_heartbeat

LOGGER = logging.getLogger("GameAutomation")

_POWERSHELL_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient
$targetName = $env:GAMEAUTO_TRAY_ICON_NAME
if ([string]::IsNullOrWhiteSpace($targetName)) { exit 2 }
$root = [System.Windows.Automation.AutomationElement]::RootElement
$children = [System.Windows.Automation.TreeScope]::Children
$descendants = [System.Windows.Automation.TreeScope]::Descendants
$trueCondition = [System.Windows.Automation.Condition]::TrueCondition
$trayCondition = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ClassNameProperty, 'Shell_TrayWnd')
$tray = $root.FindFirst($children, $trayCondition)
if ($null -eq $tray) { exit 3 }
$items = $tray.FindAll($descendants, $trueCondition)
$target = $null
foreach ($item in $items) {
    if ($item.Current.Name.Trim() -eq $targetName) { $target = $item; break }
}
if ($null -eq $target) {
    $hiddenName = -join @([char]0x663e, [char]0x793a, [char]0x9690, [char]0x85cf, [char]0x7684, [char]0x56fe, [char]0x6807)
    $hidden = $null
    foreach ($item in $items) {
        if ($item.Current.Name.Trim() -eq $hiddenName) { $hidden = $item; break }
    }
    if ($null -eq $hidden) { exit 4 }
    $hidden.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
    Start-Sleep -Milliseconds 500
    $overflowCondition = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ClassNameProperty, 'TopLevelWindowForOverflowXamlIsland')
    $overflow = $root.FindFirst($children, $overflowCondition)
    if ($null -eq $overflow) { exit 5 }
    $overflowItems = $overflow.FindAll($descendants, $trueCondition)
    foreach ($item in $overflowItems) {
        if ($item.Current.Name.Trim() -eq $targetName) { $target = $item; break }
    }
}
if ($null -eq $target) { exit 6 }
$target.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
exit 0
"""


def _encoded_command() -> str:
    return base64.b64encode(_POWERSHELL_SCRIPT.encode("utf-16le")).decode("ascii")


def activate_tray_icon(
    logger: logging.Logger,
    icon_name: str,
    timeout_sec: float = 15.0,
    stop_check: Optional[Callable[[], bool]] = None,
) -> bool:
    """调用托盘图标的 InvokePattern；成功返回 True，找不到/超时/停止返回 False。"""
    icon_name = str(icon_name or "").strip()
    if not icon_name:
        return False
    if stop_check and stop_check():
        return False

    env = os.environ.copy()
    env["GAMEAUTO_TRAY_ICON_NAME"] = icon_name
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.Popen(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-EncodedCommand",
                _encoded_command(),
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
    except OSError as exc:
        logger.debug("[托盘] 启动 UI Automation 失败: %s", exc)
        return False

    deadline = time.monotonic() + max(1.0, float(timeout_sec))
    try:
        while proc.poll() is None:
            beat_thread_heartbeat()
            if stop_check and stop_check():
                proc.terminate()
                proc.wait(timeout=3)
                return False
            if time.monotonic() >= deadline:
                proc.terminate()
                proc.wait(timeout=3)
                logger.warning("[托盘] 激活「%s」超时", icon_name)
                return False
            time.sleep(0.2)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()

    if proc.returncode != 0:
        logger.debug("[托盘] 未找到或无法激活「%s」（退出码 %s）", icon_name, proc.returncode)
        return False
    logger.info("[托盘] 已激活「%s」", icon_name)
    return True
