"""Endfield Protocol Terminal API 客户端。

封装 https://end-api.shallow.ink 的终末地数据查询与签到接口。
api_key 从 https://end.shallow.ink/ 获取。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

LOGGER = logging.getLogger("GameAutomation")

ENDFIELD_PROTOCOL_CONFIG_KEY = "endfield_protocol"

API_BASE = "https://end-api.shallow.ink"


def _request(
    method: str,
    path: str,
    params: Optional[Dict[str, Any]] = None,
    body: Optional[Dict[str, Any]] = None,
    api_key: Optional[str] = None,
    framework_token: Optional[str] = None,
    timeout: int = 15,
) -> Optional[Dict[str, Any]]:
    url = API_BASE + path
    if params:
        clean = {k: v for k, v in params.items() if v is not None}
        url += "?" + urlencode(clean)

    headers: Dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
    if framework_token:
        headers["X-Framework-Token"] = framework_token

    data = json.dumps(body).encode("utf-8") if body else None
    req = Request(url, data=data, method=method, headers=headers)

    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            result = json.loads(raw)
    except HTTPError as exc:
        LOGGER.error("[EndfieldProtocol] HTTP %s %s → %s", method, url, exc.code)
        return None
    except URLError as exc:
        LOGGER.error("[EndfieldProtocol] 网络错误 %s %s: %s", method, url, exc.reason)
        return None
    except Exception as exc:
        LOGGER.error("[EndfieldProtocol] 请求异常 %s %s: %s", method, url, exc)
        return None

    if isinstance(result, dict) and result.get("code") != 0:
        LOGGER.error("[EndfieldProtocol] API 失败 %s %s: code=%s msg=%s",
                      method, url, result.get("code"), result.get("message", ""))
        return None

    return result


class EndfieldProtocolClient:
    """Endfield Protocol Terminal 客户端。"""

    def __init__(self, api_key: Optional[str] = None, framework_token: Optional[str] = None):
        self.api_key = api_key
        self.framework_token = framework_token

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        return _request("GET", path, params=params,
                        api_key=self.api_key, framework_token=self.framework_token)

    def _post(self, path: str, params: Optional[Dict[str, Any]] = None,
              body: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        return _request("POST", path, params=params, body=body,
                        api_key=self.api_key, framework_token=self.framework_token)

    def attendance(self, role_id: str, server_id: str) -> Optional[Dict[str, Any]]:
        return self._post("/api/endfield/attendance",
                          params={"role_id": role_id, "server_id": server_id})

    def note(self, role_id: str, server_id: str) -> Optional[Dict[str, Any]]:
        return self._get("/api/endfield/note",
                         params={"roleId": role_id, "serverId": server_id})

    def stamina(self, role_id: str, server_id: str) -> Optional[Dict[str, Any]]:
        return self._get("/api/endfield/stamina",
                         params={"roleId": role_id, "serverId": server_id})

    def card_detail(self, role_id: str, server_id: str, user_id: str) -> Optional[Dict[str, Any]]:
        return self._get("/api/endfield/card/detail",
                         params={"roleId": role_id, "serverId": server_id, "userId": user_id})

    def card_char(
        self,
        role_id: str,
        server_id: str,
        inst_id: Optional[str] = None,
        operator_id: Optional[str] = None,
        char_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        if inst_id:
            params = {"instId": inst_id, "roleId": role_id, "serverId": server_id}
        else:
            params = {"operatorId": operator_id, "charId": char_id,
                      "roleId": role_id, "serverId": server_id}
        return self._get("/api/endfield/card/char", params=params)

    def spaceship(self, role_id: str, server_id: str) -> Optional[Dict[str, Any]]:
        return self._get("/api/endfield/spaceship",
                         params={"roleId": role_id, "serverId": server_id})

    def domain(self, role_id: str, server_id: str) -> Optional[Dict[str, Any]]:
        return self._get("/api/endfield/domain",
                         params={"roleId": role_id, "serverId": server_id})

    def achieve(self, role_id: str, server_id: str) -> Optional[Dict[str, Any]]:
        return self._get("/api/endfield/achieve",
                         params={"roleId": role_id, "serverId": server_id})

    def search_chars(self) -> Optional[Dict[str, Any]]:
        return self._get("/api/endfield/search/chars")

    def gacha_accounts(self) -> Optional[Dict[str, Any]]:
        return self._get("/api/endfield/gacha/accounts")

    def gacha_fetch(self, role_id: str) -> Optional[Dict[str, Any]]:
        return self._post("/api/endfield/gacha/fetch", body={"role_id": role_id})

    def gacha_records(
        self, pools: str, page: int = 1, limit: int = 500
    ) -> Optional[Dict[str, Any]]:
        return self._get("/api/endfield/gacha/records",
                         params={"pools": pools, "page": str(page), "limit": str(limit)})

    def gacha_stats(self) -> Optional[Dict[str, Any]]:
        return self._get("/api/endfield/gacha/stats")

    def gacha_global_stats(
        self, pool_period: Optional[str] = None, provider: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        params: Dict[str, Any] = {}
        if pool_period:
            params["pool_period"] = pool_period
        if provider:
            params["provider"] = provider
        return self._get("/api/endfield/gacha/global-stats", params=params or None)

    def all_gacha_records(self, pools: str) -> Optional[List[Dict[str, Any]]]:
        records: List[Dict[str, Any]] = []
        page = 1
        while True:
            result = self.gacha_records(pools=pools, page=page, limit=500)
            if result is None:
                return None
            data = result.get("data", {})
            if isinstance(data, dict):
                items = data.get("records") or data.get("data") or []
            elif isinstance(data, list):
                items = data
            else:
                items = []
            if not items:
                break
            records.extend(items)
            page += 1
        return records

    def bindings(
        self, user_identifier: str, client_type: str, provider: str
    ) -> Optional[Dict[str, Any]]:
        return _request(
            "GET", "/api/v1/bindings",
            params={"user_identifier": user_identifier, "client_type": client_type,
                    "provider": provider},
            api_key=self.api_key,
            framework_token=None,
        )


def perform_query(
    api_key: Optional[str],
    framework_token: Optional[str],
    role_id: Optional[str],
    server_id: Optional[str],
    query_type: str,
    **kwargs: Any,
) -> Optional[Dict[str, Any]]:
    client = EndfieldProtocolClient(api_key=api_key, framework_token=framework_token)

    def _require_role_server() -> bool:
        if not role_id or not server_id:
            LOGGER.warning("[EndfieldProtocol] %s 缺少 role_id 或 server_id", query_type)
            return False
        return True

    if query_type == "attendance":
        if not _require_role_server():
            return None
        return client.attendance(role_id, server_id)  # type: ignore[arg-type]
    elif query_type == "note":
        if not _require_role_server():
            return None
        return client.note(role_id, server_id)  # type: ignore[arg-type]
    elif query_type == "stamina":
        if not _require_role_server():
            return None
        return client.stamina(role_id, server_id)  # type: ignore[arg-type]
    elif query_type == "card_detail":
        user_id = kwargs.get("user_id", "")
        if not role_id or not server_id or not user_id:
            LOGGER.warning("[EndfieldProtocol] %s 缺少 role_id/server_id/user_id", query_type)
            return None
        return client.card_detail(role_id, server_id, user_id)  # type: ignore[arg-type]
    elif query_type == "card_char":
        inst_id = kwargs.get("inst_id")
        operator_id = kwargs.get("operator_id")
        char_id = kwargs.get("char_id")
        if not _require_role_server():
            return None
        return client.card_char(role_id, server_id,  # type: ignore[arg-type]
                                inst_id=inst_id, operator_id=operator_id, char_id=char_id)
    elif query_type == "spaceship":
        if not _require_role_server():
            return None
        return client.spaceship(role_id, server_id)  # type: ignore[arg-type]
    elif query_type == "domain":
        if not _require_role_server():
            return None
        return client.domain(role_id, server_id)  # type: ignore[arg-type]
    elif query_type == "achieve":
        if not _require_role_server():
            return None
        return client.achieve(role_id, server_id)  # type: ignore[arg-type]
    elif query_type == "search_chars":
        return client.search_chars()
    elif query_type == "gacha_accounts":
        return client.gacha_accounts()
    elif query_type == "gacha_fetch":
        if not role_id:
            LOGGER.warning("[EndfieldProtocol] %s 缺少 role_id", query_type)
            return None
        return client.gacha_fetch(role_id)
    elif query_type == "gacha_records":
        pools = kwargs.get("pools", "")
        page = int(kwargs.get("page", 1))
        limit = int(kwargs.get("limit", 500))
        return client.gacha_records(pools=pools, page=page, limit=limit)
    elif query_type == "gacha_stats":
        return client.gacha_stats()
    elif query_type == "gacha_global_stats":
        return client.gacha_global_stats(
            pool_period=kwargs.get("pool_period"), provider=kwargs.get("provider"))
    elif query_type == "all_gacha_records":
        pools = kwargs.get("pools", "")
        return client.all_gacha_records(pools=pools)
    elif query_type == "bindings":
        user_identifier = kwargs.get("user_identifier", "")
        client_type = kwargs.get("client_type", "")
        provider = kwargs.get("provider", "")
        return client.bindings(user_identifier, client_type, provider)

    LOGGER.warning("[EndfieldProtocol] 未知查询类型: %s", query_type)
    return None