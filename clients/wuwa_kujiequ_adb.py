"""通过 ADB + WebView CDP 协议从库街区 APP 抓取 Token / Did。

参考教程：https://wave.999758.xyz/pages/advance/token.html

流程：
1. 通过 adb devices 检查已连接的安卓设备
2. adb shell cat /proc/net/unix 找到 com.kurogame.kjq 进程对应的
   `@webview_devtools_remote_<pid>` 抽象 unix socket
3. adb forward tcp:<port> localabstract:webview_devtools_remote_<pid>
   将调试 socket 转发到本地端口
4. 访问 http://localhost:<port>/json 拿可调试页面列表
5. 选中"养成计算器"页面，建立 WebSocket 连接到其 webSocketDebuggerUrl
6. 通过 CDP Runtime.evaluate 执行 JS，读取 localStorage 里的
   `mc-growth-simulator-user-info` 与 `mc-growth-simulator-role-info`
7. 解析出 token / did / userId / roleId / roleName
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import shutil
import socket
import struct
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from . import wuwa_kujiequ

LOGGER = logging.getLogger("GameAutomation")

KUJIEQU_PACKAGE = "com.kurogame.kjq"
WEBVIEW_SOCKET_RE = re.compile(r"@webview_devtools_remote_(\d+)")
CDP_FORWARD_PORT = 9222
ADB_TIMEOUT = 10.0
CDP_HTTP_TIMEOUT = 5.0
CDP_WS_TIMEOUT = 15.0


class KujiequAdbError(RuntimeError):
    """库街区 ADB 抓取流程错误。"""


@dataclass
class KujiequAdbCapture:
    """从库街区 APP 抓取到的完整凭据快照。"""
    token: str
    did: str
    user_id: str = ""
    role_id: str = ""
    role_name: str = ""
    role_info: Dict[str, Any] = field(default_factory=dict)
    user_info: Dict[str, Any] = field(default_factory=dict)


def find_adb() -> str:
    """查找 adb.exe 路径。

    优先 shutil.which("adb")；其次尝试 Windows 常见解压目录与项目内置搜索路径。
    """
    candidate = shutil.which("adb") or shutil.which("adb.exe")
    if candidate:
        return candidate

    candidates: List[str] = []
    user_profile = os.environ.get("USERPROFILE") or os.environ.get("HOME") or ""
    if user_profile:
        candidates.append(
            os.path.join(user_profile, "Downloads",
                         "platform-tools-latest-windows", "platform-tools", "adb.exe")
        )
    candidates.extend([
        r"C:\platform-tools\adb.exe",
        r"C:\Program Files\platform-tools\adb.exe",
        r"C:\Program Files (x86)\platform-tools\adb.exe",
    ])

    for path in candidates:
        if path and os.path.exists(path):
            return path
    raise KujiequAdbError(
        "未找到 adb.exe。请将 Android platform-tools 解压目录加入 PATH，"
        "或解压到 C:\\platform-tools。"
    )


def _run_adb(adb_path: str, args: List[str], timeout: float = ADB_TIMEOUT) -> str:
    """执行 adb 命令并返回 stdout。"""
    cmd = [adb_path] + args
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired as exc:
        raise KujiequAdbError(f"adb 命令超时: {' '.join(cmd)}") from exc
    except FileNotFoundError as exc:
        raise KujiequAdbError(f"找不到 adb 可执行文件: {adb_path}") from exc
    if result.returncode != 0:
        err = (result.stderr or "").strip()
        raise KujiequAdbError(f"adb 命令失败 [{result.returncode}]: {err}")
    return result.stdout or ""


def _adb_shell(adb_path: str, serial: Optional[str], shell_cmd: str,
               timeout: float = ADB_TIMEOUT) -> str:
    args: List[str] = []
    if serial:
        args += ["-s", serial]
    args += ["shell", shell_cmd]
    return _run_adb(adb_path, args, timeout=timeout)


def list_devices(adb_path: str) -> List[str]:
    """列出处于 device 状态的设备序列号。"""
    out = _run_adb(adb_path, ["devices"])
    serials: List[str] = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            serials.append(parts[0])
    return serials


def _find_kujiequ_pid(adb_path: str, serial: Optional[str]) -> Optional[str]:
    """通过 `ps -A` 找 com.kurogame.kjq 进程的 PID。"""
    out = _adb_shell(adb_path, serial, f"ps -A | grep {KUJIEQU_PACKAGE}")
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1] == KUJIEQU_PACKAGE:
            return parts[1]
    return None


def find_webview_socket(adb_path: str, serial: Optional[str]
                        ) -> Tuple[str, str]:
    """扫描 /proc/net/unix 找到 com.kurogame.kjq 进程对应的 webview 调试 socket。

    返回 (pid, socket_name)，例如 ("12899", "webview_devtools_remote_12899")。
    """
    out = _adb_shell(adb_path, serial, "cat /proc/net/unix")
    socket_pids = set(WEBVIEW_SOCKET_RE.findall(out))
    if not socket_pids:
        raise KujiequAdbError(
            "未找到 WebView 调试 socket，请确认已在库街区 APP 打开'养成计算器'页面"
        )

    kujiequ_pid = _find_kujiequ_pid(adb_path, serial)
    target_pid: Optional[str] = None
    if kujiequ_pid:
        for pid in socket_pids:
            if pid == kujiequ_pid:
                target_pid = pid
                break
    if target_pid is None:
        # 退而求其次：取第一个 webview socket（多 WebView 应用场景下不安全，
        # 但单 APP 场景下能 work）
        target_pid = next(iter(socket_pids))
        if kujiequ_pid:
            raise KujiequAdbError(
                f"库街区进程 PID={kujiequ_pid} 与 WebView socket PID 不匹配，"
                f"扫描到的 socket PIDs={sorted(socket_pids)}；"
                f"请确认 APP 内'养成计算器'页面处于打开状态"
            )

    return target_pid, f"webview_devtools_remote_{target_pid}"


def forward_cdp(adb_path: str, serial: Optional[str], socket_name: str,
                port: int = CDP_FORWARD_PORT) -> None:
    args: List[str] = []
    if serial:
        args += ["-s", serial]
    args += ["forward", f"tcp:{port}", f"localabstract:{socket_name}"]
    _run_adb(adb_path, args)
    # 清理残留的旧 forward（保险措施）
    # adb forward --remove tcp:<port> 会在重新 forward 时自动覆盖


def list_cdp_pages(port: int = CDP_FORWARD_PORT) -> List[Dict[str, Any]]:
    """GET http://localhost:<port>/json 返回可调试页面列表。"""
    url = f"http://localhost:{port}/json"
    try:
        with urllib.request.urlopen(url, timeout=CDP_HTTP_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise KujiequAdbError(
            f"无法访问 CDP HTTP 端口 {port}: {exc.reason}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise KujiequAdbError(f"CDP /json 返回非 JSON: {exc}") from exc
    if not isinstance(data, list):
        raise KujiequAdbError("CDP /json 返回格式异常")
    return data


def find_growth_calculator_page(pages: List[Dict[str, Any]]) -> Dict[str, Any]:
    """从 CDP 页面列表里挑出养成计算器页面。"""
    for page in pages:
        if not isinstance(page, dict):
            continue
        url = str(page.get("url") or "")
        title = str(page.get("title") or "")
        if "growth-calculator" in url or "养成计算器" in title:
            ws = page.get("webSocketDebuggerUrl")
            if ws:
                return page
    raise KujiequAdbError(
        "未找到'养成计算器' WebView 页面，请在库街区 APP 内进入养成计算器后重试"
    )


# ── 纯标准库 WebSocket 客户端（够用即可，不追求 RFC 6455 全特性） ─────


def _ws_handshake(host: str, port: int, path: str) -> socket.socket:
    """HTTP 升级握手，返回已升级的 socket。

    注意：**不要发送 Origin 头**。Chrome 111+ / 新版 Android WebView 会对
    WebSocket 升级做 Origin 校验，带 `http://host:port` 这类 Origin 会被直接
    拒绝（HTTP 403），而 CDP 官方前端用的是 `devtools://devtools`。非浏览器
    客户端（pychrome 等）的通行做法是完全省略 Origin —— 缺省时不会被拦截。
    """
    sock = socket.create_connection((host, port), timeout=CDP_HTTP_TIMEOUT)
    try:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"\r\n"
        )
        sock.sendall(req.encode("ascii"))

        buf = bytearray()
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
        status_line = buf.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
        if " 101 " not in status_line and not status_line.endswith(" 101"):
            sock.close()
            raise KujiequAdbError(f"WebSocket 握手失败: {status_line}")
        return sock
    except Exception:
        sock.close()
        raise


def _ws_send_text(sock: socket.socket, text: str) -> None:
    """发送 WebSocket 文本帧（客户端必须 mask）。"""
    data = text.encode("utf-8")
    length = len(data)
    mask_key = os.urandom(4)  # 随机掩码

    header = bytearray()
    header.append(0x81)  # FIN=1, opcode=1 (text)

    if length < 126:
        header.append(0x80 | length)  # mask=1
    elif length < 65536:
        header.append(0x80 | 126)
        header += struct.pack(">H", length)
    else:
        header.append(0x80 | 127)
        header += struct.pack(">Q", length)
    header += mask_key

    masked = bytes(b ^ mask_key[i % 4] for i, b in enumerate(data))
    sock.sendall(bytes(header) + masked)


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            break
        buf += chunk
    return bytes(buf)


def _ws_recv_message(sock: socket.socket, timeout: float = CDP_WS_TIMEOUT
                     ) -> Optional[str]:
    """接收一个完整 WebSocket 消息（处理分片与控制帧），返回文本。"""
    sock.settimeout(timeout)
    fragments: List[str] = []
    while True:
        header = _recv_exact(sock, 2)
        if len(header) < 2:
            break
        b1, b2 = header[0], header[1]
        fin = (b1 & 0x80) != 0
        opcode = b1 & 0x0F
        masked = (b2 & 0x80) != 0
        length = b2 & 0x7F

        if length == 126:
            length = struct.unpack(">H", _recv_exact(sock, 2))[0]
        elif length == 127:
            length = struct.unpack(">Q", _recv_exact(sock, 8))[0]

        mask_key = b""
        if masked:
            mask_key = _recv_exact(sock, 4)

        payload = _recv_exact(sock, length) if length else b""
        if masked and mask_key:
            payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))

        if opcode == 0x8:  # close
            break
        if opcode == 0x9:  # ping -> pong
            sock.sendall(bytes([0x8A, len(payload)]) + payload)
            continue
        if opcode == 0xA:  # pong
            continue
        # text (0x1) or continuation (0x0)
        fragments.append(payload.decode("utf-8", errors="replace"))
        if fin:
            break
    return "".join(fragments) if fragments else None


def cdp_runtime_eval(ws_url: str, expression: str,
                     timeout: float = CDP_WS_TIMEOUT) -> Dict[str, Any]:
    """通过 CDP Runtime.evaluate 在页面里执行 JS 表达式。"""
    m = re.match(r"ws://([^:/]+):(\d+)(/.*)?", ws_url)
    if not m:
        raise KujiequAdbError(f"无效的 WebSocket URL: {ws_url}")
    host = m.group(1)
    port = int(m.group(2))
    path = m.group(3) or "/"

    sock = _ws_handshake(host, port, path)
    try:
        msg = json.dumps({
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": False,
            },
        })
        _ws_send_text(sock, msg)
        # 收到第一条消息即返回（CDP page-level socket 不会主动推送其他事件）
        text = _ws_recv_message(sock, timeout=timeout)
        if not text:
            raise KujiequAdbError("CDP 返回空响应")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise KujiequAdbError(f"CDP 返回非 JSON: {text[:200]}") from exc
    finally:
        try:
            sock.close()
        except Exception:
            pass


# ── 主流程 ──────────────────────────────────────────────────────────


_LOCAL_STORAGE_EXPR = """
(function () {
    try {
        var ui_raw = localStorage.getItem('mc-growth-simulator-user-info') || '{}';
        var ri_raw = localStorage.getItem('mc-growth-simulator-role-info') || '{}';
        var ui = JSON.parse(ui_raw);
        var ri = JSON.parse(ri_raw);
        return JSON.stringify({
            user_info: ui,
            role_info: ri,
            location: location.href
        });
    } catch (e) {
        return JSON.stringify({ error: String(e) });
    }
})()
""".strip()


def _parse_capture(payload: Dict[str, Any]) -> KujiequAdbCapture:
    """从 Runtime.evaluate 返回里解析 token/did。"""
    result_obj = (payload or {}).get("result", {}) or {}
    if "exceptionDetails" in result_obj:
        exc = result_obj["exceptionDetails"]
        raise KujiequAdbError(
            f"页面 JS 执行异常: {exc.get('text') or exc.get('exception', {}).get('description')}"
        )
    value = (result_obj.get("result", {}) or {}).get("value")
    if not value:
        raise KujiequAdbError(f"CDP 返回中没有 value 字段: {payload}")

    try:
        data = json.loads(value) if isinstance(value, str) else value
    except json.JSONDecodeError as exc:
        raise KujiequAdbError(f"页面返回非 JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise KujiequAdbError("页面返回结构异常")

    if data.get("error"):
        raise KujiequAdbError(f"页面 JS 抛错: {data['error']}")

    ui = data.get("user_info") or {}
    ri = data.get("role_info") or {}
    token = str(ui.get("token") or "").strip()
    did = str(ui.get("did") or "").strip()
    user_id = str(ui.get("userId") or "").strip()
    role_id = str(ri.get("roleId") or "").strip()
    role_name = str(ri.get("roleName") or "").strip()

    if not token or not did:
        raise KujiequAdbError(
            "localStorage 中未找到 token / did，请确认库街区 APP 已登录账号"
        )

    return KujiequAdbCapture(
        token=token,
        did=did,
        user_id=user_id,
        role_id=role_id,
        role_name=role_name,
        role_info=ri if isinstance(ri, dict) else {},
        user_info=ui if isinstance(ui, dict) else {},
    )


def capture_kujiequ_token(
    adb_path: Optional[str] = None,
    serial: Optional[str] = None,
    port: int = CDP_FORWARD_PORT,
    progress_cb=None,
) -> KujiequAdbCapture:
    """从库街区 APP 通过 ADB + CDP 抓取 token / did。

    Args:
        adb_path: adb.exe 路径；为 None 时自动查找
        serial: 设备序列号；为 None 时自动选第一台
        port: CDP forward 端口，默认 9222
        progress_cb: 可选回调 (msg: str) -> None，用于 UI 进度提示

    Returns:
        KujiequAdbCapture: 含 token / did / userId / roleId / roleName
    """

    def _notify(msg: str) -> None:
        LOGGER.info("[库街区ADB] %s", msg)
        if progress_cb:
            try:
                progress_cb(msg)
            except Exception:
                pass

    if adb_path is None:
        adb_path = find_adb()
    _notify(f"adb 路径: {adb_path}")

    if serial is None:
        serials = list_devices(adb_path)
        if not serials:
            raise KujiequAdbError(
                "未检测到 ADB 设备，请确认手机已通过 USB 数据线连接、"
                "USB 调试已开启并已授权此电脑"
            )
        serial = serials[0]
        if len(serials) > 1:
            _notify(f"检测到多台设备，使用第一台: {serial}")
    _notify(f"使用设备: {serial}")

    # 启动 adb server（如果未启动）
    try:
        _run_adb(adb_path, ["start-server"])
    except KujiequAdbError:
        pass  # server 已运行时会快速返回

    pid, socket_name = find_webview_socket(adb_path, serial)
    _notify(f"WebView socket: {socket_name} (PID={pid})")

    forward_cdp(adb_path, serial, socket_name, port=port)
    _notify(f"已 forward 到本地端口 {port}")

    pages = list_cdp_pages(port=port)
    page = find_growth_calculator_page(pages)
    ws_url = page["webSocketDebuggerUrl"]
    _notify("找到养成计算器页面，正在读取 localStorage")

    response = cdp_runtime_eval(ws_url, _LOCAL_STORAGE_EXPR)
    capture = _parse_capture(response)
    _notify(
        f"抓取成功: userId={capture.user_id} roleId={capture.role_id} "
        f"roleName={capture.role_name}"
    )
    return capture


def apply_capture_to_store(capture: KujiequAdbCapture) -> Dict[str, Any]:
    """把抓取结果落盘：token/did 加密写入数据库，角色信息写回 app_config.json。

    token / did 属于密钥，走 services.credential_store（DPAPI 密文存
    workflow_db 的 credentials 表）；配置文件只保留 roles / user_id /
    role_id / role_name 等非密钥字段，避免密钥散落在配置文件里、
    再被凭据推送回灌到 QML 输入框。

    Args:
        capture: 抓取到的凭据

    Returns:
        更新后的 wuwa_kujiequ 子配置 dict（不含 token/did）
    """
    from ..services import credential_store

    # 1) 密钥加密入库
    credential_store.set_secret(
        credential_store.WUWA_CATEGORY, "token", capture.token,
    )
    credential_store.set_secret(
        credential_store.WUWA_CATEGORY, "did", capture.did,
    )

    # 2) 非密钥信息写回配置
    from ..settings import manager as cfg_manager

    cfg = cfg_manager.load(force=True)
    wuwa_cfg = dict(cfg.get("wuwa_kujiequ") or {})

    # profile
    profile = dict(wuwa_cfg.get("profile") or {})
    profile.update({
        "is_web": False,
        "user_id": capture.user_id,
        "role_id": capture.role_id,
        "role_name": capture.role_name,
    })
    # 旧版本曾把密钥写在 config 里，这里显式清掉（新值已入库）
    profile.pop("token", None)
    profile.pop("did", None)
    wuwa_cfg["profile"] = profile

    # roles 列表（同步更新或新增第一条）
    roles = list(wuwa_cfg.get("roles") or [])
    if capture.user_id and capture.role_id:
        new_role = {
            "user_id": capture.user_id,
            "role_id": capture.role_id,
            "role_name": capture.role_name,
        }
        # 替换 user_id 相同的项，否则前插
        replaced = False
        for i, r in enumerate(roles):
            if r.get("user_id") == capture.user_id:
                roles[i] = new_role
                replaced = True
                break
        if not replaced:
            roles.insert(0, new_role)
    wuwa_cfg["roles"] = roles

    cfg["wuwa_kujiequ"] = wuwa_cfg
    cfg_manager.save(cfg)
    LOGGER.info(
        "[库街区ADB] token/did 已加密入库，角色信息写回配置 (userId=%s roleId=%s)",
        capture.user_id, capture.role_id,
    )
    return wuwa_cfg


def grab_and_persist(
    adb_path: Optional[str] = None,
    serial: Optional[str] = None,
    port: int = CDP_FORWARD_PORT,
    progress_cb=None,
) -> Tuple[KujiequAdbCapture, "WuwaKujiequProfile"]:
    """一步式：抓取 + 加密入库 + 返回可直接用于签到的 profile。

    Returns:
        (capture, profile) — capture 是原始抓取结果，profile 是构造好的
        clients.wuwa_kujiequ.WuwaKujiequProfile 对象，可直接传给 perform_sign / fetch_*
    """
    capture = capture_kujiequ_token(
        adb_path=adb_path, serial=serial, port=port, progress_cb=progress_cb,
    )
    apply_capture_to_store(capture)
    profile = wuwa_kujiequ.profile_from_dict({
        "token": capture.token,
        "did": capture.did,
        "is_web": False,
        "user_id": capture.user_id,
        "role_id": capture.role_id,
        "role_name": capture.role_name,
    })
    if profile is None:
        raise KujiequAdbError("构造 WuwaKujiequProfile 失败")
    return capture, profile
