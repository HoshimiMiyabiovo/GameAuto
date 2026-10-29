from __future__ import annotations

import copy
import glob
import os
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import flet as ft

from ..settings.discovery import DISCOVERY_KEY
from ..settings import APP_ORDER, APPS, LOG_DIR, get_app_display_name, get_script_display_name, refresh_runtime_config
from ..settings import manager as cm
from ..runtime.ocr_click_tester import run_xm_seer_test
from ..runtime.task_batch_runner import TaskBatchHooks, run_task_batch
from .shared import CONFIG_FILE, FLET_EXAMPLES_DIR


FT_ICONS = getattr(ft, "Icons", None) or getattr(ft, "icons")
DISCOVERY_MONITOR_LABELS = {
    "bettergi_log_monitor": "BetterGI 日志目录",
    "ok_script_log_monitor": "OK 脚本日志目录",
    "ok_ww_log_monitor": "OK 鸣潮日志目录",
    "whimbox_log_monitor": "Whimbox 日志目录",
}


class FletAutomationMixin:
    XM_SEER_PATH_PATTERN = ""  # 从 app_config.json 的 XMFlow.launcher 读取，不写死

    def _build_automation_view(self) -> ft.Container:
        self.launch_table_rows = ft.Column(spacing=0, scroll=ft.ScrollMode.AUTO)
        selection_actions = ft.Row(
            controls=[
                ft.TextButton("全选", on_click=self._select_all),
                ft.TextButton("清空", on_click=self._clear_all),
                ft.TextButton("保存启动设置", on_click=self._save_launch_preferences),
                ft.TextButton("重新读取配置", on_click=self._reload_config),
            ],
            spacing=4,
            wrap=True,
        )
        selection_panel = self._panel(
            ft.Column(
                controls=[
                    ft.Row(
                        controls=[
                            ft.Column(
                                controls=[
                                    ft.Text("任务选择", size=20, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"]),
                                    ft.Text(
                                        "脚本路径和执行顺序写入 app_config.json；勾选状态与默认启动方案写入 workflow.sqlite3。XM 和重聚现在作为独立任务参与排序。",
                                        size=13,
                                        color=self.PALETTE["muted"],
                                    ),
                                ],
                                spacing=4,
                                expand=True,
                            ),
                            selection_actions,
                        ],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        vertical_alignment=ft.CrossAxisAlignment.START,
                    ),
                    self._build_launch_table_shell(),
                ],
                spacing=10,
            ),
        )

        self.selection_summary = ft.Text("还没有选择任务", size=26, weight=ft.FontWeight.BOLD, color=self.PALETTE["accent"])
        self.status_chip_text = ft.Text("待命中", size=12, weight=ft.FontWeight.BOLD, color=self.PALETTE["accent"])
        self.status_chip = ft.Container(
            bgcolor=self.PALETTE["accent_soft"],
            border_radius=999,
            padding=ft.Padding(12, 6, 12, 6),
            content=self.status_chip_text,
        )
        self.status_title = ft.Text("准备就绪", size=18, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"])
        self.status_body = ft.Text("先勾选任务，再点击开始运行。", size=13, color=self.PALETTE["muted"])
        self.progress_ring = ft.ProgressRing(width=18, height=18, stroke_width=2, visible=False)

        self.start_button = ft.ElevatedButton(
            "开始运行",
            icon=FT_ICONS.PLAY_ARROW_ROUNDED,
            on_click=self._start_run,
        )
        self.stop_button = ft.OutlinedButton(
            "停止",
            icon=FT_ICONS.STOP_ROUNDED,
            disabled=True,
            on_click=self._request_stop,
        )

        status_panel = self._panel(
            ft.Column(
                controls=[
                    ft.Text("开始运行", size=20, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"]),
                    self.selection_summary,
                    ft.Text("会按任务表当前顺序逐个执行。点击每行的上移 / 下移会立即写回 app_config.json。", size=13, color=self.PALETTE["muted"]),
                    ft.Row([self.start_button, self.stop_button], spacing=10),
                    ft.Container(
                        bgcolor=self.PALETTE["info_soft"],
                        border_radius=16,
                        padding=ft.Padding(14, 14, 14, 14),
                        content=ft.Column(
                            controls=[
                                ft.Row([self.status_chip, self.progress_ring], spacing=8),
                                self.status_title,
                                self.status_body,
                            ],
                            spacing=8,
                        ),
                    ),
                ],
                spacing=12,
            )
        )

        self.queue_list = ft.Column(spacing=8)
        queue_panel = self._panel(
            ft.Column(
                controls=[
                    ft.Text("本轮顺序", size=20, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"]),
                    self.queue_list,
                ],
                spacing=12,
            )
        )

        self.launch_path_list = ft.Column(spacing=8, scroll=ft.ScrollMode.AUTO)
        path_panel = self._panel(
            ft.Column(
                controls=[
                    ft.Row(
                        controls=[
                            ft.Column(
                                controls=[
                                    ft.Text("脚本路径", size=20, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"]),
                                    ft.Text(
                                        "这里只改 app_config.json 里的启动路径。独立 XM / 重聚任务不需要额外路径，所以不会出现在这里。",
                                        size=13,
                                        color=self.PALETTE["muted"],
                                    ),
                                ],
                                spacing=4,
                                expand=True,
                            ),
                            ft.Row(
                                controls=[
                                    ft.TextButton("保存路径配置", on_click=self._save_launcher_paths),
                                    ft.TextButton("打开配置文件", on_click=self._open_config_file),
                                ],
                                spacing=4,
                                wrap=True,
                            ),
                        ],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        vertical_alignment=ft.CrossAxisAlignment.START,
                    ),
                    ft.Container(
                        height=250,
                        bgcolor=self.PALETTE["card"],
                        border=self._border_all(1, self.PALETTE["line"]),
                        border_radius=16,
                        padding=ft.Padding(10, 10, 10, 10),
                        content=self.launch_path_list,
                    ),
                ],
                spacing=10,
            )
        )

        self.discovery_cache_list = ft.Column(spacing=8, scroll=ft.ScrollMode.AUTO)
        discovery_panel = self._panel(
            ft.Column(
                controls=[
                    ft.Row(
                        controls=[
                            ft.Column(
                                controls=[
                                    ft.Text("自动识别缓存", size=20, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"]),
                                    ft.Text(
                                        "这里展示本机已经识别过的启动器、进程名和日志目录。清空缓存不会删除你手动填写的路径。",
                                        size=13,
                                        color=self.PALETTE["muted"],
                                    ),
                                ],
                                spacing=4,
                                expand=True,
                            ),
                            ft.TextButton("全部清空缓存", data="__all__", on_click=self._clear_discovery_cache),
                        ],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        vertical_alignment=ft.CrossAxisAlignment.START,
                    ),
                    ft.Container(
                        height=240,
                        bgcolor=self.PALETTE["card"],
                        border=self._border_all(1, self.PALETTE["line"]),
                        border_radius=16,
                        padding=ft.Padding(10, 10, 10, 10),
                        content=self.discovery_cache_list,
                    ),
                ],
                spacing=10,
            )
        )

        self.tools_toggle = ft.TextButton("展开", on_click=self._toggle_tools)
        self.tools_body = ft.Container(
            visible=False,
            padding=ft.Padding(0, 10, 0, 0),
            content=ft.Row(
                wrap=True,
                spacing=8,
                run_spacing=8,
                controls=[
                    ft.OutlinedButton("打开配置文件", icon=FT_ICONS.EDIT_DOCUMENT, on_click=self._open_config_file),
                    ft.OutlinedButton("打开日志目录", icon=FT_ICONS.FOLDER_OPEN, on_click=self._open_logs_folder),
                    ft.OutlinedButton("打开 Flet 示例", icon=FT_ICONS.SOURCE, on_click=self._open_examples_folder),
                    ft.OutlinedButton("测试识别点击", icon=FT_ICONS.PLAY_CIRCLE_OUTLINE, on_click=self._start_test_click),
                    ft.OutlinedButton("测试 XM 流程", icon=FT_ICONS.PLAY_CIRCLE_OUTLINE, on_click=self._start_test_xm),
                    ft.OutlinedButton("测试重聚流程", icon=FT_ICONS.PLAY_CIRCLE_OUTLINE, on_click=self._start_test_cj),
                ],
            ),
        )
        tools_panel = self._panel(
            ft.Column(
                controls=[
                    ft.Row(
                        controls=[
                            ft.Text("工具箱", size=20, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"]),
                            self.tools_toggle,
                        ],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    ),
                    ft.Text("日常使用时可以不用展开。这里放的是测试和排查工具。", size=13, color=self.PALETTE["muted"]),
                    self.tools_body,
                ],
                spacing=6,
            )
        )

        self.result_box = ft.TextField(
            value="运行结束后，这里会汇总本轮结果。",
            multiline=True,
            min_lines=8,
            max_lines=10,
            read_only=True,
            bgcolor=self.PALETTE["panel_alt"],
            border_color=self.PALETTE["line"],
        )
        result_panel = self._panel(
            ft.Column(
                controls=[
                    ft.Text("本轮结果", size=20, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"]),
                    self.result_box,
                ],
                spacing=10,
            )
        )

        right_sidebar = ft.Column(
            controls=[status_panel, queue_panel, tools_panel, result_panel],
            spacing=10,
            expand=2,
        )

        self.log_box = ft.TextField(
            value="",
            multiline=True,
            min_lines=8,
            max_lines=10,
            read_only=True,
            bgcolor=self.PALETTE["log_bg"],
            color=self.PALETTE["log_fg"],
            border_color=self.PALETTE["line"],
        )
        self.log_toggle = ft.TextButton("展开日志", on_click=self._toggle_log)
        self.log_container = ft.Container(
            visible=False,
            padding=ft.Padding(0, 10, 0, 0),
            content=self.log_box,
        )
        log_panel = self._panel(
            ft.Column(
                controls=[
                    ft.Row(
                        controls=[
                            ft.Column(
                                controls=[
                                    ft.Text("运行日志", size=20, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"]),
                                    ft.Text("平时可以收起，只有排查问题时才需要展开。", size=13, color=self.PALETTE["muted"]),
                                ],
                                spacing=4,
                                expand=True,
                            ),
                            self.log_toggle,
                        ],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        vertical_alignment=ft.CrossAxisAlignment.START,
                    ),
                    self.log_container,
                ],
                spacing=6,
            )
        )

        left_column = ft.Column(
            controls=[selection_panel, path_panel, discovery_panel],
            spacing=12,
            expand=3,
        )

        return ft.Container(
            padding=ft.Padding(18, 16, 18, 16),
            content=ft.Column(
                controls=[
                    ft.Row([left_column, right_sidebar], spacing=12, vertical_alignment=ft.CrossAxisAlignment.START),
                    log_panel,
                ],
                spacing=12,
            ),
        )

    def _build_launch_table_shell(self) -> ft.Container:
        return ft.Container(
            bgcolor=self.PALETTE["card"],
            border=self._border_all(1, self.PALETTE["line"]),
            border_radius=16,
            content=ft.Column(
                controls=[
                    self._build_launch_table_header(),
                    ft.Container(height=1, bgcolor=self.PALETTE["line"]),
                    ft.Container(
                        height=360,
                        padding=ft.Padding(8, 6, 8, 8),
                        content=self.launch_table_rows,
                    ),
                ],
                spacing=0,
            ),
        )

    def _build_launch_table_header(self) -> ft.Container:
        return ft.Container(
            bgcolor=self.PALETTE["panel_alt"],
            padding=ft.Padding(14, 12, 14, 12),
            content=ft.Row(
                controls=[
                    ft.Container(width=92, content=ft.Text("顺序", size=12, weight=ft.FontWeight.BOLD, color=self.PALETTE["muted"])),
                    ft.Container(width=72, content=ft.Text("启用", size=12, weight=ft.FontWeight.BOLD, color=self.PALETTE["muted"])),
                    ft.Container(width=170, content=ft.Text("任务", size=12, weight=ft.FontWeight.BOLD, color=self.PALETTE["muted"])),
                    ft.Container(width=240, content=ft.Text("启动方式", size=12, weight=ft.FontWeight.BOLD, color=self.PALETTE["muted"])),
                    ft.Container(width=140, content=ft.Text("类型", size=12, weight=ft.FontWeight.BOLD, color=self.PALETTE["muted"])),
                    ft.Container(expand=True, content=ft.Text("说明 / 当前设置", size=12, weight=ft.FontWeight.BOLD, color=self.PALETTE["muted"])),
                ],
                spacing=12,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )

    def _load_apps_config(self) -> Tuple[List[str], Dict[str, Dict]]:
        try:
            runtime_config = refresh_runtime_config(force=True)
            app_order = runtime_config.get("app_order") or APP_ORDER
            apps_data = runtime_config.get("apps") or APPS
        except Exception:
            app_order = APP_ORDER
            apps_data = APPS
        return list(app_order), copy.deepcopy(apps_data)

    def _sync_runtime_apps_config(self):
        refresh_runtime_config(force=True)

    def _friendly_note(self, app_key: str) -> str:
        app_cfg = self.apps_data.get(app_key, {}) if isinstance(getattr(self, "apps_data", None), dict) else {}
        if app_cfg.get("friendly_note"):
            return str(app_cfg.get("friendly_note"))
        notes = {
            "GenshinImpact": "原神常规自动流程。XM 已改成独立任务，可单独勾选并排任意位置。",
            "HonkaiStarRail": "星铁支持多套脚本方案，选你熟悉的即可。",
            "ZenlessZoneZero": "绝区零支持不同启动方式。",
            "WutheringWaves": "鸣潮使用 ok-ww，一键启动并监控。",
            "ArknightsEndfield": "终末地使用 ok-ef，直接拉起脚本。",
            "NevernessToEverness": "异环会先处理启动器，再接入 ok-nte。",
            "InfinityNikki": "无限暖暖常规任务。重聚已改成独立任务，可单独勾选并排任意位置。",
        }
        return notes.get(app_key, "按当前配置自动启动。")

    def _option_map(self, app_cfg: Dict) -> Dict[str, str]:
        return {
            opt["name"]: opt.get("display_name", opt["name"])
            for opt in app_cfg.get("launcher_options", [])
        }

    def _default_script_choice(self, app_cfg: Dict) -> Optional[str]:
        options = app_cfg.get("launcher_options", [])
        if not options:
            return None
        first = options[0]
        return first.get("name")

    def _resolve_script_display_name(self, app_key: str, app_cfg: Dict, script_choice: Optional[str] = None) -> str:
        for option in app_cfg.get("launcher_options", []):
            if option.get("name") == script_choice:
                return option.get("display_name", option.get("name", "默认方案"))
        if app_cfg.get("launcher_options"):
            default_choice = self._default_script_choice(app_cfg)
            if default_choice and default_choice != script_choice:
                return self._resolve_script_display_name(app_key, app_cfg, default_choice)
        return app_cfg.get("script_name") or get_script_display_name(app_key, script_choice)

    def _saved_script_choice(self, app_key: str, app_cfg: Dict) -> Optional[str]:
        option_map = self._option_map(app_cfg)
        saved = str(self.launch_preferences.get(app_key, {}).get("script_choice", "") or "")
        if saved and saved in option_map:
            return saved
        return self._default_script_choice(app_cfg)

    def _current_script_choice(self, app_key: str) -> Optional[str]:
        refs = self.launch_row_refs.get(app_key, {})
        dropdown = refs.get("dropdown")
        if dropdown is None:
            return None
        return getattr(dropdown, "value", None)

    def _current_helper_enabled(self, app_key: str) -> bool:
        return False

    def _helper_summary_text(self, app_key: str) -> str:
        app_cfg = self.apps_data.get(app_key, {})
        return "类型：独立流程" if app_cfg.get("virtual_task") else "类型：常规任务"

    def _capture_launch_ui_state(self) -> Dict[str, Dict[str, object]]:
        snapshot: Dict[str, Dict[str, object]] = {}
        for app_key, refs in self.launch_row_refs.items():
            snapshot[app_key] = {
                "enabled": bool(refs.get("checkbox") and refs["checkbox"].value),
                "script_choice": self._current_script_choice(app_key),
            }
        return snapshot

    def _restore_launch_ui_state(self, snapshot: Dict[str, Dict[str, object]]):
        for app_key, state in snapshot.items():
            refs = self.launch_row_refs.get(app_key)
            if not refs:
                continue
            checkbox = refs.get("checkbox")
            if checkbox is not None:
                checkbox.value = bool(state.get("enabled", False))
            dropdown = refs.get("dropdown")
            if dropdown is not None:
                script_choice = state.get("script_choice")
                if script_choice:
                    dropdown.value = str(script_choice)
            self._refresh_launch_row(app_key)

    def _rebuild_launch_rows(self, snapshot: Optional[Dict[str, Dict[str, object]]] = None):
        if self.launch_table_rows is None:
            return

        self.launch_row_refs.clear()
        self.launch_table_rows.controls.clear()
        visible_apps = [app for app in self.app_order if app in self.apps_data]
        for app_key in visible_apps:
            self.launch_table_rows.controls.append(self._build_launch_row(app_key, self.apps_data[app_key]))

        if snapshot:
            self._restore_launch_ui_state(snapshot)

        self._rebuild_launch_path_list()
        self._rebuild_discovery_cache_list()

    def _persist_app_order(self, show_toast: bool = True):
        config = cm.load(force=True)
        config["app_order"] = list(self.app_order)
        cm.save(config)
        self._sync_runtime_apps_config()
        if show_toast:
            self._toast("执行顺序已保存到 app_config.json。", self.PALETTE["accent"])

    def _move_launch_order(self, e):
        if self.is_running:
            self._toast("任务运行中，暂时不能调整顺序。", self.PALETTE["warning"])
            return

        payload = str(e.control.data or "")
        app_key, _, delta_text = payload.partition("|")
        if app_key not in self.app_order:
            return
        try:
            delta = int(delta_text or "0")
        except ValueError:
            return

        index = self.app_order.index(app_key)
        new_index = index + delta
        if new_index < 0 or new_index >= len(self.app_order):
            return

        snapshot = self._capture_launch_ui_state()
        self.app_order[index], self.app_order[new_index] = self.app_order[new_index], self.app_order[index]
        self._persist_app_order(show_toast=False)
        self._rebuild_launch_rows(snapshot=snapshot)
        self._refresh_summary_and_queue()
        self.page.update()
        self._toast("执行顺序已更新并保存。", self.PALETTE["accent"])

    def _selected_targets(self) -> List[Tuple[str, Optional[str]]]:
        targets: List[Tuple[str, Optional[str]]] = []
        for app_key in self.app_order:
            refs = self.launch_row_refs.get(app_key)
            if not refs:
                continue
            checkbox = refs["checkbox"]
            if checkbox.value:
                targets.append((app_key, self._current_script_choice(app_key)))
        return targets

    def _reload_config(self, e=None, show_toast: bool = True):
        if self.is_running:
            self._toast("任务运行中，暂时不能重读配置。", self.PALETTE["warning"])
            return

        self.apps_data = {}
        self.app_order = []
        self.launch_preferences = self.preference_db.get_all_app_launch_preferences()
        self.launch_row_refs.clear()
        self.launch_path_input_refs.clear()
        self.launch_table_rows.controls.clear()
        if self.launch_path_list is not None:
            self.launch_path_list.controls.clear()
        if self.discovery_cache_list is not None:
            self.discovery_cache_list.controls.clear()

        self.app_order, self.apps_data = self._load_apps_config()
        self._sync_runtime_apps_config()
        self._rebuild_launch_rows()

        self._refresh_summary_and_queue()
        if show_toast:
            self._toast("已重新读取配置，并套用数据库里的启动设置。", self.PALETTE["accent"])
        self.page.update()

    def _launcher_path_ref_key(self, option_name: Optional[str]) -> str:
        return str(option_name or "").strip() or "__default__"

    def _launcher_path_entries(self) -> List[Dict[str, str]]:
        entries: List[Dict[str, str]] = []
        for app_key in self.app_order:
            app_cfg = self.apps_data.get(app_key)
            if not isinstance(app_cfg, dict) or app_cfg.get("virtual_task"):
                continue
            app_label = str(app_cfg.get("display_name") or app_key)
            launcher_options = app_cfg.get("launcher_options") or []
            if isinstance(launcher_options, list) and launcher_options:
                for option in launcher_options:
                    if not isinstance(option, dict):
                        continue
                    option_name = str(option.get("name") or "").strip()
                    option_label = str(option.get("display_name") or option_name or "启动方案").strip()
                    entries.append(
                        {
                            "app_key": app_key,
                            "option_key": self._launcher_path_ref_key(option_name),
                            "title": f"{app_label} / {option_label}",
                            "path": str(option.get("launcher") or ""),
                        }
                    )
            else:
                entries.append(
                    {
                        "app_key": app_key,
                        "option_key": self._launcher_path_ref_key(None),
                        "title": app_label,
                        "path": str(app_cfg.get("launcher") or ""),
                    }
                )
        return entries

    def _rebuild_launch_path_list(self):
        if self.launch_path_list is None:
            return
        self.launch_path_input_refs.clear()
        controls: List[ft.Control] = []
        for entry in self._launcher_path_entries():
            path_input = ft.TextField(
                value=entry["path"],
                hint_text="脚本或启动器路径",
                border_color=self.PALETTE["line"],
                filled=True,
                fill_color=self.PALETTE["panel_alt"],
                expand=True,
            )
            browse_button = ft.OutlinedButton(
                "浏览",
                icon=FT_ICONS.FOLDER_OPEN,
                data=f"{entry['app_key']}|{entry['option_key']}",
                on_click=self._pick_launch_path,
            )
            self.launch_path_input_refs.setdefault(entry["app_key"], {})[entry["option_key"]] = {
                "input": path_input,
                "browse": browse_button,
            }
            controls.append(
                ft.Container(
                    bgcolor=self.PALETTE["panel_alt"],
                    border_radius=12,
                    padding=ft.Padding(10, 8, 10, 8),
                    content=ft.Column(
                        controls=[
                            ft.Text(entry["title"], size=12, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"]),
                            ft.Row([path_input, browse_button], spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER),
                        ],
                        spacing=4,
                    ),
                )
            )
        self.launch_path_list.controls = controls or [self._build_pool_empty_state("当前没有可编辑的脚本路径配置。")]

    def _discovery_entries(self) -> List[Dict[str, object]]:
        entries: List[Dict[str, object]] = []
        for app_key in self.app_order:
            app_cfg = self.apps_data.get(app_key)
            if not isinstance(app_cfg, dict):
                continue
            discovery = app_cfg.get(DISCOVERY_KEY)
            if not isinstance(discovery, dict) or not discovery:
                continue
            lines = self._discovery_summary_lines(app_cfg, discovery)
            entries.append(
                {
                    "app_key": app_key,
                    "title": str(app_cfg.get("display_name") or get_app_display_name(app_key)),
                    "lines": lines or ["已创建缓存节点，但当前还没有可复用的自动识别结果。"],
                }
            )
        return entries

    def _discovery_summary_lines(self, app_cfg: Dict, discovery: Dict) -> List[str]:
        lines: List[str] = []

        launcher_path = str(discovery.get("launcher") or "").strip()
        if launcher_path:
            lines.append(f"默认启动器：{launcher_path}")

        script_parts: List[str] = []
        script_process_name = str(discovery.get("script_process_name") or "").strip()
        script_exe_path = str(discovery.get("script_exe_path") or "").strip()
        if script_process_name:
            script_parts.append(f"进程 {script_process_name}")
        if script_exe_path:
            script_parts.append(f"路径 {script_exe_path}")
        if script_parts:
            lines.append(f"默认脚本：{'；'.join(script_parts)}")

        game_parts: List[str] = []
        game_process_name = str(discovery.get("game_process_name") or "").strip()
        game_exe_path = str(discovery.get("game_exe_path") or "").strip()
        if game_process_name:
            game_parts.append(f"进程 {game_process_name}")
        if game_exe_path:
            game_parts.append(f"路径 {game_exe_path}")
        if game_parts:
            lines.append(f"游戏进程：{'；'.join(game_parts)}")

        option_labels = self._option_map(app_cfg)
        launcher_options = discovery.get("launcher_options")
        if isinstance(launcher_options, dict):
            for option_name, option_cache in launcher_options.items():
                if not isinstance(option_cache, dict) or not option_cache:
                    continue
                option_parts: List[str] = []
                option_launcher = str(option_cache.get("launcher") or "").strip()
                option_process_name = str(option_cache.get("script_process_name") or "").strip()
                option_exe_path = str(option_cache.get("script_exe_path") or "").strip()
                if option_launcher:
                    option_parts.append(f"启动器 {option_launcher}")
                if option_process_name:
                    option_parts.append(f"进程 {option_process_name}")
                if option_exe_path:
                    option_parts.append(f"路径 {option_exe_path}")
                if option_parts:
                    option_label = option_labels.get(option_name, option_name)
                    lines.append(f"{option_label}：{'；'.join(option_parts)}")

        monitors = discovery.get("monitors")
        if isinstance(monitors, dict):
            for monitor_key, monitor_cache in monitors.items():
                if not isinstance(monitor_cache, dict):
                    continue
                log_dir = str(monitor_cache.get("log_dir") or "").strip()
                if log_dir:
                    lines.append(f"{DISCOVERY_MONITOR_LABELS.get(monitor_key, monitor_key)}：{log_dir}")

        return lines

    def _rebuild_discovery_cache_list(self):
        if self.discovery_cache_list is None:
            return

        controls: List[ft.Control] = []
        for entry in self._discovery_entries():
            line_controls = [
                ft.Text(line, size=12, color=self.PALETTE["muted"], selectable=True)
                for line in entry["lines"]
            ]
            controls.append(
                ft.Container(
                    bgcolor=self.PALETTE["panel_alt"],
                    border_radius=12,
                    padding=ft.Padding(10, 8, 10, 8),
                    content=ft.Column(
                        controls=[
                            ft.Row(
                                controls=[
                                    ft.Text(entry["title"], size=13, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"], expand=True),
                                    ft.TextButton("清空该项", data=entry["app_key"], on_click=self._clear_discovery_cache),
                                ],
                                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                                vertical_alignment=ft.CrossAxisAlignment.START,
                            ),
                            *line_controls,
                        ],
                        spacing=4,
                    ),
                )
            )

        self.discovery_cache_list.controls = controls or [
            self._build_pool_empty_state("当前还没有自动识别缓存。首次在这台设备上成功识别后，这里会显示可复用的结果。")
        ]

    def _clear_discovery_cache(self, e=None):
        if self.is_running:
            self._toast("任务运行中，暂时不能清空自动识别缓存。", self.PALETTE["warning"])
            return

        target = str(getattr(getattr(e, "control", None), "data", "") or "")
        config = cm.load(force=True)
        apps = config.get("apps")
        if not isinstance(apps, dict):
            self._toast("当前配置文件中没有 apps 配置。", self.PALETTE["danger"])
            return

        cleared_apps: List[str] = []
        if target == "__all__":
            for app_key, app_cfg in apps.items():
                if not isinstance(app_cfg, dict):
                    continue
                if app_cfg.pop(DISCOVERY_KEY, None) is not None:
                    cleared_apps.append(get_app_display_name(app_key))
        else:
            app_cfg = apps.get(target)
            if isinstance(app_cfg, dict) and app_cfg.pop(DISCOVERY_KEY, None) is not None:
                cleared_apps.append(get_app_display_name(target))

        if not cleared_apps:
            self._toast("当前没有可清空的自动识别缓存。", self.PALETTE["muted"])
            return

        config["apps"] = apps
        cm.save(config)
        self.controller.logger.info("[GUI] 用户已清空自动识别缓存: %s", "、".join(cleared_apps))
        self._reload_config(show_toast=False)
        if target == "__all__":
            self._toast(f"已清空 {len(cleared_apps)} 个应用的自动识别缓存。", self.PALETTE["accent"])
        else:
            self._toast(f"已清空 {cleared_apps[0]} 的自动识别缓存。", self.PALETTE["accent"])

    async def _pick_launch_path(self, e):
        try:
            app_key, _, option_key = str(e.control.data or "").partition("|")
            refs = self.launch_path_input_refs.get(app_key, {}).get(option_key or "__default__")
            if not refs:
                return
            files = await ft.FilePicker().pick_files(allow_multiple=False)
            if not files:
                return
            selected_path = getattr(files[0], "path", "") or getattr(files[0], "name", "")
            input_control = refs.get("input")
            if input_control is not None and selected_path:
                input_control.value = selected_path
                self.page.update()
        except Exception as exc:
            self._toast(f"选择脚本路径失败：{str(exc)}", self.PALETTE["danger"])

    def _save_launcher_paths(self, e=None):
        if self.is_running:
            self._toast("任务运行中，暂时不能保存脚本路径。", self.PALETTE["warning"])
            return
        config = cm.load(force=True)
        apps = config.get("apps")
        if not isinstance(apps, dict):
            self._toast("当前配置文件中没有 apps 配置。", self.PALETTE["danger"])
            return

        changed_count = 0
        for app_key, option_refs in self.launch_path_input_refs.items():
            app_cfg = apps.get(app_key)
            if not isinstance(app_cfg, dict):
                continue
            launcher_options = app_cfg.get("launcher_options") or []
            if isinstance(launcher_options, list) and launcher_options:
                new_options = []
                for option in launcher_options:
                    option_data = dict(option) if isinstance(option, dict) else {}
                    option_key = self._launcher_path_ref_key(option_data.get("name"))
                    input_control = option_refs.get(option_key, {}).get("input")
                    if input_control is not None:
                        new_path = str(input_control.value or "").strip()
                        if new_path != str(option_data.get("launcher") or ""):
                            changed_count += 1
                        option_data["launcher"] = new_path
                    new_options.append(option_data)
                new_app_cfg = dict(app_cfg)
                new_app_cfg["launcher_options"] = new_options
                apps[app_key] = new_app_cfg
            else:
                input_control = option_refs.get("__default__", {}).get("input")
                if input_control is None:
                    continue
                new_path = str(input_control.value or "").strip()
                if new_path != str(app_cfg.get("launcher") or ""):
                    changed_count += 1
                new_app_cfg = dict(app_cfg)
                new_app_cfg["launcher"] = new_path
                apps[app_key] = new_app_cfg

        config["apps"] = apps
        cm.save(config)
        self.apps_data = config.get("apps", {}) if isinstance(config.get("apps"), dict) else {}
        self._sync_runtime_apps_config()
        self.app_order, self.apps_data = self._load_apps_config()
        self._rebuild_launch_path_list()
        self._rebuild_discovery_cache_list()
        self.page.update()
        self._toast(
            "脚本路径已保存到 app_config.json。" if changed_count else "路径未变化，已与当前配置保持一致。",
            self.PALETTE["accent"],
        )

    def _build_launch_row(self, app_key: str, app_cfg: Dict) -> ft.Container:
        saved_pref = self.launch_preferences.get(app_key, {})
        enabled = bool(saved_pref.get("enabled", 0))
        current_script_choice = self._saved_script_choice(app_key, app_cfg)
        checkbox = ft.Checkbox(
            label="",
            value=enabled,
            data=app_key,
            on_change=self._on_launch_setting_change,
        )
        name_text = ft.Text(app_cfg.get("display_name", app_key), size=14, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"])
        current_text = ft.Text("", size=12, color=self.PALETTE["muted"])
        note_text = ft.Text(self._friendly_note(app_key), size=12, color=self.PALETTE["muted"])
        move_up = ft.TextButton("上移", data=f"{app_key}|-1", on_click=self._move_launch_order)
        move_down = ft.TextButton("下移", data=f"{app_key}|1", on_click=self._move_launch_order)
        move_control = ft.Row([move_up, move_down], spacing=0)

        dropdown = None
        if app_cfg.get("launcher_options"):
            option_map = self._option_map(app_cfg)
            dropdown = ft.Dropdown(
                value=current_script_choice,
                options=[ft.dropdown.Option(key=name, text=label) for name, label in option_map.items()],
                data=app_key,
                on_select=self._on_launch_setting_change,
                border_color=self.PALETTE["line"],
                filled=True,
                fill_color=self.PALETTE["panel_alt"],
                dense=True,
                content_padding=10,
            )
            launch_control: ft.Control = dropdown
        else:
            launch_control = ft.Text(
                self._resolve_script_display_name(app_key, app_cfg, None),
                size=13,
                color=self.PALETTE["text"],
            )

        type_control: ft.Control = ft.Text(
            "独立流程" if app_cfg.get("virtual_task") else "常规任务",
            size=13,
            color=self.PALETTE["muted"],
        )

        row = ft.Container(
            border_radius=12,
            padding=ft.Padding(12, 10, 12, 10),
            content=ft.Row(
                controls=[
                    ft.Container(width=92, content=move_control),
                    ft.Container(width=72, content=checkbox),
                    ft.Container(width=170, content=name_text),
                    ft.Container(width=240, content=launch_control),
                    ft.Container(width=140, content=type_control),
                    ft.Container(
                        expand=True,
                        content=ft.Column(
                            controls=[current_text, note_text],
                            spacing=2,
                        ),
                    ),
                ],
                spacing=12,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )

        self.launch_row_refs[app_key] = {
            "container": row,
            "checkbox": checkbox,
            "current_text": current_text,
            "move_up": move_up,
            "move_down": move_down,
        }
        if dropdown is not None:
            self.launch_row_refs[app_key]["dropdown"] = dropdown
        self._refresh_launch_row(app_key)
        return row

    def _refresh_launch_row(self, app_key: str):
        refs = self.launch_row_refs[app_key]
        selected = bool(refs["checkbox"].value)
        row = refs["container"]
        app_cfg = self.apps_data.get(app_key, {})
        script_choice = self._current_script_choice(app_key)
        script_label = self._resolve_script_display_name(app_key, app_cfg, script_choice)
        row.bgcolor = self.PALETTE["card_active"] if selected else self.PALETTE["panel"]
        refs["current_text"].value = (
            f"本轮会执行：{script_label}；{self._helper_summary_text(app_key)}"
            if selected
            else f"已保存默认：{script_label}；{self._helper_summary_text(app_key)}"
        )
        refs["current_text"].color = self.PALETTE["accent"] if selected else self.PALETTE["muted"]

        index = self.app_order.index(app_key)
        refs["move_up"].disabled = index == 0
        refs["move_down"].disabled = index == len(self.app_order) - 1
        refs["container"].tooltip = self._friendly_note(app_key)

    def _save_launch_preferences(self, e=None):
        if self.is_running:
            self._toast("任务运行中，暂时不能保存启动设置。", self.PALETTE["warning"])
            return

        for app_key in self.app_order:
            refs = self.launch_row_refs.get(app_key)
            if not refs:
                continue
            self.preference_db.save_app_launch_preference(
                app_key,
                bool(refs["checkbox"].value),
                self._current_script_choice(app_key),
                False,
            )

        self.launch_preferences = self.preference_db.get_all_app_launch_preferences()
        for app_key in self.launch_row_refs:
            self._refresh_launch_row(app_key)
        self._refresh_summary_and_queue()
        self.page.update()
        self._toast("启动设置已保存到 workflow.sqlite3。", self.PALETTE["accent"])

    def _refresh_summary_and_queue(self):
        targets = self.current_queue if self.current_queue else self._selected_targets()
        count = len(targets)
        self.selection_summary.value = "还没有选择任务" if count == 0 else f"已选 {count} 项任务"

        self.queue_list.controls.clear()
        if not targets:
            self.queue_list.controls.append(ft.Text("选择任务后，这里会显示本轮执行顺序。", size=13, color=self.PALETTE["muted"]))
        else:
            state_style = {
                "待执行": (self.PALETTE["muted"], "待执行"),
                "运行中": (self.PALETTE["info"], "运行中"),
                "成功": (self.PALETTE["accent"], "已完成"),
                "失败": (self.PALETTE["danger"], "失败"),
                "已停止": (self.PALETTE["warning"], "已停止"),
            }
            for index, (app_key, script_choice) in enumerate(targets, start=1):
                state = self.queue_state.get(app_key, "待执行")
                tone, label = state_style.get(state, (self.PALETTE["muted"], state))
                app_cfg = self.apps_data.get(app_key, {})
                helper_summary = self._helper_summary_text(app_key)
                self.queue_list.controls.append(
                    ft.Container(
                        bgcolor=self.PALETTE["panel_alt"],
                        border_radius=14,
                        padding=ft.Padding(12, 10, 12, 10),
                        content=ft.Row(
                            controls=[
                                ft.Text(f"{index}.", width=24, color=self.PALETTE["muted"]),
                                ft.Column(
                                    controls=[
                                        ft.Text(get_app_display_name(app_key), size=14, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"]),
                                        ft.Text(
                                            f"{self._resolve_script_display_name(app_key, app_cfg, script_choice)}；{helper_summary}",
                                            size=12,
                                            color=self.PALETTE["muted"],
                                        ),
                                    ],
                                    spacing=2,
                                    expand=True,
                                ),
                                ft.Container(
                                    bgcolor=tone + "22" if tone.startswith("#") else self.PALETTE["panel"],
                                    border_radius=999,
                                    padding=ft.Padding(10, 6, 10, 6),
                                    content=ft.Text(label, size=11, weight=ft.FontWeight.BOLD, color=tone),
                                ),
                            ],
                            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                    )
                )

        self.start_button.disabled = self.is_running or count == 0
        self.stop_button.disabled = not self.is_running

    def _on_launch_setting_change(self, e):
        app_key = e.control.data
        self._refresh_launch_row(app_key)
        self._refresh_summary_and_queue()
        self.page.update()

    def _select_all(self, e):
        if self.is_running:
            return
        for app_key in self.launch_row_refs:
            self.launch_row_refs[app_key]["checkbox"].value = True
            self._refresh_launch_row(app_key)
        self._refresh_summary_and_queue()
        self.page.update()

    def _clear_all(self, e):
        if self.is_running:
            return
        for app_key in self.launch_row_refs:
            self.launch_row_refs[app_key]["checkbox"].value = False
            self._refresh_launch_row(app_key)
        self._refresh_summary_and_queue()
        self.page.update()

    def _request_stop(self, e):
        if not self.is_running:
            return
        self.controller.stop_current_task = True
        self._set_status("正在停止", "已经发送停止请求，请等待当前任务安全收尾。", "warning")
        self.page.update()

    def _start_run(self, e):
        if self.is_running:
            return
        targets = self._selected_targets()
        if not targets:
            self._toast("请先选择至少一个任务。", self.PALETTE["warning"])
            return

        self.runtime_options = {"xm": False, "cj": False}
        self.is_running = True
        self.current_queue = list(targets)
        self.queue_state = {app_key: "待执行" for app_key, _ in targets}
        self.result_box.value = "任务开始运行后，这里会汇总本轮结果。"
        self._set_status("准备启动", "正在检查环境并准备本轮任务。", "running")
        self._refresh_summary_and_queue()
        self.page.update()
        threading.Thread(target=self._run_tasks_backend, args=(targets,), daemon=True).start()

    def _run_tasks_backend(self, target_apps: List[Tuple[str, Optional[str]]]):
        background_warnings: List[str] = []
        try:
            def _on_startup_lines(_startup_lines: List[str]):
                self._emit(
                    "status",
                    title="正在准备：启动前签到",
                    body="已按当前配置执行米游社 / 库街区启动前签到，请查看结果区获取详情。",
                    tone="running",
                )

            def _on_before_app(app_key: str, display_name: str, script_name: str):
                self._emit("queue_state", app_key=app_key, state="运行中")
                self._emit(
                    "status",
                    title=f"正在运行：{display_name}",
                    body=f"当前脚本方案：{script_name}。运行中尽量不要切出游戏窗口。",
                    tone="running",
                )

            def _on_before_ok_retry(
                app_key: str,
                display_name: str,
                script_name: str,
                next_attempt_no: int,
                restart_delay: float,
                _attempt,
            ):
                self._emit(
                    "status",
                    title=f"正在重试：{display_name}",
                    body=f"检测到 {script_name} 日志异常，{int(restart_delay)} 秒后开始第 {next_attempt_no + 1} 次尝试。",
                    tone="warning",
                )

            def _on_after_app(result):
                if result.state == "stopped":
                    self._emit("queue_state", app_key=result.app_key, state="已停止")
                elif result.state == "success":
                    self._emit("queue_state", app_key=result.app_key, state="成功")
                else:
                    self._emit("queue_state", app_key=result.app_key, state="失败")

            def _on_before_cj():
                self._emit(
                    "status",
                    title="正在运行：重聚流程",
                    body="暖暖任务结束后，继续执行重聚前台流程。",
                    tone="running",
                )

            batch_result = run_task_batch(
                self.controller,
                target_apps,
                xm_enabled=self.runtime_options["xm"],
                cj_enabled=self.runtime_options["cj"],
                force_rerun=self.runtime_options.get("force_rerun", False),
                hooks=TaskBatchHooks(
                    on_startup_lines=_on_startup_lines,
                    on_before_app=_on_before_app,
                    on_before_ok_retry=_on_before_ok_retry,
                    on_after_app=_on_after_app,
                    on_before_cj=_on_before_cj,
                ),
            )
            report = list(batch_result.report)
            background_warnings.extend(batch_result.warnings)

            tone = "success"
            title = "本轮任务已结束"
            body = "所有已选择任务都已执行完毕。"
            toast = "本轮任务已完成。"
            if self.controller.stop_current_task:
                tone = "warning"
                title = "任务已停止"
                body = "已按请求停止任务，请查看结果区和日志。"
                toast = "任务已停止。"
            elif any("❌" in line for line in report) or background_warnings:
                tone = "warning"
                body = "有任务失败或额外提醒，请查看结果区和日志。"
                toast = "本轮任务有失败项或提醒。"
            elif not report:
                tone = "warning"
                body = "没有产生可展示的结果，请查看日志。"
                toast = "本轮任务没有结果输出。"

            self._emit("finished", report=report, warnings=background_warnings, title=title, body=body, tone=tone, toast=toast)
        except Exception as e:
            self.controller.logger.error(f"Flet GUI 运行任务异常: {str(e)}", exc_info=True)
            self._emit(
                "finished",
                report=[f"系统错误: {str(e)}"],
                warnings=background_warnings,
                title="运行异常",
                body="任务运行过程中发生异常，请查看日志。",
                tone="danger",
                toast="任务异常结束。",
            )

    def _run_tool(self, title: str, handler):
        if self.is_running:
            self._toast("请先等待当前任务结束，再使用工具箱。", self.PALETTE["warning"])
            return
        self._set_status(title, "工具任务已启动，请留意下方日志。", "running")
        self.page.update()
        threading.Thread(target=handler, daemon=True).start()

    def _start_test_click(self, e):
        self._run_tool("测试识别点击", self._run_test_click_backend)

    def _run_test_click_backend(self):
        try:
            if not self.test_exe_path:
                self._emit("status", title="测试失败", body="未找到 XM Seer 可执行文件。", tone="danger")
                self._toast("未找到 XM Seer 程序。", self.PALETTE["danger"])
                return
            ok = run_xm_seer_test(self.controller.logger, self.test_exe_path)
            if ok:
                self._emit("status", title="测试完成", body="识别点击流程执行成功。", tone="success")
                self._toast("识别点击流程执行成功。", self.PALETTE["accent"])
            else:
                self._emit("status", title="测试失败", body="识别点击流程执行失败。", tone="danger")
                self._toast("识别点击流程执行失败。", self.PALETTE["danger"])
        except Exception as e:
            self.controller.logger.error(f"测试识别点击流程异常: {str(e)}", exc_info=True)
            self._emit("status", title="测试异常", body=str(e), tone="danger")

    def _start_test_xm(self, e):
        self._run_tool("测试 XM 流程", self._run_test_xm_backend)

    def _run_test_xm_backend(self):
        try:
            batch_result = run_task_batch(self.controller, [("XMFlow", None)], hooks=TaskBatchHooks())
            report = "\n".join(batch_result.report)
            if any("❌" in line for line in batch_result.report) or batch_result.warnings:
                self._emit("status", title="测试失败", body=report or "XM 流程执行失败。", tone="danger")
                self._toast("XM 流程执行失败。", self.PALETTE["danger"])
            else:
                self._emit("status", title="测试完成", body=report or "XM 流程执行成功。", tone="success")
                self._toast("XM 流程执行成功。", self.PALETTE["accent"])
        except Exception as e:
            self.controller.logger.error(f"测试 XM 流程异常: {str(e)}", exc_info=True)
            self._emit("status", title="测试异常", body=str(e), tone="danger")

    def _start_test_cj(self, e):
        self._run_tool("测试重聚流程", self._run_test_cj_backend)

    def _run_test_cj_backend(self):
        try:
            batch_result = run_task_batch(self.controller, [("CJFlow", None)], hooks=TaskBatchHooks())
            report = "\n".join(batch_result.report)
            if any("❌" in line for line in batch_result.report) or batch_result.warnings:
                self._emit("status", title="测试失败", body=report or "重聚流程执行失败。", tone="danger")
                self._toast("重聚流程执行失败。", self.PALETTE["danger"])
            else:
                self._emit("status", title="测试完成", body=report or "重聚流程执行成功。", tone="success")
                self._toast("重聚流程执行成功。", self.PALETTE["accent"])
        except Exception as e:
            self.controller.logger.error(f"测试重聚流程异常: {str(e)}", exc_info=True)
            self._emit("status", title="测试异常", body=str(e), tone="danger")

    def _find_xm_seer_exe(self) -> Optional[str]:
        # 优先从 app_config.json 读取 XMFlow 的 launcher 路径
        try:
            cfg = cm.load()
            xm_cfg = (cfg.get("apps") or {}).get("XMFlow") or {}
            launcher = str(xm_cfg.get("launcher") or "").strip()
            if launcher and os.path.isfile(launcher):
                return launcher
        except Exception as exc:
            self.controller.logger.debug("[XM] 读取启动器配置失败: %s", exc)
        # 回退到 glob 模式（如果配置了）
        if self.XM_SEER_PATH_PATTERN:
            matches = glob.glob(self.XM_SEER_PATH_PATTERN)
            if matches:
                return max(matches, key=lambda p: Path(p).stat().st_mtime)
        return None

    def _open_path(self, path: Path):
        try:
            if not path.exists():
                raise FileNotFoundError(str(path))
            os.startfile(str(path))
        except Exception as e:
            self._toast(f"打开失败：{str(e)}", self.PALETTE["danger"])

    def _open_config_file(self, e=None):
        self._open_path(CONFIG_FILE)

    def _open_logs_folder(self, e=None):
        Path(LOG_DIR).mkdir(parents=True, exist_ok=True)
        self._open_path(Path(LOG_DIR))

    def _open_examples_folder(self, e=None):
        self._open_path(FLET_EXAMPLES_DIR)