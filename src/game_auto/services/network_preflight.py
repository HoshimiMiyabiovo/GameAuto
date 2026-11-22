"""启动前出网检查：把"系统代理没启动"从通用网络错误里识别出来。

urllib 在 Windows 上会自动读注册表系统代理。代理软件没开时，请求发往
``127.0.0.1:7897`` 会得到 ``WinError 10061 目标计算机积极拒绝``，日志看起来
像"外网断了"，实际只需要启动代理。本模块只探测**指向本机**的代理端口：
远程代理不可连的原因太多，不在这里判定，避免误报。

调用方：``services/startup_sign_runner.py`` 的各个网络签到分支。使用
``make_proxy_guard()`` 可以在同一批次内只探测一次。
"""

from __future__ import annotations

import logging
import socket
from typing import Callable, Optional
from urllib.parse import urlsplit
from urllib.request import getproxies

LOGGER = logging.getLogger("GameAutomation")

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


class SystemProxyUnavailable(RuntimeError):
    """系统代理指向本机端口但连接被拒绝（通常是代理软件未启动）。"""


def _parse_loopback_proxy(value: str) -> Optional[tuple[str, int]]:
    """把代理配置值解析成 (host, port)；只返回本机代理。"""
    raw = value.strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = "http://" + raw
    try:
        parts = urlsplit(raw)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if not host or port is None or host not in _LOOPBACK_HOSTS:
        return None
    return host, port


def probe_system_proxy(timeout: float = 0.5) -> Optional[str]:
    """探测系统代理中的本机端口。

    返回 None 表示无需干预（没配代理 / 代理不是本机 / 端口可连）；
    返回中文提示表示本机代理端口连不上，调用方应直接展示该文案。
    """
    try:
        proxies = getproxies()
    except OSError as exc:
        LOGGER.debug("[网络预检] 读取系统代理配置失败，跳过探测: %s", exc)
        return None

    checked: set[tuple[str, int]] = set()
    for scheme in ("https", "http"):
        parsed = _parse_loopback_proxy(str(proxies.get(scheme) or ""))
        if not parsed or parsed in checked:
            continue
        checked.add(parsed)
        host, port = parsed
        try:
            with socket.create_connection((host, port), timeout=timeout):
                pass
        except OSError as exc:
            LOGGER.warning("[网络预检] 系统代理 %s:%s 无法连接: %s", host, port, exc)
            return (
                f"系统代理 {host}:{port} 未启动或无法连接（{exc}）；"
                "请先启动代理软件，或在 Windows 设置中关闭系统代理后重试。"
            )
    return None


def make_proxy_guard() -> Callable[[], None]:
    """返回一次探测、多次复用的 guard，供一批网络任务共用。

    guard 在代理不可用时抛 ``SystemProxyUnavailable``，异常文案就是给用户看的
    提示；代理正常时什么都不做。
    """
    state: dict[str, object] = {"checked": False, "message": None}

    def _guard() -> None:
        if not state["checked"]:
            state["checked"] = True
            state["message"] = probe_system_proxy()
        message = state["message"]
        if message:
            raise SystemProxyUnavailable(str(message))

    return _guard
