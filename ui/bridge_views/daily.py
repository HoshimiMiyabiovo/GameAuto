"""每日进度 / 失败诊断 / 签到详情视图的后台逻辑。"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Slot

from ...settings.paths import WORKFLOW_DB_PATH
from .common import TONE_DANGER, TONE_INFO, TONE_SUCCESS, TONE_WARNING, logger


class DailyView:
    """每日进度、失败记录、签到状态与签到按钮实现。"""
    # ── Daily progress slots ──────────────────────────────────────

    @Slot(str)
    def checkDailyProgress(self, games_json: str):
        """查询每日进度。games_json: ["genshin","honkai_sr","zzz",...]"""
        try:
            game_keys = json.loads(games_json)
        except json.JSONDecodeError:
            game_keys = []
        threading.Thread(
            target=self._run_daily_check_backend,
            args=(game_keys,),
            daemon=True,
        ).start()

    @Slot()
    def loadDailyProgress(self):
        """从 SQLite 加载今日持久化进度。"""
        threading.Thread(target=self._load_daily_progress_backend, daemon=True).start()

    @Slot()
    def loadDailyDates(self):
        """加载有每日进度记录的日期列表（倒序）。"""
        threading.Thread(target=self._load_daily_dates_backend, daemon=True).start()

    @Slot(str)
    def loadDailyHistory(self, date_key: str):
        """回看指定日期的每日进度。date_key 形如 "2026-09-27"。"""
        threading.Thread(
            target=self._load_daily_history_backend, args=(str(date_key or "").strip(),),
            daemon=True,
        ).start()

    @Slot(int)
    def loadDailyStats(self, days: int):
        """统计最近 days 天各游戏完成率。"""
        try:
            n = int(days)
        except (TypeError, ValueError):
            n = 30
        n = max(1, min(n, 365))
        threading.Thread(target=self._load_daily_stats_backend, args=(n,), daemon=True).start()

    @Slot(str)
    def openBaijingVerifyImage(self, image_url: str):
        """用系统默认程序打开验证截图（file:// URL 或相对路径）。"""
        try:
            from pathlib import Path as _P
            from ...settings.paths import BASE_DIR as _BD, BAIJING_VERIFY_DIR
            p = None
            if image_url and image_url.startswith("file://"):
                try:
                    # file URI -> path
                    from urllib.parse import unquote
                    from urllib.request import url2pathname
                    p = _P(url2pathname(unquote(image_url[7:])))
                except Exception:
                    p = None
            elif image_url:
                p = _P(image_url)
                if not p.is_absolute():
                    p = _BD / image_url
            if p is None or not p.exists():
                # 回退：打开目录
                p = BAIJING_VERIFY_DIR
            if p.is_file():
                os.startfile(str(p))
            elif p.is_dir():
                os.startfile(str(p))
            else:
                # 目录不存在时创建并打开
                p.mkdir(parents=True, exist_ok=True)
                os.startfile(str(p))
        except Exception as exc:
            logger.error("[Bridge] 打开验证图失败: %s", exc)
            self._request_toast(f"打开失败: {exc}", "#b04b44")

    @Slot()
    def openBaijingVerifyDir(self):
        try:
            from ...settings.paths import BAIJING_VERIFY_DIR
            BAIJING_VERIFY_DIR.mkdir(parents=True, exist_ok=True)
            os.startfile(str(BAIJING_VERIFY_DIR))
        except Exception as exc:
            logger.error("[Bridge] 打开验证目录失败: %s", exc)
            self._request_toast(f"打开失败: {exc}", "#b04b44")

    # ── 失败诊断记录（flow_failures 表 + artifacts/flow_failures 截图） ──

    @Slot(int, result=str)
    def getRecentFlowFailures(self, days: int = 7):
        """返回最近 N 天的失败记录（JSON 字符串，含截图可打开路径）。

        同步读 SQLite（WAL 下只读很快），但仍放在后台线程避免阻塞 QML 主线程：
        首次冷启动要建索引，且别把 GUI 卡顿的锅再背到数据查询上。
        """
        days = max(1, min(int(days or 7), 365))
        out: Dict[str, Any] = {"days": days, "summary": [], "items": [], "error": ""}
        done = threading.Event()

        def _load() -> None:
            try:
                from ...settings.paths import FLOW_FAILURE_DIR, WORKFLOW_DB_PATH
                from ...storage.workflow_db import WorkflowDB

                db = WorkflowDB(str(WORKFLOW_DB_PATH))
                out["summary"] = db.summarize_flow_failures(days=days, group_limit=20)
                rows = db.query_flow_failures(days=days, limit=60)
                items = []
                for r in rows:
                    item = dict(r)
                    img = item.get("image_path") or ""
                    if img:
                        fp = FLOW_FAILURE_DIR / img
                        # QML Image.source 的相对路径会按 .qml 文件目录解析
                        # （曾拼成 ui/qml/views/artifacts/...），必须回绝对 file:/// URL。
                        item["image_url"] = fp.resolve().as_uri() if fp.exists() else ""
                    else:
                        item["image_url"] = ""
                    item["time_text"] = time.strftime(
                        "%m-%d %H:%M:%S", time.localtime(item.get("ts") or 0)
                    )
                    item["ocr_texts"] = (item.get("ocr_texts") or [])[:8]
                    items.append(item)
                out["items"] = items
            except Exception as exc:
                out["error"] = str(exc)
                logger.warning("[Bridge] 读取失败记录失败: %s", exc)
            finally:
                done.set()

        threading.Thread(target=_load, daemon=True).start()
        # 最多等 3s；超时先返回空列表，由 QML 侧提示"加载慢，可再点一次刷新"
        if not done.wait(timeout=3.0):
            out["error"] = out.get("error") or "加载超时"
        try:
            return json.dumps(out, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"error": f"序列化失败: {exc}"}, ensure_ascii=False)

    @Slot(str)
    def openFlowFailureImage(self, image_url: str):
        """打开失败当刻截图（兼容 file:/// URL、裸文件名和旧的相对路径）。"""
        try:
            from pathlib import Path as _P
            from urllib.parse import unquote, urlparse
            from ...settings.paths import FLOW_FAILURE_DIR

            base = FLOW_FAILURE_DIR.resolve()
            raw = str(image_url or "").strip()
            candidate = base
            if raw.startswith("file:"):
                parsed = urlparse(raw)
                local = unquote(parsed.path or "")
                # Windows: file:///E:/... 的 path 是 /E:/...，去掉前导斜杠
                if local.startswith("/") and len(local) >= 3 and local[2] == ":":
                    local = local[1:]
                candidate = _P(local)
            elif raw:
                # 旧调用可能传 artifacts/flow_failures/xxx.jpg；只取文件名
                candidate = base / _P(raw).name
            try:
                resolved = candidate.resolve()
                if resolved.is_file() and resolved.is_relative_to(base):
                    candidate = resolved
                else:
                    candidate = base
            except Exception:
                candidate = base
            candidate.parent.mkdir(parents=True, exist_ok=True)
            os.startfile(str(candidate))
        except Exception as exc:
            logger.error("[Bridge] 打开失败截图失败: %s", exc)
            self._request_toast(f"打开失败: {exc}", "#b04b44")

    @Slot()
    def openFlowFailureDir(self):
        try:
            from ...settings.paths import FLOW_FAILURE_DIR
            FLOW_FAILURE_DIR.mkdir(parents=True, exist_ok=True)
            os.startfile(str(FLOW_FAILURE_DIR))
        except Exception as exc:
            logger.error("[Bridge] 打开失败证据目录失败: %s", exc)
            self._request_toast(f"打开失败: {exc}", "#b04b44")

    # ── 第三方脚本体力刷本配置（第一期 JSON：原神/鸣潮/终末地/异环/暖暖） ──

    @Slot(result=str)
    def getStaminaConfigs(self) -> str:
        """读取 5 个脚本的体力刷本配置，返回 JSON 数组。"""
        try:
            from ...services.stamina_configs import list_configs

            data = list_configs()
            logger.info("[刷本配置] 读取 %d 个脚本配置", len(data))
            return json.dumps(data, ensure_ascii=False)
        except Exception as exc:
            logger.warning("[刷本配置] 读取失败: %s", exc)
            return json.dumps(
                [{"game_key": "", "display_name": "", "error": str(exc), "fields": [], "values": {}}],
                ensure_ascii=False,
            )

    @Slot(str, str, result=str)
    def saveStaminaConfig(self, game_key: str, values_json: str) -> str:
        """保存某个脚本的刷本配置；返回 {ok, message, backups} 或 {ok:false,error}。"""
        try:
            from ...services.stamina_configs import get_adapter

            values = json.loads(values_json or "{}")
            if not isinstance(values, dict):
                raise ValueError("配置参数不是对象")
            adapter = get_adapter(str(game_key))
            result = adapter.write(values)
            logger.info("[刷本配置] 保存 %s: %s", game_key, result.get("message"))
            self._request_toast(f"{adapter.display_name} 配置已保存", "#2f7d4a")
            return json.dumps(result, ensure_ascii=False)
        except Exception as exc:
            logger.warning("[刷本配置] 保存 %s 失败: %s", game_key, exc)
            self._request_toast(f"保存失败: {exc}", "#b04b44")
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    @Slot(str)
    def openStaminaConfigFile(self, game_key: str):
        """用系统默认程序打开脚本配置文件（方便看原文/高级字段）。"""
        try:
            from ...services.stamina_configs import get_adapter

            adapter = get_adapter(str(game_key))
            path = adapter.primary_path()
            if not path or not path.is_file():
                raise FileNotFoundError("配置文件不存在")
            os.startfile(str(path))
        except Exception as exc:
            logger.warning("[刷本配置] 打开配置文件失败: %s", exc)
            self._request_toast(f"打开失败: {exc}", "#b04b44")

    # ── Tool slots ────────────────────────────────────────────────

    @Slot()
    def openConfigFile(self):
        try:
            from ...settings.paths import APP_CONFIG_PATH
            os.startfile(str(APP_CONFIG_PATH))
        except Exception as exc:
            logger.error("[Bridge] 打开配置文件失败: %s", exc)
            self._request_toast(f"打开失败: {exc}", "#b04b44")

    @Slot()
    def openLogDir(self):
        try:
            from ...settings.paths import LOG_DIR
            os.startfile(str(LOG_DIR))
        except Exception as exc:
            logger.error("[Bridge] 打开日志目录失败: %s", exc)
            self._request_toast(f"打开失败: {exc}", "#b04b44")

    def _refresh_daily_snapshot_for_report(self, *args, **kwargs):
        from .bridge_backends.daily_progress import _refresh_daily_snapshot_for_report as _impl
        return _impl(self, *args, **kwargs)

    def _send_daily_email_report(self, *args, **kwargs):
        from .bridge_backends.daily_progress import _send_daily_email_report as _impl
        return _impl(self, *args, **kwargs)

    def _save_preferences_backend(self, *args, **kwargs):
        from .bridge_backends.daily_progress import _save_preferences_backend as _impl
        return _impl(self, *args, **kwargs)

    def _run_daily_check_backend(self, *args, **kwargs):
        from .bridge_backends.daily_progress import _run_daily_check_backend as _impl
        return _impl(self, *args, **kwargs)

    def _serialize_snapshot_games(self, *args, **kwargs):
        from .bridge_backends.daily_progress import _serialize_snapshot_games as _impl
        return _impl(self, *args, **kwargs)

    def _load_daily_dates_backend(self, *args, **kwargs):
        from .bridge_backends.daily_progress import _load_daily_dates_backend as _impl
        return _impl(self, *args, **kwargs)

    def _load_daily_history_backend(self, *args, **kwargs):
        from .bridge_backends.daily_progress import _load_daily_history_backend as _impl
        return _impl(self, *args, **kwargs)

    def _load_daily_stats_backend(self, *args, **kwargs):
        from .bridge_backends.daily_progress import _load_daily_stats_backend as _impl
        return _impl(self, *args, **kwargs)

    def _load_daily_progress_backend(self, *args, **kwargs):
        from .bridge_backends.daily_progress import _load_daily_progress_backend as _impl
        return _impl(self, *args, **kwargs)

    @staticmethod
    @staticmethod
    def _real_completion_percent(*args, **kwargs):
        from .bridge_backends.daily_progress import _real_completion_percent as _impl
        return _impl(*args, **kwargs)

    @staticmethod
    @staticmethod
    def _attach_completion_state(*args, **kwargs):
        from .bridge_backends.daily_progress import _attach_completion_state as _impl
        return _impl(*args, **kwargs)

    @staticmethod
    @staticmethod
    def _extract_task_counts(*args, **kwargs):
        from .bridge_backends.daily_progress import _extract_task_counts as _impl
        return _impl(*args, **kwargs)

    def _inject_external_log_progress(self, *args, **kwargs):
        from .bridge_backends.daily_progress import _inject_external_log_progress as _impl
        return _impl(self, *args, **kwargs)

    def _ensure_baijing_placeholder(self, *args, **kwargs):
        from .bridge_backends.daily_progress import _ensure_baijing_placeholder as _impl
        return _impl(self, *args, **kwargs)

    # ── Sign action backends (run in background thread) ─────────────

    def _run_skendfield_sign_backend(self):
        """森空岛签到：从 app_config.json 读 token + server，调 perform_sign。"""
        try:
            from ...settings import manager as cfg_manager
            from ...clients import skendfield_sign

            cfg = cfg_manager.load(force=True)
            sk = cfg.get("skendfield_sign") or {}
            token = str(sk.get("token") or "").strip()
            server = str(sk.get("server") or "cn").strip()
            if not token:
                self.emit_event("toast", message="森空岛 Token 为空，请先填写", color="#b04b44")
                return

            logger.info("[Bridge] 森空岛签到开始: server=%s token_len=%d", server, len(token))
            result = skendfield_sign.perform_sign(token=token, server=server)
            for line in skendfield_sign.format_sign_report(result):
                logger.info("[Bridge] 森空岛签到: %s", line)
            # 摘要用 toast 推回 QML
            status = str(result.get("status") or "")
            success = status in ("success", "already")
            head = "森空岛签到成功" if success else "森空岛签到失败"
            color = "#2f7d4a" if success else "#b04b44"
            award = result.get("award_name") or result.get("message") or ""
            msg = f"{head}：{award}" if award else head
            self.emit_event("toast", message=msg, color=color)
            # 写入签到状态供 DailyView 终末地卡片读取
            record_status = status if status in ("success", "already") else "failed"
            self._update_sign_status(
                "skendfield_sign", record_status, award,
            )
        except Exception as exc:
            logger.exception("[Bridge] 森空岛签到失败")
            self.emit_event("toast", message=f"森空岛签到失败: {exc}", color="#b04b44")
            self._update_sign_status("skendfield_sign", "failed", str(exc))

    def _run_wuwa_kujiequ_sign_backend(self):
        """库街区签到：token/did 从数据库（DPAPI 加密）读取，调 perform_sign。"""
        try:
            from ...services import credential_store
            from ...clients import wuwa_kujiequ

            profile = credential_store.load_wuwa_kujiequ_profile()
            if profile is None:
                self.emit_event(
                    "toast",
                    message="库街区 Token 或 DID 未配置，请先 ADB 抓取或手动填写",
                    color="#b04b44",
                )
                return

            logger.info(
                "[Bridge] 库街区签到开始: role=%s user_id=%s",
                profile.role_name, profile.user_id,
            )
            result = wuwa_kujiequ.perform_sign(profile)
            code = result.get("code")
            msg_text = str(result.get("message") or result.get("msg") or "").strip()
            already = "请勿重复签到" in msg_text
            success = code == 200 and (not msg_text or "请求成功" in msg_text or already)
            head = "库街区签到成功" if success else "库街区签到失败"
            color = "#2f7d4a" if success else "#b04b44"
            self.emit_event("toast", message=f"{head}：{msg_text}" if msg_text else head, color=color)
            self._update_sign_status(
                "wuwa_kujiequ",
                "success" if success else "failed",
                msg_text or ("请求成功" if success else ""),
            )
        except Exception as exc:
            logger.exception("[Bridge] 库街区签到失败")
            self.emit_event("toast", message=f"库街区签到失败: {exc}", color="#b04b44")
            self._update_sign_status("wuwa_kujiequ", "failed", str(exc))

    def _run_wuwa_kujiequ_adb_grab_backend(self):
        """通过 ADB + WebView CDP 从库街区 APP 抓取 token/did 并加密入库。

        前置条件：手机已启用 USB 调试并连接电脑、库街区 APP 已登录并进入"养成计算器"。
        token/did 以 DPAPI 密文写入 workflow_db 的 credentials 表，角色信息写回
        app_config.json；可被 _run_wuwa_kujiequ_sign_backend / fetch_* 直接使用。
        """
        from ...clients import wuwa_kujiequ_adb
        from ...clients.wuwa_kujiequ_adb import KujiequAdbError

        def _progress(msg: str) -> None:
            self.emit_event("toast", message=msg, color="#295d86")

        try:
            self.emit_event(
                "toast",
                message="开始 ADB 抓取库街区 Token...",
                color="#295d86",
            )
            capture, profile = wuwa_kujiequ_adb.grab_and_persist(progress_cb=_progress)
            self.emit_event(
                "toast",
                message=(
                    f"库街区 Token 抓取成功（已加密入库）：角色 {profile.role_name} "
                    f"(UID {profile.role_id})"
                ),
                color="#2f7d4a",
            )
            self._update_sign_status(
                "wuwa_kujiequ",
                "success",
                f"ADB 抓取成功：{profile.role_name}",
            )
        except KujiequAdbError as exc:
            logger.warning("[Bridge] 库街区 ADB 抓取失败: %s", exc)
            self.emit_event(
                "toast",
                message=f"库街区 ADB 抓取失败: {exc}",
                color="#b04b44",
            )
        except Exception as exc:
            logger.exception("[Bridge] 库街区 ADB 抓取异常")
            self.emit_event(
                "toast",
                message=f"库街区 ADB 抓取异常: {exc}",
                color="#b04b44",
            )

    def _run_tajiduo_sign_backend(self):
        """塔吉多签到：从 app_config.json 读 token/deviceid/uid/options，调 perform_sign。"""
        try:
            from ...settings import manager as cfg_manager
            from ...clients import tajiduo_sign

            cfg = cfg_manager.load(force=True)
            tj = cfg.get("tajiduo_sign") or {}
            token = str(tj.get("token") or "").strip()
            deviceid = str(tj.get("deviceid") or "").strip()
            uid = str(tj.get("uid") or "0").strip()
            if not token or not deviceid:
                self.emit_event(
                    "toast",
                    message="塔吉多 Token 或 deviceid 为空，请先填写",
                    color="#b04b44",
                )
                return

            logger.info("[Bridge] 塔吉多签到开始: uid=%s token_len=%d", uid, len(token))
            result = tajiduo_sign.perform_sign(
                token=token,
                deviceid=deviceid,
                uid=uid,
                do_bbs_signin=bool(tj.get("do_bbs_signin", True)),
                do_game_signin=bool(tj.get("do_game_signin", True)),
                do_browse=bool(tj.get("do_browse", False)),
                do_like=bool(tj.get("do_like", False)),
                do_share=bool(tj.get("do_share", False)),
                phone=str(tj.get("phone") or ""),
                password=str(tj.get("password") or ""),
            )
            for line in tajiduo_sign.format_sign_report(result):
                logger.info("[Bridge] 塔吉多签到: %s", line)
            # 塔吉多返回 {"bbs": {...}, "game": [...], ...}，逐项判断
            bbs = result.get("bbs") or {}
            game = result.get("game") or []
            bbs_ok = bbs.get("status") in ("success", "already")
            game_ok = bool(game) and all(
                g.get("status") in ("success", "already") for g in game
            )
            success = bbs_ok and game_ok
            head = "塔吉多签到成功" if success else "塔吉多签到失败"
            color = "#2f7d4a" if success else "#b04b44"
            detail = bbs.get("message") or ""
            self.emit_event(
                "toast",
                message=f"{head}：{detail}" if detail else head,
                color=color,
            )
            self._update_sign_status(
                "tajiduo_sign", "success" if success else "failed",
                detail or (game[0].get("message", "") if game else ""),
            )
        except Exception as exc:
            logger.exception("[Bridge] 塔吉多签到失败")
            self.emit_event("toast", message=f"塔吉多签到失败: {exc}", color="#b04b44")
            self._update_sign_status("tajiduo_sign", "failed", str(exc))

    # ── Sign status recording & DailyView integration ─────────────
    # 把签到结果（成功/失败/消息）写到 app_config.json 对应 sign 段的
    # last_sign_date / last_sign_status / last_sign_message 字段，供
    # DailyView 在每个游戏卡片里展示"签到"行；该行不参与 is_all_success 计算。

    def _update_sign_status(self, section_name: str, status: str, message: str = ""):
        """在 app_config.json 顶层 {section_name}.last_sign_* 写入本次签到结果。"""
        try:
            from ...settings.paths import APP_CONFIG_PATH
            try:
                cfg = json.loads(APP_CONFIG_PATH.read_text(encoding="utf-8"))
            except FileNotFoundError:
                cfg = {}
            section = cfg.get(section_name) or {}
            if not isinstance(section, dict):
                section = {}
            section["last_sign_date"] = time.strftime("%Y-%m-%d")
            section["last_sign_status"] = status
            section["last_sign_message"] = message or ""
            cfg[section_name] = section
            from ...settings.manager import atomic_write_json
            from ...settings.paths import APP_CONFIG_PATH
            atomic_write_json(APP_CONFIG_PATH, cfg)
            logger.info(
                "[Bridge] sign 状态已写入: section=%s status=%s",
                section_name, status,
            )
        except Exception as exc:
            logger.warning("[Bridge] 写 sign 状态失败: %s", exc)

    def _build_sign_task_for_game(self, game_key: str) -> Optional[Dict[str, Any]]:
        """根据 game_key 生成一条"签到"附属任务。

        返回 None 表示该游戏没有可用的签到源（不在显示范围内）。
        返回的 task 带 is_sign_task=True 标记，前端识别后展示为附属项，
        不参与 is_all_success / has_failed / progress_percent 计算。

        来源说明：
          - endfield / wuwa / nte：签到结果写到 app_config.json 的
            skendfield_sign / wuwa_kujiequ / tajiduo_sign.last_sign_*
          - genshin / honkai_sr / zzz：mihoyo_sign 一键三签，结果写到
            tracker（task_key='sign_米游社'），所以这里要从 tracker 反查
        """
        from ...settings.defaults import GAME_KEY_TO_SIGN_SECTION
        section_name = GAME_KEY_TO_SIGN_SECTION.get(game_key)
        if not section_name:
            return None

        today = time.strftime("%Y-%m-%d")

        # 米游社三件套：状态在 tracker 里
        if section_name == "mihoyo_sign":
            try:
                from ...services.daily_progress_tracker import get_tracker
                from ...settings.paths import WORKFLOW_DB_PATH
                from ...settings import resolve_game_key
                tracker = get_tracker(str(WORKFLOW_DB_PATH))
                snap = tracker.load_today()
                target_key = resolve_game_key(game_key)
                for rec in (snap.games or []):
                    if resolve_game_key(rec.game_key) != target_key:
                        continue
                    for t in rec.tasks:
                        if t.task_key.startswith("sign_"):
                            return {
                                "task_key": "__sign__",
                                "task_name": "签到",
                                "status": t.status,
                                "message": t.message or "已签到",
                                "is_sign_task": True,
                                "sign_source": "mihoyo_sign",
                            }
            except Exception as exc:
                logger.warning("[Bridge] 读取 mihoyo_sign tracker 状态失败: %s", exc)

            # tracker 里没有 → 兜底显示"尚未签到"
            try:
                from ...settings.paths import APP_CONFIG_PATH
                cfg = json.loads(APP_CONFIG_PATH.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError):
                cfg = {}
            ms = cfg.get("mihoyo_sign") or {}
            cookie = str(ms.get("cookie") or ms.get("stoken_cookie") or "").strip()
            if not cookie:
                return {
                    "task_key": "__sign__",
                    "task_name": "签到",
                    "status": "pending",
                    "message": "未配置米游社 cookie",
                    "is_sign_task": True,
                    "sign_source": "mihoyo_sign",
                }
            return {
                "task_key": "__sign__",
                "task_name": "签到",
                "status": "pending",
                "message": "今日尚未签到",
                "is_sign_task": True,
                "sign_source": "mihoyo_sign",
            }

        # 其余来源：状态在 app_config.json 的 last_sign_*
        try:
            from ...settings.paths import APP_CONFIG_PATH
            cfg = json.loads(APP_CONFIG_PATH.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return None
        section = cfg.get(section_name) or {}
        if not isinstance(section, dict):
            return None

        token = ""
        if section_name == "wuwa_kujiequ":
            # 密钥已迁到数据库加密存储，配置文件里不再有 token，需查库判断是否已配置
            from ...services import credential_store
            if credential_store.has_wuwa_credentials():
                token = "configured"
        else:
            token = str(section.get("token") or "").strip()
            if section_name == "tajiduo_sign" and not token:
                token = str(section.get("deviceid") or "").strip()

        last_date = str(section.get("last_sign_date") or "")
        last_status = str(section.get("last_sign_status") or "")
        last_message = str(section.get("last_sign_message") or "")

        if not token:
            status = "pending"
            message = "未配置 token，无法签到"
        elif last_date == today and last_status in ("success", "already"):
            status = "success"
            message = last_message or "今日已签到"
        elif last_date == today and last_status == "failed":
            status = "failed"
            message = last_message or "今日签到失败"
        elif last_date == today and last_status:
            status = last_status
            message = last_message or "今日签到状态未知"
        else:
            status = "pending"
            message = "今日尚未签到"

        return {
            "task_key": "__sign__",
            "task_name": "签到",
            "status": status,
            "message": message,
            "is_sign_task": True,
            "sign_source": section_name,
        }

    def _inject_sign_task_into_games(self, games_data: List[Dict[str, Any]]):
        """对每个 game，在其 tasks 列表前插入"签到"任务。

        已经在 tracker 里以 sign_ 前缀存在的签到任务会被统一标记为
        is_sign_task=True 并重命名为"签到"（避免和这里注入的重复）。
        这里只对没有签到任务的游戏补一条（pending / 今日尚未签到）。
        """
        from ...settings import resolve_game_key
        for game in games_data:
            gk = resolve_game_key(game.get("game_key", ""))

            # 1) 把 tracker 里残留的 sign_* 任务标记为附属项
            tasks = list(game.get("tasks") or [])
            upgraded = False
            for t in tasks:
                if (t.get("task_key") or "").startswith("sign_") and not t.get("is_sign_task"):
                    t["is_sign_task"] = True
                    t["task_key"] = "__sign__"
                    t["task_name"] = "签到"
                    upgraded = True
            if upgraded:
                game["tasks"] = tasks

            # 2) 已经存在 is_sign_task 的，不再注入
            if any(t.get("is_sign_task") for t in (game.get("tasks") or [])):
                continue

            sign_task = self._build_sign_task_for_game(gk)
            if sign_task is None:
                continue
            tasks = list(game.get("tasks") or [])
            tasks.insert(0, sign_task)
            game["tasks"] = tasks
