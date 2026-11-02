"""终末地抽卡记录抓取客户端（token 直接同步）。

参考 https://github.com/bhaoo/endfield-gacha 的协议实现：
1. 用户提供的长效 token + uid → 调 OAuth grant 接口换 short-lived auth token
2. auth token + uid → 调 u8_token_by_uid 接口换 u8Token
3. u8Token 作为查询参数，分页拉取 `ef-webview.{provider}.com` 的 char / weapon 记录
4. 输出与 gacha/endfield_analysis.py 中的 gachaData.json 同构的嵌套字典

凭证安全：本模块绝不打印 token/u8Token 明文；日志只输出长度或脱敏的 uid。
"""

from __future__ import annotations

import json
import logging
import random
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

LOGGER = logging.getLogger("GameAutomation")

ENDFIELD_GACHA_CONFIG_KEY = "endfield_gacha"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.6422.112 Safari/537.36"
)

# 与 bhaoo/endfield-gacha 的 POOL_TYPES / POOL_NAME_MAP 对齐
CHAR_POOL_TYPES: Tuple[str, ...] = (
    "E_CharacterGachaPoolType_Special",
    "E_CharacterGachaPoolType_Joint",
    "E_CharacterGachaPoolType_Standard",
    "E_CharacterGachaPoolType_Beginner",
)

# OAuth 应用码：国服用 hypergryph + be36d44aa36bfb5b，国际服用 gryphline + 3dacefa138426cfe
PROVIDER_APP_CODE = {
    "hypergryph": "be36d44aa36bfb5b",
    "gryphline": "3dacefa138426cfe",
}

MAX_PAGE_RETRY = 3
DEFAULT_LANG = "zh-cn"


class EndfieldGachaError(RuntimeError):
    """终末地抽卡抓取错误。"""


def _mask_token(token: str) -> str:
    """脱敏 token 用于日志：只保留前 4 后 4。"""
    if not token:
        return "<empty>"
    if len(token) <= 10:
        return "*" * len(token)
    return f"{token[:4]}...{token[-4:]}(len={len(token)})"


def _request(
    method: str,
    url: str,
    body: Optional[bytes] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = 20,
) -> dict:
    req = Request(url, data=body, method=method)
    base_headers = {
        "User-Agent": UA,
        "Accept": "*/*",
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
            raise EndfieldGachaError(
                f"HTTP {exc.code}: {body_bytes.decode(errors='replace')[:500]}"
            ) from exc
    except URLError as exc:
        raise EndfieldGachaError(f"网络错误: {exc}") from exc


def _delay(min_ms: int = 500, max_ms: int = 1000) -> None:
    """分页之间随机延迟，避免触发风控。"""
    time.sleep(random.uniform(min_ms, max_ms) / 1000.0)


class EndfieldGachaClient:
    """终末地抽卡抓取客户端。

    用法：
        client = EndfieldGachaClient(token=..., uid=..., provider="hypergryph", server_id="1")
        data = client.fetch_all()  # 返回 {character: {...}, weapon: {...}}
    """

    def __init__(
        self,
        token: str,
        uid: str,
        provider: str = "hypergryph",
        server_id: str = "1",
        lang: str = DEFAULT_LANG,
    ) -> None:
        if provider not in PROVIDER_APP_CODE:
            raise EndfieldGachaError(
                f"无效 provider: {provider}（支持 hypergryph / gryphline）"
            )
        if not token:
            raise EndfieldGachaError("token 不能为空")
        if not uid:
            raise EndfieldGachaError("uid 不能为空")

        self.token = token.strip()
        self.uid = str(uid).strip()
        self.provider = provider
        self.server_id = str(server_id or "1").strip() or "1"
        self.lang = lang
        self._u8_token: str = ""

    # ── Auth: token → u8Token ─────────────────────────────────────

    def _exchange_auth_token(self) -> str:
        """Step 1: token → auth_token via OAuth grant。"""
        url = f"https://as.{self.provider}.com/user/oauth2/v2/grant"
        body = json.dumps({
            "type": 1,
            "appCode": PROVIDER_APP_CODE[self.provider],
            "token": self.token,
        }).encode("utf-8")
        LOGGER.info(
            "[终末地抽卡] OAuth grant: provider=%s uid=%s token=%s",
            self.provider, self.uid, _mask_token(self.token),
        )
        resp = _request("POST", url, body=body, headers={
            "Content-Type": "application/json",
        })
        # 不同 provider 返回结构略有差异：hypergryph 用 status/msg/data，gryphline 用 code/msg/data
        status = resp.get("status")
        code = resp.get("code")
        if status not in (0, None) or code not in (0, None):
            raise EndfieldGachaError(
                f"OAuth grant 失败: {resp.get('msg') or resp.get('message') or resp}"
            )
        data = resp.get("data") or {}
        auth_token = data.get("token") or ""
        if not auth_token:
            raise EndfieldGachaError(f"OAuth grant 返回无 token: {resp}")
        return auth_token

    def _exchange_u8_token(self, auth_token: str) -> str:
        """Step 2: auth_token + uid → u8Token。"""
        url = (
            f"https://binding-api-account-prod.{self.provider}.com"
            f"/account/binding/v1/u8_token_by_uid"
        )
        body = json.dumps({
            "uid": self.uid,
            "token": auth_token,
        }).encode("utf-8")
        resp = _request("POST", url, body=body, headers={
            "Content-Type": "application/json",
        })
        status = resp.get("status")
        code = resp.get("code")
        if status not in (0, None) or code not in (0, None):
            raise EndfieldGachaError(
                f"u8_token_by_uid 失败: {resp.get('msg') or resp.get('message') or resp}"
            )
        data = resp.get("data") or {}
        u8_token = data.get("token") or ""
        if not u8_token:
            raise EndfieldGachaError(f"u8_token_by_uid 返回无 token: {resp}")
        LOGGER.info(
            "[终末地抽卡] u8Token 获取成功: uid=%s u8=%s",
            self.uid, _mask_token(u8_token),
        )
        return u8_token

    def login(self) -> str:
        """完成 token → u8Token 的两步交换。"""
        auth_token = self._exchange_auth_token()
        _delay(300, 600)
        self._u8_token = self._exchange_u8_token(auth_token)
        return self._u8_token

    # ── Paginated fetch ──────────────────────────────────────────

    def _fetch_page(
        self,
        base_url: str,
        extra_params: Dict[str, str],
        seq_id: str = "",
    ) -> Tuple[List[Dict[str, Any]], bool, Optional[str]]:
        """拉一页数据，返回 (list, has_more, error_reason)。

        失败时返回 ([], False, "错误描述")；调用方负责重试。
        """
        params = {
            "lang": self.lang,
            "token": self._u8_token,
            "server_id": self.server_id,
        }
        params.update(extra_params)
        if seq_id:
            params["seq_id"] = seq_id
        url = f"{base_url}?{urlencode(params)}"

        resp = _request("GET", url, headers={"User-Agent": UA})
        if resp.get("code") != 0 or not isinstance(resp.get("data"), dict):
            return (
                [],
                False,
                f"API code={resp.get('code')} msg={resp.get('msg') or ''}",
            )
        data = resp["data"]
        lst = data.get("list") or []
        has_more = bool(data.get("hasMore"))
        return list(lst), has_more, None

    def _fetch_paginated(
        self,
        base_url: str,
        extra_params: Dict[str, str],
        pool_label: str,
    ) -> List[Dict[str, Any]]:
        """分页拉取某个池的全部记录，自动 seq_id 翻页 + 失败重试。"""
        all_data: List[Dict[str, Any]] = []
        next_seq_id = ""
        page = 0

        while True:
            page += 1
            err: Optional[str] = None
            lst: List[Dict[str, Any]] = []
            has_more = False

            for attempt in range(1, MAX_PAGE_RETRY + 1):
                lst, has_more, err = self._fetch_page(
                    base_url, extra_params, next_seq_id,
                )
                if err is None:
                    break
                LOGGER.warning(
                    "[终末地抽卡] %s 第 %d 页失败 (%d/%d): %s",
                    pool_label, page, attempt, MAX_PAGE_RETRY, err,
                )
                if attempt < MAX_PAGE_RETRY:
                    _delay(500, 900)

            if err is not None:
                # 重试耗尽，记录已获取的部分并返回
                LOGGER.warning(
                    "[终末地抽卡] %s 在第 %d 页重试耗尽，已获取 %d 条",
                    pool_label, page, len(all_data),
                )
                return all_data

            if not lst:
                break

            all_data.extend(lst)

            if not has_more:
                break

            # 取最后一条的 seqId 作为下一页游标
            next_seq_id = str(lst[-1].get("seqId") or "")
            if not next_seq_id:
                break

            _delay(500, 1000)

        LOGGER.info(
            "[终末地抽卡] %s 完成: 共 %d 条 / %d 页",
            pool_label, len(all_data), page,
        )
        return all_data

    # ── Top-level fetchers ───────────────────────────────────────

    def fetch_characters(self) -> Dict[str, List[Dict[str, Any]]]:
        """拉取全部角色池记录，返回 {pool_type: [items]}。"""
        if not self._u8_token:
            self.login()

        result: Dict[str, List[Dict[str, Any]]] = {}
        base_url = f"https://ef-webview.{self.provider}.com/api/record/char"
        for pool_type in CHAR_POOL_TYPES:
            label = f"char:{pool_type}"
            LOGGER.info("[终末地抽卡] 开始拉取 %s", label)
            records = self._fetch_paginated(
                base_url, {"pool_type": pool_type}, label,
            )
            result[pool_type] = records
            _delay(300, 600)
        return result

    def fetch_weapons(self) -> Dict[str, List[Dict[str, Any]]]:
        """拉取全部武器池记录，返回 {pool_id: [items]}。"""
        if not self._u8_token:
            self.login()

        # 1. 先取武器池列表
        list_url = f"https://ef-webview.{self.provider}.com/api/record/weapon/pool"
        params = {
            "lang": self.lang,
            "token": self._u8_token,
            "server_id": self.server_id,
        }
        url = f"{list_url}?{urlencode(params)}"
        resp = _request("GET", url, headers={"User-Agent": UA})
        if resp.get("code") != 0 or not isinstance(resp.get("data"), list):
            raise EndfieldGachaError(
                f"获取武器池列表失败: code={resp.get('code')} msg={resp.get('msg') or ''}"
            )
        pools = resp["data"]
        LOGGER.info("[终末地抽卡] 武器池列表: 共 %d 个", len(pools))

        # 2. 逐池分页拉取
        result: Dict[str, List[Dict[str, Any]]] = {}
        weapon_url = f"https://ef-webview.{self.provider}.com/api/record/weapon"
        for pool in pools:
            pool_id = str(pool.get("poolId") or "")
            pool_name = str(pool.get("poolName") or pool_id)
            if not pool_id:
                continue
            label = f"weapon:{pool_name}"
            LOGGER.info("[终末地抽卡] 开始拉取 %s", label)
            records = self._fetch_paginated(
                weapon_url, {"pool_id": pool_id}, label,
            )
            result[pool_id] = records
            _delay(300, 600)
        return result

    def fetch_all(self) -> Dict[str, Any]:
        """拉取角色 + 武器全部记录。

        返回与 gacha/endfield_analysis.py 中 gachaData.json 同构的字典：
            {"character": {pool_type: [...]}, "weapon": {pool_id: [...]}}
        """
        character = self.fetch_characters()
        _delay(300, 600)
        weapon = self.fetch_weapons()
        return {"character": character, "weapon": weapon}


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------

def fetch_gacha_records(
    token: str,
    uid: str,
    provider: str = "hypergryph",
    server_id: str = "1",
) -> Dict[str, Any]:
    """拉取终末地抽卡记录。

    返回 {character: {pool_type: [...]}, weapon: {pool_id: [...]}}，
    可直接传给 gacha.endfield_analysis 的 _normalize_*_records 函数。

    token 不会出现在日志中（只输出脱敏长度）。
    """
    client = EndfieldGachaClient(
        token=token, uid=uid, provider=provider, server_id=server_id,
    )
    return client.fetch_all()


def summarize_records(api_data: Dict[str, Any]) -> Dict[str, int]:
    """统计 API 返回的记录数，用于日志/通知（不包含敏感字段）。"""
    char_total = sum(
        len(lst) for lst in api_data.get("character", {}).values()
        if isinstance(lst, list)
    )
    weapon_total = sum(
        len(lst) for lst in api_data.get("weapon", {}).values()
        if isinstance(lst, list)
    )
    return {
        "character_records": char_total,
        "weapon_records": weapon_total,
        "total_records": char_total + weapon_total,
    }
