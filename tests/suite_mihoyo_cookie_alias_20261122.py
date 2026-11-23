# -*- coding: utf-8 -*-
r"""米游社 v2 凭证旧键名镜像回归（2026-11-22）。

背景：11-22 启动前签到三家全部报「登录状态失效，请重新登录」，看起来像掉登录、
实际凭证完全有效。现场取证（本机直连米游社）：

  存储 cookie 字段 = account_id / cookie_token_v2 / ltuid / mid / stoken /
                     stoken_v2 / stuid  ← 没有 cookie_token
  ① 原样发 getGameRolesByCookie            -> retcode=-100「登录状态失效」
  ② 同一枚 token 改名 cookie_token 再发    -> retcode=0 OK，三个角色全读到

即绑定角色 / luna 签到接口只认旧键名 cookie_token；App 扫码等登录流程只落了
cookie_token_v2。另一条兜底路也断了：getCookieAccountInfoBySToken 已停用，
返回 retcode=-464「请使用最新版本产品/链接…」，_ensure_fresh_cookie_token 刷不回来。

修复：mirror_v2_token_aliases() 在拼请求头时把 *_v2 凭证按旧键名补一份（只补缺失
键、绝不覆盖），不需要用户重扫二维码。

跑法：.venv\Scripts\python.exe tests\suite_mihoyo_cookie_alias_20261122.py
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import game_auto.clients.mihoyo_sign as ms  # noqa: E402

FAILURES = []

V2_ONLY = (
    "account_id=335835185; stuid=335835185; ltuid=335835185; mid=09uuoiABCDE12; "
    "stoken=v2_stokenABC; stoken_v2=v2_stokenABC; cookie_token_v2=v2_cookieTokenXYZ"
)


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


def test_mirror_adds_legacy_key():
    print("[1] 只有 cookie_token_v2 时补出 cookie_token")
    fields = ms.extract_cookie_fields(ms.mirror_v2_token_aliases(V2_ONLY))
    check("补出 cookie_token", fields.get("cookie_token") == "v2_cookieTokenXYZ", fields.get("cookie_token"))
    check("cookie_token_v2 保留", fields.get("cookie_token_v2") == "v2_cookieTokenXYZ")
    check("stoken_v2 保留", fields.get("stoken_v2") == "v2_stokenABC")
    check("stoken 保留", fields.get("stoken") == "v2_stokenABC")
    check("mid 保留", fields.get("mid") == "09uuoiABCDE12")
    check("account_id 保留", fields.get("account_id") == "335835185")


def test_mirror_never_overwrites():
    print("[2] 已有旧键时不覆盖")
    cookie = V2_ONLY + "; cookie_token=legacyToken"
    fields = ms.extract_cookie_fields(ms.mirror_v2_token_aliases(cookie))
    check("cookie_token 保持原值", fields.get("cookie_token") == "legacyToken", fields.get("cookie_token"))


def test_mirror_idempotent_and_edge():
    print("[3] 幂等与边界")
    once = ms.mirror_v2_token_aliases(V2_ONLY)
    twice = ms.mirror_v2_token_aliases(once)
    check("两次镜像结果一致", once == twice)
    check("空 cookie 返回空串", ms.mirror_v2_token_aliases("") == "")
    check("无 v2 字段时原样返回", ms.mirror_v2_token_aliases("a=1; b=2") == "a=1; b=2")


def test_headers_carry_mirrored_cookie():
    print("[4] 请求头里带的 cookie 已含旧键名（真实失败点）")
    headers = ms._build_game_headers(V2_ONLY, referer="https://www.miyoushe.com/ys/")
    cookie_header = headers.get("Cookie") or ""
    check("Cookie 头含 cookie_token=", "cookie_token=v2_cookieTokenXYZ" in cookie_header, cookie_header[-80:])
    check("不重复发 cookie_token_v2", cookie_header.count("cookie_token_v2=") == 1)
    check("仍带 x-rpc-device_id", bool(headers.get("x-rpc-device_id")))


def test_validate_unchanged():
    print("[5] 既有校验口径不受影响")
    ok_v2 = ms.validate_cookie_text(V2_ONLY)
    check("仅 v2 仍判 ok", ok_v2["ok"] is True, ok_v2["missing"])
    bad = ms.validate_cookie_text("account_id=1; foo=bar")
    check("缺 token 仍判不 ok", bad["ok"] is False, bad["missing"])


def main():
    test_mirror_adds_legacy_key()
    test_mirror_never_overwrites()
    test_mirror_idempotent_and_edge()
    test_headers_carry_mirrored_cookie()
    test_validate_unchanged()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
