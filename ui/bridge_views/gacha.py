"""抽卡分析 / 各游戏专项页 / 凭据 / WebUI 设置。"""

import json
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

from PySide6.QtCore import QTimer, Slot

from ...settings.paths import CACHE_DIR, ENDFIELD_SKLAND_CACHE, WUWA_DAILY_CACHE
from .common import TONE_DANGER, TONE_INFO, TONE_SUCCESS, TONE_WARNING, logger

# 终末地森空岛 / 鸣潮库街区缓存新鲜度窗口（秒）
ENDFIELD_SKLAND_TTL = 300
WUWA_DAILY_TTL = 300


class GachaView:
    """抽卡导入分析、终末地/鸣潮/暖暖专项页、凭据与 WebUI 账号。"""
    # ── Gacha slots ──

    _GACHA_GAME_MAP = {
        "原神": "hk4e",
        "星穹铁道": "hkrpg",
        "绝区零": "nap",
    }

    @Slot(str)
    def loadGachaAccounts(self, game_label: str):
        """加载指定游戏的已导入 UID 列表。"""
        game_code = self._GACHA_GAME_MAP.get(game_label, "hk4e")
        threading.Thread(
            target=self._load_gacha_accounts_backend,
            args=(game_code,),
            daemon=True,
        ).start()

    @Slot(str, str)
    def loadGachaAnalysis(self, game_label: str, uid: str):
        """加载指定游戏 + UID 的抽卡分析数据。"""
        game_code = self._GACHA_GAME_MAP.get(game_label, "hk4e")
        threading.Thread(
            target=self._load_gacha_analysis_backend,
            args=(game_code, uid),
            daemon=True,
        ).start()

    @Slot(str)
    def importGachaFile(self, file_path: str):
        """导入 UIGF JSON 文件。"""
        if not file_path:
            self._request_toast("请先选择文件", "#b04b44")
            return
        threading.Thread(
            target=self._import_gacha_backend,
            args=(file_path,),
            daemon=True,
        ).start()

    @Slot(str, str)
    def importHoyogachaDb(self, db_path: str, game_label: str):
        """从 HoYo.Gacha 本地数据库直接导入（v1/旧版双兼容）。
        game_label: 当前选中的游戏（"原神"/"星穹铁道"/"绝区零"），用于导入后刷新对应账号列表。
        """
        if not db_path:
            self._request_toast("请先选择 HoYo.Gacha 数据库文件", "#b04b44")
            return
        threading.Thread(
            target=self._import_hoyogacha_db_backend,
            args=(db_path, game_label or ""),
            daemon=True,
        ).start()

    @Slot()
    def loadNikkiGacha(self):
        """加载无限暖暖抽卡数据（从数据库）。"""
        threading.Thread(target=self._load_nikki_gacha_backend, daemon=True).start()

    @Slot()
    def loadWuwaGacha(self):
        """加载鸣潮抽卡数据（从数据库）。"""
        threading.Thread(target=self._load_wuwa_gacha_backend, daemon=True).start()

    @Slot()
    def refreshWuwaDaily(self):
        """拉取鸣潮库街区每日数据（体力/活跃度/周本/深塔等）。"""
        threading.Thread(target=self._load_wuwa_daily_backend, daemon=True).start()

    @Slot()
    def loadWuwaDailyCache(self):
        """视图进入时调用：有缓存先即时展示，缺失/过期才后台刷新。

        与终末地森空岛同套机制：切页面/重启 app 都能立刻看到上次结果，
        仅在缓存超过 TTL（默认 5 分钟）或根本没有缓存时才打网络。
        """
        cache = self._wuwa_daily_cache
        if cache is not None:
            try:
                emitted = dict(cache["payload"])
                emitted["cached_at"] = cache.get("fetched_at")
                self.wuwaDailyChanged.emit(json.dumps(emitted, ensure_ascii=False))
            except Exception as exc:  # noqa: BLE001
                logger.debug("[Bridge] 推送鸣潮日报缓存失败: %s", exc)
        if self._wuwa_daily_needs_refresh():
            self._wuwa_daily_refreshing = True
            threading.Thread(target=self._load_wuwa_daily_backend, daemon=True).start()

    def _wuwa_daily_needs_refresh(self) -> bool:
        """缓存是否过期/缺失，需要后台刷新（已在进行中则返回 False）。"""
        if self._wuwa_daily_refreshing:
            return False
        cache = self._wuwa_daily_cache
        if cache is None:
            return True
        fetched_at = float(cache.get("fetched_at", 0) or 0)
        return (time.time() - fetched_at) > WUWA_DAILY_TTL

    def _load_wuwa_daily_cache(self) -> None:
        """从磁盘读入上次库街区每日数据（结构：{fetched_at, payload}）。"""
        try:
            p = WUWA_DAILY_CACHE
            if p.exists():
                obj = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(obj, dict) and "payload" in obj:
                    self._wuwa_daily_cache = obj
        except Exception:  # noqa: BLE001 - 损坏的缓存直接忽略
            self._wuwa_daily_cache = None

    def _save_wuwa_daily_cache(self, payload: Dict[str, Any]) -> None:
        """写入磁盘缓存（仅成功结果会调用，失败保留旧缓存）。"""
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            obj = {"fetched_at": time.time(), "payload": payload}
            WUWA_DAILY_CACHE.write_text(
                json.dumps(obj, ensure_ascii=False), encoding="utf-8")
            self._wuwa_daily_cache = obj
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Bridge] 鸣潮库街区缓存写入失败: %s", exc)

    @Slot(str)
    def importWuwaGachaFromJson(self, file_paths_json: str):
        """从 WutheringWavesTool 的 pool.json 导入抽卡记录（支持多文件合并去重）。

        Args:
            file_paths_json: JSON 数组字符串，例如 '["path1.json", "path2.json"]'
                            也兼容单路径字符串。
        """
        threading.Thread(
            target=self._import_wuwa_gacha_from_json_backend,
            args=(file_paths_json,),
            daemon=True,
        ).start()

    @Slot(str)
    def importEndfieldGacha(self, file_path: str):
        """从文件加载终末地抽卡数据并分析。"""
        if not file_path:
            self._request_toast("请先选择文件", "#b04b44")
            return
        threading.Thread(
            target=self._load_endfield_gacha_backend,
            args=(file_path,),
            daemon=True,
        ).start()

    @Slot()
    def loadEndfieldDefault(self):
        """从数据库加载终末地抽卡数据（无数据则提示用户导入文件）。"""
        threading.Thread(target=self._load_endfield_db_backend, daemon=True).start()

    @Slot()
    def fetchEndfieldGachaByToken(self):
        """用 user 提供的 token 直接从终末地官方接口拉取抽卡记录。

        参考 https://github.com/bhaoo/endfield-gacha 的协议：
        1. token + uid → OAuth grant → auth_token
        2. auth_token + uid → u8_token_by_uid → u8Token
        3. u8Token → ef-webview.{provider}.com/api/record/{char,weapon} 分页拉取

        凭据从 app_config.json 的 endfield_gacha 块读取（由 QML
        CredentialInput 写入），日志只输出 token 长度，绝不打印明文。
        """
        threading.Thread(
            target=self._fetch_endfield_gacha_by_token_backend,
            daemon=True,
        ).start()

    # ── Credentials slots ─────────────────────────────────────────

    @Slot()
    def loadCredentials(self):
        """加载所有凭据（cookie/token/api_key 等）从 app_config.json 到 QML。"""
        threading.Thread(target=self._load_credentials_backend, daemon=True).start()

    @Slot(str, str, str)
    def saveCredential(self, category: str, key: str, value: str):
        """保存单个凭据到 app_config.json。
        category: 顶层键 (如 "mihoyo_sign", "nikki")
        key: 子键 (如 "cookie", "momo_token")，支持点分多级
        value: 字符串值
        """
        threading.Thread(
            target=self._save_credential_backend,
            args=(category, key, value),
            daemon=True,
        ).start()

    @Slot(str, str, bool)
    def saveCredentialBool(self, category: str, key: str, value: bool):
        """保存布尔型凭据/配置到 app_config.json。"""
        threading.Thread(
            target=self._save_credential_backend,
            args=(category, key, value),
            daemon=True,
        ).start()

    @Slot(str)
    def saveMihoyoDevice(self, rawText: str):
        """识别并保存米游社设备指纹字段集合（APK 参数或抓包 fp/device_id）。"""
        threading.Thread(
            target=self._save_mihoyo_device_backend,
            args=(rawText,),
            daemon=True,
        ).start()

    @Slot()
    def clearMihoyoDevice(self):
        """清除米游社设备指纹绑定。"""
        threading.Thread(
            target=self._clear_mihoyo_device_backend,
            daemon=True,
        ).start()

    @Slot(str)
    def queryEndfieldCenter(self, query_type: str):
        """终末地数据中心查询（第三方协议终端，需 api_key/framework_token）。
        query_type: note | stamina | search_chars | spaceship | achieve | gacha_records | ...
        """
        threading.Thread(
            target=self._query_endfield_center_backend,
            args=(query_type,),
            daemon=True,
        ).start()

    @Slot()
    def queryEndfieldSkland(self):
        """终末地每日数据查询（森空岛官方 API，默认方式）。

        复用「森空岛签到」卡片里已填的 token，无需额外注册
        api_key / framework_token。
        """
        # 强制刷新（按钮触发）：忽略缓存新鲜度，直接打网络
        self._endfield_skland_refreshing = True
        threading.Thread(
            target=self._query_endfield_skland_backend,
            daemon=True,
        ).start()

    @Slot()
    def loadEndfieldSklandCache(self):
        """视图进入时调用：有缓存先即时展示，缺失/过期才后台刷新。

        这样切页面/重启 app 都能立刻看到上次结果，不必每次手动查询；
        仅在缓存超过 TTL（默认 5 分钟）或根本没有缓存时才打网络。
        """
        cache = self._endfield_skland_cache
        if cache is not None:
            try:
                # 给缓存结果打上 cached_at 标记，供 UI 显示"更新于"，不写入磁盘缓存
                emitted = dict(cache["result"])
                emitted["cached_at"] = cache.get("fetched_at")
                self.endfieldQueryResultChanged.emit(
                    json.dumps(emitted, ensure_ascii=False))
            except Exception as exc:  # noqa: BLE001
                logger.debug("[Bridge] 推送终末地查询缓存失败: %s", exc)
        if self._endfield_skland_needs_refresh():
            self._endfield_skland_refreshing = True
            threading.Thread(
                target=self._query_endfield_skland_backend, daemon=True).start()

    def _endfield_skland_needs_refresh(self) -> bool:
        """缓存是否过期/缺失，需要后台刷新（已在进行中则返回 False）。"""
        if self._endfield_skland_refreshing:
            return False
        cache = self._endfield_skland_cache
        if cache is None:
            return True
        fetched_at = float(cache.get("fetched_at", 0) or 0)
        return (time.time() - fetched_at) > ENDFIELD_SKLAND_TTL

    def _load_endfield_skland_cache(self) -> None:
        """从磁盘读入上次查询结果（结构：{fetched_at, result}）。"""
        try:
            p = ENDFIELD_SKLAND_CACHE
            if p.exists():
                obj = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(obj, dict) and "result" in obj:
                    self._endfield_skland_cache = obj
        except Exception:  # noqa: BLE001 - 损坏的缓存直接忽略
            self._endfield_skland_cache = None

    def _save_endfield_skland_cache(self, result: Dict[str, Any]) -> None:
        """写入磁盘缓存（仅成功结果会调用，失败保留旧缓存）。"""
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            obj = {"fetched_at": time.time(), "result": result}
            ENDFIELD_SKLAND_CACHE.write_text(
                json.dumps(obj, ensure_ascii=False), encoding="utf-8")
            self._endfield_skland_cache = obj
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Bridge] 终末地缓存写入失败: %s", exc)

    @Slot()
    def queryTajiduoDaily(self):
        """查询塔吉多（异环）每日任务进度。

        复用签到用的 token + deviceid，不需要额外凭据。
        """
        threading.Thread(
            target=self._query_tajiduo_daily_backend,
            daemon=True,
        ).start()

    # ── Sign action slots (QML 按钮调用) ────────────────────────

    @Slot()
    def runSkendfieldSign(self):
        """手动触发森空岛签到（QML "执行签到" 按钮）。"""
        threading.Thread(target=self._run_skendfield_sign_backend, daemon=True).start()

    @Slot()
    def runWuwaKujiequSign(self):
        """手动触发库街区签到（QML "执行签到" 按钮）。"""
        threading.Thread(target=self._run_wuwa_kujiequ_sign_backend, daemon=True).start()

    @Slot()
    def runWuwaKujiequAdbGrab(self):
        """通过 ADB + WebView CDP 从手机端库街区 APP 抓取 token/did。

        参考：https://wave.999758.xyz/pages/advance/token.html
        前置条件：手机已启用 USB 调试并连接电脑、库街区 APP 已进入"养成计算器"。
        """
        threading.Thread(target=self._run_wuwa_kujiequ_adb_grab_backend, daemon=True).start()

    @Slot()
    def runTajiduoSign(self):
        """手动触发塔吉多（异环）签到（QML "执行签到" 按钮）。"""
        threading.Thread(target=self._run_tajiduo_sign_backend, daemon=True).start()

    # ── WebUI 账号与状态（GUI 内设置，WebUI 通过 /api/auth 鉴权） ──
    @Slot(result=str)
    def getWebUIStatus(self) -> str:
        """返回 {has_account, username, port, url, running} 供 QML 展示。"""
        try:
            from ...webui.auth import has_account, get_username
            from ...webui.app import WEBUI_DEFAULT_PORT
            has_acc = bool(has_account())
            user = get_username() if has_acc else ""
            # 检测是否在运行（尝试连端口）
            running = False
            try:
                import socket
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(0.3)
                running = s.connect_ex(("127.0.0.1", int(WEBUI_DEFAULT_PORT))) == 0
                s.close()
            except Exception:
                running = False
            info = {
                "has_account": has_acc,
                "username": user,
                "port": int(WEBUI_DEFAULT_PORT),
                "url": f"http://127.0.0.1:{int(WEBUI_DEFAULT_PORT)}",
                "running": running,
            }
            self._webui_status_json = json.dumps(info, ensure_ascii=False)
            self.webUIStatusChanged.emit(self._webui_status_json)
            return self._webui_status_json
        except Exception as exc:
            logger.warning("[Bridge] 获取 WebUI 状态失败: %s", exc)
            return json.dumps({"has_account": False, "username": "", "port": 60171, "url": "http://127.0.0.1:60171", "running": False}, ensure_ascii=False)

    @Slot(str, str)
    def saveWebUIAccount(self, username: str, password: str):
        """保存 WebUI 登录账号（GUI 输入，DPAPI 加密落盘）。"""
        try:
            from ...webui.auth import set_account
            set_account(username, password)
            started = False
            try:
                from ...webui.app import start_webui_background
                started = start_webui_background()
            except Exception as exc:
                logger.warning("[Bridge] WebUI 账号已保存但服务启动失败: %s", exc)
            logger.info("[Bridge] WebUI 账号已更新: %s (webui_started=%s)", (username or "").strip(), started)
            self.getWebUIStatus()
            if started:
                # uvicorn 绑定端口需要几百毫秒，稍后再刷新一次状态供 QML 展示
                QTimer.singleShot(800, self.getWebUIStatus)
            message = "WebUI 账号已保存（已加密），WebUI 已启动" if started else "WebUI 账号已保存（已加密），重启 GUI 后启动 WebUI"
            self.emit_event("toast", message=message, color="#2f7d4a")
        except Exception as exc:
            logger.warning("[Bridge] 保存 WebUI 账号失败: %s", exc)
            self.emit_event("toast", message=f"保存失败: {exc}", color="#b04b44")

    @Slot()
    def openWebUIInBrowser(self):
        """用系统默认浏览器打开 WebUI。"""
        try:
            from ...webui.app import WEBUI_DEFAULT_PORT
            import webbrowser
            url = f"http://127.0.0.1:{int(WEBUI_DEFAULT_PORT)}"
            webbrowser.open(url)
            self.emit_event("toast", message=f"已打开 {url}", color="#2f7d4a")
        except Exception as exc:
            self.emit_event("toast", message=f"打开失败: {exc}", color="#b04b44")

    def _load_gacha_accounts_backend(self, *args, **kwargs):
        from .bridge_backends.gacha import _load_gacha_accounts_backend as _impl
        return _impl(self, *args, **kwargs)

    def _load_gacha_analysis_backend(self, *args, **kwargs):
        from .bridge_backends.gacha import _load_gacha_analysis_backend as _impl
        return _impl(self, *args, **kwargs)

    def _import_gacha_backend(self, *args, **kwargs):
        from .bridge_backends.gacha import _import_gacha_backend as _impl
        return _impl(self, *args, **kwargs)

    def _import_hoyogacha_db_backend(self, *args, **kwargs):
        from .bridge_backends.gacha import _import_hoyogacha_db_backend as _impl
        return _impl(self, *args, **kwargs)

    def _load_nikki_gacha_backend(self, *args, **kwargs):
        from .bridge_backends.gacha import _load_nikki_gacha_backend as _impl
        return _impl(self, *args, **kwargs)

    def _load_wuwa_daily_backend(self, *args, **kwargs):
        from .bridge_backends.gacha import _load_wuwa_daily_backend as _impl
        return _impl(self, *args, **kwargs)

    def _load_wuwa_gacha_backend(self, *args, **kwargs):
        from .bridge_backends.gacha import _load_wuwa_gacha_backend as _impl
        return _impl(self, *args, **kwargs)

    def _import_wuwa_gacha_from_json_backend(self, *args, **kwargs):
        from .bridge_backends.gacha import _import_wuwa_gacha_from_json_backend as _impl
        return _impl(self, *args, **kwargs)

    def _load_endfield_gacha_backend(self, *args, **kwargs):
        from .bridge_backends.gacha import _load_endfield_gacha_backend as _impl
        return _impl(self, *args, **kwargs)

    def _migrate_endfield_uids_to(self, *args, **kwargs):
        from .bridge_backends.gacha import _migrate_endfield_uids_to as _impl
        return _impl(self, *args, **kwargs)

    def _load_endfield_db_backend(self, *args, **kwargs):
        from .bridge_backends.gacha import _load_endfield_db_backend as _impl
        return _impl(self, *args, **kwargs)

    def _fetch_endfield_gacha_by_token_backend(self, *args, **kwargs):
        from .bridge_backends.gacha import _fetch_endfield_gacha_by_token_backend as _impl
        return _impl(self, *args, **kwargs)

    # ═══════════════════════════════════════════════════════════════
    #  Credentials backend (app_config.json)
    # ═══════════════════════════════════════════════════════════════

    # 顶层凭据键 → 应返回给 QML 的子键列表；空列表表示整块返回
    _CREDENTIAL_KEYS = (
        "mihoyo_sign",
        "wuwa_kujiequ",
        "tajiduo_sign",
        "skendfield_sign",
        "endfield_protocol",
        "endfield_gacha",
        "nikki",
    )

    def _load_credentials_backend(self):
        """读取所有凭据并推送到 QML。

        库街区 token/did 存数据库（DPAPI 加密），这里不回灌明文，
        只补 `profile.token_configured` / `did_configured` 布尔标志供界面提示。
        """
        try:
            from ...settings import manager as cfg_manager
            from ...services import credential_store

            cfg = cfg_manager.load(force=True)
            credentials: Dict[str, Any] = {}
            for top_key in self._CREDENTIAL_KEYS:
                section = cfg.get(top_key)
                credentials[top_key] = dict(section) if isinstance(section, dict) else {}

            # 库街区：剥离密钥字段（配置里可能残留迁移前的旧值），换成布尔标志
            wuwa = credentials.get(credential_store.WUWA_CATEGORY) or {}
            profile = dict(wuwa.get("profile") or {})
            for secret_key in credential_store.WUWA_SECRET_KEYS:
                profile.pop(secret_key, None)
            profile["token_configured"] = credential_store.has_secret(
                credential_store.WUWA_CATEGORY, "token",
            )
            profile["did_configured"] = credential_store.has_secret(
                credential_store.WUWA_CATEGORY, "did",
            )
            wuwa["profile"] = profile
            credentials[credential_store.WUWA_CATEGORY] = wuwa

            self.emit_event("credentials", credentials=credentials)
            logger.info(
                "[Bridge] 凭据已加载: %s",
                ", ".join(k for k, v in credentials.items() if v),
            )
        except Exception as exc:
            logger.exception("[Bridge] 加载凭据失败")
            self.emit_event("toast", message=f"加载凭据失败: {exc}", color="#b04b44")

    def _save_credential_backend(self, category: str, key: str, value: Any):
        """写回单个凭据。key 支持点分多级，例如 "profile.token"。

        库街区 token/did 属密钥，改存数据库（DPAPI 加密）；
        其余凭据仍写 app_config.json（由 settings.manager 加密敏感字段）。
        """
        try:
            if not category or not key:
                self.emit_event("toast", message="参数无效", color="#b04b44")
                return

            # 密钥路由：加密入库，配置里不再保留
            from ...services import credential_store

            if category == credential_store.WUWA_CATEGORY and key.startswith("profile."):
                sub_key = key.split(".", 1)[1]
                if sub_key in credential_store.WUWA_SECRET_KEYS:
                    credential_store.set_secret(category, sub_key, str(value or ""))
                    logger.info("[Bridge] 库街区密钥已加密入库: %s.%s", category, sub_key)
                    self._load_credentials_backend()
                    return

            from ...settings import manager as cfg_manager

            cfg = cfg_manager.load(force=True)
            section = cfg.setdefault(category, {})

            keys = key.split(".")
            target = section
            for k in keys[:-1]:
                if not isinstance(target.get(k), dict):
                    target[k] = {}
                target = target[k]
            target[keys[-1]] = value

            cfg_manager.save(cfg)
            logger.info(
                "[Bridge] 凭据已保存: %s.%s (类型=%s)",
                category, key, type(value).__name__,
            )
            # 重新推送凭据到 QML 以便刷新所有视图
            self._load_credentials_backend()
        except Exception as exc:
            logger.exception("[Bridge] 保存凭据失败")
            self.emit_event("toast", message=f"保存失败: {exc}", color="#b04b44")

    def _save_mihoyo_device_backend(self, raw_text: str):
        """识别粘贴的设备字段集合，写回 app_config.json 的 mihoyo_sign.device。"""
        try:
            from ...clients.daily_checker import recognize_device_input

            device, info = recognize_device_input(raw_text)
            if device is None:
                self.emit_event(
                    "toast",
                    message=info.get("error") or "设备字段识别失败",
                    color="#b04b44",
                )
                return

            from ...settings import manager as cfg_manager

            cfg = cfg_manager.load(force=True)
            section = cfg.setdefault("mihoyo_sign", {})
            old = section.get("device") if isinstance(section.get("device"), dict) else {}

            # 设备身份没变时保留旧缓存（fp/device_id/seed/registered），换设备则丢弃重新生成。
            # 只比较本次粘贴提供的字段：旧 dict 里的缓存键（fp/device_id 等）不参与比较。
            keep_cache = bool(device) and all(
                str(old.get(k) or "") == str(v or "") for k, v in device.items()
            )
            if "fp" not in device and keep_cache and old.get("fp"):
                device["fp"] = old["fp"]
            for cache_key in ("device_id", "seed_id", "seed_time", "registered"):
                if cache_key not in device and keep_cache and old.get(cache_key) is not None:
                    device[cache_key] = old[cache_key]

            section["device"] = device
            cfg_manager.save(cfg)
            source_label = "抓包 fp" if info.get("format") == "captured_fp" else "APK 参数"
            fp_suffix = f", fp={str(device.get('fp'))[:8]}…" if device.get("fp") else ""
            logger.info(
                "[Bridge] 设备指纹已保存: 格式=%s, 字段=%d 个%s",
                info.get("format"), len(info.get("fields") or []), fp_suffix,
            )
            self.emit_event(
                "toast",
                message=f"已识别并保存设备指纹（{source_label}，识别到 {len(info.get('fields') or [])} 个字段）",
                color="#2f7d4a",
            )
            self._load_credentials_backend()
        except Exception as exc:
            logger.exception("[Bridge] 保存设备指纹失败")
            self.emit_event("toast", message=f"保存设备指纹失败: {exc}", color="#b04b44")

    def _clear_mihoyo_device_backend(self):
        """从 app_config.json 移除 mihoyo_sign.device 绑定。"""
        try:
            from ...settings import manager as cfg_manager

            cfg = cfg_manager.load(force=True)
            section = cfg.get("mihoyo_sign")
            if isinstance(section, dict) and "device" in section:
                section.pop("device")
                cfg_manager.save(cfg)
                logger.info("[Bridge] 设备指纹绑定已清除")
                self.emit_event("toast", message="已清除设备指纹绑定", color="#2f7d4a")
                self._load_credentials_backend()
            else:
                self.emit_event("toast", message="当前没有设备指纹绑定", color="#b66f1f")
        except Exception as exc:
            logger.exception("[Bridge] 清除设备指纹失败")
            self.emit_event("toast", message=f"清除失败: {exc}", color="#b04b44")

    def _query_tajiduo_daily_backend(self):
        """后台查询塔吉多（异环）每日任务进度。

        复用签到用的 token + deviceid，走 getUserTasks 的
        limitTimes / completeTimes 计算完成度。
        """
        try:
            from ...settings import manager as cfg_manager
            from ...clients.daily_checker import check_tajiduo_daily

            cfg = cfg_manager.load(force=True)
            tj = cfg.get("tajiduo_sign", {}) or {}
            token = str(tj.get("token", "") or "").strip()
            deviceid = str(tj.get("deviceid", "") or "").strip()

            if not token or not deviceid:
                self.emit_event("toast", message="请先填写塔吉多 Token 和 Device ID",
                                color="#b66f1f")
                self.emit_event("tajiduo_daily_result", result={
                    "ok": False,
                    "message": "缺少 token 或 deviceid（Device ID 在签到配置里，"
                               "QML 未暴露输入框，需手动写进 app_config.json）",
                    "items": [],
                })
                return

            self.emit_event("toast", message="正在查询塔吉多每日任务...", color="#295d86")

            status = check_tajiduo_daily(
                token=token,
                deviceid=deviceid,
                uid=str(tj.get("uid", "0") or "0"),
                phone=str(tj.get("phone", "") or ""),
                password=str(tj.get("password", "") or ""),
            )

            if status.errors:
                msg = "；".join(map(str, status.errors))
                self.emit_event("toast", message="塔吉多查询失败", color="#b66f1f")
                self.emit_event("tajiduo_daily_result", result={
                    "ok": False, "message": msg, "items": [],
                })
                return

            items = [
                {"name": t.name, "current": t.current,
                 "total": t.total, "done": bool(t.is_completed)}
                for t in status.tasks
            ]
            self.emit_event("toast", message="塔吉多查询成功", color="#2f7d4a")
            self.emit_event("tajiduo_daily_result", result={
                "ok": True,
                "message": "",
                "items": items,
                "all_done": bool(status.is_daily_completed),
                "game_cards": (status.extra or {}).get("game_cards", []),
            })
            logger.info("[Bridge] 塔吉多每日查询完成: %d 项任务", len(items))
        except Exception as exc:
            logger.exception("[Bridge] 塔吉多每日查询异常")
            self.emit_event("toast", message=f"查询异常: {exc}", color="#b04b44")
            self.emit_event("tajiduo_daily_result", result={
                "ok": False, "message": f"查询异常: {exc}", "items": [],
            })

    def _query_endfield_skland_backend(self):
        """执行终末地每日数据查询（森空岛官方 API）。

        复用 skendfield_sign 里已填的 token，不需要 api_key / framework_token。
        结果沿用 endfield_query_result 事件，QML 侧可复用同一套结果展示。
        成功结果会写入本地缓存（供进页面即时展示）。
        """
        try:
            from ...settings import manager as cfg_manager
            from ...clients.skendfield_data import (
                fetch_endfield_data,
                format_endfield_data_report,
            )

            cfg = cfg_manager.load(force=True)
            sk_cfg = cfg.get("skendfield_sign", {}) or {}
            token = str(sk_cfg.get("token", "") or "").strip()
            server = str(sk_cfg.get("server", "cn") or "cn").strip() or "cn"

            if not token:
                self.emit_event("toast", message="请先在下方「森空岛签到」填写 Token",
                                color="#b66f1f")
                self.emit_event("endfield_query_result", result={
                    "query_type": "skland",
                    "success": False,
                    "message": "未配置森空岛 token。请在「森空岛签到」卡片填写，"
                               "取自 web-api.skland.com/account/info/hg 的 data.content",
                    "data": None,
                })
                return

            self.emit_event("toast", message="正在通过森空岛查询终末地数据...",
                            color="#295d86")

            data = fetch_endfield_data(token, server=server)

            if data.get("ok"):
                summary = "；".join(format_endfield_data_report(data))
                result = {
                    "query_type": "skland",
                    "success": True,
                    "message": summary,
                    "data": {
                        "uid": data.get("uid", ""),
                        "role_name": data.get("role_name", ""),
                        "server_name": data.get("server_name", ""),
                        "daily": data.get("daily"),
                        "extras": data.get("extras", {}),
                        "raw_keys": data.get("raw_keys", []),
                        "raw": data.get("raw", {}),
                    },
                }
                self.emit_event("toast", message="森空岛查询成功", color="#2f7d4a")
                # 仅成功结果落地缓存；失败保留上次好缓存
                self._save_endfield_skland_cache(result)
                # 给 UI 打上 cached_at（不写进磁盘缓存），用于显示"更新于"
                emitted = dict(result)
                emitted["cached_at"] = time.time()
                self.emit_event("endfield_query_result", result=emitted)
            else:
                msg = data.get("message") or "查询失败"
                self.emit_event("toast", message=f"森空岛查询失败：{msg}", color="#b66f1f")
                self.emit_event("endfield_query_result", result={
                    "query_type": "skland",
                    "success": False,
                    "message": msg,
                    "data": None,
                })
            logger.info("[Bridge] 森空岛终末地查询完成: ok=%s", data.get("ok"))
        except Exception as exc:
            logger.exception("[Bridge] 森空岛终末地查询异常")
            self.emit_event("toast", message=f"查询异常: {exc}", color="#b04b44")
            self.emit_event("endfield_query_result", result={
                "query_type": "skland",
                "success": False,
                "message": f"查询异常: {exc}",
                "data": None,
            })
        finally:
            self._endfield_skland_refreshing = False

    def _query_endfield_center_backend(self, query_type: str):
        """执行终末地数据中心查询。"""
        try:
            if not query_type:
                self.emit_event("toast", message="未知查询类型", color="#b04b44")
                return

            from ...settings import manager as cfg_manager
            from ...clients.endfield_protocol import perform_query

            cfg = cfg_manager.load(force=True)
            proto_cfg = cfg.get("endfield_protocol", {}) or {}
            api_key = proto_cfg.get("api_key", "")
            framework_token = proto_cfg.get("framework_token", "")
            role_id = proto_cfg.get("role_id", "")
            server_id = proto_cfg.get("server_id", "")

            if not api_key:
                self.emit_event("toast", message="请先填写 API Key", color="#b66f1f")
                return

            self.emit_event("toast", message=f"正在执行 {query_type} 查询...", color="#295d86")

            result = perform_query(
                api_key=api_key,
                framework_token=framework_token,
                role_id=role_id,
                server_id=server_id,
                query_type=query_type,
            )

            if result is None:
                self.emit_event("toast", message=f"{query_type} 查询失败或无数据", color="#b66f1f")
                self.emit_event("endfield_query_result", result={
                    "query_type": query_type,
                    "success": False,
                    "message": "查询失败或缺少必要参数（role_id/server_id 等）",
                    "data": None,
                })
            else:
                self.emit_event("toast", message=f"{query_type} 查询成功", color="#2f7d4a")
                self.emit_event("endfield_query_result", result={
                    "query_type": query_type,
                    "success": True,
                    "message": "查询成功",
                    "data": result,
                })
            logger.info("[Bridge] 终末地查询完成: type=%s success=%s",
                        query_type, result is not None)
        except Exception as exc:
            logger.exception("[Bridge] 终末地查询异常")
            self.emit_event("toast", message=f"查询异常: {exc}", color="#b04b44")
            self.emit_event("endfield_query_result", result={
                "query_type": query_type,
                "success": False,
                "message": f"查询异常: {exc}",
                "data": None,
            })
