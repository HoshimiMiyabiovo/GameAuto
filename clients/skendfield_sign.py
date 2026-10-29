"""森空岛终末地签到客户端。

参考 https://github.com/sjtt2/endfield_auto_sign 项目的协议实现，
适配到本项目的配置体系与 Flet GUI 中。

流程：
1. token → grant code（Hypergryph/Gryphline OAuth）
2. grant code → cred + sign_token
3. cred + sign_token → 获取终末地绑定角色
4. 角色信息 + HMAC-SHA256/MD5 签名 → 执行签到

签名算法：
  t = int(time.time())
  sign_header = {"platform":"3","timestamp":t,"dId":"","vName":"1.0.0"}
  sign_str = path + body + t + json.dumps(sign_header)
  hmac_hex = hmac.new(sign_token, sign_str, sha256).hexdigest()
  sign = md5(hmac_hex).hexdigest()
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import random
import time
from typing import Any, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

LOGGER = logging.getLogger("GameAutomation")

SKENDFIELD_SIGN_CONFIG_KEY = "skendfield_sign"

PLATFORM = "3"
VNAME = "1.0.0"

UA = "Skland/1.0.0 (com.skland.grass; Android; SDK_INT 34; Build/UP1A.231005.004)"

SERVER_CONFIG = {
    "cn": {
        "name": "国服",
        "app_code": "4ca99fa6b56cc2ba",
        "grant_url": "https://as.hypergryph.com/user/oauth2/v2/grant",
        "cred_url": "https://zonai.skland.com/api/v1/user/auth/generate_cred_by_code",
        "bind_url": "https://zonai.skland.com/api/v1/game/player/binding",
        "sign_url": "https://zonai.skland.com/api/v1/game/endfield/attendance",
    },
    "global": {
        "name": "国际服",
        "app_code": "6eb76d4e13aa36e6",
        "grant_url": "https://as.gryphline.com/user/oauth2/v2/grant",
        "cred_url": "https://zonai.skport.com/web/v1/user/auth/generate_cred_by_code",
        "bind_url": "https://zonai.skport.com/api/v1/game/player/binding",
        "sign_url": "https://zonai.skport.com/web/v1/game/endfield/attendance",
    },
}


class SkEndfieldSignError(RuntimeError):
    """森空岛签到错误。"""


def _parse_token(raw: str) -> str:
    """解析 token，自动处理 {'data':{'content':'...'}} 格式。"""
    raw = raw.strip()
    if not raw:
        return ""
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            content = obj.get("data", {}).get("content", "")
            if content:
                return str(content)
    except (json.JSONDecodeError, TypeError, AttributeError):
        # 返回体不是预期 JSON 结构，按原始文本返回
        pass
    return raw


def _generate_sign(sign_token: str, path: str, body: str = "") -> tuple:
    """生成接口签名，返回 (sign, sign_header_dict)。"""
    t = str(int(time.time()))
    sign_header = {"platform": PLATFORM, "timestamp": t, "dId": "", "vName": VNAME}
    sign_header_str = json.dumps(sign_header, separators=(",", ":"))
    sign_str = path + body + t + sign_header_str
    hmac_hex = hmac.new(
        sign_token.encode("utf-8"),
        sign_str.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    md5_sign = hashlib.md5(hmac_hex.encode("utf-8")).hexdigest()
    return md5_sign, sign_header


def _request(method: str, url: str, body: Optional[bytes] = None, headers: Optional[Dict[str, str]] = None, timeout: int = 15) -> dict:
    req = Request(url, data=body, method=method)
    base_headers = {
        "User-Agent": UA,
        "Accept": "*/*",
        # 注：不要发送 Accept-Encoding: gzip，urlopen 不会自动解压，
        # 会拿到 gzip 压缩的二进制（以 0x1f 0x8b 开头）导致 json.loads 报
        # "'utf-8' codec can't decode byte 0x8b"。让服务端发原始字节即可。
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Connection": "keep-alive",
    }
    base_headers.update(headers or {})
    for k, v in base_headers.items():
        req.add_header(k, v)
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return json.loads(raw)
    except HTTPError as exc:
        body_bytes = exc.read()
        try:
            return json.loads(body_bytes)
        except Exception:
            raise SkEndfieldSignError(f"HTTP {exc.code}: {body_bytes.decode(errors='replace')[:500]}") from exc
    except URLError as exc:
        raise SkEndfieldSignError(f"网络错误: {exc}") from exc


class SkEndfieldClient:
    """森空岛终末地签到客户端。"""

    def __init__(self, token: str, server: str = "cn"):
        cfg = SERVER_CONFIG.get(server)
        if cfg is None:
            raise SkEndfieldSignError(f"无效的服务器: {server}（支持 cn / global）")
        self.server = server
        self.cfg = cfg
        self.token = _parse_token(token)
        self._sign_token = ""
        self._cred = ""

    def _get_grant_code(self) -> str:
        LOGGER.info("[森空岛] 正在通过 token 换取 grant code...")
        body = json.dumps({
            "appCode": self.cfg["app_code"],
            "token": self.token,
            "type": 0,
        }).encode("utf-8")
        resp = _request("POST", self.cfg["grant_url"], body=body,
                        headers={"Content-Type": "application/json"})
        if resp.get("status") != 0:
            raise SkEndfieldSignError(f"获取 grant code 失败: {resp.get('msg', resp.get('message', resp))}")
        code = resp.get("data", {}).get("code", "")
        if not code:
            raise SkEndfieldSignError(f"grant code 为空: {resp}")
        LOGGER.info("[森空岛] grant code 获取成功")
        return code

    def _get_cred(self, grant_code: str) -> str:
        LOGGER.info("[森空岛] 正在通过 grant code 换取 cred...")
        body = json.dumps({"code": grant_code, "kind": 1}).encode("utf-8")
        resp = _request("POST", self.cfg["cred_url"], body=body,
                        headers={"Content-Type": "application/json"})
        if resp.get("code") != 0:
            raise SkEndfieldSignError(f"获取 cred 失败: {resp.get('message', resp)}")
        data = resp.get("data", {})
        self._sign_token = data.get("token", "")
        if not self._sign_token:
            raise SkEndfieldSignError(f"sign_token 为空: {resp}")
        LOGGER.info("[森空岛] cred 获取成功")
        return data.get("cred", "")

    def login(self) -> dict:
        """执行森空岛登录，返回角色绑定信息。"""
        grant_code = self._get_grant_code()
        time.sleep(random.uniform(0.5, 1.5))
        self._cred = self._get_cred(grant_code)
        return self._get_endfield_roles()

    def _get_endfield_roles(self) -> dict:
        LOGGER.info("[森空岛] 正在获取终末地绑定角色...")
        parsed = urlparse(self.cfg["bind_url"])
        sign, sign_header = _generate_sign(self._sign_token, parsed.path, "")
        headers = {
            "cred": self._cred,
            "platform": sign_header["platform"],
            "vName": sign_header["vName"],
            "timestamp": sign_header["timestamp"],
            "sign": sign,
        }
        resp = _request("GET", self.cfg["bind_url"], headers=headers)
        if resp.get("code") != 0:
            raise SkEndfieldSignError(f"获取角色失败: {resp.get('message', resp)}")
        data = resp.get("data", {})
        binding = None
        for app in data.get("list", []):
            if app.get("appCode") == "endfield" and app.get("bindingList"):
                binding = app["bindingList"][0]
                break
        if not binding:
            raise SkEndfieldSignError("未绑定终末地角色，请在森空岛 App 中先绑定终末地账号")
        LOGGER.info("[森空岛] 角色获取成功: %s", binding.get("defaultRole", {}).get("nickname", ""))
        return binding

    def do_sign(self) -> Dict[str, Any]:
        """执行签到。

        如果服务端因"请勿修改设备本地时间"拒绝，先 sleep 1-2 秒再重试一次。
        这种错误通常是客户端/服务端时间漂移了几秒，再发一次用新 timestamp 就过。
        """
        last_error: Optional[SkEndfieldSignError] = None
        for attempt in range(2):
            try:
                return self._do_sign_once()
            except SkEndfieldSignError as exc:
                last_error = exc
                msg = str(exc)
                if "本地时间" in msg and attempt == 0:
                    LOGGER.warning("[森空岛] 时间戳漂移，1.5 秒后重试: %s", msg)
                    time.sleep(1.5)
                    continue
                raise
        # 理论上不会到这里
        raise last_error or SkEndfieldSignError("未知错误")

    def _do_sign_once(self) -> Dict[str, Any]:
        if not self._cred or not self._sign_token:
            raise SkEndfieldSignError("请先调用 login() 完成登录")

        roles = self._get_endfield_roles()
        default_role = roles.get("defaultRole", {})
        role = default_role or (roles.get("roles") and roles["roles"][0])
        if not role:
            raise SkEndfieldSignError("未找到有效角色信息")
        role_str = f"3_{role['roleId']}_{role['serverId']}"
        role_name = default_role.get("nickname", "未知角色")
        channel = default_role.get("serverName", "未知服务器")

        parsed = urlparse(self.cfg["sign_url"])
        sign, sign_header = _generate_sign(self._sign_token, parsed.path, "")
        headers = {
            "cred": self._cred,
            "platform": sign_header["platform"],
            "vName": sign_header["vName"],
            "timestamp": sign_header["timestamp"],
            "sign": sign,
            "sk-game-role": role_str,
            "Content-Type": "application/json",
        }

        LOGGER.info("[森空岛] 正在签到: %s (%s)", role_name, channel)
        resp = _request("POST", self.cfg["sign_url"], headers=headers)
        code = resp.get("code", -1)
        if code == 0:
            awards = []
            award_ids = resp.get("data", {}).get("awardIds", [])
            resource_map = resp.get("data", {}).get("resourceInfoMap", {})
            if award_ids and resource_map:
                for award in award_ids:
                    aid = award.get("id", "")
                    if aid in resource_map:
                        res = resource_map[aid]
                        awards.append(f"{res['name']}x{res.get('count', 1)}")
            msg = f"签到成功。获得：{'、'.join(awards)}" if awards else "签到成功。"
            LOGGER.info("[森空岛] %s: %s", role_name, msg)
            return {"status": "success", "role": role_name, "channel": channel, "message": msg, "awards": awards, "raw": resp}
        else:
            error_msg = resp.get("message", "未知错误")
            if "重复签到" in error_msg or "sign in again" in error_msg.lower():
                LOGGER.info("[森空岛] %s: 今日已签到", role_name)
                return {"status": "already", "role": role_name, "channel": channel, "message": "今日已签到。"}
            LOGGER.warning("[森空岛] %s 签到失败: %s", role_name, error_msg)
            return {"status": "failed", "role": role_name, "channel": channel, "message": error_msg, "raw": resp}


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------

def perform_sign(token: str, server: str = "cn") -> Dict[str, Any]:
    client = SkEndfieldClient(token=token, server=server)
    try:
        client.login()
    except SkEndfieldSignError as exc:
        LOGGER.error("[森空岛] 登录失败: %s", exc)
        server_name = SERVER_CONFIG.get(server, {}).get("name", server)
        return {"server": server, "server_name": server_name, "status": "login_failed", "message": str(exc)}

    try:
        result = client.do_sign()
        result["server"] = server
        result["server_name"] = SERVER_CONFIG.get(server, {}).get("name", server)
        if result.get("status") == "failed":
            result["status"] = "failed"
        return result
    except SkEndfieldSignError as exc:
        LOGGER.error("[森空岛] 签到失败: %s", exc)
        return {"server": server, "server_name": SERVER_CONFIG.get(server, {}).get("name", server),
                "status": "failed", "message": str(exc)}


def format_sign_report(result: Dict[str, Any]) -> List[str]:
    lines: List[str] = []
    server_name = result.get("server_name", result.get("server", ""))
    status = result.get("status", "")
    if status == "login_failed":
        lines.append(f"森空岛({server_name})登录失败: {result.get('message', '')}")
    elif status == "success":
        role = result.get("role", "")
        channel = result.get("channel", "")
        lines.append(f"森空岛({server_name}) {role}({channel}): {result.get('message', '')}")
    elif status == "already":
        role = result.get("role", "")
        channel = result.get("channel", "")
        lines.append(f"森空岛({server_name}) {role}({channel}): 已签到")
    elif status == "failed":
        role = result.get("role", "")
        lines.append(f"森空岛({server_name}) {role or ''}: 失败 - {result.get('message', '')}")
    return lines