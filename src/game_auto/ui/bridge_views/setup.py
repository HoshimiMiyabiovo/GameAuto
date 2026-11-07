"""首次启动配置向导视图的槽。"""

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Slot

from .common import TONE_DANGER, TONE_INFO, TONE_SUCCESS, TONE_WARNING, logger


class SetupWizardView:
    """配置向导：路径探测、保存、跳过。"""
    # ── Setup wizard slots ─────────────────────────────────────────

    @Slot(result=bool)
    def isSetupCompleted(self) -> bool:
        from ...settings.manager import is_setup_completed
        return is_setup_completed()

    # 注意：QML 会使用这几个方法的返回值，@Slot 必须声明 result 类型，
    # 否则 Qt 按 void slot 处理，QML 拿到 undefined
    # （2026-09-29 事故：@Slot() 丢返回值 → 向导列表全空、保存后不跳转）
    @Slot(result=str)
    def getSetupItems(self) -> str:
        """返回需要配置的路径项清单（JSON 字符串）。"""
        import os
        from ...settings import APPS, QBITTORRENT
        items = []

        app_configs = [
            ("GenshinImpact", "原神", "launcher", "BetterGI 启动器", True),
            ("WutheringWaves", "鸣潮", "launcher", "ok-ww 启动器", True),
            ("ArknightsEndfield", "终末地", "launcher", "ok-ef 启动器", True),
            ("NevernessToEverness", "异环", "launcher", "ok-nte 启动器", True),
            ("InfinityNikki", "无限暖暖", "launcher", "whimbox 启动器 (run_one_dragon.bat)", True),
            ("BaiJing", "白荆回廊", "launcher", "白荆回廊游戏程序", False),
        ]
        for app_key, display_name, field, label, required in app_configs:
            cfg = APPS.get(app_key, {})
            current = ""
            if field == "launcher":
                current = cfg.get("launcher", "")
            items.append({
                "app_key": app_key,
                "display_name": display_name,
                "field": field,
                "label": label,
                "current_value": current,
                "required": required,
            })

        hsr_cfg = APPS.get("HonkaiStarRail", {})
        for i, opt in enumerate(hsr_cfg.get("launcher_options", [])):
            items.append({
                "app_key": "HonkaiStarRail",
                "display_name": "星穹铁道",
                "field": f"launcher_options[{i}].launcher",
                "label": opt.get("display_name", f"选项{i}"),
                "current_value": opt.get("launcher", ""),
                "required": True,
            })

        zzz_cfg = APPS.get("ZenlessZoneZero", {})
        for i, opt in enumerate(zzz_cfg.get("launcher_options", [])):
            items.append({
                "app_key": "ZenlessZoneZero",
                "display_name": "绝区零",
                "field": f"launcher_options[{i}].launcher",
                "label": opt.get("display_name", f"选项{i}"),
                "current_value": opt.get("launcher", ""),
                "required": True,
            })

        items.append({
            "app_key": "",
            "display_name": "下载工具",
            "field": "qbittorrent.path",
            "label": "qBittorrent（可选）",
            "current_value": QBITTORRENT.get("path", ""),
            "required": False,
        })

        # 独立/每月流程的 exe 路径：
        # - 崩铁/绝区零每月流程直接复用 apps 里该游戏的启动路径（apps[app_key].launcher），
        #   向导里编辑的是同一份数据，无默认值，未配置显示为空
        # - MiniMax 等独立程序走 flow_exe_paths（GUI 覆盖，无默认值）
        from ...settings import manager as _cm
        config = _cm.load()
        flow_overrides = config.get("flow_exe_paths") or {}
        for app_key, flow_label in (
            ("HonkaiStarRail", "崩铁游戏程序（每月商店流程）"),
            ("ZenlessZoneZero", "绝区零游戏程序（每月商店流程）"),
        ):
            items.append({
                "app_key": app_key,
                "display_name": "",
                "field": "launcher",
                "label": flow_label,
                "current_value": (APPS.get(app_key) or {}).get("launcher", ""),
                "required": False,
            })
        items.append({
            "app_key": "",
            "display_name": "",
            "field": "flow_exe.MINIMAX",
            "label": "MiniMax Code 程序（薅羊毛集合）",
            "current_value": flow_overrides.get("MINIMAX", ""),
            "required": False,
        })
        items.append({
            "app_key": "",
            "display_name": "",
            "field": "flow_exe.WORKBUDDY",
            "label": "WorkBuddy 程序（薅羊毛集合）",
            "current_value": flow_overrides.get("WORKBUDDY", ""),
            "required": False,
        })
        items.append({
            "app_key": "",
            "display_name": "",
            "field": "flow_exe.TRAE",
            "label": "Trae CN 程序（薅羊毛集合）",
            "current_value": flow_overrides.get("TRAE", ""),
            "required": False,
        })
        items.append({
            "app_key": "",
            "display_name": "",
            "field": "flow_exe.QODER",
            "label": "Qoder 启动器程序（薅羊毛集合）",
            "current_value": flow_overrides.get("QODER", ""),
            "required": False,
        })
        for flow_key, flow_label in (
            ("XM", "XM 脱机程序（XM 独立流程，可含 * 通配符）"),
            ("CJ", "重聚脱机程序（重聚独立流程，可含 * 通配符）"),
        ):
            items.append({
                "app_key": "",
                "display_name": "",
                "field": f"flow_exe.{flow_key}",
                "label": flow_label,
                "current_value": flow_overrides.get(flow_key, ""),
                "required": False,
            })

        # ── 分组 + 排序：让页面按「游戏启动器 → 独立/每月流程 → 其它工具」分区展示 ──
        # 之前 18 项混排且行 label 不带游戏名，用户分不清哪行是哪个游戏（2026-09-29 反馈）。
        def _group_of(it: Dict) -> str:
            label = it.get("label") or ""
            if "薅羊毛" in label:
                return "薅羊毛集合"
            if it.get("field", "").startswith("flow_exe.") or "每月任务" in label or "每月商店" in label:
                return "独立 / 每月流程"
            if it.get("app_key") == "" or "qBittorrent" in label:
                return "其它工具"
            return "游戏启动器 / 自动化工具"

        for it in items:
            it["group"] = _group_of(it)
        group_order = {"游戏启动器 / 自动化工具": 0, "独立 / 每月流程": 1,
                       "薅羊毛集合": 2, "其它工具": 3}
        items.sort(key=lambda it: (group_order.get(it["group"], 9),
                                   0 if it.get("required") else 1))

        return json.dumps(items, ensure_ascii=False)

    @Slot(str, result=bool)
    def checkFileExists(self, path: str) -> bool:
        import os
        return bool(path) and os.path.isfile(path)

    @Slot(str, result=bool)
    def saveSetupPaths(self, json_paths: str) -> bool:
        """保存路径配置到 app_config.json，标记 setup 已完成。"""
        try:
            items = json.loads(json_paths)
        except json.JSONDecodeError:
            logger.warning("[Bridge] saveSetupPaths 参数解析失败")
            return False

        from ...settings import manager as cm
        config = cm.load()
        apps = config.setdefault("apps", {})

        for item in items:
            app_key = item.get("app_key", "")
            field = item.get("field", "")
            value = item.get("value", "").strip()

            if not field:
                continue

            if field == "qbittorrent.path":
                config.setdefault("qbittorrent", {})["path"] = value
                continue

            if field.startswith("flow_exe."):
                # 独立/每月流程的 exe 路径（GUI 覆盖 flow_configs.json 默认值）
                flow_key = field.split(".", 1)[1]
                if flow_key:
                    config.setdefault("flow_exe_paths", {})[flow_key] = value
                continue

            if not app_key:
                continue

            app_cfg = apps.setdefault(app_key, {})
            discovery = app_cfg.setdefault("_discovery", {})

            if field == "launcher":
                # 同时写 apps 顶层覆盖与 _discovery 缓存：
                # 顶层覆盖保证 defaults 里有写死路径的游戏（白荆回廊）
                # 在 GUI 修改后也能生效；清空时两者同空，回落 defaults。
                app_cfg["launcher"] = value
                discovery["launcher"] = value
            elif field.startswith("launcher_options["):
                import re
                m = re.match(r"launcher_options\[(\d+)\]\.launcher", field)
                if m:
                    idx = int(m.group(1))
                    opts = app_cfg.setdefault("launcher_options", [])
                    while len(opts) <= idx:
                        opts.append({})
                    opts[idx]["launcher"] = value
                    opt_name = opts[idx].get("name", f"option_{idx}")
                    discovery.setdefault("launcher_options", {})
                    discovery["launcher_options"][opt_name] = {"launcher": value}

        config["setup_completed"] = True
        cm.save(config)

        from ...settings import refresh_runtime_config
        refresh_runtime_config(force=True)

        logger.info("[Bridge] 路径配置已保存，setup 已完成")
        self._request_toast("路径配置已保存", "#3b7d3b")
        return True

    @Slot()
    def skipSetup(self):
        """跳过向导，标记 setup 已完成。"""
        from ...settings.manager import mark_setup_completed
        mark_setup_completed()
        logger.info("[Bridge] 用户跳过路径配置向导")
        self.switchView("automation")

    # ── 第三方脚本版本检测 ─────────────────────────────────────────

    @Slot(result=str)
    def getScriptVersions(self) -> str:
        """脚本版本报告（JSON 字符串）。只读缓存不联网，供页面进入时立即渲染。"""
        try:
            from ...services.script_version_check import check_all
            rows, _toasts = check_all(allow_network=False)
            return json.dumps(rows, ensure_ascii=False)
        except Exception as exc:
            logger.warning("[Bridge] 读取脚本版本缓存失败: %s", exc)
            return "[]"

    @Slot()
    def checkScriptUpdates(self):
        """手动在线检查（后台线程），完成后经 script_versions 事件推送结果。"""
        if self._script_checking:
            return
        self._script_checking = True
        self.emit_event("script_versions", running=True,
                        rows=self._script_versions_json, toasts=[])
        threading.Thread(target=self._script_check_worker, daemon=True).start()

    def _auto_check_script_versions(self):
        """GUI 启动 5s 后的静默检查（bridge.__init__ 定时触发）。

        与手动检查同一worker：TTL 内直接用缓存不联网；本次真正取到数据且
        发现有更新时，由 script_versions 事件分支弹 toast（同版本只提醒一次）。
        """
        self.checkScriptUpdates()

    def _script_check_worker(self):
        """后台执行 check_all(force=True)；异常只记日志 + toast，不打断 GUI。"""
        try:
            from ...services.script_version_check import check_all
            rows, toasts = check_all(force=True)
            self.emit_event("script_versions",
                            rows=json.dumps(rows, ensure_ascii=False),
                            running=False, toasts=toasts)
        except Exception as exc:
            logger.warning("[Bridge] 脚本版本检查失败: %s", exc)
            self.emit_event("script_versions", running=False, toasts=[])
            self.emit_event("toast", message=f"脚本版本检查失败: {exc}",
                            color="#b04b44")
        finally:
            self._script_checking = False
