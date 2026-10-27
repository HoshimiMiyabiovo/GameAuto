"""塔吉多（异环社区）签到客户端。

复刻 https://github.com/SkyBlue997/tjd-daily 项目的核心签到逻辑，
适配到本项目的配置体系与 Flet GUI 中。

支持：
- BBS 每日签到（+40 金币）
- 游戏签到（异环等绑定游戏）
- 浏览帖子、点赞、分享（可选）
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import string
import time
from typing import Any, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

LOGGER = logging.getLogger("GameAutomation")

TAJIDUO_SIGN_CONFIG_KEY = "tajiduo_sign"

# ---------------------------------------------------------------------------
# 协议常量 (与 tjd-daily 保持一致)
# ---------------------------------------------------------------------------
API = "https://bbs-api.tajiduo.com"
APP_VER = "1.2.2"
SECRET = "pUds3dfMkl"
UA = "HTAssistant/106 CFNetwork/3860.200.71 Darwin/25.1.0"
H5_ORIGIN = "https://webstatic.tajiduo.com"
H5_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"
)

COMMUNITY_ID = "2"  # 异环社区
SHARE_PLATFORM = "qq"

# taskKey 常量
TK_SIGNIN = "signin_c"
TK_BROWSE = "browse_post_c"
TK_LIKE = "like_post_c"
TK_SHARE = "share"

ALPH = string.ascii_letters + string.digits


class TajiduoSignError(RuntimeError):
    """塔吉多签到错误。"""


# ---------------------------------------------------------------------------
# 配置模型
# ---------------------------------------------------------------------------

def _mask_phone(p: str) -> str:
    if p and len(p) == 11:
        return f"{p[:3]}****{p[-4:]}"
    return p or "?"


def _make_ds() -> str:
    ts = str(int(time.time()))
    nonce = "".join(random.choice(ALPH) for _ in range(8))
    sig = hashlib.md5(f"{ts}{nonce}{APP_VER}{SECRET}".encode()).hexdigest()
    return f"{ts},{nonce},{sig}"


def _build_native_headers(token: str, deviceid: str, uid: str = "0") -> Dict[str, str]:
    return {
        "Accept": "application/json, text/plain, */*",
        "User-Agent": UA,
        "Authorization": token,
        "appversion": APP_VER,
        "platform": "ios",
        "uid": uid,
        "deviceid": deviceid,
        "ds": _make_ds(),
    }


def _build_h5_headers(token: str) -> Dict[str, str]:
    return {
        "Accept": "application/json, text/plain, */*",
        "User-Agent": H5_UA,
        "Authorization": token,
        "Origin": H5_ORIGIN,
        "Referer": H5_ORIGIN + "/",
    }


def _send_request(
    url: str,
    method: str,
    headers: Dict[str, str],
    data: Optional[bytes] = None,
    timeout: int = 15,
) -> tuple:
    req = Request(url, data=data, method=method, headers=headers)
    try:
        with urlopen(req, timeout=timeout) as r:
            txt = r.read().decode("utf-8", "replace")
            code = r.status
    except HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    try:
        return code, json.loads(txt)
    except json.JSONDecodeError:
        return code, txt


def _is_token_expired(http_code: int, body: Any) -> bool:
    if http_code in (401, 402):
        return True
    if isinstance(body, dict):
        c = body.get("code")
        if c in (401, 402, 4011):
            return True
    return False


def _is_ok(j: Any) -> bool:
    return isinstance(j, dict) and j.get("ok") and j.get("code") == 0


# ---------------------------------------------------------------------------
# 核心客户端类
# ---------------------------------------------------------------------------

class TajiduoClient:
    """塔吉多 API 客户端。"""

    def __init__(
        self,
        token: str,
        deviceid: str,
        uid: str = "0",
        phone: str = "",
        password: str = "",
    ):
        self.token = str(token or "").strip()
        self.deviceid = str(deviceid or "").strip()
        self.uid = str(uid or "0").strip()
        self.phone = str(phone or "").strip()
        self.password = str(password or "").strip()
        self._relogin_attempted = False

    def _native_request(
        self,
        method: str,
        path: str,
        body: Optional[Dict[str, str]] = None,
        query: Optional[Dict[str, str]] = None,
        timeout: int = 15,
        _retry: bool = True,
    ) -> tuple:
        url = API + path
        if query:
            url += "?" + urlencode(query)
        data = None
        if body is not None:
            data = urlencode(body).encode()
        headers = _build_native_headers(self.token, self.deviceid, self.uid)
        if data is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        code, j = _send_request(url, method, headers, data, timeout)
        if _retry and _is_token_expired(code, j) and self._try_relogin():
            headers = _build_native_headers(self.token, self.deviceid, self.uid)
            if data is not None:
                headers["Content-Type"] = "application/x-www-form-urlencoded"
            return _send_request(url, method, headers, data, timeout)
        return code, j

    def _h5_request(
        self,
        method: str,
        path: str,
        body: Optional[Dict[str, str]] = None,
        query: Optional[Dict[str, str]] = None,
        timeout: int = 15,
        _retry: bool = True,
    ) -> tuple:
        url = API + path
        if query:
            url += "?" + urlencode(query)
        data = None
        if body is not None:
            data = urlencode(body).encode()
        headers = _build_h5_headers(self.token)
        if data is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        code, j = _send_request(url, method, headers, data, timeout)
        if _retry and _is_token_expired(code, j) and self._try_relogin():
            headers = _build_h5_headers(self.token)
            if data is not None:
                headers["Content-Type"] = "application/x-www-form-urlencoded"
            return _send_request(url, method, headers, data, timeout)
        return code, j

    def _try_relogin(self) -> bool:
        if self._relogin_attempted:
            return False
        if not self.phone or not self.password:
            LOGGER.warning("[塔吉多] token 已失效，但 config 中未配置 phone/password，无法自动重登")
            return False
        self._relogin_attempted = True
        LOGGER.info("[塔吉多] token 已失效，尝试用账密重登 %s", _mask_phone(self.phone))
        try:
            from .tajiduo_login import relogin_with_password
            new_token = relogin_with_password(
                self.phone,
                self.password,
                self.deviceid,
            )
            if new_token:
                self.token = new_token
                LOGGER.info("[塔吉多] 账密重登成功，已获取新 token")
                return True
        except Exception as exc:
            LOGGER.error("[塔吉多] 账密重登失败：%s", exc)
        return False

    # ------------------------------------------------------------------
    # 任务查询
    # ------------------------------------------------------------------

    def refresh_tasks(self) -> Dict[str, Any]:
        _, j = self._native_request("GET", "/apihub/api/getUserTasks", query={"gid": "1"})
        tasks: Dict[str, Any] = {}
        if _is_ok(j):
            for t in j.get("data", {}).get("task_list1", []):
                if isinstance(t, dict) and t.get("taskKey"):
                    tasks[t["taskKey"]] = t
        return tasks

    def remaining(self, task_key: str, tasks: Optional[Dict[str, Any]] = None) -> Optional[int]:
        if tasks is None:
            tasks = self.refresh_tasks()
        t = tasks.get(task_key)
        if not t:
            return None
        return max(0, t.get("limitTimes", 0) - t.get("completeTimes", 0))

    def get_account_uid(self) -> Optional[str]:
        _, j = self._native_request("GET", "/apihub/api/getUserTasks", query={"gid": "1"})
        if _is_ok(j):
            for k in ("task_list1", "task_list2"):
                for t in j.get("data", {}).get(k, []):
                    if t.get("uid"):
                        return str(t["uid"])
        return None

    def fetch_game_cards(self) -> List[Dict[str, Any]]:
        account_uid = self.get_account_uid()
        if not account_uid:
            return []
        _, j = self._native_request("GET", "/apihub/api/getGameRecordCard", query={"uid": account_uid})
        if _is_ok(j):
            return j.get("data") or []
        return []

    def fetch_role_home(self, role_id) -> Dict[str, Any]:
        """游戏名片聚合数据：体力/城市体力/每日活跃度（dayvalue）等。

        数据源是塔吉多 H5 游戏名片页（webstatic.tajiduo.com/bbs/yh-game-record）
        的同款接口，2026-10-24 从其前端 bundle 挖掘并实测可用：
        GET /apihub/awapi/yh/roleHome?roleId=（H5 请求头，凭据同签到）。
        返回 data 字典，失败返回空 dict。
        """
        _, j = self._h5_request("GET", "/apihub/awapi/yh/roleHome",
                                query={"roleId": str(role_id)})
        if _is_ok(j):
            return j.get("data") or {}
        return {}

    # ------------------------------------------------------------------
    # 单个任务
    # ------------------------------------------------------------------

    def do_signin(self) -> Dict[str, Any]:
        tasks = self.refresh_tasks()
        if self.remaining(TK_SIGNIN, tasks) == 0:
            return {"status": "already", "message": "今日已签到"}
        _, j = self._native_request("POST", "/apihub/api/signin", body={"communityId": COMMUNITY_ID})
        if _is_ok(j):
            d = j.get("data", {}) if isinstance(j, dict) else {}
            return {"status": "success", "message": f"签到成功 +{d.get('goldCoin', 0)}金币", "data": d}
        if isinstance(j, dict) and "已签" in (j.get("msg") or ""):
            return {"status": "already", "message": "今日已签到"}
        if isinstance(j, dict):
            return {"status": "failed", "message": str(j.get("msg") or j)}
        return {"status": "failed", "message": str(j)[:200]}

    def fetch_posts(self, count: int = 20) -> List[Dict[str, Any]]:
        _, j = self._native_request(
            "GET",
            "/bbs/api/getRecommendPostList",
            query={"communityId": COMMUNITY_ID, "count": str(count), "page": "1"},
        )
        if _is_ok(j):
            return j.get("data", {}).get("posts", []) or []
        return []

    def do_browse(self) -> Dict[str, Any]:
        tasks = self.refresh_tasks()
        n = self.remaining(TK_BROWSE, tasks)
        if n is None:
            n = 5
        if n == 0:
            return {"status": "already", "message": "浏览任务已完成", "count": 0}
        posts = self.fetch_posts()
        picked = posts[:n]
        success = 0
        for p in picked:
            pid = p.get("postId")
            if not pid:
                continue
            _, j = self._native_request("GET", "/bbs/api/getPostFull", query={"postId": str(pid)})
            if _is_ok(j):
                success += 1
            time.sleep(random.uniform(0.7, 1.5))
        return {"status": "success", "message": f"浏览 {success}/{len(picked)} 篇", "count": success}

    def do_like(self) -> Dict[str, Any]:
        tasks = self.refresh_tasks()
        n = self.remaining(TK_LIKE, tasks)
        if n is None:
            n = 5
        if n == 0:
            return {"status": "already", "message": "点赞任务已完成", "count": 0}
        posts = self.fetch_posts()
        todo = [p for p in posts if not p.get("selfOperation", {}).get("liked")]
        if len(todo) < n:
            n = len(todo)
        liked = 0
        for p in todo[:n]:
            pid = p.get("postId")
            if not pid:
                continue
            _, j = self._native_request("POST", "/bbs/api/post/like", body={"postId": str(pid)})
            if _is_ok(j):
                liked += 1
            time.sleep(random.uniform(0.5, 1.0))
        return {"status": "success", "message": f"点赞 {liked}/{n} 篇", "count": liked}

    def do_share(self) -> Dict[str, Any]:
        tasks = self.refresh_tasks()
        if self.remaining(TK_SHARE, tasks) == 0:
            return {"status": "already", "message": "分享任务已完成"}
        posts = self.fetch_posts(count=5)
        if not posts:
            return {"status": "failed", "message": "无可分享帖子"}
        pid = posts[0].get("postId")
        if not pid:
            return {"status": "failed", "message": "无可分享帖子"}
        _, j = self._native_request(
            "POST",
            "/bbs/api/post/share",
            body={"platform": SHARE_PLATFORM, "postId": str(pid)},
        )
        if _is_ok(j):
            return {"status": "success", "message": "分享成功"}
        if isinstance(j, dict):
            return {"status": "failed", "message": str(j.get("msg") or j)}
        return {"status": "failed", "message": str(j)[:200]}

    def do_game_signin(self) -> List[Dict[str, Any]]:
        cards = self.fetch_game_cards()
        if not cards:
            return []
        results: List[Dict[str, Any]] = []
        for card in cards:
            gid = card.get("gameId")
            gname = card.get("gameName") or f"gid={gid}"
            role = card.get("bindRoleInfo") or {}
            rid = role.get("roleId")
            if not rid:
                results.append({"game": gname, "status": "skipped", "message": "未绑定角色"})
                continue
            _, st = self._h5_request("GET", "/apihub/awapi/signin/state", query={"gameId": str(gid)})
            if _is_ok(st) and st.get("data", {}).get("todaySign"):
                d = st.get("data", {})
                results.append({
                    "game": gname,
                    "status": "already",
                    "message": f"今日已签到 (本月累计 {d.get('days', 0)} 天)",
                })
                continue
            _, r = self._h5_request("POST", "/apihub/awapi/sign", body={"gameId": str(gid), "roleId": str(rid)})
            if _is_ok(r):
                results.append({"game": gname, "status": "success", "message": "签到成功"})
            elif isinstance(r, dict) and r.get("code") == 5052:
                results.append({"game": gname, "status": "already", "message": "已签到"})
            elif isinstance(r, dict):
                results.append({"game": gname, "status": "failed", "message": str(r.get("msg") or r)})
            else:
                results.append({"game": gname, "status": "failed", "message": str(r)[:200]})
        return results

    def get_coin_state(self) -> Dict[str, Any]:
        _, j = self._native_request("GET", "/apihub/api/getUserCoinTaskState")
        if _is_ok(j):
            return j.get("data", {})
        return {}

    def game_signin_states(self) -> List[Dict[str, Any]]:
        """只读查询各游戏的签到状态（不执行签到）。

        返回 [{game, game_id, signed, days, role_name, level, server_name}]。
        角色信息来自 getGameRecordCard，签到状态来自 /apihub/awapi/signin/state。
        """
        states: List[Dict[str, Any]] = []
        for card in (self.fetch_game_cards() or []):
            if not isinstance(card, dict):
                continue
            gid = card.get("gameId")
            gname = card.get("gameName") or f"gid={gid}"
            role = card.get("bindRoleInfo") or {}
            entry: Dict[str, Any] = {
                "game": gname,
                "game_id": gid,
                "signed": False,
                "days": 0,
                "role_name": role.get("roleName", ""),
                "level": role.get("lev", ""),
                "server_name": role.get("serverName", ""),
            }
            if gid is None:
                states.append(entry)
                continue
            try:
                _, st = self._h5_request("GET", "/apihub/awapi/signin/state",
                                         query={"gameId": str(gid)})
                if _is_ok(st):
                    d = st.get("data", {}) or {}
                    entry["signed"] = bool(d.get("todaySign"))
                    entry["days"] = int(d.get("days") or 0)
            except Exception as exc:  # noqa: BLE001 - 单个游戏查不到不影响其他
                LOGGER.debug("[塔吉多] %s 签到状态查询失败: %s", gname, exc)
            states.append(entry)
        return states


# ---------------------------------------------------------------------------
# 对外封装（与 startup_sign_runner / GUI 对接）
# ---------------------------------------------------------------------------

def perform_sign(
    token: str,
    deviceid: str,
    uid: str = "0",
    *,
    do_bbs_signin: bool = True,
    do_game_signin: bool = True,
    do_browse: bool = False,
    do_like: bool = False,
    do_share: bool = False,
    phone: str = "",
    password: str = "",
) -> Dict[str, Any]:
    """执行塔吉多签到任务。

    默认只执行 BBS 签到和游戏签到，不执行浏览/点赞/分享（避免过度调用）。
    """
    client = TajiduoClient(token=token, deviceid=deviceid, uid=uid, phone=phone, password=password)
    results: Dict[str, Any] = {"bbs": None, "game": [], "browse": None, "like": None, "share": None}

    if do_bbs_signin:
        try:
            results["bbs"] = client.do_signin()
        except Exception as exc:
            results["bbs"] = {"status": "failed", "message": str(exc)}
            LOGGER.error("[塔吉多] BBS 签到失败：%s", exc)

    if do_game_signin:
        try:
            results["game"] = client.do_game_signin()
        except Exception as exc:
            results["game"] = [{"game": "全部", "status": "failed", "message": str(exc)}]
            LOGGER.error("[塔吉多] 游戏签到失败：%s", exc)

    if do_browse:
        try:
            results["browse"] = client.do_browse()
        except Exception as exc:
            results["browse"] = {"status": "failed", "message": str(exc)}

    if do_like:
        try:
            results["like"] = client.do_like()
        except Exception as exc:
            results["like"] = {"status": "failed", "message": str(exc)}

    if do_share:
        try:
            results["share"] = client.do_share()
        except Exception as exc:
            results["share"] = {"status": "failed", "message": str(exc)}

    coin_state = client.get_coin_state()
    results["coin"] = coin_state
    return results


def format_sign_report(result: Dict[str, Any]) -> List[str]:
    lines: List[str] = []
    bbs = result.get("bbs")
    if bbs:
        lines.append(f"BBS 签到：{bbs.get('message', '')}")
    game_results = result.get("game") or []
    if game_results:
        for g in game_results:
            lines.append(f"  {g.get('game', '')}：{g.get('message', '')}")
    elif bbs is None:
        lines.append("没有执行签到任务。")
    coin = result.get("coin") or {}
    if coin:
        lines.append(
            f"今日金币：{coin.get('todayGet', 0)}/{coin.get('todayTotal', 0)}  累计：{coin.get('total', 0)}"
        )
    return lines
