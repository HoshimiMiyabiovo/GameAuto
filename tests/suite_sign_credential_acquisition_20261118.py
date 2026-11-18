# -*- coding: utf-8 -*-
"""签到凭证获取链路回归（2026-11-18）。

背景：QML 迁移后 GachaView 的「打开登录页 / 抓取 Cookie / 扫码登录 /
执行签到」四个按钮没有 onClicked；WuwaView 的「登录 / 刷新信息」也是死按钮。
旧 Flet 页面的能力（browser_cookie3 抓 Cookie、App 扫码、手机号+验证码登录）
还在 clients 里，但 QML 没有桥接，用户只能手动粘贴 Cookie/Token。

本套件锁后端接线：
[1] 米游社 Cookie 持久化：cookie/browser/stoken 合并口径（空 stoken 不覆盖旧值）。
[2] 浏览器抓取：调用 extract_cookie_from_browser + COM 初始化 + toast。
[3] 二维码登录：complete_qrcode_login 结果写 cookie/stoken_cookie。
[4] 手动签到：传入的 Cookie 先同步保存，再签勾选的游戏，写每日进度。
[5] 库街区验证码登录：token/did 加密入库，profile 不落明文。
[6] 塔吉多密码登录：deviceid 复用/生成 + token/uid/phone/password 保存。
[7] 塔吉多短信：发送验证码 + 验证码登录（不覆盖已有密码）。
[8] 森空岛：浏览器 ACCOUNT Cookie → 官方 account/info/hg → token，并写配置。

跑法：.venv\\Scripts\\python.exe tests\\suite_sign_credential_acquisition_20261118.py
"""

import io
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from game_auto.ui.bridge_views.gacha import GachaView  # noqa: E402

FAILURES = []


def check(label, ok, detail=None):
    mark = "PASS" if ok else "FAIL"
    print(f"  {mark} {label}" + (f"  ← {detail}" if not ok and detail is not None else ""))
    if not ok:
        FAILURES.append(label)


class DummyBridge(GachaView):
    """只保留 bridge 侧效果，不启动 Qt/网络。"""

    def __init__(self):
        self.toasts = []
        self.reloads = 0
        self.refreshed = 0

    def emit_event(self, kind, **payload):
        self.toasts.append((kind, payload))

    def _load_credentials_backend(self):
        self.reloads += 1

    def refreshWuwaDaily(self):
        self.refreshed += 1


class FakeTracker:
    def __init__(self):
        self.records = []

    def record_sign_result(self, game_key, game_name, source, status, message, uid=""):
        self.records.append((game_key, status, message, uid))


def _config_patches(cfg):
    saved = {}

    def _save(new_cfg):
        saved["cfg"] = new_cfg

    return (
        mock.patch("game_auto.settings.manager.load", return_value=cfg),
        mock.patch("game_auto.settings.manager.save", side_effect=_save),
        saved,
    )


def test_persist_mihoyo_cookie():
    print("[1] 米游社 Cookie 持久化")
    bridge = DummyBridge()
    cfg = {"mihoyo_sign": {"stoken_cookie": "stoken=old", "browser": "edge"}}
    p_load, p_save, saved = _config_patches(cfg)
    with p_load, p_save:
        bridge._persist_mihoyo_login(
            "cookie_token=abc; account_id=123", browser="chrome",
        )
    section = saved["cfg"]["mihoyo_sign"]
    check("cookie 已写入", section["cookie"] == "cookie_token=abc; account_id=123", section)
    check("browser 已记录", section["browser"] == "chrome", section)
    check("空 stoken 不覆盖旧 stoken", section["stoken_cookie"] == "stoken=old", section)
    check("保存后刷新凭据", bridge.reloads == 1, bridge.reloads)

    cfg2 = {"mihoyo_sign": {}}
    p_load, p_save, saved = _config_patches(cfg2)
    with p_load, p_save:
        bridge._persist_mihoyo_login(
            "stoken=new; account_id=123", browser="edge", stoken_cookie="stoken=new",
        )
    check("扫码 stoken 可写入", saved["cfg"]["mihoyo_sign"]["stoken_cookie"] == "stoken=new")


def test_fetch_browser_cookie():
    print("[2] 浏览器抓取 Cookie")
    bridge = DummyBridge()
    fake_pythoncom = SimpleNamespace(
        CoInitialize=lambda: None, CoUninitialize=lambda: None,
    )
    result = {
        "cookie": "cookie_token=abc; account_id=456",
        "record_ready": True,
    }
    with mock.patch.dict(sys.modules, {"pythoncom": fake_pythoncom}), \
         mock.patch(
             "game_auto.clients.mihoyo_sign.extract_cookie_from_browser",
             return_value=result,
         ) as extract, \
         mock.patch.object(bridge, "_persist_mihoyo_login") as persist:
        bridge._fetch_mihoyo_cookie_backend("edge")
    extract.assert_called_once_with("edge")
    persist.assert_called_once()
    check("按浏览器读取并保存", persist.call_count == 1)
    check("toast 报告账号与便笺就绪", bridge.toasts and "456" in bridge.toasts[-1][1]["message"],
          bridge.toasts)


def test_qr_login():
    print("[3] 米游社扫码登录")
    bridge = DummyBridge()
    result = {
        "cookie": "account_id=789; stoken=xyz",
        "stoken_cookie": "account_id=789; stoken=xyz",
        "account_id": "789",
    }
    with mock.patch(
             "game_auto.clients.mihoyo_sign.complete_qrcode_login",
             return_value=result,
         ), \
         mock.patch.object(bridge, "_persist_mihoyo_login") as persist:
        bridge._run_mihoyo_qr_login_backend("edge")
    persist.assert_called_once_with(
        "account_id=789; stoken=xyz",
        browser="edge",
        stoken_cookie="account_id=789; stoken=xyz",
    )
    check("扫码结果写入 cookie + stoken_cookie", persist.call_count == 1)
    check("toast 提示扫码成功", bridge.toasts and "扫码登录成功" in bridge.toasts[-1][1]["message"],
          bridge.toasts)


def test_manual_sign():
    print("[4] 米游社手动签到")
    bridge = DummyBridge()
    cfg = {"mihoyo_sign": {
        "cookie": "cookie_token=old; account_id=1",
        "enabled_games": {"genshin": True, "honkai_sr": False, "zzz": True},
    }}
    sign_result = {"games": [
        {"game_key": "genshin", "success_count": 1, "already_count": 0, "failed_count": 0,
         "accounts": [{"uid": "100", "status": "success", "message": "签到成功"}]},
        {"game_key": "zzz", "success_count": 0, "already_count": 1, "failed_count": 0,
         "accounts": [{"uid": "200", "status": "already", "message": "已签到"}]},
    ]}
    tracker = FakeTracker()
    p_load, p_save, _ = _config_patches(cfg)
    with p_load, p_save, \
         mock.patch.object(bridge, "_persist_mihoyo_login") as persist, \
         mock.patch("game_auto.clients.mihoyo_sign.sign_games", return_value=sign_result) as sign_games, \
         mock.patch("game_auto.services.daily_progress_tracker.get_tracker", return_value=tracker):
        bridge._run_mihoyo_sign_backend("cookie_token=new; account_id=1")
    persist.assert_called_once_with("cookie_token=new; account_id=1")
    sign_games.assert_called_once()
    called_cookie, called_games = sign_games.call_args.args
    check("先保存手工 Cookie 再签到", persist.call_count == 1)
    check("只签勾选的游戏", list(called_games) == ["genshin", "zzz"], called_games)
    check("签到结果写每日进度", [r[0] for r in tracker.records] == ["genshin", "zzz"], tracker.records)
    check("toast 汇总成功/已签",
          bridge.toasts and "成功 1" in bridge.toasts[-1][1]["message"]
          and "已签 1" in bridge.toasts[-1][1]["message"],
          bridge.toasts)


def test_kujiequ_code_login():
    print("[5] 库街区手机号+验证码登录")
    from game_auto.clients.wuwa_kujiequ import WuwaKujiequProfile

    bridge = DummyBridge()
    profile = WuwaKujiequProfile(
        token="token-secret", did="did-secret", is_web=True,
        user_id="u-1", role_id="role-1", role_name="漂泊者",
    )
    roles = [{"user_id": "u-1", "role_id": "role-1", "role_name": "漂泊者"}]
    cfg = {"wuwa_kujiequ": {"auto_sign": True}}
    p_load, p_save, saved = _config_patches(cfg)
    with p_load, p_save, \
         mock.patch("game_auto.clients.wuwa_kujiequ.login_with_code", return_value=(profile, roles)), \
         mock.patch("game_auto.services.credential_store.set_secret") as set_secret:
        bridge._run_wuwa_kujiequ_code_login_backend("13800000000", "code-1", True)
    secrets = {call.args[1]: call.args[2] for call in set_secret.call_args_list}
    check("token/did 均加密入库",
          secrets.get("token") == "token-secret" and secrets.get("did") == "did-secret",
          secrets)
    section = saved["cfg"]["wuwa_kujiequ"]
    check("profile 不落 token/did 明文",
          "token" not in section["profile"] and "did" not in section["profile"],
          section["profile"])
    check("phone/source/roles 已保存",
          section["phone"] == "13800000000" and section["source"] == "h5"
          and section["roles"] == roles,
          section)
    check("登录后刷新凭据与日报", bridge.reloads == 1 and bridge.refreshed == 1,
          (bridge.reloads, bridge.refreshed))


def test_tajiduo_password_login():
    print("[6] 塔吉多密码登录")
    bridge = DummyBridge()
    cfg = {"tajiduo_sign": {"deviceid": "dev-1", "password": "old"}}
    fp = {"deviceid": "dev-1", "openudid": "open-1", "vendorid": "vendor-1"}
    result = {"token": "token-1", "deviceid": "dev-1", "uid": "9", "password": "pw-1"}
    p_load, p_save, saved = _config_patches(cfg)
    with p_load, p_save, \
         mock.patch("game_auto.clients.tajiduo_login.gen_fingerprint", return_value=fp), \
         mock.patch("game_auto.clients.tajiduo_login.complete_password_login",
                    return_value=result) as login:
        bridge._run_tajiduo_password_login_backend("13800000000", "pw-1")
    login.assert_called_once_with("13800000000", "pw-1", "dev-1", "open-1", "vendor-1")
    section = saved["cfg"]["tajiduo_sign"]
    check("token/uid/phone 已保存",
          section["token"] == "token-1" and section["uid"] == "9"
          and section["phone"] == "13800000000", section)
    check("密码已保存", section["password"] == "pw-1", section)
    check("登录后刷新凭据", bridge.reloads == 1, bridge.reloads)


def test_tajiduo_sms():
    print("[7] 塔吉多发送验证码 + 验证码登录")
    bridge = DummyBridge()
    cfg = {"tajiduo_sign": {"deviceid": "dev-1", "password": "keep"}}
    fp = {"deviceid": "dev-1", "openudid": "open-1", "vendorid": "vendor-1"}
    p_load, p_save, saved = _config_patches(cfg)
    with p_load, p_save, \
         mock.patch("game_auto.clients.tajiduo_login.gen_fingerprint", return_value=fp), \
         mock.patch("game_auto.clients.tajiduo_login.send_captcha",
                    return_value=(200, {"code": 0})) as send:
        bridge._send_tajiduo_sms_code_backend("13800000000")
    send.assert_called_once_with("13800000000", "dev-1", "open-1", "vendor-1")
    check("发送验证码 toast", bridge.toasts and "验证码已发送" in bridge.toasts[-1][1]["message"],
          bridge.toasts)

    result = {"token": "token-2", "deviceid": "dev-1", "uid": "9", "password": ""}
    p_load, p_save, saved = _config_patches(cfg)
    with p_load, p_save, \
         mock.patch("game_auto.clients.tajiduo_login.gen_fingerprint", return_value=fp), \
         mock.patch("game_auto.clients.tajiduo_login.complete_sms_login",
                    return_value=result) as login:
        bridge._run_tajiduo_sms_login_backend("13800000000", "1234")
    login.assert_called_once_with("13800000000", "1234", "dev-1", "open-1", "vendor-1")
    section = saved["cfg"]["tajiduo_sign"]
    check("验证码登录写入新 token", section["token"] == "token-2", section)
    check("验证码登录不覆盖已有密码", section["password"] == "keep", section)


def test_skland_browser_token():
    print("[8] 森空岛浏览器登录态换 Token")
    import game_auto.clients.skendfield_sign as sk

    class FakeCookie:
        def __init__(self, name, value):
            self.name = name
            self.value = value

    class FakeJar(list):
        pass

    fake_bc3 = SimpleNamespace(
        edge=lambda domain_name=None: FakeJar([FakeCookie("ACCOUNT", "account-cookie")]),
    )
    with mock.patch.dict(sys.modules, {"browser_cookie3": fake_bc3}), \
         mock.patch.object(
             sk, "_request",
             return_value={"code": 0, "data": {"content": "skland-token"}},
         ) as request:
        result = sk.extract_token_from_browser("edge")
    check("从 ACCOUNT Cookie 换到 token", result.get("token") == "skland-token", result)
    request.assert_called_once()
    url = request.call_args.args[1]
    headers = request.call_args.kwargs.get("headers") or {}
    check("请求官方 account/info/hg", url.endswith("/account/info/hg"), url)
    check("带 ACCOUNT Cookie", headers.get("Cookie") == "ACCOUNT=account-cookie", headers)

    bridge = DummyBridge()
    cfg = {"skendfield_sign": {"server": "cn"}}
    p_load, p_save, saved = _config_patches(cfg)
    with p_load, p_save, \
         mock.patch("game_auto.clients.skendfield_sign.extract_token_from_browser",
                    return_value={"token": "skland-token"}):
        bridge._fetch_skland_token_backend("edge")
    section = saved["cfg"]["skendfield_sign"]
    check("桥接保存 token/browser",
          section["token"] == "skland-token" and section["browser"] == "edge", section)
    check("桥接保存后刷新凭据", bridge.reloads == 1, bridge.reloads)


def main():
    test_persist_mihoyo_cookie()
    test_fetch_browser_cookie()
    test_qr_login()
    test_manual_sign()
    test_kujiequ_code_login()
    test_tajiduo_password_login()
    test_tajiduo_sms()
    test_skland_browser_token()
    print("-" * 60)
    if FAILURES:
        print(f"FAILED {len(FAILURES)}: {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
