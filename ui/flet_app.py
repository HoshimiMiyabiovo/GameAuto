import asyncio
import copy
import ctypes
import glob
import json
import logging
import os
import queue
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import flet as ft

from ..settings import APP_ORDER, APPS, MIN_RUN_TIME, get_app_display_name, get_script_display_name, refresh_runtime_config
from ..settings import manager as cm
from ..runtime.automation_controller import AutomationController
from ..runtime.process_supervisor import ProcessSupervisor
from ..services.utils import format_duration
from ..storage.workflow_db import WorkflowDB
from ..clients.mihoyo_sign import (
    MIHOYO_SIGN_CONFIG_KEY,
    complete_qrcode_login as complete_mihoyo_qr_login,
    complete_sms_login as complete_mihoyo_sms_login,
    complete_password_login as complete_mihoyo_password_login,
    extract_cookie_fields as extract_mihoyo_cookie_fields,
    extract_cookie_from_browser as extract_mihoyo_cookie_from_browser,
    format_sign_report as format_mihoyo_sign_report,
    get_login_url as get_mihoyo_login_url,
    send_mobile_captcha as send_mihoyo_mobile_captcha,
    sign_games as sign_mihoyo_games,
    tidy_cookie as tidy_mihoyo_cookie,
    validate_cookie_text as validate_mihoyo_cookie_text,
)
from ..clients.daily_checker import (
    DailyCheckResult,
    GameDailyStatus,
    check_games_progress,
)
from ..services.daily_progress_tracker import (
    DailyProgressSnapshot,
    DailyProgressTracker,
    DailyTaskRecord,
    GameDailyRecord,
    get_tracker,
)
from ..clients.wuwa_kujiequ import (
    fetch_dashboard as fetch_wuwa_kujiequ_dashboard,
    login_with_code as login_wuwa_kujiequ_with_code,
    login_with_token as login_wuwa_kujiequ_with_token,
    perform_sign as perform_wuwa_kujiequ_sign,
    profile_from_dict as wuwa_kujiequ_profile_from_dict,
    profile_to_dict as wuwa_kujiequ_profile_to_dict,
)
from ..clients.tajiduo_sign import (
    TAJIDUO_SIGN_CONFIG_KEY,
    format_sign_report as format_tajiduo_sign_report,
    perform_sign as perform_tajiduo_sign,
)
from ..clients.tajiduo_login import (
    complete_password_login as complete_tajiduo_password_login,
    complete_sms_login as complete_tajiduo_sms_login,
    gen_fingerprint as gen_tajiduo_fingerprint,
    send_captcha as send_tajiduo_captcha,
)
from ..clients.skendfield_sign import (
    SKENDFIELD_SIGN_CONFIG_KEY,
    format_sign_report as format_skendfield_sign_report,
    perform_sign as perform_skendfield_sign,
)
from ..clients.endfield_protocol import (
    ENDFIELD_PROTOCOL_CONFIG_KEY,
    EndfieldProtocolClient,
    perform_query as perform_endfield_query,
)
from ..gacha.endfield_analysis import (
    load_all_records as load_endfield_records,
    build_overview as build_endfield_overview,
    build_pool_analysis,
    build_pity_analysis,
    build_six_star_history,
    build_character_stats as build_endfield_character_stats,
    build_weapon_stats as build_endfield_weapon_stats,
)
from ..gacha.hoyo_analysis import (
    build_character_stats as build_hoyo_character_stats,
    build_high_rarity_history,
    build_overview as build_hoyo_overview,
    build_pity_summary,
    build_pool_analysis as build_hoyo_pool_analysis,
    build_weapon_stats as build_hoyo_weapon_stats,
    get_high_rarity_label,
    get_rank_label,
)
from ..gacha.hoyo_importer import (
    GENSHIN_GAME_CODE,
    HOYO_GAME_OPTIONS,
    get_game_config,
    get_game_display_name,
    import_uigf,
)
from ..gacha.nikki_resonance_analysis import (
    build_overview as build_nikki_overview,
    build_pearpal_overview as build_nikki_pearpal_overview,
    build_pearpal_pool_summary as build_nikki_pearpal_pool_summary,
    build_pool_summary as build_nikki_pool_summary,
    build_recent_records as build_nikki_recent_records,
    db_to_nikki_document,
    load_resonance_document as load_nikki_resonance_document,
    nikki_to_db_records,
)
from ..gacha.nikki_resonance_fetch import (
    CLICKHOUSE_DOMAIN as DEFAULT_NIKKI_CLICKHOUSE_DOMAIN,
    GACHA_POOLS as NIKKI_GACHA_POOLS,
    decode_jwt_payload as decode_nikki_jwt,
    fetch_all_records as fetch_nikki_all_records,
    fetch_pearpal_notebook_summary as fetch_nikki_pearpal_notebook_summary,
    get_momo_token_console_script as get_nikki_momo_console_script,
    resolve_force_pool_ids as resolve_nikki_force_pool_ids,
)
from ..gacha.wuwa_analysis import (
    db_to_wuwa_records,
    load_all_wuwa_records,
    build_wuwa_overview,
    build_wuwa_pool_analysis,
    build_wuwa_five_star_history,
    build_wuwa_pity_analysis,
    build_wuwa_character_stats,
    build_wuwa_weapon_stats,
    wuwa_to_db_records,
)
from .automation import FletAutomationMixin
from .shared import (
    CONFIG_FILE,
    DEFAULT_ENDFIELD_PATH,
    DEFAULT_NIKKI_PATH,
    DEFAULT_WUWA_PATH,
    FLET_EXAMPLES_DIR,
    UILogHandler,
    WORKFLOW_DB_PATH,
    WUWA_KUJIEQU_CONFIG_KEY,
    TAJIDUO_SIGN_CONFIG_KEY,
    SKENDFIELD_SIGN_CONFIG_KEY,
)


FT_COLORS = getattr(ft, "Colors", None) or getattr(ft, "colors")
FT_ICONS = getattr(ft, "Icons", None) or getattr(ft, "icons")


class FletGameAutoApp(FletAutomationMixin):
    UI_FONT = "Microsoft YaHei UI"

    PALETTE = {
        "page": "#f6f1e8",
        "panel": "#fffaf2",
        "panel_alt": "#f4ecdf",
        "card": "#fffdf9",
        "card_active": "#eef8f1",
        "line": "#dbcdbd",
        "accent": "#2f7d4a",
        "accent_soft": "#dff1e4",
        "text": "#2c241d",
        "muted": "#6d6257",
        "warning": "#b66f1f",
        "warning_soft": "#faebd7",
        "danger": "#b04b44",
        "danger_soft": "#f7dfdd",
        "info": "#295d86",
        "info_soft": "#e9f3fb",
        "log_bg": "#1f1d1a",
        "log_fg": "#f5efe6",
    }

    def __init__(self, page: ft.Page, dep_report=None):
        self.page = page
        self.dep_report = dep_report
        self.controller = AutomationController()
        self.preference_db = WorkflowDB(str(WORKFLOW_DB_PATH))
        self.daily_progress_tracker = get_tracker(str(WORKFLOW_DB_PATH))

        self.ui_queue: "queue.Queue[Tuple[str, dict]]" = queue.Queue()
        self.log_lines: List[str] = []
        self.results_lines: List[str] = []
        self.current_queue: List[Tuple[str, Optional[str]]] = []
        self.queue_state: Dict[str, str] = {}
        self.apps_data: Dict[str, Dict] = {}
        self.app_order: List[str] = []
        self.launch_row_refs: Dict[str, Dict[str, ft.Control]] = {}
        self.launch_path_input_refs: Dict[str, Dict[str, Dict[str, ft.Control]]] = {}
        self.launch_preferences: Dict[str, Dict[str, object]] = {}
        self.current_view = "automation"
        self.nav_refs: Dict[str, Dict[str, ft.Control]] = {}
        self.gacha_accounts: List[Dict[str, Any]] = []
        self.gacha_current_uid: Optional[str] = None
        self.gacha_is_importing = False
        self.mihoyo_sign_busy = False
        self.mihoyo_cookie_draft = ""

        self.is_running = False
        self.runtime_options = {"xm": True, "cj": False}
        self.test_exe_path = self._find_xm_seer_exe()

        self.banner_box: Optional[ft.Container] = None
        self.banner_text: Optional[ft.Text] = None
        self.banner_action: Optional[ft.TextButton] = None
        self.launch_table_rows: Optional[ft.Column] = None
        self.launch_path_list: Optional[ft.Column] = None
        self.discovery_cache_list: Optional[ft.Column] = None
        self.selection_summary: Optional[ft.Text] = None
        self.status_chip: Optional[ft.Container] = None
        self.status_chip_text: Optional[ft.Text] = None
        self.status_title: Optional[ft.Text] = None
        self.status_body: Optional[ft.Text] = None
        self.progress_ring: Optional[ft.ProgressRing] = None
        self.start_button: Optional[ft.ElevatedButton] = None
        self.stop_button: Optional[ft.OutlinedButton] = None
        self.queue_list: Optional[ft.Column] = None
        self.result_box: Optional[ft.TextField] = None
        self.log_box: Optional[ft.TextField] = None
        self.log_container: Optional[ft.Container] = None
        self.log_toggle: Optional[ft.TextButton] = None
        self.tools_body: Optional[ft.Container] = None
        self.tools_toggle: Optional[ft.TextButton] = None
        self.automation_view: Optional[ft.Container] = None
        self.daily_view: Optional[ft.Container] = None
        self.gacha_view: Optional[ft.Container] = None
        self.nikki_view: Optional[ft.Container] = None
        self.endfield_view: Optional[ft.Container] = None
        self.wuwa_view: Optional[ft.Container] = None
        self.daily_check_busy = False
        self.daily_result: Optional[DailyCheckResult] = None
        self.daily_refresh_button: Optional[ft.ElevatedButton] = None
        self.daily_genshin_checkbox: Optional[ft.Checkbox] = None
        self.daily_hsr_checkbox: Optional[ft.Checkbox] = None
        self.daily_zzz_checkbox: Optional[ft.Checkbox] = None
        self.daily_wuwa_checkbox: Optional[ft.Checkbox] = None
        self.daily_nikki_checkbox: Optional[ft.Checkbox] = None
        self.daily_endfield_checkbox: Optional[ft.Checkbox] = None
        self.daily_status_text: Optional[ft.Text] = None
        self.daily_config_text: Optional[ft.Text] = None
        self.daily_metrics_row: Optional[ft.Row] = None
        self.daily_result_list: Optional[ft.Column] = None
        self.daily_persisted_row: Optional[ft.Row] = None
        self.daily_persisted_list: Optional[ft.Column] = None
        self.gacha_path_input: Optional[ft.TextField] = None
        self.gacha_game_dropdown: Optional[ft.Dropdown] = None
        self.gacha_uid_dropdown: Optional[ft.Dropdown] = None
        self.gacha_import_button: Optional[ft.ElevatedButton] = None
        self.gacha_browse_button: Optional[ft.OutlinedButton] = None
        self.gacha_reload_button: Optional[ft.TextButton] = None
        self.gacha_import_summary: Optional[ft.Text] = None
        self.gacha_meta_text: Optional[ft.Text] = None
        self.gacha_banner_text: Optional[ft.Text] = None
        self.gacha_overview_cards: Optional[ft.Row] = None
        self.gacha_pool_tabs: Optional[ft.Column] = None
        self.gacha_pool_selector: Optional[ft.Row] = None
        self.gacha_pool_content: Optional[ft.Container] = None
        self.gacha_pool_views: Dict[str, ft.Control] = {}
        self.gacha_selected_pool: Optional[str] = None
        self.mihoyo_browser_dropdown: Optional[ft.Dropdown] = None
        self.mihoyo_cookie_input: Optional[ft.TextField] = None
        self.mihoyo_cookie_button: Optional[ft.OutlinedButton] = None
        self.mihoyo_open_login_button: Optional[ft.OutlinedButton] = None
        self.mihoyo_qr_button: Optional[ft.OutlinedButton] = None
        self.mihoyo_sign_button: Optional[ft.ElevatedButton] = None
        self.mihoyo_clear_button: Optional[ft.TextButton] = None
        self.mihoyo_genshin_checkbox: Optional[ft.Checkbox] = None
        self.mihoyo_hsr_checkbox: Optional[ft.Checkbox] = None
        self.mihoyo_zzz_checkbox: Optional[ft.Checkbox] = None
        self.mihoyo_startup_checkbox: Optional[ft.Checkbox] = None
        self.mihoyo_status_text: Optional[ft.Text] = None
        self.mihoyo_account_text: Optional[ft.Text] = None
        self.mihoyo_qr_panel: Optional[ft.Container] = None
        self.mihoyo_qr_image: Optional[ft.Image] = None
        self.mihoyo_qr_hint_text: Optional[ft.Text] = None
        self.mihoyo_metrics_row: Optional[ft.Row] = None
        self.mihoyo_result_list: Optional[ft.Column] = None
        self.mihoyo_qr_session_url: str = ""
        self.mihoyo_phone_input: Optional[ft.TextField] = None
        self.mihoyo_sms_code_input: Optional[ft.TextField] = None
        self.mihoyo_send_sms_button: Optional[ft.OutlinedButton] = None
        self.mihoyo_sms_login_button: Optional[ft.ElevatedButton] = None
        self.mihoyo_sms_phone: str = ""
        self.mihoyo_pw_account_input: Optional[ft.TextField] = None
        self.mihoyo_pw_password_input: Optional[ft.TextField] = None
        self.mihoyo_pw_login_button: Optional[ft.ElevatedButton] = None
        self.gacha_high_rarity_title_text: Optional[ft.Text] = None
        self.gacha_high_rarity_note_text: Optional[ft.Text] = None
        self.gacha_high_rarity_list: Optional[ft.Column] = None
        self.gacha_character_list: Optional[ft.Column] = None
        self.gacha_weapon_list: Optional[ft.Column] = None
        self.gacha_pity_text: Optional[ft.Text] = None
        self.gacha_current_game_code: str = GENSHIN_GAME_CODE
        # Infinity Nikki resonance refs
        self.nikki_is_fetching = False
        self.nikki_path_input: Optional[ft.TextField] = None
        self.nikki_browse_button: Optional[ft.OutlinedButton] = None
        self.nikki_analyze_button: Optional[ft.ElevatedButton] = None
        self.nikki_jwt_input: Optional[ft.TextField] = None
        self.nikki_uid_input: Optional[ft.TextField] = None
        self.nikki_momo_id_input: Optional[ft.TextField] = None
        self.nikki_momo_token_input: Optional[ft.TextField] = None
        self.nikki_force_pool_input: Optional[ft.TextField] = None
        self.nikki_domain_input: Optional[ft.TextField] = None
        self.nikki_output_input: Optional[ft.TextField] = None
        self.nikki_fetch_button: Optional[ft.ElevatedButton] = None
        self.nikki_fetch_pearpal_button: Optional[ft.OutlinedButton] = None
        self.nikki_status_text: Optional[ft.Text] = None
        self.nikki_meta_text: Optional[ft.Text] = None
        self.nikki_overview_cards: Optional[ft.Row] = None
        self.nikki_pool_tabs: Optional[ft.Column] = None
        self.nikki_pool_selector: Optional[ft.Row] = None
        self.nikki_pool_content: Optional[ft.Container] = None
        self.nikki_pool_views: Dict[str, ft.Control] = {}
        self.nikki_selected_pool: Optional[str] = None
        self.nikki_recent_hint_text: Optional[ft.Text] = None
        self.nikki_recent_list: Optional[ft.Column] = None
        # Endfield gacha refs
        self.endfield_path_input: Optional[ft.TextField] = None
        self.endfield_browse_button: Optional[ft.OutlinedButton] = None
        self.endfield_analyze_button: Optional[ft.ElevatedButton] = None
        self.endfield_status_text: Optional[ft.Text] = None
        self.endfield_overview_cards: Optional[ft.Row] = None
        self.endfield_pool_tabs: Optional[ft.Column] = None
        self.endfield_pool_selector: Optional[ft.Row] = None
        self.endfield_pool_content: Optional[ft.Container] = None
        self.endfield_pool_views: Dict[str, ft.Control] = {}
        self.endfield_selected_pool: Optional[str] = None
        self.endfield_six_star_list: Optional[ft.Column] = None
        self.endfield_char_list: Optional[ft.Column] = None
        self.endfield_weapon_list: Optional[ft.Column] = None
        self.endfield_pity_text: Optional[ft.Text] = None
        # Wuwa gacha refs
        self.wuwa_kujiequ_busy = False
        self.wuwa_kujiequ_phone_input: Optional[ft.TextField] = None
        self.wuwa_kujiequ_code_input: Optional[ft.TextField] = None
        self.wuwa_kujiequ_token_input: Optional[ft.TextField] = None
        self.wuwa_kujiequ_did_input: Optional[ft.TextField] = None
        self.wuwa_kujiequ_source_dropdown: Optional[ft.Dropdown] = None
        self.wuwa_kujiequ_role_dropdown: Optional[ft.Dropdown] = None
        self.wuwa_kujiequ_auto_sign_checkbox: Optional[ft.Checkbox] = None
        self.wuwa_kujiequ_login_button: Optional[ft.ElevatedButton] = None
        self.wuwa_kujiequ_token_button: Optional[ft.OutlinedButton] = None
        self.wuwa_kujiequ_refresh_button: Optional[ft.ElevatedButton] = None
        self.wuwa_kujiequ_sign_button: Optional[ft.OutlinedButton] = None
        self.wuwa_kujiequ_clear_button: Optional[ft.TextButton] = None
        self.wuwa_kujiequ_status_text: Optional[ft.Text] = None
        self.wuwa_kujiequ_account_text: Optional[ft.Text] = None
        self.wuwa_kujiequ_metrics_row: Optional[ft.Row] = None
        self.wuwa_kujiequ_daily_list: Optional[ft.Column] = None
        self.wuwa_kujiequ_base_list: Optional[ft.Column] = None
        self.wuwa_kujiequ_sign_list: Optional[ft.Column] = None
        self.wuwa_kujiequ_roles: List[Dict[str, str]] = []
        self.wuwa_path_input: Optional[ft.TextField] = None
        self.wuwa_browse_button: Optional[ft.OutlinedButton] = None
        self.wuwa_analyze_button: Optional[ft.ElevatedButton] = None
        self.wuwa_status_text: Optional[ft.Text] = None
        self.wuwa_overview_cards: Optional[ft.Row] = None
        self.wuwa_pool_tabs: Optional[ft.Column] = None
        self.wuwa_pool_selector: Optional[ft.Row] = None
        self.wuwa_pool_content: Optional[ft.Container] = None
        self.wuwa_pool_views: Dict[str, ft.Control] = {}
        self.wuwa_selected_pool: Optional[str] = None
        self.wuwa_five_star_list: Optional[ft.Column] = None
        self.wuwa_char_list: Optional[ft.Column] = None
        self.wuwa_weapon_list: Optional[ft.Column] = None
        self.wuwa_pity_text: Optional[ft.Text] = None
        # Tajiduo refs
        self.tajiduo_view: Optional[ft.Container] = None
        self.tajiduo_busy = False
        self.tajiduo_phone_input: Optional[ft.TextField] = None
        self.tajiduo_password_input: Optional[ft.TextField] = None
        self.tajiduo_sms_code_input: Optional[ft.TextField] = None
        self.tajiduo_token_input: Optional[ft.TextField] = None
        self.tajiduo_deviceid_input: Optional[ft.TextField] = None
        self.tajiduo_uid_input: Optional[ft.TextField] = None
        self.tajiduo_bbs_checkbox: Optional[ft.Checkbox] = None
        self.tajiduo_game_checkbox: Optional[ft.Checkbox] = None
        self.tajiduo_browse_checkbox: Optional[ft.Checkbox] = None
        self.tajiduo_like_checkbox: Optional[ft.Checkbox] = None
        self.tajiduo_share_checkbox: Optional[ft.Checkbox] = None
        self.tajiduo_startup_checkbox: Optional[ft.Checkbox] = None
        self.tajiduo_login_button: Optional[ft.ElevatedButton] = None
        self.tajiduo_sms_send_button: Optional[ft.OutlinedButton] = None
        self.tajiduo_sms_login_button: Optional[ft.ElevatedButton] = None
        self.tajiduo_token_button: Optional[ft.OutlinedButton] = None
        self.tajiduo_sign_button: Optional[ft.ElevatedButton] = None
        self.tajiduo_clear_button: Optional[ft.TextButton] = None
        self.tajiduo_status_text: Optional[ft.Text] = None
        self.tajiduo_account_text: Optional[ft.Text] = None
        self.tajiduo_metrics_row: Optional[ft.Row] = None
        self.tajiduo_result_list: Optional[ft.Column] = None
        self.skendfield_busy = False
        self.skendfield_token_input: Optional[ft.TextField] = None
        self.skendfield_server_dropdown: Optional[ft.Dropdown] = None
        self.skendfield_startup_checkbox: Optional[ft.Checkbox] = None
        self.skendfield_sign_button: Optional[ft.ElevatedButton] = None
        self.skendfield_clear_button: Optional[ft.TextButton] = None
        self.skendfield_status_text: Optional[ft.Text] = None
        self.skendfield_metrics_row: Optional[ft.Row] = None
        self.skendfield_result_list: Optional[ft.Column] = None
        self.endfield_center_view: Optional[ft.Container] = None
        self.endfield_center_busy = False
        self.endfield_center_api_key_input: Optional[ft.TextField] = None
        self.endfield_center_framework_token_input: Optional[ft.TextField] = None
        self.endfield_center_role_id_input: Optional[ft.TextField] = None
        self.endfield_center_server_id_input: Optional[ft.TextField] = None
        self.endfield_center_result_area: Optional[ft.Column] = None
        self.endfield_center_status_text: Optional[ft.Text] = None
        self.endfield_center_startup_checkbox: Optional[ft.Checkbox] = None

        self._setup_page()
        self._attach_log_handler()
        self._build_layout()
        self._reload_config(show_toast=False)
        self._reload_gacha_data(show_toast=False)
        self._update_banner()
        self._refresh_summary_and_queue()
        self.page.run_task(self._pump_ui_events)

    def _setup_page(self):
        self.page.title = "游戏自动化助手"
        self.page.padding = 0
        self.page.spacing = 0
        self.page.bgcolor = self.PALETTE["page"]
        self.page.theme = ft.Theme(font_family=self.UI_FONT)
        self.page.dark_theme = ft.Theme(font_family=self.UI_FONT)
        self.page.theme_mode = ft.ThemeMode.LIGHT
        self.page.scroll = ft.ScrollMode.AUTO
        try:
            self.page.window.width = 1320
            self.page.window.height = 880
            self.page.window.min_width = 1120
            self.page.window.min_height = 720
        except Exception as exc:
            self.controller.logger.error("[终末地数据中心] 加载本地抽卡数据失败: %s", exc, exc_info=True)

    def _attach_log_handler(self):
        logger = logging.getLogger("GameAutomation")
        for handler in logger.handlers[:]:
            if getattr(handler, "_flet_ui_handler", False):
                logger.removeHandler(handler)
        handler = UILogHandler(self.ui_queue)
        handler.setFormatter(
            logging.Formatter(
                "[%(asctime)s.%(msecs)03d] [%(levelname)-8s] %(name)s:%(funcName)s:%(lineno)d - %(message)s",
                "%H:%M:%S",
            )
        )
        logger.addHandler(handler)

    def _build_layout(self):
        title = ft.Text("游戏自动化助手", size=28, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"])
        subtitle = ft.Text(
            "Flet 版界面：只保留小白真正会用到的操作。先勾选任务，再点开始运行。",
            size=13,
            color=self.PALETTE["muted"],
        )

        steps = ft.Row(
            controls=[self._step_chip(text) for text in ("1 选任务", "2 点开始", "3 看结果")],
            spacing=8,
            wrap=True,
        )

        self.banner_text = ft.Text(size=13, color=self.PALETTE["warning"], expand=True)
        self.banner_action = ft.TextButton("打开配置文件", on_click=self._open_config_file)
        self.banner_box = ft.Container(
            visible=False,
            bgcolor=self.PALETTE["warning_soft"],
            border=self._border_all(1, self.PALETTE["line"]),
            border_radius=16,
            padding=ft.Padding(16, 12, 16, 12),
            content=ft.Row(
                controls=[
                    ft.Icon(FT_ICONS.INFO_ROUNDED, color=self.PALETTE["warning"]),
                    self.banner_text,
                    self.banner_action,
                ],
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )

        header = ft.Container(
            bgcolor=self.PALETTE["panel_alt"],
            padding=ft.Padding(18, 18, 18, 18),
            content=ft.Column(
                controls=[
                    ft.Row(
                        controls=[
                            ft.Column([title, subtitle], spacing=6, expand=True),
                            steps,
                        ],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        vertical_alignment=ft.CrossAxisAlignment.START,
                    ),
                    self.banner_box,
                ],
                spacing=10,
            ),
        )

        self.automation_view = self._build_automation_view()

        nav_bar = ft.Container(
            padding=ft.Padding(24, 12, 24, 0),
            content=ft.Row(
                controls=[
                    self._build_view_nav("automation", "自动化运行", "运行游戏脚本、工具箱和日志面板"),
                    self._build_view_nav("daily", "每日进度", "统一查询原神 / 星铁 / 绝区零 / 鸣潮 / 暖暖的每日完成度"),
                    self._build_view_nav("gacha", "HoYo 抽卡分析", "导入原神 / 崩铁 / 绝区零 UIGF，并按卡池查看最高稀有度与统计"),
                    self._build_view_nav("nikki", "无限暖暖共鸣", "读取档案 JSON、抓取完整共鸣，或导入 Pearpal 摘要"),
                    self._build_view_nav("endfield", "终末地抽卡分析", "读取 JSON 分析角色、武器、保底分布"),
                    self._build_view_nav("wuwa", "鸣潮专项", "库街区签到 / 体力 / 活跃度 / 周本，以及 XLSX 抽卡分析"),
                    self._build_view_nav("tajiduo", "塔吉多签到", "异环社区 BBS 签到、游戏签到与每日任务"),
                    self._build_view_nav("endfield_center", "终末地数据中心", "便笺/理智/干员/建设/抽卡/成就查询 + 森空岛直接签到"),
                ],
                spacing=10,
                wrap=True,
            ),
        )

        self.daily_view = self._build_daily_view()
        self.gacha_view = self._build_gacha_view()
        self.nikki_view = self._build_nikki_view()
        self.endfield_view = self._build_endfield_view()
        self.wuwa_view = self._build_wuwa_view()
        self.tajiduo_view = self._build_tajiduo_view()
        self.endfield_center_view = self._build_endfield_center_view()
        self.page.add(
            ft.Column(
                [
                    header,
                    nav_bar,
                    self.automation_view,
                    self.daily_view,
                    self.gacha_view,
                    self.nikki_view,
                    self.endfield_view,
                    self.wuwa_view,
                    self.tajiduo_view,
                    self.endfield_center_view,
                ],
                spacing=0,
            )
        )
        self._refresh_view_nav()

    def _panel(self, content: ft.Control, expand: Optional[int] = None) -> ft.Container:
        return ft.Container(
            expand=expand,
            bgcolor=self.PALETTE["panel"],
            border=self._border_all(1, self.PALETTE["line"]),
            border_radius=20,
            padding=ft.Padding(14, 14, 14, 14),
            content=content,
        )

    def _border_all(self, width: float, color: str) -> ft.Border:
        side = ft.BorderSide(width=width, color=color)
        return ft.Border(left=side, top=side, right=side, bottom=side)

    def _step_chip(self, text: str) -> ft.Container:
        return ft.Container(
            bgcolor=self.PALETTE["accent_soft"],
            border_radius=999,
            padding=ft.Padding(12, 8, 12, 8),
            content=ft.Text(text, size=12, weight=ft.FontWeight.BOLD, color=self.PALETTE["accent"]),
        )

    def _build_pool_empty_state(self, message: str) -> ft.Container:
        return ft.Container(
            bgcolor=self.PALETTE["panel_alt"],
            border=self._border_all(1, self.PALETTE["line"]),
            border_radius=16,
            padding=ft.Padding(16, 14, 16, 14),
            content=ft.Text(message, size=13, color=self.PALETTE["muted"]),
        )

    def _build_pool_switch_button(self, label: str, selected: bool, on_click) -> ft.Container:
        return ft.Container(
            bgcolor=self.PALETTE["accent_soft"] if selected else self.PALETTE["panel_alt"],
            border=self._border_all(1, self.PALETTE["accent"] if selected else self.PALETTE["line"]),
            border_radius=999,
            padding=ft.Padding(14, 8, 14, 8),
            on_click=on_click,
            content=ft.Text(
                label,
                size=12,
                weight=ft.FontWeight.BOLD,
                color=self.PALETTE["accent"] if selected else self.PALETTE["text"],
            ),
        )

    def _refresh_pool_switcher(
        self,
        selector_row: Optional[ft.Row],
        content_host: Optional[ft.Container],
        pool_views: Dict[str, ft.Control],
        selected_pool: Optional[str],
        on_select,
        empty_message: str,
    ) -> Optional[str]:
        if selector_row is None or content_host is None:
            return selected_pool

        pool_names = list(pool_views.keys())
        if not pool_names:
            selector_row.controls = []
            content_host.content = self._build_pool_empty_state(empty_message)
            return None

        if selected_pool not in pool_views:
            selected_pool = pool_names[0]

        selector_row.controls = [
            self._build_pool_switch_button(
                pool_name,
                pool_name == selected_pool,
                lambda e, name=pool_name: on_select(name),
            )
            for pool_name in pool_names
        ]
        content_host.content = pool_views[selected_pool]
        return selected_pool

    def _select_endfield_pool(self, pool_name: str):
        if pool_name not in self.endfield_pool_views:
            return
        self.endfield_selected_pool = pool_name
        self.endfield_selected_pool = self._refresh_pool_switcher(
            self.endfield_pool_selector,
            self.endfield_pool_content,
            self.endfield_pool_views,
            self.endfield_selected_pool,
            self._select_endfield_pool,
            "当前文件中没有可展示的卡池数据。",
        )
        if getattr(self, "page", None) is not None:
            self.page.update()

    def _select_wuwa_pool(self, pool_name: str):
        if pool_name not in self.wuwa_pool_views:
            return
        self.wuwa_selected_pool = pool_name
        self.wuwa_selected_pool = self._refresh_pool_switcher(
            self.wuwa_pool_selector,
            self.wuwa_pool_content,
            self.wuwa_pool_views,
            self.wuwa_selected_pool,
            self._select_wuwa_pool,
            "当前文件中没有可展示的卡池数据。",
        )
        if getattr(self, "page", None) is not None:
            self.page.update()

    def _build_view_nav(self, view_key: str, title: str, subtitle: str) -> ft.Container:
        title_text = ft.Text(title, size=15, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"])
        subtitle_text = ft.Text(subtitle, size=12, color=self.PALETTE["muted"])
        container = ft.Container(
            data=view_key,
            border=self._border_all(1, self.PALETTE["line"]),
            border_radius=18,
            padding=ft.Padding(16, 12, 16, 12),
            on_click=self._on_view_nav_click,
            content=ft.Column([title_text, subtitle_text], spacing=3),
        )
        self.nav_refs[view_key] = {
            "container": container,
            "title": title_text,
            "subtitle": subtitle_text,
        }
        return container

    def _refresh_view_nav(self):
        for view_key, refs in self.nav_refs.items():
            active = view_key == self.current_view
            refs["container"].bgcolor = self.PALETTE["card_active"] if active else self.PALETTE["panel"]
            refs["title"].color = self.PALETTE["accent"] if active else self.PALETTE["text"]
            refs["subtitle"].color = self.PALETTE["accent"] if active else self.PALETTE["muted"]
        if self.automation_view is not None:
            self.automation_view.visible = self.current_view == "automation"
        if self.daily_view is not None:
            self.daily_view.visible = self.current_view == "daily"
        if self.gacha_view is not None:
            self.gacha_view.visible = self.current_view == "gacha"
        if self.nikki_view is not None:
            self.nikki_view.visible = self.current_view == "nikki"
        if self.endfield_view is not None:
            self.endfield_view.visible = self.current_view == "endfield"
        if self.wuwa_view is not None:
            self.wuwa_view.visible = self.current_view == "wuwa"
        if self.tajiduo_view is not None:
            self.tajiduo_view.visible = self.current_view == "tajiduo"
        if self.endfield_center_view is not None:
            self.endfield_center_view.visible = self.current_view == "endfield_center"

    def _on_view_becomes_visible(self, view_key: str) -> None:
        """把已保存的 token/配置重新灌进对应视图的输入框。

        切到任意视图（首次显示 / 从别的视图切回 / 重新点同一项）时都会调一次，
        避免输入框因为构建时机、主题切换、Flet 重建控件等原因变成空值。
        """
        try:
            if view_key == "endfield_center":
                self._apply_endfield_protocol_config_to_controls()
                self._apply_skendfield_config_to_controls()
                self._render_skendfield_sign_result(None)
                self._render_endfield_center_gacha()
            elif view_key == "wuwa":
                self._apply_wuwa_kujiequ_config_to_controls()
            elif view_key == "tajiduo":
                self._apply_tajiduo_config_to_controls()
            elif view_key == "nikki":
                self._apply_nikki_config_to_controls()
            elif view_key == "daily":
                self._apply_mihoyo_sign_config_to_controls()
        except Exception as exc:  # noqa: BLE001 - 刷新失败不应阻塞 UI 切换
            try:
                self.controller.logger.warning(
                    "[UI] 视图 %s 刷新配置失败：%s", view_key, exc)
            except Exception:
                pass

    def _on_view_nav_click(self, e):
        self._switch_view(str(e.control.data))

    def _switch_view(self, view_key: str):
        if view_key not in {"automation", "daily", "gacha", "nikki", "endfield", "endfield_center", "wuwa", "tajiduo"}:
            self.controller.logger.warning("[UI] 未知视图: %s，已忽略", view_key)
            return
        if self.current_view == view_key:
            if view_key == "daily" and not self.daily_check_busy:
                self._start_daily_check(show_toast=False)
            elif view_key == "gacha":
                self._reload_gacha_data(show_toast=False)
            elif view_key == "nikki":
                self._reload_nikki_data(show_toast=False)
            elif view_key == "endfield":
                self._reload_endfield_data(show_toast=False)
            elif view_key == "wuwa":
                self._reload_wuwa_data(show_toast=False)
            elif view_key == "tajiduo":
                self._render_tajiduo_sign_result(None)
            self._on_view_becomes_visible(view_key)
            self._refresh_view_nav()
            self.page.update()
            return
        self.controller.logger.info("[UI] 切换视图: %s -> %s", self.current_view, view_key)
        self.current_view = view_key
        if view_key == "daily":
            self._render_daily_check_result(self.daily_result)
        elif view_key == "gacha":
            self._reload_gacha_data(show_toast=False)
        elif view_key == "nikki":
            self._reload_nikki_data(show_toast=False)
        elif view_key == "endfield":
            self._reload_endfield_data(show_toast=False)
        elif view_key == "wuwa":
            self._reload_wuwa_data(show_toast=False)
        elif view_key == "tajiduo":
            self._render_tajiduo_sign_result(None)
        self._on_view_becomes_visible(view_key)
        self._refresh_view_nav()
        self.page.update()

    def _build_analysis_output(self, initial_text: str, min_lines: int, max_lines: int) -> ft.TextField:
        return ft.TextField(
            value=initial_text,
            multiline=True,
            min_lines=min_lines,
            max_lines=max_lines,
            read_only=True,
            bgcolor=self.PALETTE["panel_alt"],
            border_color=self.PALETTE["line"],
        )

    def _build_gacha_metric_card(self, title: str, value: str, note: str, color: Optional[str] = None) -> ft.Container:
        value_color = color if color is not None else self.PALETTE["text"]
        return ft.Container(
            width=210,
            bgcolor=self.PALETTE["card"],
            border=self._border_all(1, self.PALETTE["line"]),
            border_radius=16,
            padding=ft.Padding(14, 12, 14, 12),
            content=ft.Column(
                controls=[
                    ft.Text(title, size=12, color=self.PALETTE["muted"]),
                    ft.Text(value, size=24, weight=ft.FontWeight.BOLD, color=value_color),
                    ft.Text(note, size=12, color=self.PALETTE["muted"]),
                ],
                spacing=4,
            ),
        )

    def _build_daily_view(self) -> ft.Container:
        self.daily_refresh_button = ft.ElevatedButton("刷新每日进度", icon=FT_ICONS.REFRESH, on_click=self._start_daily_check)
        self.daily_genshin_checkbox = ft.Checkbox(label="原神", value=True)
        self.daily_hsr_checkbox = ft.Checkbox(label="星穹铁道", value=True)
        self.daily_zzz_checkbox = ft.Checkbox(label="绝区零", value=True)
        self.daily_wuwa_checkbox = ft.Checkbox(label="鸣潮", value=True)
        self.daily_nikki_checkbox = ft.Checkbox(label="无限暖暖", value=True)
        self.daily_endfield_checkbox = ft.Checkbox(label="终末地", value=True)
        self.daily_status_text = ft.Text(
            "点击刷新后，会按当前配置真实请求每日 API，并把每个游戏的失败原因原样展示出来。",
            size=13,
            color=self.PALETTE["muted"],
        )
        self.daily_config_text = ft.Text(size=13, color=self.PALETTE["muted"])
        self.daily_metrics_row = ft.Row(wrap=True, spacing=10)
        self.daily_result_list = ft.Column(spacing=10)
        self.daily_persisted_row = ft.Row(wrap=True, spacing=10)
        self.daily_persisted_list = ft.Column(spacing=10)

        intro_panel = self._panel(
            ft.Column(
                controls=[
                    ft.Container(
                        bgcolor=self.PALETTE["accent_soft"],
                        border_radius=18,
                        padding=ft.Padding(16, 14, 16, 14),
                        content=ft.Row(
                            controls=[
                                ft.Container(
                                    width=52,
                                    height=52,
                                    border_radius=16,
                                    bgcolor=self.PALETTE["card"],
                                    content=ft.Icon(FT_ICONS.AUTO_AWESOME, color=self.PALETTE["accent"], size=28),
                                    alignment=ft.Alignment(0, 0),
                                ),
                                ft.Column(
                                    controls=[
                                        ft.Text("每日进度检测", size=20, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"]),
                                        ft.Text(
                                            "原神 / 星铁 / 绝区零走米游社实时便笺，鸣潮走库街区，暖暖走 Pearpal notebook。",
                                            size=13,
                                            color=self.PALETTE["muted"],
                                        ),
                                    ],
                                    spacing=4,
                                    expand=True,
                                ),
                            ],
                            spacing=12,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                    ),
                    ft.Text(
                        "参考 LittlePaimon 的实现，米游社这类实时便笺通常需要 stoken 级别认证；仅有 cookie_token / ltoken 时，往往只能签到，不能稳定查询星铁 / 绝区零便笺。",
                        size=13,
                        color=self.PALETTE["muted"],
                    ),
                    ft.Row(
                        controls=[
                            self.daily_genshin_checkbox,
                            self.daily_hsr_checkbox,
                            self.daily_zzz_checkbox,
                            self.daily_wuwa_checkbox,
                            self.daily_nikki_checkbox,
                            self.daily_endfield_checkbox,
                            self.daily_refresh_button,
                        ],
                        spacing=8,
                        wrap=True,
                    ),
                    self.daily_status_text,
                    self.daily_config_text,
                    self.daily_metrics_row,
                ],
                spacing=10,
            )
        )

        persisted_panel = self._panel(
            ft.Column(
                controls=[
                    ft.Row(
                        controls=[
                            ft.Text("今日进度总览", size=20, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"]),
                            ft.Container(
                                content=ft.Text(
                                    "每天 4:00 自动重置",
                                    size=11,
                                    color=self.PALETTE["muted"],
                                ),
                                bgcolor=self.PALETTE["panel_alt"],
                                border_radius=8,
                                padding=ft.Padding(6, 4, 6, 4),
                            ),
                        ],
                        spacing=10,
                    ),
                    ft.Text("社区便笺 + 每日一条龙记录，两条路走路。绿色=已完成，红色=失败，黄色=进行中/未查询。", size=13, color=self.PALETTE["muted"]),
                    self.daily_persisted_row,
                    self.daily_persisted_list,
                ],
                spacing=10,
            )
        )

        result_panel = self._panel(
            ft.Column(
                controls=[
                    ft.Text("逐游戏结果", size=20, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"]),
                    ft.Text("每张卡片都会直接显示真实进度、关键体力信息，以及当前这款游戏查不到时的明确原因。", size=13, color=self.PALETTE["muted"]),
                    self.daily_result_list,
                ],
                spacing=10,
            )
        )

        view = ft.Container(
            visible=False,
            padding=ft.Padding(18, 16, 18, 16),
            content=ft.Column(
                controls=[intro_panel, persisted_panel, result_panel],
                spacing=12,
                scroll=ft.ScrollMode.AUTO,
            ),
        )
        self._render_daily_check_result(None)
        return view

    def _set_daily_check_busy(self, busy: bool):
        self.daily_check_busy = busy
        for control in (
            self.daily_refresh_button,
            self.daily_genshin_checkbox,
            self.daily_hsr_checkbox,
            self.daily_zzz_checkbox,
            self.daily_wuwa_checkbox,
            self.daily_nikki_checkbox,
            self.daily_endfield_checkbox,
        ):
            if control is not None:
                control.disabled = busy

    def _selected_daily_game_keys(self) -> List[str]:
        selected: List[str] = []
        if self.daily_genshin_checkbox is not None and self.daily_genshin_checkbox.value:
            selected.append("genshin")
        if self.daily_hsr_checkbox is not None and self.daily_hsr_checkbox.value:
            selected.append("honkai_sr")
        if self.daily_zzz_checkbox is not None and self.daily_zzz_checkbox.value:
            selected.append("zzz")
        if self.daily_wuwa_checkbox is not None and self.daily_wuwa_checkbox.value:
            selected.append("wuwa")
        if self.daily_nikki_checkbox is not None and self.daily_nikki_checkbox.value:
            selected.append("nikki")
        if self.daily_endfield_checkbox is not None and self.daily_endfield_checkbox.value:
            selected.append("endfield")
        return selected

    def _build_daily_check_config_snapshot(self) -> Dict[str, Any]:
        config = copy.deepcopy(cm.load())

        mihoyo_cfg = config.get(MIHOYO_SIGN_CONFIG_KEY)
        mihoyo_data = dict(mihoyo_cfg) if isinstance(mihoyo_cfg, dict) else {}
        cookie = self._get_current_mihoyo_cookie_text() or str(mihoyo_data.get("cookie") or "").strip()
        if cookie:
            mihoyo_data["cookie"] = cookie
        if self.mihoyo_browser_dropdown is not None:
            mihoyo_data["browser"] = str(self.mihoyo_browser_dropdown.value or mihoyo_data.get("browser") or "edge")
        mihoyo_data["enabled_games"] = {
            "genshin": bool(self.mihoyo_genshin_checkbox and self.mihoyo_genshin_checkbox.value),
            "honkai_sr": bool(self.mihoyo_hsr_checkbox and self.mihoyo_hsr_checkbox.value),
            "zzz": bool(self.mihoyo_zzz_checkbox and self.mihoyo_zzz_checkbox.value),
        }
        config[MIHOYO_SIGN_CONFIG_KEY] = mihoyo_data

        wuwa_cfg = config.get(WUWA_KUJIEQU_CONFIG_KEY)
        wuwa_data = dict(wuwa_cfg) if isinstance(wuwa_cfg, dict) else {}
        current_profile = self._current_wuwa_kujiequ_profile()
        if current_profile is not None:
            wuwa_data["profile"] = wuwa_kujiequ_profile_to_dict(current_profile)
        config[WUWA_KUJIEQU_CONFIG_KEY] = wuwa_data

        tajiduo_cfg = config.get(TAJIDUO_SIGN_CONFIG_KEY)
        tajiduo_data = dict(tajiduo_cfg) if isinstance(tajiduo_cfg, dict) else {}
        token = (self.tajiduo_token_input.value or "").strip() if self.tajiduo_token_input is not None else ""
        if token:
            tajiduo_data["token"] = token
        deviceid = (self.tajiduo_deviceid_input.value or "").strip() if self.tajiduo_deviceid_input is not None else ""
        if deviceid:
            tajiduo_data["deviceid"] = deviceid
        uid = (self.tajiduo_uid_input.value or "").strip() if self.tajiduo_uid_input is not None else ""
        if uid:
            tajiduo_data["uid"] = uid
        phone = (self.tajiduo_phone_input.value or "").strip() if self.tajiduo_phone_input is not None else ""
        if phone:
            tajiduo_data["phone"] = phone
        password = (self.tajiduo_password_input.value or "").strip() if self.tajiduo_password_input is not None else ""
        if password:
            tajiduo_data["password"] = password
        config[TAJIDUO_SIGN_CONFIG_KEY] = tajiduo_data

        skendfield_cfg = config.get(SKENDFIELD_SIGN_CONFIG_KEY)
        skendfield_data = dict(skendfield_cfg) if isinstance(skendfield_cfg, dict) else {}
        token = (self.skendfield_token_input.value or "").strip() if self.skendfield_token_input is not None else ""
        if token:
            skendfield_data["token"] = token
        if self.skendfield_server_dropdown is not None:
            skendfield_data["server"] = str(self.skendfield_server_dropdown.value or "cn").strip()
        config[SKENDFIELD_SIGN_CONFIG_KEY] = skendfield_data

        endfield_protocol_cfg = config.get(ENDFIELD_PROTOCOL_CONFIG_KEY)
        endfield_protocol_data = dict(endfield_protocol_cfg) if isinstance(endfield_protocol_cfg, dict) else {}
        api_key = (self.endfield_center_api_key_input.value or "").strip() if self.endfield_center_api_key_input is not None else ""
        if api_key:
            endfield_protocol_data["api_key"] = api_key
        framework_token = (self.endfield_center_framework_token_input.value or "").strip() if self.endfield_center_framework_token_input is not None else ""
        if framework_token:
            endfield_protocol_data["framework_token"] = framework_token
        role_id = (self.endfield_center_role_id_input.value or "").strip() if self.endfield_center_role_id_input is not None else ""
        if role_id:
            endfield_protocol_data["role_id"] = role_id
        server_id = (self.endfield_center_server_id_input.value or "").strip() if self.endfield_center_server_id_input is not None else ""
        if server_id:
            endfield_protocol_data["server_id"] = server_id
        config[ENDFIELD_PROTOCOL_CONFIG_KEY] = endfield_protocol_data

        nikki_cfg = config.get("nikki")
        nikki_data = dict(nikki_cfg) if isinstance(nikki_cfg, dict) else {}
        if self.nikki_uid_input is not None:
            nikki_data["uid"] = str(self.nikki_uid_input.value or "").strip()
        if self.nikki_momo_id_input is not None:
            nikki_data["momo_id"] = str(self.nikki_momo_id_input.value or "").strip()
        if self.nikki_momo_token_input is not None:
            nikki_data["momo_token"] = str(self.nikki_momo_token_input.value or "").strip()
        config["nikki"] = nikki_data

        return config

    def _get_nikki_config(self) -> Dict[str, Any]:
        raw = cm.load().get("nikki", {})
        return raw if isinstance(raw, dict) else {}

    def _apply_nikki_config_to_controls(self):
        config = self._get_nikki_config()
        if self.nikki_uid_input is not None:
            self.nikki_uid_input.value = str(config.get("uid") or "")
        if self.nikki_momo_id_input is not None:
            self.nikki_momo_id_input.value = str(config.get("momo_id") or "")
        if self.nikki_momo_token_input is not None:
            self.nikki_momo_token_input.value = str(config.get("momo_token") or "")

    def _start_daily_check(self, e=None, show_toast: bool = True):
        if self.daily_check_busy:
            return
        selected_games = self._selected_daily_game_keys()
        if not selected_games:
            if self.daily_status_text is not None:
                self.daily_status_text.value = "请先勾选至少一款需要查询的游戏。"
                self.daily_status_text.color = self.PALETTE["danger"]
            self.page.update()
            return

        self.controller.logger.info("[每日检查] 用户发起每日进度刷新：%s", "、".join(selected_games))
        if self.daily_status_text is not None:
            self.daily_status_text.value = "正在按当前配置查询每日进度..."
            self.daily_status_text.color = self.PALETTE["accent"]
        self._set_daily_check_busy(True)
        self.page.update()

        config = self._build_daily_check_config_snapshot()
        threading.Thread(
            target=self._run_daily_check_backend,
            args=(config, selected_games, show_toast),
            daemon=True,
        ).start()

    def _run_daily_check_backend(self, config: Dict[str, Any], selected_games: List[str], show_toast: bool):
        try:
            tracker = self.daily_progress_tracker
            result = check_games_progress(config=config, game_keys=selected_games, tracker=tracker)
            self.controller.logger.info(
                "[每日检查] 刷新完成：检查 %d 项，完成 %d 项，总进度 %d%%",
                len(result.games),
                len(result.completed_games),
                result.progress_percent,
            )
            self._emit("daily_check_finished", result=result, show_toast=show_toast)
        except Exception as exc:
            self.controller.logger.error("[每日检查] 刷新失败：%s", str(exc), exc_info=True)
            self._emit("daily_check_failed", message=str(exc), show_toast=show_toast)

    def _format_daily_identity(self, game: GameDailyStatus) -> str:
        parts: List[str] = []
        if game.nickname:
            parts.append(game.nickname)
        if game.uid:
            parts.append(f"UID {game.uid}")
        return " | ".join(parts) if parts else "当前账号信息未返回。"

    def _summarize_daily_extra(self, game: GameDailyStatus) -> str:
        extra = game.extra or {}
        parts: List[str] = []

        resource = extra.get("resin") or extra.get("stamina") or extra.get("energy")
        if isinstance(resource, dict):
            current = resource.get("current")
            maximum = resource.get("max") or resource.get("limit")
            if current is not None and maximum is not None:
                parts.append(f"体力 {current}/{maximum}")
        elif resource not in (None, ""):
            parts.append(f"体力 {resource}")

        home_coin = extra.get("home_coin")
        if isinstance(home_coin, dict):
            current = home_coin.get("current")
            maximum = home_coin.get("max")
            if current is not None and maximum is not None:
                parts.append(f"洞天宝钱 {current}/{maximum}")

        weekly = extra.get("weekly") or extra.get("weekly_mission")
        if isinstance(weekly, dict):
            current = weekly.get("current")
            if current is None:
                current = weekly.get("cur_points")
            maximum = weekly.get("max")
            if maximum is None:
                maximum = weekly.get("max_points")
            if current is not None and maximum is not None:
                parts.append(f"周常 {current}/{maximum}")

        dispatch = extra.get("dispatch")
        if isinstance(dispatch, dict):
            active = dispatch.get("active")
            total = dispatch.get("total")
            if active is not None and total is not None:
                parts.append(f"派遣进行中 {active}/{total}")

        if isinstance(extra.get("has_signed"), bool):
            parts.append("今日已签到" if extra.get("has_signed") else "今日未签到")

        active_days = extra.get("active_days")
        if isinstance(active_days, int) and active_days > 0:
            parts.append(f"活跃 {active_days} 天")

        liveness_val = extra.get("liveness_int")
        liveness_max_val = extra.get("liveness_max")
        if isinstance(liveness_val, int):
            if isinstance(liveness_max_val, int) and liveness_max_val > 0:
                parts.append(f"活跃度 {liveness_val}/{liveness_max_val}")
            elif liveness_val > 0:
                parts.append(f"活跃度 {liveness_val}")

        return "；".join(parts[:4])

    def _build_daily_game_card(self, game: GameDailyStatus) -> ft.Container:
        has_errors = bool(game.errors)
        tone = self.PALETTE["accent"] if game.is_daily_completed else (self.PALETTE["danger"] if has_errors else self.PALETTE["warning"])
        status_label = "已完成" if game.is_daily_completed else ("查询异常" if has_errors else "未完成")

        controls: List[ft.Control] = [
            ft.Row(
                controls=[
                    ft.Column(
                        controls=[
                            ft.Text(game.game_name, size=18, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"]),
                            ft.Text(self._format_daily_identity(game), size=13, color=self.PALETTE["muted"]),
                        ],
                        spacing=4,
                        expand=True,
                    ),
                    ft.Column(
                        controls=[
                            ft.Text(f"{game.progress_percent}%", size=24, weight=ft.FontWeight.BOLD, color=tone),
                            ft.Text(status_label, size=12, color=tone),
                        ],
                        spacing=2,
                        horizontal_alignment=ft.CrossAxisAlignment.END,
                    ),
                ],
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                vertical_alignment=ft.CrossAxisAlignment.START,
            )
        ]

        extra_summary = self._summarize_daily_extra(game)
        if extra_summary:
            controls.append(ft.Text(extra_summary, size=13, color=self.PALETTE["muted"]))

        for error in game.errors:
            controls.append(
                ft.Container(
                    bgcolor=self.PALETTE["danger_soft"],
                    border_radius=12,
                    padding=ft.Padding(12, 10, 12, 10),
                    content=ft.Text(error, size=13, color=self.PALETTE["danger"]),
                )
            )

        for task in game.tasks:
            task_value = f"{task.current}/{task.total}" if task.total > 0 else str(task.current)
            task_note = "已完成" if task.is_completed else f"进度 {task.progress_percent}%"
            controls.append(
                self._build_wuwa_kujiequ_info_line(
                    task.name,
                    task_value,
                    task_note,
                    self.PALETTE["accent"] if task.is_completed else self.PALETTE["warning"],
                )
            )

        if not game.tasks and not game.errors:
            controls.append(self._build_pool_empty_state("当前没有可展示的任务明细。"))

        return ft.Container(
            bgcolor=self.PALETTE["card"],
            border=self._border_all(1, tone if game.is_daily_completed else self.PALETTE["line"]),
            border_radius=16,
            padding=ft.Padding(14, 12, 14, 12),
            content=ft.Column(controls=controls, spacing=10),
        )

    def _render_daily_check_result(self, result: Optional[DailyCheckResult]):
        if self.daily_metrics_row is None or self.daily_result_list is None:
            return

        # 确保暖暖配置已加载到 UI 控件
        self._apply_nikki_config_to_controls()

        self.daily_result = result
        snapshot = self._build_daily_check_config_snapshot()
        selected_games = self._selected_daily_game_keys()

        # 加载今日持久化进度并合并
        persisted = self._load_and_merge_persisted_progress(result)
        self._render_persisted_progress(persisted)

        mihoyo_cfg = snapshot.get(MIHOYO_SIGN_CONFIG_KEY)
        mihoyo_data = mihoyo_cfg if isinstance(mihoyo_cfg, dict) else {}
        mihoyo_cookie = str(mihoyo_data.get("cookie") or "").strip()
        mihoyo_fields = extract_mihoyo_cookie_fields(mihoyo_cookie)
        has_hoyo_record_token = any(key in mihoyo_fields for key in ("stoken", "stoken_v2"))
        hoyo_state = "未配置"
        hoyo_note = "未读取到米游社 Cookie。"
        if mihoyo_cookie:
            if has_hoyo_record_token:
                hoyo_state = "可查"
                hoyo_note = "已带 stoken 级别认证，可尝试请求实时便笺。"
            else:
                hoyo_state = "仅签到"
                hoyo_note = "当前只有签到级别 Cookie；参考 LittlePaimon，实时便笺通常还需要 stoken。"

        has_wuwa_profile = self._current_wuwa_kujiequ_profile() is not None
        has_nikki_auth = bool(
            self.nikki_momo_id_input is not None
            and self.nikki_momo_token_input is not None
            and (self.nikki_momo_id_input.value or "").strip()
            and (self.nikki_momo_token_input.value or "").strip()
        )

        if self.daily_config_text is not None:
            self.daily_config_text.value = (
                f"当前接入状态：米游社便笺 {hoyo_state}；鸣潮库街区 {'已配置' if has_wuwa_profile else '未配置'}；"
                f"暖暖 Pearpal {'已配置' if has_nikki_auth else '未配置'}。{hoyo_note}"
            )

        if result is None:
            if self.daily_status_text is not None and not self.daily_check_busy:
                if persisted.games:
                    total = len(persisted.games)
                    success = sum(1 for g in persisted.games if g.is_all_success)
                    failed = sum(1 for g in persisted.games if g.has_failed)
                    self.daily_status_text.value = (
                        f"上次运行记录：{total} 款游戏，{success} 款完成，{failed} 款失败。"
                        f"点击刷新可查询最新 API 数据。"
                    )
                    self.daily_status_text.color = self.PALETTE["accent"] if failed == 0 else self.PALETTE["warning"]
                else:
                    self.daily_status_text.value = "点击刷新后，会按当前配置真实请求每日 API，并把每个游戏的失败原因原样展示出来。"
                    self.daily_status_text.color = self.PALETTE["muted"]
            controls = [
                self._build_gacha_metric_card("已选游戏", str(len(selected_games)), "这次刷新会实际查询这些游戏。"),
                self._build_gacha_metric_card("米游社便笺", hoyo_state, hoyo_note),
                self._build_gacha_metric_card("鸣潮库街区", "已配置" if has_wuwa_profile else "未配置", "已登录后即可拉取体力、活跃度和签到。"),
                self._build_gacha_metric_card("暖暖 Pearpal", "已配置" if has_nikki_auth else "未配置", "配置 Momo Token / Momo ID 后即可读取 notebook。"),
            ]
            if persisted.games:
                total = len(persisted.games)
                success = sum(1 for g in persisted.games if g.is_all_success)
                failed = sum(1 for g in persisted.games if g.has_failed)
                controls.insert(0, self._build_gacha_metric_card(
                    "今日已记录", str(total),
                    f"完成 {success} / 失败 {failed}，详情见下方「今日进度总览」",
                    color=self.PALETTE["accent"] if failed == 0 else self.PALETTE["warning"],
                ))
            self.daily_metrics_row.controls = controls
            self._set_column_placeholder(self.daily_result_list, "点击“刷新每日进度”后，这里会显示各游戏的真实查询结果、任务进度和错误原因。")
            return

        issue_count = sum(1 for game in result.games if game.errors)
        completed_count = len(result.completed_games)
        tone = self.PALETTE["accent"] if result.games and result.all_completed and issue_count == 0 else self.PALETTE["warning"]
        if self.daily_status_text is not None:
            if result.games:
                self.daily_status_text.value = (
                    f"每日进度刷新完成：{completed_count}/{len(result.games)} 款游戏已完成，"
                    f"总进度 {result.progress_percent}%"
                )
            else:
                self.daily_status_text.value = "当前没有命中任何可查询的游戏配置。"
            self.daily_status_text.color = tone if result.games else self.PALETTE["warning"]

        self.daily_metrics_row.controls = [
            self._build_gacha_metric_card("已检查游戏", str(len(result.games)), "按当前勾选和配置实际发起请求。"),
            self._build_gacha_metric_card("完全完成", str(completed_count), "所有任务均已完成的游戏数量。"),
            self._build_gacha_metric_card("总体进度", f"{result.progress_percent}%", "按每个游戏的任务进度平均计算。"),
            self._build_gacha_metric_card("需处理问题", str(issue_count), "包括认证不足、隐私未开、接口异常等情况。"),
        ]

        if result.games:
            self.daily_result_list.controls = [self._build_daily_game_card(game) for game in result.games]
        else:
            self._set_column_placeholder(self.daily_result_list, "当前没有命中任何可查询的游戏配置。")

    def _load_and_merge_persisted_progress(self, fresh_result: Optional[DailyCheckResult]) -> DailyProgressSnapshot:
        """加载今日持久化进度，并与新鲜 API 结果合并。"""
        if self.daily_progress_tracker is None:
            self.controller.logger.warning("[每日进度] tracker 未初始化，返回空快照")
            return DailyProgressSnapshot(date_key="")
        persisted = self.daily_progress_tracker.load_today()
        self.controller.logger.info(
            "[每日进度] 加载持久化进度: date_key=%s games=%d fresh=%s",
            persisted.date_key, len(persisted.games), fresh_result is not None,
        )
        if fresh_result is None:
            return persisted

        # 用新鲜结果覆盖 community 来源的任务
        fresh_game_map = {g.game_key: g for g in fresh_result.games}
        existing_keys = {g.game_key for g in persisted.games}
        for g in persisted.games:
            if g.game_key in fresh_game_map:
                fresh = fresh_game_map[g.game_key]
                # 保留 script 来源的任务
                script_tasks = [t for t in g.tasks if t.source == "script"]
                # 用 fresh 的 community 任务替换
                g.tasks = script_tasks
                for task in fresh.tasks:
                    g.tasks.append(DailyTaskRecord(
                        task_key=task.name,
                        task_name=task.name,
                        source="community",
                        status="success" if task.is_completed else "pending",
                        message=f"{task.current}/{task.total}",
                        extra={"current": task.current, "total": task.total},
                    ))
                g.uid = fresh.uid
                g.nickname = fresh.nickname
                g.errors = fresh.errors[:]
                g.checked_at = fresh_result.checked_at
        # 添加持久化中没有的新游戏
        for game_key, fresh in fresh_game_map.items():
            if game_key not in existing_keys:
                new_game = GameDailyRecord(
                    game_key=game_key,
                    game_name=fresh.game_name,
                    uid=fresh.uid,
                    nickname=fresh.nickname,
                    errors=fresh.errors[:],
                    checked_at=fresh_result.checked_at,
                )
                for task in fresh.tasks:
                    new_game.tasks.append(DailyTaskRecord(
                        task_key=task.name,
                        task_name=task.name,
                        source="community",
                        status="success" if task.is_completed else "pending",
                        message=f"{task.current}/{task.total}",
                        extra={"current": task.current, "total": task.total},
                    ))
                persisted.games.append(new_game)
                self.controller.logger.info("[每日进度] 新增持久化游戏: %s (%s)", game_key, fresh.game_name)
        return persisted

    def _render_persisted_progress(self, snapshot: DailyProgressSnapshot):
        """渲染今日持久化进度总览。"""
        if self.daily_persisted_row is None or self.daily_persisted_list is None:
            return

        if not snapshot.games:
            self.controller.logger.info("[每日进度] 持久化快照为空，显示占位文本")
            self.daily_persisted_row.controls = [
                self._build_gacha_metric_card("今日记录", "暂无", "还没有任何签到或检查记录。执行签到/刷新后会自动记录。"),
            ]
            self._set_column_placeholder(self.daily_persisted_list, "今日暂无进度记录。执行签到或刷新每日进度后会自动显示。")
            return

        success_count = sum(1 for g in snapshot.games if g.is_all_success)
        failed_count = sum(1 for g in snapshot.games if g.has_failed)
        total_games = len(snapshot.games)
        self.controller.logger.info(
            "[每日进度] 渲染持久化总览: games=%d success=%d failed=%d",
            total_games, success_count, failed_count,
        )

        self.daily_persisted_row.controls = [
            self._build_gacha_metric_card("已记录游戏", str(total_games), "今日有记录的游戏数量"),
            self._build_gacha_metric_card("全部完成", str(success_count), "所有任务均已完成的游戏", color=self.PALETTE["accent"]),
            self._build_gacha_metric_card("存在失败", str(failed_count), "签到失败或查询异常的游戏", color=self.PALETTE["danger"] if failed_count > 0 else self.PALETTE["muted"]),
        ]

        cards: List[ft.Control] = []
        for game in snapshot.games:
            cards.append(self._build_persisted_game_card(game))
        self.daily_persisted_list.controls = cards

    def _build_persisted_game_card(self, game: GameDailyRecord) -> ft.Container:
        """构建持久化进度游戏卡片。"""
        has_errors = bool(game.errors)
        is_success = game.is_all_success
        tone = self.PALETTE["accent"] if is_success else (self.PALETTE["danger"] if has_errors else self.PALETTE["warning"])
        status_label = "已完成" if is_success else ("有失败" if has_errors else "进行中")

        # 顶部状态横幅
        banner = ft.Container(
            bgcolor=tone,
            border_radius=ft.BorderRadius(16, 16, 0, 0),
            padding=ft.Padding(14, 8, 14, 8),
            content=ft.Row(
                controls=[
                    ft.Text(game.game_name, size=16, weight=ft.FontWeight.BOLD, color="white"),
                    ft.Text(f"{status_label}", size=13, color="white"),
                ],
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            ),
        ) if is_success or has_errors else None

        controls: List[ft.Control] = []
        if banner is not None:
            controls.append(banner)
        else:
            controls.append(
                ft.Row(
                    controls=[
                        ft.Text(game.game_name, size=16, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"]),
                        ft.Text(status_label, size=13, color=tone),
                    ],
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                )
            )

        if game.nickname or game.uid:
            controls.append(ft.Text(f"{game.nickname} | UID {game.uid}", size=12, color=self.PALETTE["muted"]))

        # 按来源分组显示任务
        community_tasks = [t for t in game.tasks if t.source == "community"]
        script_tasks = [t for t in game.tasks if t.source == "script"]

        if community_tasks:
            controls.append(ft.Text("社区便笺", size=12, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"]))
            for task in community_tasks:
                task_color = self.PALETTE["accent"] if task.status in ("success", "already") else (
                    self.PALETTE["danger"] if task.status == "failed" else self.PALETTE["warning"]
                )
                controls.append(
                    self._build_wuwa_kujiequ_info_line(
                        task.task_name,
                        "已完成" if task.status in ("success", "already") else (
                            "失败" if task.status == "failed" else "未完成"
                        ),
                        task.message or "",
                        task_color,
                    )
                )

        if script_tasks:
            controls.append(ft.Text("每日一条龙", size=12, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"]))
            for task in script_tasks:
                task_color = self.PALETTE["accent"] if task.status in ("success", "already") else (
                    self.PALETTE["danger"] if task.status == "failed" else self.PALETTE["warning"]
                )
                controls.append(
                    self._build_wuwa_kujiequ_info_line(
                        task.task_name,
                        "已完成" if task.status in ("success", "already") else (
                            "失败" if task.status == "failed" else "未完成"
                        ),
                        task.message or "",
                        task_color,
                    )
                )

        for error in game.errors:
            controls.append(
                ft.Container(
                    bgcolor=self.PALETTE["danger_soft"],
                    border_radius=12,
                    padding=ft.Padding(12, 10, 12, 10),
                    content=ft.Text(error, size=13, color=self.PALETTE["danger"]),
                )
            )

        if not game.tasks and not game.errors:
            controls.append(self._build_pool_empty_state("暂无任务记录。"))

        return ft.Container(
            bgcolor=self.PALETTE["card"],
            border=self._border_all(1, tone if (is_success or has_errors) else self.PALETTE["line"]),
            border_radius=16,
            padding=ft.Padding(14, 12, 14, 12) if banner is None else ft.Padding(14, 0, 14, 12),
            content=ft.Column(controls=controls, spacing=8),
        )

    def _build_gacha_view(self) -> ft.Container:
        mihoyo_cfg = self._get_mihoyo_sign_config()
        enabled_games = mihoyo_cfg.get("enabled_games") or {}

        self.mihoyo_browser_dropdown = ft.Dropdown(
            label="浏览器",
            options=[
                ft.dropdown.Option(key="edge", text="Microsoft Edge"),
                ft.dropdown.Option(key="chrome", text="Google Chrome"),
                ft.dropdown.Option(key="brave", text="Brave"),
            ],
            value=str(mihoyo_cfg.get("browser") or "edge"),
            on_select=self._on_mihoyo_browser_change,
            border_color=self.PALETTE["line"],
            filled=True,
            fill_color=self.PALETTE["panel_alt"],
            dense=True,
            content_padding=10,
            width=220,
        )
        self.mihoyo_cookie_input = ft.TextField(
            label="米游社 Cookie",
            value=str(mihoyo_cfg.get("cookie") or ""),
            hint_text="登录浏览器后可一键抓取；也可手动粘贴完整 Cookie。",
            password=True,
            can_reveal_password=True,
            on_change=self._on_mihoyo_cookie_input_change,
            border_color=self.PALETTE["line"],
            filled=True,
            fill_color=self.PALETTE["panel_alt"],
        )
        self.mihoyo_open_login_button = ft.OutlinedButton("打开米游社登录页", icon=FT_ICONS.OPEN_IN_BROWSER, on_click=self._open_mihoyo_login_page)
        self.mihoyo_cookie_button = ft.OutlinedButton("从浏览器抓取 Cookie", icon=FT_ICONS.KEY, on_click=self._start_mihoyo_cookie_extract)
        self.mihoyo_qr_button = ft.OutlinedButton("App 扫码登录", on_click=self._start_mihoyo_qr_login)
        self.mihoyo_sign_button = ft.ElevatedButton("立即执行签到", icon=FT_ICONS.CHECK_CIRCLE_OUTLINE, on_click=self._start_mihoyo_sign)
        self.mihoyo_clear_button = ft.TextButton("清空 Cookie", on_click=self._clear_mihoyo_cookie)
        self.mihoyo_genshin_checkbox = ft.Checkbox(label="原神", value=bool(enabled_games.get("genshin", True)), on_change=self._on_mihoyo_game_toggle)
        self.mihoyo_hsr_checkbox = ft.Checkbox(label="星穹铁道", value=bool(enabled_games.get("honkai_sr", True)), on_change=self._on_mihoyo_game_toggle)
        self.mihoyo_zzz_checkbox = ft.Checkbox(label="绝区零", value=bool(enabled_games.get("zzz", True)), on_change=self._on_mihoyo_game_toggle)
        self.mihoyo_startup_checkbox = ft.Checkbox(
            label="启动一条龙前，若本轮包含对应游戏则自动签到",
            value=bool(mihoyo_cfg.get("startup_sign", False)),
            on_change=self._on_mihoyo_startup_toggle,
        )
        self.mihoyo_status_text = ft.Text(
            "先在浏览器登录米游社，再抓取 Cookie。当前只做原神、星铁、绝区零游戏签到，不启用 BBS 模块。",
            size=13,
            color=self.PALETTE["muted"],
        )
        self.mihoyo_account_text = ft.Text("还没有加载米游社 Cookie。", size=13, color=self.PALETTE["muted"])
        self.mihoyo_qr_image = ft.Image(src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7", width=180, height=180, visible=False)
        self.mihoyo_qr_hint_text = ft.Text(
            "如果浏览器 Cookie 只有签到权限，可用米游社 App 扫码直接补齐战绩/实时便笺所需登录态。",
            size=13,
            color=self.PALETTE["muted"],
        )
        self.mihoyo_qr_panel = ft.Container(
            visible=False,
            bgcolor=self.PALETTE["panel_alt"],
            border=self._border_all(1, self.PALETTE["line"]),
            border_radius=16,
            padding=ft.Padding(14, 12, 14, 12),
            content=ft.Column(
                controls=[
                    ft.Text("App 扫码登录", size=16, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"]),
                    self.mihoyo_qr_hint_text,
                    self.mihoyo_qr_image,
                ],
                spacing=8,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )
        self.mihoyo_metrics_row = ft.Row(wrap=True, spacing=10)
        self.mihoyo_result_list = ft.Column(spacing=8)

        self.mihoyo_phone_input = ft.TextField(
            label="手机号",
            hint_text="输入 +86 手机号，不含国家代码",
            width=180,
            border_color=self.PALETTE["line"],
            filled=True,
            fill_color=self.PALETTE["panel_alt"],
            dense=True,
        )
        self.mihoyo_send_sms_button = ft.OutlinedButton("发送验证码", icon=FT_ICONS.SEND, on_click=self._start_mihoyo_sms_captcha)
        self.mihoyo_sms_code_input = ft.TextField(
            label="短信验证码",
            hint_text="输入 6 位验证码",
            width=150,
            border_color=self.PALETTE["line"],
            filled=True,
            fill_color=self.PALETTE["panel_alt"],
            dense=True,
        )
        self.mihoyo_sms_login_button = ft.ElevatedButton("验证码登录", icon=FT_ICONS.LOGIN, on_click=self._start_mihoyo_sms_login)

        self.mihoyo_pw_account_input = ft.TextField(
            label="米游社账号",
            hint_text="手机号",
            width=180,
            border_color=self.PALETTE["line"],
            filled=True,
            fill_color=self.PALETTE["panel_alt"],
            dense=True,
        )
        self.mihoyo_pw_password_input = ft.TextField(
            label="米游社密码",
            hint_text="输入密码",
            password=True,
            can_reveal_password=True,
            width=180,
            border_color=self.PALETTE["line"],
            filled=True,
            fill_color=self.PALETTE["panel_alt"],
            dense=True,
        )
        self.mihoyo_pw_login_button = ft.ElevatedButton("密码登录", icon=FT_ICONS.LOCK, on_click=self._start_mihoyo_password_login)

        mihoyo_panel = self._panel(
            ft.Column(
                controls=[
                    ft.Container(
                        bgcolor=self.PALETTE["accent_soft"],
                        border_radius=18,
                        padding=ft.Padding(16, 14, 16, 14),
                        content=ft.Row(
                            controls=[
                                ft.Container(
                                    width=52,
                                    height=52,
                                    border_radius=16,
                                    bgcolor=self.PALETTE["card"],
                                    content=ft.Icon(FT_ICONS.AUTO_AWESOME, color=self.PALETTE["accent"], size=28),
                                    alignment=ft.Alignment(0, 0),
                                ),
                                ft.Column(
                                    controls=[
                                        ft.Text("米游社游戏签到", size=20, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"]),
                                        ft.Text(
                                            "手动网页登录，程序从本机浏览器会话读取 Cookie，再只执行原神 / 星铁 / 绝区零签到。",
                                            size=13,
                                            color=self.PALETTE["muted"],
                                        ),
                                    ],
                                    spacing=4,
                                    expand=True,
                                ),
                            ],
                            spacing=12,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                    ),
                    ft.Row(controls=[self.mihoyo_browser_dropdown, self.mihoyo_cookie_input], spacing=8, wrap=True),
                    ft.Row(
                        controls=[
                            self.mihoyo_open_login_button,
                            self.mihoyo_cookie_button,
                            self.mihoyo_qr_button,
                            self.mihoyo_sign_button,
                            self.mihoyo_clear_button,
                        ],
                        spacing=8,
                        wrap=True,
                    ),
                    ft.Row(
                        controls=[
                            self.mihoyo_genshin_checkbox,
                            self.mihoyo_hsr_checkbox,
                            self.mihoyo_zzz_checkbox,
                        ],
                        spacing=8,
                        wrap=True,
                    ),
                    self.mihoyo_startup_checkbox,
                    self.mihoyo_qr_panel,
                    ft.Row(
                        controls=[
                            self.mihoyo_phone_input,
                            self.mihoyo_send_sms_button,
                            self.mihoyo_sms_code_input,
                            self.mihoyo_sms_login_button,
                        ],
                        spacing=8,
                        wrap=True,
                    ),
                    ft.Row(
                        controls=[
                            self.mihoyo_pw_account_input,
                            self.mihoyo_pw_password_input,
                            self.mihoyo_pw_login_button,
                        ],
                        spacing=8,
                        wrap=True,
                    ),
                    self.mihoyo_status_text,
                    self.mihoyo_account_text,
                    self.mihoyo_metrics_row,
                    self.mihoyo_result_list,
                ],
                spacing=10,
            )
        )

        self.gacha_path_input = ft.TextField(
            label="UIGF JSON 路径",
            hint_text="粘贴原神 / 崩铁 / 绝区零抽卡 JSON 路径，或点击下方浏览 JSON。",
            border_color=self.PALETTE["line"],
            filled=True,
            fill_color=self.PALETTE["panel_alt"],
        )
        self.gacha_browse_button = ft.OutlinedButton("浏览 JSON", icon=FT_ICONS.FOLDER_OPEN, on_click=self._pick_gacha_file)
        self.gacha_import_button = ft.ElevatedButton("导入到数据库", on_click=self._start_gacha_import)
        self.gacha_reload_button = ft.TextButton("刷新统计", on_click=self._reload_gacha_data)
        self.gacha_import_summary = ft.Text("导入时会自动识别原神、崩铁或绝区零，并按 UID 去重更新。", size=13, color=self.PALETTE["muted"])

        self.gacha_game_dropdown = ft.Dropdown(
            label="查看游戏",
            options=[ft.dropdown.Option(key=game_code, text=game_name) for game_code, game_name in HOYO_GAME_OPTIONS],
            value=self.gacha_current_game_code,
            on_select=self._on_gacha_game_change,
            border_color=self.PALETTE["line"],
            filled=True,
            fill_color=self.PALETTE["panel_alt"],
            dense=True,
            content_padding=10,
            width=220,
        )

        self.gacha_uid_dropdown = ft.Dropdown(
            label="查看 UID",
            options=[],
            value=None,
            on_select=self._on_gacha_uid_change,
            border_color=self.PALETTE["line"],
            filled=True,
            fill_color=self.PALETTE["panel_alt"],
            dense=True,
            content_padding=10,
            width=240,
        )
        self.gacha_meta_text = ft.Text("数据库里还没有 HoYo 抽卡记录。", size=13, color=self.PALETTE["muted"])
        self.gacha_banner_text = ft.Text("先导入一个 UIGF JSON，下面会按游戏和 UID 展示卡池分析。", size=13, color=self.PALETTE["muted"])
        self.gacha_overview_cards = ft.Row(wrap=True, spacing=10)
        self.gacha_pool_selector = ft.Row(wrap=True, spacing=8)
        self.gacha_pool_content = ft.Container(content=self._build_pool_empty_state("分析后会在这里显示卡池明细。"), height=400)
        self.gacha_pool_tabs = ft.Column(
            controls=[self.gacha_pool_selector, self.gacha_pool_content],
            spacing=12,
        )
        self.gacha_high_rarity_title_text = ft.Text("最高稀有度历史", size=20, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"])
        self.gacha_high_rarity_note_text = ft.Text("按最近记录倒序展示，并计算所在保底组的出货抽数。", size=13, color=self.PALETTE["muted"])
        self.gacha_high_rarity_list = ft.Column(spacing=6, scroll=ft.ScrollMode.AUTO, height=400)
        self.gacha_character_list = ft.Column(spacing=6, scroll=ft.ScrollMode.AUTO, height=400)
        self.gacha_weapon_list = ft.Column(spacing=6, scroll=ft.ScrollMode.AUTO, height=400)
        self.gacha_pity_text = ft.Text("保底摘要会显示在这里。", size=13, color=self.PALETTE["muted"])

        import_panel = self._panel(
            ft.Column(
                controls=[
                    ft.Text("HoYo 抽卡导入", size=20, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"]),
                    ft.Text(
                        "支持直接导入 UIGF v4.2 JSON。导入后会自动识别游戏与 UID，重复导入只会按记录 ID 更新。",
                        size=13,
                        color=self.PALETTE["muted"],
                    ),
                    self.gacha_path_input,
                    ft.Row(
                        controls=[self.gacha_browse_button, self.gacha_import_button, self.gacha_reload_button],
                        spacing=8,
                        wrap=True,
                    ),
                    self.gacha_import_summary,
                ],
                spacing=10,
            )
        )

        overview_panel = self._panel(
            ft.Column(
                controls=[
                    ft.Row(
                        controls=[
                            ft.Column(
                                controls=[
                                    ft.Text("抽卡总览", size=20, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"]),
                                    ft.Text("切换游戏或 UID 后，下面的卡池、历史和统计会一起刷新。", size=13, color=self.PALETTE["muted"]),
                                ],
                                spacing=4,
                                expand=True,
                            ),
                            self.gacha_game_dropdown,
                            self.gacha_uid_dropdown,
                        ],
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        vertical_alignment=ft.CrossAxisAlignment.START,
                    ),
                    self.gacha_meta_text,
                    self.gacha_banner_text,
                    self.gacha_overview_cards,
                ],
                spacing=10,
            )
        )

        pool_panel = self._panel(
            ft.Column(
                controls=[
                    ft.Text("各卡池详细分析", size=20, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"]),
                    self.gacha_pool_tabs,
                ],
                spacing=10,
            )
        )

        high_rarity_panel = self._panel(
            ft.Column(
                controls=[
                    self.gacha_high_rarity_title_text,
                    self.gacha_high_rarity_note_text,
                    self.gacha_high_rarity_list,
                ],
                spacing=10,
            )
        )

        character_panel = self._panel(
            ft.Column(
                controls=[
                    ft.Text("角色统计", size=20, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"]),
                    self.gacha_character_list,
                ],
                spacing=10,
            )
        )

        weapon_panel = self._panel(
            ft.Column(
                controls=[
                    ft.Text("武器统计", size=20, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"]),
                    self.gacha_weapon_list,
                ],
                spacing=10,
            )
        )

        pity_panel = self._panel(
            ft.Column(
                controls=[
                    ft.Text("保底摘要", size=20, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"]),
                    self.gacha_pity_text,
                ],
                spacing=10,
            )
        )

        view = ft.Container(
            visible=False,
            padding=ft.Padding(18, 16, 18, 16),
            content=ft.Column(
                controls=[mihoyo_panel, import_panel, overview_panel, pool_panel, high_rarity_panel, character_panel, weapon_panel, pity_panel],
                spacing=12,
                scroll=ft.ScrollMode.AUTO,
            ),
        )
        self._apply_mihoyo_sign_config_to_controls()
        self._render_mihoyo_sign_result(None)
        return view

    def _get_mihoyo_sign_config(self) -> Dict[str, Any]:
        raw = cm.load().get(MIHOYO_SIGN_CONFIG_KEY, {})
        return raw if isinstance(raw, dict) else {}

    def _update_mihoyo_sign_config(self, **updates: Any) -> Dict[str, Any]:
        config = cm.load()
        section = config.get(MIHOYO_SIGN_CONFIG_KEY)
        data = dict(section) if isinstance(section, dict) else {}
        data.update(updates)
        config[MIHOYO_SIGN_CONFIG_KEY] = data
        cm.save(config)
        return data

    def _set_mihoyo_sign_busy(self, busy: bool):
        self.mihoyo_sign_busy = busy
        for control in (
            self.mihoyo_browser_dropdown,
            self.mihoyo_cookie_input,
            self.mihoyo_cookie_button,
            self.mihoyo_open_login_button,
            self.mihoyo_qr_button,
            self.mihoyo_sign_button,
            self.mihoyo_clear_button,
            self.mihoyo_genshin_checkbox,
            self.mihoyo_hsr_checkbox,
            self.mihoyo_zzz_checkbox,
            self.mihoyo_startup_checkbox,
        ):
            if control is not None:
                control.disabled = busy

    def _reset_mihoyo_qr_prompt(self):
        self.mihoyo_qr_session_url = ""
        if self.mihoyo_qr_panel is not None:
            self.mihoyo_qr_panel.visible = False
        if self.mihoyo_qr_image is not None:
            self.mihoyo_qr_image.src = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
            self.mihoyo_qr_image.visible = False
        if self.mihoyo_qr_hint_text is not None:
            self.mihoyo_qr_hint_text.value = "如果浏览器 Cookie 只有签到权限，可用米游社 App 扫码直接补齐战绩/实时便笺所需登录态。"
            self.mihoyo_qr_hint_text.color = self.PALETTE["muted"]

    def _show_mihoyo_qr_prompt(self, message: str, qr_base64: str = "", url: str = ""):
        self.mihoyo_qr_session_url = url
        if self.mihoyo_qr_panel is not None:
            self.mihoyo_qr_panel.visible = True
        if self.mihoyo_qr_image is not None:
            if qr_base64:
                self.mihoyo_qr_image.src = f"data:image/png;base64,{qr_base64}"
            self.mihoyo_qr_image.visible = bool(qr_base64)
        if self.mihoyo_qr_hint_text is not None:
            self.mihoyo_qr_hint_text.value = message
            self.mihoyo_qr_hint_text.color = self.PALETTE["text"] if qr_base64 else self.PALETTE["muted"]

    def _selected_mihoyo_game_keys(self) -> List[str]:
        selected: List[str] = []
        if self.mihoyo_genshin_checkbox is not None and self.mihoyo_genshin_checkbox.value:
            selected.append("genshin")
        if self.mihoyo_hsr_checkbox is not None and self.mihoyo_hsr_checkbox.value:
            selected.append("honkai_sr")
        if self.mihoyo_zzz_checkbox is not None and self.mihoyo_zzz_checkbox.value:
            selected.append("zzz")
        return selected

    def _apply_mihoyo_game_selection(self, enabled_games: Dict[str, Any]):
        if self.mihoyo_genshin_checkbox is not None:
            self.mihoyo_genshin_checkbox.value = bool(enabled_games.get("genshin", True))
        if self.mihoyo_hsr_checkbox is not None:
            self.mihoyo_hsr_checkbox.value = bool(enabled_games.get("honkai_sr", True))
        if self.mihoyo_zzz_checkbox is not None:
            self.mihoyo_zzz_checkbox.value = bool(enabled_games.get("zzz", True))

    def _apply_mihoyo_sign_config_to_controls(self):
        config = self._get_mihoyo_sign_config()
        if self.mihoyo_browser_dropdown is not None:
            browser_value = str(config.get("browser") or "edge")
            self.mihoyo_browser_dropdown.value = browser_value if browser_value in {"edge", "chrome", "brave"} else "edge"
        self.mihoyo_cookie_draft = str(config.get("cookie") or "")
        if self.mihoyo_cookie_input is not None:
            self.mihoyo_cookie_input.value = self.mihoyo_cookie_draft
        self._apply_mihoyo_game_selection(config.get("enabled_games") or {})
        if self.mihoyo_startup_checkbox is not None:
            self.mihoyo_startup_checkbox.value = bool(config.get("startup_sign", False))

    def _get_current_mihoyo_stoken_cookie_text(self) -> str:
        config = self._get_mihoyo_sign_config()
        return tidy_mihoyo_cookie(str(config.get("stoken_cookie") or ""))

    def _get_current_mihoyo_cookie_text(self) -> str:
        raw = self.mihoyo_cookie_draft
        if self.mihoyo_cookie_input is not None and self.mihoyo_cookie_input.value:
            raw = str(self.mihoyo_cookie_input.value)
        return tidy_mihoyo_cookie(raw)

    def _on_mihoyo_cookie_input_change(self, e):
        self.mihoyo_cookie_draft = str(e.control.value or "")
        if not self.mihoyo_sign_busy:
            self._render_mihoyo_sign_result(None)
            self.page.update()

    def _on_mihoyo_game_toggle(self, e):
        self._update_mihoyo_sign_config(
            enabled_games={
                "genshin": bool(self.mihoyo_genshin_checkbox and self.mihoyo_genshin_checkbox.value),
                "honkai_sr": bool(self.mihoyo_hsr_checkbox and self.mihoyo_hsr_checkbox.value),
                "zzz": bool(self.mihoyo_zzz_checkbox and self.mihoyo_zzz_checkbox.value),
            }
        )
        self._render_mihoyo_sign_result(None)
        self.page.update()

    def _on_mihoyo_startup_toggle(self, e):
        self._update_mihoyo_sign_config(startup_sign=bool(e.control.value))
        self._render_mihoyo_sign_result(None)
        self.page.update()

    def _on_mihoyo_browser_change(self, e):
        self._update_mihoyo_sign_config(browser=str(e.control.value or "edge"))

    def _open_mihoyo_login_page(self, e):
        login_url = get_mihoyo_login_url(self._selected_mihoyo_game_keys()[0] if self._selected_mihoyo_game_keys() else "genshin")
        try:
            webbrowser.open(login_url)
            self.controller.logger.info("[米游社] 已从 Flet 页面打开登录页：%s", login_url)
            if self.mihoyo_status_text is not None:
                self.mihoyo_status_text.value = "已打开米游社登录页，请在浏览器完成登录后回到这里抓取 Cookie。"
                self.mihoyo_status_text.color = self.PALETTE["accent"]
            self.page.update()
        except Exception as exc:
            self.controller.logger.error("[米游社] 打开登录页失败：%s", str(exc), exc_info=True)
            if self.mihoyo_status_text is not None:
                self.mihoyo_status_text.value = f"打开登录页失败：{str(exc)}"
                self.mihoyo_status_text.color = self.PALETTE["danger"]
            self.page.update()

    def _clear_mihoyo_cookie(self, e):
        if self.mihoyo_sign_busy:
            return
        self.controller.logger.info("[米游社] 用户已在 Flet 页面清空保存的 Cookie")
        self.mihoyo_cookie_draft = ""
        self._update_mihoyo_sign_config(cookie="", stoken_cookie="")
        self._reset_mihoyo_qr_prompt()
        if self.mihoyo_cookie_input is not None:
            self.mihoyo_cookie_input.value = ""
        if self.mihoyo_status_text is not None:
            self.mihoyo_status_text.value = "已清空米游社 Cookie。"
            self.mihoyo_status_text.color = self.PALETTE["accent"]
        self._render_mihoyo_sign_result(None)
        self.page.update()

    def _start_mihoyo_cookie_extract(self, e):
        if self.mihoyo_sign_busy:
            return
        browser_name = str(self.mihoyo_browser_dropdown.value or "edge") if self.mihoyo_browser_dropdown is not None else "edge"
        self.controller.logger.info("[米游社] 用户发起浏览器 Cookie 读取，浏览器：%s", browser_name)
        self._reset_mihoyo_qr_prompt()
        if self.mihoyo_status_text is not None:
            self.mihoyo_status_text.value = f"正在从 {browser_name} 读取米游社 Cookie..."
            self.mihoyo_status_text.color = self.PALETTE["accent"]
        self._set_mihoyo_sign_busy(True)
        self.page.update()
        threading.Thread(target=self._run_mihoyo_cookie_extract_backend, args=(browser_name,), daemon=True).start()

    def _run_mihoyo_cookie_extract_backend(self, browser_name: str):
        import pythoncom
        pythoncom.CoInitialize()
        try:
            result = extract_mihoyo_cookie_from_browser(browser_name)
            self._emit("mihoyo_cookie_loaded", browser=browser_name, result=result)
        except Exception as exc:
            self._emit("mihoyo_sign_failed", stage="读取浏览器 Cookie", message=str(exc))
        finally:
            pythoncom.CoUninitialize()

    def _start_mihoyo_qr_login(self, e):
        if self.mihoyo_sign_busy:
            return
        self.controller.logger.info("[米游社] 用户发起 App 扫码登录")
        if self.mihoyo_status_text is not None:
            self.mihoyo_status_text.value = "正在生成米游社扫码二维码，请用米游社 App 扫描弹出的二维码..."
            self.mihoyo_status_text.color = self.PALETTE["accent"]
        self._set_mihoyo_sign_busy(True)
        self.page.update()
        threading.Thread(target=self._run_mihoyo_qr_login_backend, daemon=True).start()

    def _run_mihoyo_qr_login_backend(self):
        try:
            browser = str(self.mihoyo_browser_dropdown.value or "edge") if self.mihoyo_browser_dropdown is not None else "edge"
            result = complete_mihoyo_qr_login(browser_name=browser)
            self._emit("mihoyo_qr_login_finished", result=result)
        except Exception as exc:
            self._emit("mihoyo_sign_failed", stage="米游社扫码登录", message=str(exc))

    def _start_mihoyo_sms_captcha(self, e):
        if self.mihoyo_sign_busy:
            return
        phone = str(self.mihoyo_phone_input.value or "").strip()
        if not phone or not phone.isdigit() or len(phone) != 11:
            if self.mihoyo_status_text is not None:
                self.mihoyo_status_text.value = "请输入正确的 11 位手机号。"
                self.mihoyo_status_text.color = self.PALETTE["danger"]
            self.page.update()
            return
        self.mihoyo_sms_phone = phone
        self.controller.logger.info("[米游社] 用户发起手机验证码发送，手机号 %s", phone[:3] + "****" + phone[7:])
        if self.mihoyo_status_text is not None:
            self.mihoyo_status_text.value = f"正在向 {phone[:3]}****{phone[7:]} 发送验证码..."
            self.mihoyo_status_text.color = self.PALETTE["accent"]
        self._set_mihoyo_sign_busy(True)
        if self.mihoyo_send_sms_button is not None:
            self.mihoyo_send_sms_button.disabled = True
        self.page.update()
        threading.Thread(target=self._run_mihoyo_sms_captcha_backend, args=(phone,), daemon=True).start()

    def _run_mihoyo_sms_captcha_backend(self, phone: str):
        try:
            send_mihoyo_mobile_captcha(phone)
            self._emit("mihoyo_sms_sent", phone=phone)
        except Exception as exc:
            self._emit("mihoyo_sign_failed", stage="发送验证码", message=str(exc))

    def _start_mihoyo_sms_login(self, e):
        if self.mihoyo_sign_busy:
            return
        phone = self.mihoyo_sms_phone or str(self.mihoyo_phone_input.value or "").strip()
        captcha = str(self.mihoyo_sms_code_input.value or "").strip()
        if not phone or len(phone) != 11:
            if self.mihoyo_status_text is not None:
                self.mihoyo_status_text.value = "请先输入手机号并发送验证码。"
                self.mihoyo_status_text.color = self.PALETTE["danger"]
            self.page.update()
            return
        if not captcha or len(captcha) < 4:
            if self.mihoyo_status_text is not None:
                self.mihoyo_status_text.value = "请输入收到的短信验证码。"
                self.mihoyo_status_text.color = self.PALETTE["danger"]
            self.page.update()
            return
        self.controller.logger.info("[米游社] 用户发起手机验证码登录")
        if self.mihoyo_status_text is not None:
            self.mihoyo_status_text.value = "正在使用手机验证码登录米游社..."
            self.mihoyo_status_text.color = self.PALETTE["accent"]
        self._set_mihoyo_sign_busy(True)
        self.page.update()
        threading.Thread(target=self._run_mihoyo_sms_login_backend, args=(phone, captcha), daemon=True).start()

    def _run_mihoyo_sms_login_backend(self, phone: str, captcha: str):
        try:
            browser = str(self.mihoyo_browser_dropdown.value or "edge") if self.mihoyo_browser_dropdown is not None else "edge"
            result = complete_mihoyo_sms_login(phone, captcha, browser_name=browser)
            self._emit("mihoyo_sms_login_finished", result=result)
        except Exception as exc:
            self._emit("mihoyo_sign_failed", stage="手机验证码登录", message=str(exc))

    def _start_mihoyo_password_login(self, e=None):
        if self.mihoyo_sign_busy:
            return
        account = str(self.mihoyo_pw_account_input.value or "").strip() if self.mihoyo_pw_account_input is not None else ""
        password = str(self.mihoyo_pw_password_input.value or "").strip() if self.mihoyo_pw_password_input is not None else ""
        if not account:
            if self.mihoyo_status_text is not None:
                self.mihoyo_status_text.value = "请输入米游社账号（手机号）。"
                self.mihoyo_status_text.color = self.PALETTE["danger"]
            self.page.update()
            return
        if not password:
            if self.mihoyo_status_text is not None:
                self.mihoyo_status_text.value = "请输入米游社密码。"
                self.mihoyo_status_text.color = self.PALETTE["danger"]
            self.page.update()
            return
        self.controller.logger.info("[米游社] 用户发起密码登录")
        if self.mihoyo_status_text is not None:
            self.mihoyo_status_text.value = "正在使用密码登录米游社..."
            self.mihoyo_status_text.color = self.PALETTE["accent"]
        self._set_mihoyo_sign_busy(True)
        self.page.update()
        threading.Thread(target=self._run_mihoyo_password_login_backend, args=(account, password), daemon=True).start()

    def _run_mihoyo_password_login_backend(self, account: str, password: str):
        try:
            browser = str(self.mihoyo_browser_dropdown.value or "edge") if self.mihoyo_browser_dropdown is not None else "edge"
            result = complete_mihoyo_password_login(account, password, browser_name=browser)
            self._emit("mihoyo_password_login_finished", result=result)
        except Exception as exc:
            self._emit("mihoyo_sign_failed", stage="密码登录", message=str(exc))

    def _start_mihoyo_sign(self, e=None, show_toast: bool = True):
        if self.mihoyo_sign_busy:
            return
        cookie = self._get_current_mihoyo_cookie_text()
        selected_games = self._selected_mihoyo_game_keys()
        if not cookie:
            if self.mihoyo_status_text is not None:
                self.mihoyo_status_text.value = "请先填写或抓取米游社 Cookie。"
                self.mihoyo_status_text.color = self.PALETTE["danger"]
            self.page.update()
            return
        validation = validate_mihoyo_cookie_text(cookie)
        if not validation["ok"]:
            self.controller.logger.warning("[米游社] 手动 Cookie 校验失败：%s", validation["message"])
            if self.mihoyo_status_text is not None:
                self.mihoyo_status_text.value = f"手动 Cookie 校验失败：{validation['message']}"
                self.mihoyo_status_text.color = self.PALETTE["danger"]
            self._render_mihoyo_sign_result(None)
            self.page.update()
            return
        if not selected_games:
            if self.mihoyo_status_text is not None:
                self.mihoyo_status_text.value = "请先勾选至少一个需要签到的 HoYo 游戏。"
                self.mihoyo_status_text.color = self.PALETTE["danger"]
            self.page.update()
            return
        self.controller.logger.info("[米游社] 用户发起手动签到，游戏：%s", "、".join(selected_games))
        self.controller.logger.info(
            "[米游社] 手动 Cookie 本地校验通过：账号 %s，字段 %s",
            validation["account_display"],
            "、".join(sorted(validation["fields"].keys())),
        )
        browser_name = str(self.mihoyo_browser_dropdown.value or "edge") if self.mihoyo_browser_dropdown is not None else "edge"
        self._update_mihoyo_sign_config(
            cookie=cookie,
            browser=browser_name,
            enabled_games={
                "genshin": "genshin" in selected_games,
                "honkai_sr": "honkai_sr" in selected_games,
                "zzz": "zzz" in selected_games,
            },
            startup_sign=bool(self.mihoyo_startup_checkbox and self.mihoyo_startup_checkbox.value),
        )
        if self.mihoyo_status_text is not None:
            self.mihoyo_status_text.value = "正在执行米游社游戏签到..."
            self.mihoyo_status_text.color = self.PALETTE["accent"]
        self._set_mihoyo_sign_busy(True)
        self.page.update()
        threading.Thread(target=self._run_mihoyo_sign_backend, args=(cookie, selected_games, show_toast), daemon=True).start()

    def _run_mihoyo_sign_backend(self, cookie: str, selected_games: List[str], show_toast: bool):
        try:
            result = sign_mihoyo_games(cookie, selected_games)
            self._emit("mihoyo_sign_finished", result=result, show_toast=show_toast)
        except Exception as exc:
            self._emit("mihoyo_sign_failed", stage="米游社签到", message=str(exc))

    def _render_mihoyo_sign_result(self, result: Optional[Dict[str, Any]]):
        if self.mihoyo_metrics_row is None or self.mihoyo_result_list is None:
            return
        config = self._get_mihoyo_sign_config()
        cookie = self._get_current_mihoyo_cookie_text() or tidy_mihoyo_cookie(str(config.get("cookie") or ""))
        validation = validate_mihoyo_cookie_text(cookie)
        fields = validation["fields"]
        browser_name = str(config.get("browser") or "edge")
        startup_enabled = bool(self.mihoyo_startup_checkbox and self.mihoyo_startup_checkbox.value)
        enabled_games = self._selected_mihoyo_game_keys()
        record_ready = bool(fields.get("stoken") or fields.get("stoken_v2"))

        if self.mihoyo_account_text is not None:
            if not cookie:
                self.mihoyo_account_text.value = "还没有加载米游社 Cookie。"
                self.mihoyo_account_text.color = self.PALETTE["muted"]
            else:
                account_id = fields.get("account_id_v2") or fields.get("account_id") or fields.get("ltuid_v2") or fields.get("ltuid") or fields.get("stuid_v2") or fields.get("stuid") or "-"
                login_ticket = fields.get("login_ticket") or "未发现"
                if validation["ok"]:
                    self.mihoyo_account_text.value = (
                        f"当前账号 ID: {account_id} | login_ticket: {'已读取' if login_ticket != '未发现' else '未发现'} | "
                        f"stoken: {'已读取' if record_ready else '未发现'} | "
                        f"关键字段 {len(fields)} 个 | 浏览器: {browser_name}"
                    )
                    self.mihoyo_account_text.color = self.PALETTE["text"]
                else:
                    self.mihoyo_account_text.value = f"当前 Cookie 不完整：{validation['message']}"
                    self.mihoyo_account_text.color = self.PALETTE["warning"]

        if result is None:
            self.mihoyo_metrics_row.controls = [
                self._build_gacha_metric_card("Cookie 状态", "已加载" if cookie else "未加载", "支持手动粘贴或从 Edge / Chrome 自动抓取。"),
                self._build_gacha_metric_card("已启用游戏", str(len(enabled_games)), "原神 / 星铁 / 绝区零"),
                self._build_gacha_metric_card("启动前签到", "已开启" if startup_enabled else "未开启", "若本轮包含对应一条龙，则在启动前先执行签到。"),
                self._build_gacha_metric_card("便笺查询", "已就绪" if record_ready else "待补齐", "星铁 / 绝区零通常需要 stoken；可用 App 扫码直接补齐。"),
                self._build_gacha_metric_card("浏览器", browser_name, validation["message"] if cookie else "登录后从本机浏览器会话读取 Cookie"),
            ]
            self._set_column_placeholder(self.mihoyo_result_list, "签到结果会显示在这里；如果只是想配置启动前签到，抓取 Cookie 后直接勾选即可。")
            return

        game_results = result.get("games") or []
        success_count = sum(int(item.get("success_count") or 0) for item in game_results)
        already_count = sum(int(item.get("already_count") or 0) for item in game_results)
        failed_count = sum(int(item.get("failed_count") or 0) for item in game_results)
        self.mihoyo_metrics_row.controls = [
            self._build_gacha_metric_card("本次游戏数", str(len(game_results)), "本轮执行过签到的 HoYo 游戏数量"),
            self._build_gacha_metric_card("签到成功", str(success_count), "本次实际新签到的账号数"),
            self._build_gacha_metric_card("今日已签", str(already_count), "今天已经完成过签到的账号数"),
            self._build_gacha_metric_card("失败 / 需手动", str(failed_count), "Cookie 失效、首次绑定或触发验证码都会落到这里"),
        ]
        lines = format_mihoyo_sign_report(result)
        self.mihoyo_result_list.controls = [
            self._build_wuwa_kujiequ_info_line("结果", line, "") for line in lines
        ] or [self._build_pool_empty_state("本次没有可展示的签到结果。")]

    def _select_gacha_pool(self, pool_name: str):
        if pool_name not in self.gacha_pool_views:
            return
        self.gacha_selected_pool = pool_name
        self.gacha_selected_pool = self._refresh_pool_switcher(
            self.gacha_pool_selector,
            self.gacha_pool_content,
            self.gacha_pool_views,
            self.gacha_selected_pool,
            self._select_gacha_pool,
            "当前账号中没有可展示的卡池数据。",
        )
        if getattr(self, "page", None) is not None:
            self.page.update()

    def _ordered_pool_names(self, game_code: str, available_names: List[str]) -> List[str]:
        config = get_game_config(game_code)
        configured = [
            str(name)
            for name in config.get("banner_names", {}).values()
            if str(name) in available_names
        ]
        rest = sorted(name for name in available_names if name not in configured)
        return configured + rest

    async def _pick_gacha_file(self, e):
        try:
            picker_kwargs = {"allow_multiple": False}
            file_picker_type = getattr(ft, "FilePickerFileType", None)
            if file_picker_type is not None and hasattr(file_picker_type, "CUSTOM"):
                picker_kwargs["file_type"] = file_picker_type.CUSTOM
                picker_kwargs["allowed_extensions"] = ["json"]
            files = await ft.FilePicker().pick_files(**picker_kwargs)
            if not files:
                return
            selected_path = getattr(files[0], "path", "") or getattr(files[0], "name", "")
            if self.gacha_path_input is not None and selected_path:
                self.gacha_path_input.value = selected_path
                self.page.update()
        except Exception as exc:
            self._toast(f"选择文件失败：{str(exc)}", self.PALETTE["danger"])

    def _set_gacha_import_busy(self, busy: bool):
        self.gacha_is_importing = busy
        if self.gacha_import_button is not None:
            self.gacha_import_button.disabled = busy
        if self.gacha_browse_button is not None:
            self.gacha_browse_button.disabled = busy
        if self.gacha_reload_button is not None:
            self.gacha_reload_button.disabled = busy
        if self.gacha_game_dropdown is not None:
            self.gacha_game_dropdown.disabled = busy
        if self.gacha_uid_dropdown is not None:
            self.gacha_uid_dropdown.disabled = busy or not bool(self.gacha_accounts)

    def _start_gacha_import(self, e):
        if self.is_running:
            self._toast("自动化任务运行中，先等这一轮结束再导入抽卡数据。", self.PALETTE["warning"])
            return
        if self.gacha_is_importing:
            return
        file_path = (self.gacha_path_input.value or "").strip() if self.gacha_path_input is not None else ""
        if not file_path:
            self._toast("请先填写 UIGF JSON 路径。", self.PALETTE["warning"])
            return
        self.controller.logger.info("[抽卡分析] 开始导入 HoYo UIGF：%s", file_path)
        self._set_gacha_import_busy(True)
        if self.gacha_import_summary is not None:
            self.gacha_import_summary.value = f"正在导入：{file_path}"
            self.gacha_import_summary.color = self.PALETTE["info"]
        self.page.update()
        threading.Thread(target=self._run_gacha_import_backend, args=(file_path,), daemon=True).start()

    def _run_gacha_import_backend(self, file_path: str):
        try:
            summaries = import_uigf(file_path, str(WORKFLOW_DB_PATH))
            self.controller.logger.info("[抽卡分析] HoYo UIGF 导入完成：%s", self._summarize_gacha_import(summaries))
            self._emit(
                "gacha_imported",
                file_path=file_path,
                summaries=summaries,
                selected_game_code=str(summaries[0].get("game_code", "") or "") if summaries else None,
            )
        except Exception as exc:
            self.controller.logger.error("[抽卡分析] HoYo UIGF 导入失败：%s", str(exc), exc_info=True)
            self._emit("gacha_import_failed", message=str(exc))

    def _find_gacha_account(self, uid: Optional[str]) -> Optional[Dict[str, Any]]:
        if not uid:
            return None
        for account in self.gacha_accounts:
            if str(account.get("uid", "")) == str(uid):
                return account
        return None

    def _reload_gacha_data(
        self,
        e=None,
        selected_uid: Optional[str] = None,
        selected_game_code: Optional[str] = None,
        show_toast: bool = True,
    ):
        if self.gacha_uid_dropdown is None or self.gacha_game_dropdown is None:
            return

        valid_game_codes = [game_code for game_code, _ in HOYO_GAME_OPTIONS]
        previous_game_code = self.gacha_current_game_code
        next_game_code = None
        for candidate in (
            selected_game_code,
            getattr(self.gacha_game_dropdown, "value", None),
            self.gacha_current_game_code,
            GENSHIN_GAME_CODE,
        ):
            if candidate and str(candidate) in valid_game_codes:
                next_game_code = str(candidate)
                break
        if next_game_code is None:
            next_game_code = GENSHIN_GAME_CODE

        self.gacha_game_dropdown.options = [ft.dropdown.Option(key=game_code, text=game_name) for game_code, game_name in HOYO_GAME_OPTIONS]
        self.gacha_game_dropdown.value = next_game_code
        self.gacha_current_game_code = next_game_code

        previous_uid = self.gacha_current_uid if previous_game_code == next_game_code else None
        previous_value = getattr(self.gacha_uid_dropdown, "value", None) if previous_game_code == next_game_code else None
        self.gacha_accounts = self.preference_db.get_gacha_accounts(next_game_code)

        valid_uids = [str(item.get("uid", "")) for item in self.gacha_accounts]
        next_uid = None
        for candidate in (selected_uid, previous_value, previous_uid):
            if candidate and str(candidate) in valid_uids:
                next_uid = str(candidate)
                break
        if next_uid is None and valid_uids:
            next_uid = valid_uids[0]

        self.gacha_uid_dropdown.options = [
            ft.dropdown.Option(key=str(item.get("uid", "")), text=f"UID {item.get('uid', '')} ({item.get('total_records', 0)} 条)")
            for item in self.gacha_accounts
        ]
        self.gacha_uid_dropdown.value = next_uid
        self.gacha_current_uid = next_uid
        self.controller.logger.info(
            "[抽卡分析] 刷新 HoYo 统计：游戏 %s，账号数 %d，当前 UID %s",
            get_game_display_name(next_game_code),
            len(self.gacha_accounts),
            next_uid or "-",
        )
        self._set_gacha_import_busy(self.gacha_is_importing)
        self._render_gacha_analysis(next_game_code, next_uid, previous_uid=previous_uid, previous_game_code=previous_game_code)
        if show_toast and self.current_view == "gacha":
            self._toast(f"{get_game_display_name(next_game_code)} 抽卡统计已刷新。", self.PALETTE["accent"])
        self.page.update()

    def _on_gacha_game_change(self, e):
        selected_game_code = getattr(e.control, "value", None)
        self.gacha_current_game_code = str(selected_game_code) if selected_game_code else GENSHIN_GAME_CODE
        self._reload_gacha_data(selected_game_code=self.gacha_current_game_code, show_toast=False)

    def _on_gacha_uid_change(self, e):
        selected_uid = getattr(e.control, "value", None)
        self.gacha_current_uid = str(selected_uid) if selected_uid else None
        self._render_gacha_analysis(self.gacha_current_game_code, self.gacha_current_uid)
        self.page.update()

    def _set_column_placeholder(self, column: Optional[ft.Column], message: str):
        if column is None:
            return
        column.controls = [self._build_pool_empty_state(message)]

    def _hoyo_rank_color(self, game_code: str, rank: str) -> str:
        rank_order = get_game_config(game_code).get("rank_labels", {})
        ordered = sorted((str(item) for item in rank_order.keys()), key=lambda item: int(item), reverse=True)
        palette = ["#d4a017", "#8e44ad", "#2980b9"]
        try:
            index = ordered.index(str(rank))
        except ValueError:
            index = len(palette)
        return palette[index] if index < len(palette) else self.PALETTE["text"]

    def _build_hoyo_pool_tab(self, game_code: str, pool_name: str, pool: Dict[str, Any]) -> ft.Column:
        metric_cards = [
            self._build_gacha_metric_card("总计", f"{pool['total']} 抽", "该卡池总抽数"),
        ]
        for metric in pool.get("rank_metrics", [])[:3]:
            metric_cards.append(
                self._build_gacha_metric_card(metric["label"], f"{metric['count']} 个", f"{metric['pct']}%")
            )

        controls: List[ft.Control] = [
            ft.Row(controls=metric_cards, wrap=True, spacing=10),
            ft.Text(f"时间范围: {pool['earliest_time']} ~ {pool['latest_time']}", size=13, color=self.PALETTE["muted"]),
        ]

        item_type_counts = pool.get("item_type_counts") or {}
        if item_type_counts:
            type_line = " / ".join(f"{item_type} {count}" for item_type, count in item_type_counts.items())
            controls.append(ft.Text(f"类型分布: {type_line}", size=13, color=self.PALETTE["muted"]))

        high_label = get_high_rarity_label(game_code)
        high_rows = pool.get("high_rarity_list") or []
        if high_rows:
            row_controls: List[ft.Control] = []
            for item in reversed(high_rows):
                row_controls.append(
                    ft.Container(
                        bgcolor=self.PALETTE["warning_soft"],
                        border_radius=10,
                        padding=ft.Padding(10, 8, 10, 8),
                        content=ft.Row(
                            controls=[
                                ft.Text(item["time"], size=12, color=self.PALETTE["muted"], width=140),
                                ft.Text(item["name"], size=14, weight=ft.FontWeight.BOLD, color="#d4a017", width=140),
                                ft.Text(item["item_type"], size=12, color=self.PALETTE["muted"], width=80),
                                ft.Text(f"第 {item['pity']} 抽", size=12, color=self.PALETTE["text"]),
                            ],
                            spacing=8,
                        ),
                    )
                )
            controls.append(ft.Column([ft.Text(f"{high_label}出货记录", size=14, weight=ft.FontWeight.BOLD, color="#d4a017"), *row_controls], spacing=6))
        else:
            controls.append(self._build_pool_empty_state(f"当前卡池还没有{high_label}记录。"))

        return ft.Column(controls=controls, spacing=12, scroll=ft.ScrollMode.AUTO)

    def _render_hoyo_history_list(self, game_code: str, history: List[Dict[str, Any]]):
        if self.gacha_high_rarity_list is None:
            return
        high_label = get_high_rarity_label(game_code)
        if not history:
            self._set_column_placeholder(self.gacha_high_rarity_list, f"当前 UID 还没有{high_label}历史。")
            return

        self.gacha_high_rarity_list.controls = []
        for item in history:
            self.gacha_high_rarity_list.controls.append(
                ft.Container(
                    bgcolor=self.PALETTE["panel_alt"],
                    border_radius=10,
                    padding=ft.Padding(10, 8, 10, 8),
                    content=ft.Row(
                        controls=[
                            ft.Text(item["time"], size=12, color=self.PALETTE["muted"], width=140),
                            ft.Text(item["name"], size=14, weight=ft.FontWeight.BOLD, color="#d4a017", width=140),
                            ft.Text(f"[{item['banner']}]", size=12, color=self.PALETTE["muted"], width=140),
                            ft.Text(f"第 {item['pity']} 抽", size=12, color=self.PALETTE["text"], width=80),
                        ],
                        spacing=8,
                    ),
                )
            )

    def _render_hoyo_stat_list(self, game_code: str, column: Optional[ft.Column], rows: List[Dict[str, Any]], empty_message: str):
        if column is None:
            return
        if not rows:
            self._set_column_placeholder(column, empty_message)
            return

        column.controls = []
        current_rank = None
        for item in rows:
            if current_rank != item["rank"]:
                current_rank = item["rank"]
                column.controls.append(
                    ft.Text(
                        item["rank_label"],
                        size=16,
                        weight=ft.FontWeight.BOLD,
                        color=self._hoyo_rank_color(game_code, item["rank"]),
                    )
                )
            column.controls.append(
                ft.Container(
                    bgcolor=self.PALETTE["panel_alt"],
                    border_radius=8,
                    padding=ft.Padding(8, 6, 8, 6),
                    content=ft.Row(
                        controls=[
                            ft.Text(item["name"], size=13, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"], width=160),
                            ft.Text(f"x{item['copies']}", size=13, color=self.PALETTE["muted"], width=50),
                            ft.Text(f"{item['first_time']} ~ {item['last_time']}", size=12, color=self.PALETTE["muted"]),
                        ],
                        spacing=8,
                    ),
                )
            )

    def _format_hoyo_pity_summary(self, game_code: str, rows: List[Dict[str, Any]]) -> str:
        high_label = get_high_rarity_label(game_code)
        if not rows:
            return f"当前 UID 还没有可用于{high_label}保底分析的数据。"

        lines: List[str] = []
        for item in rows:
            if item["count"] > 0:
                recent = "、".join(str(value) for value in item.get("recent", [])) or "-"
                lines.append(
                    f"{item['name']}：共 {item['count']} 次{high_label}出货，平均 {item['avg_pity']} 抽，"
                    f"最少 {item['min_pity']} / 最多 {item['max_pity']}，当前 {item['current_pity']} 抽，最近 {recent}"
                )
            else:
                lines.append(f"{item['name']}：暂未出现{high_label}，当前已累计 {item['current_pity']} 抽")
        return "\n".join(lines)

    def _render_gacha_analysis(
        self,
        game_code: str,
        uid: Optional[str],
        previous_uid: Optional[str] = None,
        previous_game_code: Optional[str] = None,
    ):
        if self.gacha_meta_text is None or self.gacha_banner_text is None or self.gacha_overview_cards is None:
            return
        if self.gacha_pool_selector is None or self.gacha_pool_content is None or self.gacha_pity_text is None:
            return

        game_name = get_game_display_name(game_code)
        high_label = get_high_rarity_label(game_code)
        if self.gacha_high_rarity_title_text is not None:
            self.gacha_high_rarity_title_text.value = f"{high_label}历史"
        if self.gacha_high_rarity_note_text is not None:
            self.gacha_high_rarity_note_text.value = f"按最近记录倒序展示，并计算对应保底组的{high_label}出货抽数。"

        if not uid:
            self.controller.logger.info("[抽卡分析] %s 当前没有可展示的 UID 记录", game_name)
            self.gacha_meta_text.value = f"数据库里还没有{game_name}抽卡记录。"
            self.gacha_banner_text.value = "先导入一个 HoYo UIGF JSON，下面的分析区才会有内容。"
            self.gacha_overview_cards.controls = [
                self._build_gacha_metric_card("当前状态", "暂无数据", f"导入任意{game_name} UIGF 文件后会自动生成统计。")
            ]
            self.gacha_pool_views = {}
            self.gacha_selected_pool = self._refresh_pool_switcher(
                self.gacha_pool_selector,
                self.gacha_pool_content,
                self.gacha_pool_views,
                None,
                self._select_gacha_pool,
                "当前账号中没有可展示的卡池数据。",
            )
            self._set_column_placeholder(self.gacha_high_rarity_list, f"还没有{high_label}历史。")
            self._set_column_placeholder(self.gacha_character_list, "还没有角色统计。")
            self._set_column_placeholder(self.gacha_weapon_list, "还没有武器统计。")
            self.gacha_pity_text.value = "还没有保底摘要。"
            return

        account = self._find_gacha_account(uid) or {}
        latest_import = self.preference_db.get_latest_gacha_import(game_code, uid) or {}
        records = self.preference_db.get_gacha_records(game_code, uid)
        overview = build_hoyo_overview(records, game_code, latest_import, int(account.get("import_count", 0) or 0))
        pool_analysis = build_hoyo_pool_analysis(records, game_code)
        history = build_high_rarity_history(records, game_code, limit=60)
        character_rows = build_hoyo_character_stats(records, game_code)
        weapon_rows = build_hoyo_weapon_stats(records, game_code)
        pity_rows = build_pity_summary(records, game_code)
        self.controller.logger.info(
            "[抽卡分析] %s UID %s：记录 %d 条，%s %d 个，卡池 %d 个",
            game_name,
            uid,
            len(records),
            high_label,
            overview["high_rarity_count"],
            len(pool_analysis),
        )

        self.gacha_meta_text.value = (
            f"{game_name} UID {uid}；最近导入 {overview['latest_imported_at'] or '-'}；"
            f"来源文件：{overview['latest_source_path'] or '-'}"
        )
        banner_line = "；".join(f"{name} {count} 条" for name, count in overview["banner_counts"].items())
        self.gacha_banner_text.value = f"卡池分布：{banner_line or '暂无'}"
        self.gacha_overview_cards.controls = [
            self._build_gacha_metric_card("总记录", str(overview["total_records"]), f"时间范围 {overview['earliest_time'] or '-'} ~ {overview['latest_time'] or '-'}"),
            self._build_gacha_metric_card(
                f"{high_label}数量",
                str(overview["high_rarity_count"]),
                " / ".join(
                    f"{get_rank_label(game_code, rank)} {overview['rank_counts'].get(rank, 0)}"
                    for rank in get_game_config(game_code).get("rank_labels", {})
                    if rank != overview["high_rarity_rank"]
                ) or "无其他稀有度",
            ),
            self._build_gacha_metric_card("导入批次", str(overview["import_count"]), "同一 UID 可反复导入，记录会按 ID 去重更新。"),
            self._build_gacha_metric_card(f"最常见{high_label}", str(overview["top_high_rarity"][0]), f"共出现 {overview['top_high_rarity'][1]} 次"),
        ]

        ordered_pool_names = self._ordered_pool_names(game_code, list(pool_analysis.keys()))
        self.gacha_pool_views = {
            pool_name: self._build_hoyo_pool_tab(game_code, pool_name, pool_analysis[pool_name])
            for pool_name in ordered_pool_names
        }
        self.gacha_selected_pool = self._refresh_pool_switcher(
            self.gacha_pool_selector,
            self.gacha_pool_content,
            self.gacha_pool_views,
            self.gacha_selected_pool if previous_game_code == game_code else None,
            self._select_gacha_pool,
            "当前账号中没有可展示的卡池数据。",
        )

        self._render_hoyo_history_list(game_code, history)
        self._render_hoyo_stat_list(game_code, self.gacha_character_list, character_rows, "当前 UID 还没有可展示的角色统计。")
        self._render_hoyo_stat_list(game_code, self.gacha_weapon_list, weapon_rows, "当前 UID 还没有可展示的武器统计。")
        self.gacha_pity_text.value = self._format_hoyo_pity_summary(game_code, pity_rows)

        if self.gacha_path_input is not None and latest_import:
            current_path = (self.gacha_path_input.value or "").strip()
            if not current_path or previous_game_code != game_code or (previous_uid is not None and str(previous_uid) != str(uid)):
                self.gacha_path_input.value = str(latest_import.get("source_path", "") or "")

    def _summarize_gacha_import(self, summaries: List[Dict[str, Any]]) -> str:
        if not summaries:
            return "导入完成，但没有可展示的账号摘要。"
        parts = []
        for summary in summaries:
            high_label = str(summary.get("high_rarity_label", "最高稀有度"))
            parts.append(
                f"{summary.get('game_name', summary.get('game_code', 'HoYo'))} UID {summary['uid']}："
                f"总计 {summary['total_records']} 条，新增 {summary['inserted_count']} 条，更新 {summary['updated_count']} 条，"
                f"{high_label} {summary.get('high_rarity_count', 0)} 个"
            )
        return "；".join(parts)

    # ────────────────────────────
    # 无限暖暖共鸣视图
    # ────────────────────────────
    def _set_nikki_fetch_busy(self, busy: bool):
        self.nikki_is_fetching = busy
        for control in (
            self.nikki_browse_button,
            self.nikki_analyze_button,
            self.nikki_fetch_button,
            self.nikki_fetch_pearpal_button,
            self.nikki_jwt_input,
            self.nikki_uid_input,
            self.nikki_momo_id_input,
            self.nikki_momo_token_input,
            self.nikki_force_pool_input,
            self.nikki_domain_input,
            self.nikki_output_input,
            self.nikki_path_input,
        ):
            if control is not None:
                control.disabled = busy

    def _build_nikki_view(self) -> ft.Container:
        momo_script = get_nikki_momo_console_script()
        self.nikki_path_input = ft.TextField(
            label="共鸣 JSON 路径",
            value=DEFAULT_NIKKI_PATH if os.path.exists(DEFAULT_NIKKI_PATH) else "",
            hint_text="支持 gacha-records-UID.json 或 nuan5 完整档案 JSON。",
            border_color=self.PALETTE["line"],
            filled=True,
            fill_color=self.PALETTE["panel_alt"],
        )
        self.nikki_browse_button = ft.OutlinedButton("浏览 JSON", icon=FT_ICONS.FOLDER_OPEN, on_click=self._pick_nikki_file)
        self.nikki_analyze_button = ft.ElevatedButton("分析文件", on_click=self._reload_nikki_data)
        self.nikki_jwt_input = ft.TextField(
            label="JWT（可选）",
            hint_text="如果你已经拿到 query 用的 JWT，可直接填写这里。",
            password=True,
            border_color=self.PALETTE["line"],
            filled=True,
            fill_color=self.PALETTE["panel_alt"],
        )
        self.nikki_uid_input = ft.TextField(
            label="UID（Momo 模式）",
            hint_text="使用 Momo Token 抓取时填写游戏 UID。",
            border_color=self.PALETTE["line"],
            filled=True,
            fill_color=self.PALETTE["panel_alt"],
            width=220,
        )
        self.nikki_momo_id_input = ft.TextField(
            label="Momo ID",
            hint_text="Pearpal / 美鸭梨 Cookie 里的 momoNid。",
            border_color=self.PALETTE["line"],
            filled=True,
            fill_color=self.PALETTE["panel_alt"],
            width=220,
        )
        self.nikki_momo_token_input = ft.TextField(
            label="Momo Token",
            hint_text="Pearpal / 美鸭梨 Cookie 里的 momoToken。",
            password=True,
            border_color=self.PALETTE["line"],
            filled=True,
            fill_color=self.PALETTE["panel_alt"],
        )
        self.nikki_force_pool_input = ft.TextField(
            label="强制刷新卡池 ID",
            hint_text="可选，逗号分隔，例如 1,57",
            border_color=self.PALETTE["line"],
            filled=True,
            fill_color=self.PALETTE["panel_alt"],
            width=220,
        )
        self.nikki_domain_input = ft.TextField(
            label="ClickHouse 域名",
            value=DEFAULT_NIKKI_CLICKHOUSE_DOMAIN,
            border_color=self.PALETTE["line"],
            filled=True,
            fill_color=self.PALETTE["panel_alt"],
            width=320,
        )
        self.nikki_output_input = ft.TextField(
            label="抓取输出路径",
            hint_text="留空时会按抓取方式自动生成文件名",
            border_color=self.PALETTE["line"],
            filled=True,
            fill_color=self.PALETTE["panel_alt"],
        )
        self.nikki_fetch_button = ft.ElevatedButton("抓取完整共鸣记录", on_click=self._start_nikki_fetch)
        self.nikki_fetch_pearpal_button = ft.OutlinedButton("导入 Pearpal 摘要", on_click=self._start_nikki_pearpal_fetch)
        self.nikki_status_text = ft.Text(
            "选择一个 JSON 文件，或填写 JWT / UID + Momo ID + Momo Token 后抓取完整记录；也可只导入 Pearpal notebook 摘要。",
            size=13,
            color=self.PALETTE["muted"],
        )
        self.nikki_meta_text = ft.Text("尚未加载文件。", size=13, color=self.PALETTE["muted"])

        self.nikki_overview_cards = ft.Row(wrap=True, spacing=10)
        self.nikki_pool_selector = ft.Row(wrap=True, spacing=8)
        self.nikki_pool_content = ft.Container(content=self._build_pool_empty_state("分析后会在这里显示卡池明细。"), height=400)
        self.nikki_pool_tabs = ft.Column(
            controls=[self.nikki_pool_selector, self.nikki_pool_content],
            spacing=12,
        )
        self.nikki_recent_list = ft.Column(spacing=6, scroll=ft.ScrollMode.AUTO, height=400)

        input_panel = self._panel(
            ft.Column(
                controls=[
                    ft.Text("无限暖暖共鸣分析", size=20, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"]),
                    ft.Text(
                        "支持两种输入：1) nuan5 的 gacha-records-UID.json；2) nuan5 完整档案备份 JSON。"
                        "如果 pulls 为空但 pearpal 存在，会按摘要模式展示 4/5 星事件与保底区间。",
                        size=13,
                        color=self.PALETTE["muted"],
                    ),
                    self.nikki_path_input,
                    ft.Row(controls=[self.nikki_browse_button, self.nikki_analyze_button], spacing=8, wrap=True),
                    ft.Divider(height=1, color=self.PALETTE["line"]),
                    ft.Text(
                        "抓取功能支持三种结果：1) 直接填 JWT 拉完整记录；2) 填 UID + Momo ID + Momo Token，由程序先调 verify 再读取完整 query；3) 只导入 Pearpal notebook 摘要，该模式只含 4/5 星事件。",
                        size=13,
                        color=self.PALETTE["muted"],
                    ),
                    self.nikki_jwt_input,
                    ft.Row(controls=[self.nikki_uid_input, self.nikki_momo_id_input], spacing=8, wrap=True),
                    self.nikki_momo_token_input,
                    ft.Text(
                        "Momo Token 获取方法：登录 Pearpal / 美鸭梨首页，按 F12 打开控制台，执行下面的脚本。输出 JSON 中 roleid=UID，token=Momo Token，id=Momo ID。",
                        size=12,
                        color=self.PALETTE["muted"],
                    ),
                    ft.TextField(
                        label="控制台脚本（只读）",
                        value=momo_script,
                        read_only=True,
                        multiline=True,
                        min_lines=3,
                        max_lines=4,
                        border_color=self.PALETTE["line"],
                        filled=True,
                        fill_color=self.PALETTE["panel_alt"],
                    ),
                    ft.Row(controls=[self.nikki_domain_input, self.nikki_force_pool_input], spacing=8, wrap=True),
                    self.nikki_output_input,
                    ft.Row(controls=[self.nikki_fetch_button, self.nikki_fetch_pearpal_button], spacing=8, wrap=True),
                    self.nikki_status_text,
                ],
                spacing=10,
            )
        )

        overview_panel = self._panel(
            ft.Column(
                controls=[
                    ft.Text("共鸣总览", size=20, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"]),
                    self.nikki_meta_text,
                    self.nikki_overview_cards,
                ],
                spacing=10,
            )
        )

        pool_panel = self._panel(
            ft.Column(
                controls=[
                    ft.Text("各卡池详细分析", size=20, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"]),
                    self.nikki_pool_tabs,
                ],
                spacing=10,
            )
        )

        recent_panel = self._panel(
            ft.Column(
                controls=[
                    ft.Text("最近记录", size=20, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"]),
                    self._build_nikki_recent_hint(),
                    self.nikki_recent_list,
                ],
                spacing=10,
            )
        )

        self._apply_nikki_config_to_controls()

        return ft.Container(
            visible=False,
            padding=ft.Padding(18, 16, 18, 16),
            content=ft.Column(
                controls=[input_panel, overview_panel, pool_panel, recent_panel],
                spacing=12,
                scroll=ft.ScrollMode.AUTO,
            ),
        )

    async def _pick_nikki_file(self, e):
        try:
            picker_kwargs = {"allow_multiple": False}
            file_picker_type = getattr(ft, "FilePickerFileType", None)
            if file_picker_type is not None and hasattr(file_picker_type, "CUSTOM"):
                picker_kwargs["file_type"] = file_picker_type.CUSTOM
                picker_kwargs["allowed_extensions"] = ["json"]
            files = await ft.FilePicker().pick_files(**picker_kwargs)
            if not files:
                return
            selected_path = getattr(files[0], "path", "") or getattr(files[0], "name", "")
            if self.nikki_path_input is not None and selected_path:
                self.nikki_path_input.value = selected_path
                self.page.update()
        except Exception as exc:
            self._toast(f"选择文件失败：{str(exc)}", self.PALETTE["danger"])

    def _start_nikki_fetch(self, e):
        if self.nikki_is_fetching:
            return
        jwt = (self.nikki_jwt_input.value or "").strip() if self.nikki_jwt_input is not None else ""
        uid = (self.nikki_uid_input.value or "").strip() if self.nikki_uid_input is not None else ""
        momo_id = (self.nikki_momo_id_input.value or "").strip() if self.nikki_momo_id_input is not None else ""
        momo_token = (self.nikki_momo_token_input.value or "").strip() if self.nikki_momo_token_input is not None else ""

        auth_mode = ""
        status_message = ""
        if jwt:
            try:
                payload = decode_nikki_jwt(jwt)
            except Exception as exc:
                if self.nikki_status_text is not None:
                    self.nikki_status_text.value = f"JWT 无效：{str(exc)}"
                    self.nikki_status_text.color = self.PALETTE["danger"]
                self.page.update()
                return
            auth_mode = "jwt"
            status_message = f"开始抓取 UID {payload.get('sub', '-')} 的共鸣记录..."
        elif uid and momo_id and momo_token:
            auth_mode = "momo_verify"
            status_message = f"开始通过 Momo Token 校验 UID {uid} 并抓取共鸣记录..."
        else:
            if self.nikki_status_text is not None:
                self.nikki_status_text.value = "请填写 JWT，或同时填写 UID、Momo ID、Momo Token。"
                self.nikki_status_text.color = self.PALETTE["danger"]
            self.page.update()
            return

        domain = (self.nikki_domain_input.value or "").strip() if self.nikki_domain_input is not None else DEFAULT_NIKKI_CLICKHOUSE_DOMAIN
        force_text = (self.nikki_force_pool_input.value or "").strip() if self.nikki_force_pool_input is not None else ""
        force_pool_ids = resolve_nikki_force_pool_ids(force_text)
        output_path = (self.nikki_output_input.value or "").strip() if self.nikki_output_input is not None else ""
        if self.nikki_status_text is not None:
            self.nikki_status_text.value = status_message
            self.nikki_status_text.color = self.PALETTE["accent"]
        self._set_nikki_fetch_busy(True)
        self.page.update()
        threading.Thread(
            target=self._run_nikki_fetch_backend,
            args=(
                jwt if auth_mode == "jwt" else None,
                uid if auth_mode == "momo_verify" else None,
                momo_id if auth_mode == "momo_verify" else None,
                momo_token if auth_mode == "momo_verify" else None,
                force_pool_ids,
                domain or DEFAULT_NIKKI_CLICKHOUSE_DOMAIN,
                output_path or None,
            ),
            daemon=True,
        ).start()

    def _start_nikki_pearpal_fetch(self, e):
        if self.nikki_is_fetching:
            return
        uid = (self.nikki_uid_input.value or "").strip() if self.nikki_uid_input is not None else ""
        momo_id = (self.nikki_momo_id_input.value or "").strip() if self.nikki_momo_id_input is not None else ""
        momo_token = (self.nikki_momo_token_input.value or "").strip() if self.nikki_momo_token_input is not None else ""
        if not momo_id or not momo_token:
            if self.nikki_status_text is not None:
                self.nikki_status_text.value = "导入 Pearpal 摘要时请先填写 Momo ID 和 Momo Token；UID 可留空。"
                self.nikki_status_text.color = self.PALETTE["danger"]
            self.page.update()
            return

        domain = (self.nikki_domain_input.value or "").strip() if self.nikki_domain_input is not None else DEFAULT_NIKKI_CLICKHOUSE_DOMAIN
        output_path = (self.nikki_output_input.value or "").strip() if self.nikki_output_input is not None else ""
        if self.nikki_status_text is not None:
            self.nikki_status_text.value = "开始从 Pearpal notebook 读取 4/5 星摘要事件..."
            self.nikki_status_text.color = self.PALETTE["accent"]
        self._set_nikki_fetch_busy(True)
        self.page.update()
        threading.Thread(
            target=self._run_nikki_pearpal_fetch_backend,
            args=(uid or None, momo_id, momo_token, domain or DEFAULT_NIKKI_CLICKHOUSE_DOMAIN, output_path or None),
            daemon=True,
        ).start()

    def _run_nikki_fetch_backend(
        self,
        jwt: Optional[str],
        uid: Optional[str],
        momo_id: Optional[str],
        momo_token: Optional[str],
        force_pool_ids: List[str],
        domain: str,
        output_path: Optional[str],
    ):
        try:
            path, _records, summary = fetch_nikki_all_records(
                jwt,
                force_pool_ids,
                uid=uid,
                momo_id=momo_id,
                momo_token=momo_token,
                domain=domain,
                output_path=output_path,
                progress_callback=lambda message: self._emit("nikki_fetch_progress", message=message),
            )
            self._emit("nikki_fetch_finished", output_path=str(path), summary=summary)
        except Exception as exc:
            self._emit("nikki_fetch_failed", message=str(exc))

    def _run_nikki_pearpal_fetch_backend(
        self,
        uid: Optional[str],
        momo_id: str,
        momo_token: str,
        domain: str,
        output_path: Optional[str],
    ):
        try:
            path, _document, summary = fetch_nikki_pearpal_notebook_summary(
                uid=uid,
                momo_id=momo_id,
                momo_token=momo_token,
                domain=domain,
                output_path=output_path,
                progress_callback=lambda message: self._emit("nikki_fetch_progress", message=message),
            )
            self._emit("nikki_fetch_finished", output_path=str(path), summary=summary)
        except Exception as exc:
            self._emit("nikki_fetch_failed", message=str(exc))

    def _select_nikki_pool(self, pool_name: str):
        if pool_name not in self.nikki_pool_views:
            return
        self.nikki_selected_pool = pool_name
        self.nikki_selected_pool = self._refresh_pool_switcher(
            self.nikki_pool_selector,
            self.nikki_pool_content,
            self.nikki_pool_views,
            self.nikki_selected_pool,
            self._select_nikki_pool,
            "当前文件中没有可展示的卡池数据。",
        )
        if getattr(self, "page", None) is not None:
            self.page.update()

    def _build_nikki_pool_tab(self, summary: Dict[str, Any]) -> ft.Column:
        repeated_items = sum(count for copies, count in summary.get("duplicate_distribution", {}).items() if copies >= 2)
        top_items = summary.get("top_items") or []
        top_item = top_items[0] if top_items else {"display_name": "-", "subtitle": "", "count": 0}
        controls: List[ft.Control] = [
            ft.Row(
                controls=[
                    self._build_gacha_metric_card("总计", f"{summary['total_records']} 抽", "该卡池总抽数"),
                    self._build_gacha_metric_card("不同 ID", str(summary["unique_item_ids"]), "去重后的物品 ID 数"),
                    self._build_gacha_metric_card("重复物品", str(repeated_items), "出现至少 2 次的物品数"),
                    self._build_gacha_metric_card("最高频物品", f"x{top_item['count']}", top_item["display_name"]),
                ],
                wrap=True,
                spacing=10,
            ),
            ft.Text(f"时间范围: {summary['earliest_time']} ~ {summary['latest_time']}", size=13, color=self.PALETTE["muted"]),
        ]

        if summary.get("duplicate_distribution"):
            dist_str = " / ".join(
                f"出现 {copies} 次的物品 {count} 个"
                for copies, count in summary["duplicate_distribution"].items()
            )
            controls.append(ft.Text(f"重复分布: {dist_str}", size=13, color=self.PALETTE["muted"]))

        top_item_controls: List[ft.Control] = []
        for item in top_items[:20]:
            top_item_controls.append(
                ft.Container(
                    bgcolor=self.PALETTE["panel_alt"],
                    border_radius=8,
                    padding=ft.Padding(8, 6, 8, 6),
                    content=ft.Row(
                        controls=[
                            ft.Column(
                                controls=[
                                    ft.Text(item["display_name_with_type"], size=13, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"]),
                                    ft.Text(item["subtitle"], size=12, color=self.PALETTE["muted"]),
                                ],
                                spacing=2,
                                expand=True,
                            ),
                            ft.Text(f"x{item['count']}", size=13, color=self.PALETTE["muted"]),
                        ],
                        spacing=8,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                )
            )
        if top_item_controls:
            controls.append(ft.Column([ft.Text("高频物品", size=14, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"]), *top_item_controls], spacing=6))

        recent_controls: List[ft.Control] = []
        for item in summary.get("recent_records", [])[:20]:
            recent_controls.append(
                ft.Container(
                    bgcolor=self.PALETTE["warning_soft"],
                    border_radius=8,
                    padding=ft.Padding(8, 6, 8, 6),
                    content=ft.Row(
                        controls=[
                            ft.Text(item["time"], size=12, color=self.PALETTE["muted"], width=160),
                            ft.Column(
                                controls=[
                                    ft.Text(item["display_name_with_type"], size=13, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"]),
                                    ft.Text(item["subtitle"], size=12, color=self.PALETTE["muted"]),
                                ],
                                spacing=2,
                                expand=True,
                            ),
                        ],
                        spacing=8,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                )
            )
        if recent_controls:
            controls.append(ft.Column([ft.Text("最近记录", size=14, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"]), *recent_controls], spacing=6))

        return ft.Column(controls=controls, spacing=12, scroll=ft.ScrollMode.AUTO)

    def _build_nikki_recent_hint(self) -> ft.Text:
        self.nikki_recent_hint_text = ft.Text(
            "原始导出只包含时间和物品 ID；这里会结合本地目录缓存优先显示物品名、套装名和部件类型。",
            size=13,
            color=self.PALETTE["muted"],
        )
        return self.nikki_recent_hint_text

    def _build_nikki_pearpal_pool_tab(self, summary: Dict[str, Any]) -> ft.Column:
        repeated_items = sum(count for copies, count in summary.get("duplicate_distribution", {}).items() if copies >= 2)
        top_items = summary.get("top_items") or []
        top_item = top_items[0] if top_items else {"display_name": "-", "count": 0}
        controls: List[ft.Control] = [
            ft.Row(
                controls=[
                    self._build_gacha_metric_card("估算总抽数", f"{summary['estimated_total_pulls']} 抽", "按最大 pool_cnt + 1 推算"),
                    self._build_gacha_metric_card("摘要事件", str(summary["event_count"]), "只包含 4/5 星事件"),
                    self._build_gacha_metric_card("4星 / 5星", f"{summary['four_count']} / {summary['five_count']}", "pearpal 稀有度计数"),
                    self._build_gacha_metric_card("重复结果", str(repeated_items), "出现至少 2 次的结果 ID 数"),
                    self._build_gacha_metric_card("当前 4星保底", f"{summary['current_four_pity']} 抽", f"平均 {summary['average_four_gap']:.1f} 抽"),
                    self._build_gacha_metric_card("当前 5星保底", f"{summary['current_five_pity']} 抽", f"平均 {summary['average_five_gap']:.1f} 抽"),
                ],
                wrap=True,
                spacing=10,
            ),
            ft.Text("说明: pearpal 是 4/5 星事件摘要，不含逐抽时间与低星明细。", size=13, color=self.PALETTE["muted"]),
            ft.Text(f"最高频结果: {top_item['display_name']}（{top_item['count']} 次）", size=13, color=self.PALETTE["muted"]),
        ]

        top_item_controls: List[ft.Control] = []
        for item in top_items[:20]:
            top_item_controls.append(
                ft.Container(
                    bgcolor=self.PALETTE["panel_alt"],
                    border_radius=8,
                    padding=ft.Padding(8, 6, 8, 6),
                    content=ft.Row(
                        controls=[
                            ft.Column(
                                controls=[
                                    ft.Text(item["display_name_with_type"], size=13, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"]),
                                    ft.Text(item["subtitle"], size=12, color=self.PALETTE["muted"]),
                                ],
                                spacing=2,
                                expand=True,
                            ),
                            ft.Text(f"x{item['count']}", size=13, color=self.PALETTE["muted"]),
                        ],
                        spacing=8,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                )
            )
        if top_item_controls:
            controls.append(ft.Column([ft.Text("高频结果", size=14, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"]), *top_item_controls], spacing=6))

        recent_controls: List[ft.Control] = []
        for item in summary.get("recent_records", [])[:20]:
            recent_controls.append(
                ft.Container(
                    bgcolor=self.PALETTE["warning_soft"],
                    border_radius=8,
                    padding=ft.Padding(8, 6, 8, 6),
                    content=ft.Row(
                        controls=[
                            ft.Text(f"第 {item['draw_index']} 抽", size=12, color=self.PALETTE["muted"], width=90),
                            ft.Text(f"{item['rarity']} 星", size=13, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"], width=52),
                            ft.Column(
                                controls=[
                                    ft.Text(item["display_name_with_type"], size=13, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"]),
                                    ft.Text(item["subtitle"], size=12, color=self.PALETTE["muted"]),
                                ],
                                spacing=2,
                                expand=True,
                            ),
                            ft.Text(f"4星间隔 {item['four_gap']} / 5星间隔 {item['five_gap']}", size=12, color=self.PALETTE["muted"]),
                        ],
                        spacing=8,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                )
            )
        if recent_controls:
            controls.append(ft.Column([ft.Text("最近摘要事件", size=14, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"]), *recent_controls], spacing=6))

        return ft.Column(controls=controls, spacing=12, scroll=ft.ScrollMode.AUTO)

    def _render_nikki_analysis(self, document: Optional[Dict[str, Any]]):
        if self.nikki_meta_text is None or self.nikki_overview_cards is None:
            return
        if self.nikki_pool_selector is None or self.nikki_pool_content is None or self.nikki_recent_list is None:
            return

        if not document:
            self.nikki_meta_text.value = "先选择一个 JSON 文件，或先用 JWT / Momo Token 抓取。"
            self.nikki_overview_cards.controls = [
                self._build_gacha_metric_card("当前状态", "未加载", "支持 gacha-records-UID.json 与 nuan5 完整档案 JSON。")
            ]
            self.nikki_pool_views = {}
            self.nikki_selected_pool = self._refresh_pool_switcher(
                self.nikki_pool_selector,
                self.nikki_pool_content,
                self.nikki_pool_views,
                None,
                self._select_nikki_pool,
                "当前文件中没有可展示的卡池数据。",
            )
            if self.nikki_recent_hint_text is not None:
                self.nikki_recent_hint_text.value = "最近记录会显示在这里。"
            self._set_column_placeholder(self.nikki_recent_list, "最近记录会显示在这里。")
            return

        records_by_pool = document["records_by_pool"]
        pearpal_by_pool = document.get("pearpal_by_pool") or {}
        has_raw_records = any(bool(rows) for rows in records_by_pool.values())
        has_pearpal_records = any(bool(rows) for rows in pearpal_by_pool.values())
        profile_label = document.get("profile_label") or "-"
        source_label = document.get("source_label") or "未知格式"
        file_name = Path(str(document.get("file_path", ""))).name or "-"
        self.nikki_meta_text.value = f"来源: {source_label}；档案标签: {profile_label}；文件: {file_name}；名称映射: gongeo.us 中文目录缓存"

        if not has_raw_records and not has_pearpal_records:
            empty_note = "这个文件里没有共鸣记录。"
            if document.get("source_format") == "site_backup":
                empty_note = "这是 nuan5 完整档案备份，但 pulls 为空，所以没有可分析的共鸣记录。"
            self.nikki_overview_cards.controls = [
                self._build_gacha_metric_card("当前状态", "暂无数据", empty_note),
                self._build_gacha_metric_card("文件格式", source_label, f"档案标签 {profile_label}"),
            ]
            self.nikki_pool_views = {}
            self.nikki_selected_pool = self._refresh_pool_switcher(
                self.nikki_pool_selector,
                self.nikki_pool_content,
                self.nikki_pool_views,
                None,
                self._select_nikki_pool,
                "当前文件中没有可展示的卡池数据。",
            )
            if self.nikki_recent_hint_text is not None:
                self.nikki_recent_hint_text.value = empty_note
            self._set_column_placeholder(self.nikki_recent_list, empty_note)
            return

        self.nikki_pool_views = {}
        if has_raw_records:
            overview = build_nikki_overview(records_by_pool)
            self.nikki_overview_cards.controls = [
                self._build_gacha_metric_card("总记录", str(overview["total_records"]), f"时间范围 {overview['earliest_time']} ~ {overview['latest_time']}"),
                self._build_gacha_metric_card("有记录卡池", str(overview["pool_count"]), "非空卡池数量"),
                self._build_gacha_metric_card("文件格式", source_label, f"档案标签 {profile_label}"),
                self._build_gacha_metric_card("文件名", file_name, "当前分析的本地 JSON"),
            ]
            for pool_id, pool_name in NIKKI_GACHA_POOLS.items():
                rows = records_by_pool.get(pool_id) or []
                if not rows:
                    continue
                summary = build_nikki_pool_summary(pool_id, rows)
                self.nikki_pool_views[pool_name] = self._build_nikki_pool_tab(summary)
            if self.nikki_recent_hint_text is not None:
                self.nikki_recent_hint_text.value = "原始导出只包含时间和物品 ID；这里会结合本地目录缓存优先显示物品名、套装名和部件类型。"
            recent_records = build_nikki_recent_records(records_by_pool, limit=60)
            if not recent_records:
                self._set_column_placeholder(self.nikki_recent_list, "当前文件中没有最近记录可展示。")
            else:
                self.nikki_recent_list.controls = []
                for item in recent_records:
                    self.nikki_recent_list.controls.append(
                        ft.Container(
                            bgcolor=self.PALETTE["panel_alt"],
                            border_radius=10,
                            padding=ft.Padding(10, 8, 10, 8),
                            content=ft.Row(
                                controls=[
                                    ft.Text(item["time"], size=12, color=self.PALETTE["muted"], width=160),
                                    ft.Column(
                                        controls=[
                                            ft.Text(item["display_name_with_type"], size=13, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"]),
                                            ft.Text(item["subtitle"], size=12, color=self.PALETTE["muted"]),
                                        ],
                                        spacing=2,
                                        expand=True,
                                    ),
                                    ft.Text(f"[{item['pool_name']}]", size=12, color=self.PALETTE["muted"]),
                                ],
                                spacing=8,
                                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            ),
                        )
                    )
        else:
            pearpal_overview = build_nikki_pearpal_overview(pearpal_by_pool)
            self.nikki_overview_cards.controls = [
                self._build_gacha_metric_card("估算总抽数", f"{pearpal_overview['total_pulls']} 抽", "按各卡池最大 pool_cnt + 1 汇总"),
                self._build_gacha_metric_card("摘要事件", str(pearpal_overview["total_events"]), "只包含 4/5 星事件"),
                self._build_gacha_metric_card("4星 / 5星", f"{pearpal_overview['four_count']} / {pearpal_overview['five_count']}", "pearpal 稀有度计数"),
                self._build_gacha_metric_card("有记录卡池", str(pearpal_overview["pool_count"]), "非空卡池数量"),
                self._build_gacha_metric_card("文件格式", source_label, f"档案标签 {profile_label}"),
                self._build_gacha_metric_card("文件名", file_name, "当前分析的本地 JSON"),
            ]
            for pool_id, pool_name in NIKKI_GACHA_POOLS.items():
                rows = pearpal_by_pool.get(pool_id) or []
                if not rows:
                    continue
                summary = build_nikki_pearpal_pool_summary(pool_id, rows)
                self.nikki_pool_views[pool_name] = self._build_nikki_pearpal_pool_tab(summary)
            if self.nikki_recent_hint_text is not None:
                self.nikki_recent_hint_text.value = "pearpal 摘要不含逐抽时间，无法跨卡池生成全局最近列表；请查看上方各卡池详细分析。"
            self._set_column_placeholder(self.nikki_recent_list, "当前文件使用 pearpal 摘要模式；全局最近记录不可用。")

        self.nikki_selected_pool = self._refresh_pool_switcher(
            self.nikki_pool_selector,
            self.nikki_pool_content,
            self.nikki_pool_views,
            self.nikki_selected_pool,
            self._select_nikki_pool,
            "当前文件中没有可展示的卡池数据。",
        )

    def _reload_nikki_data(self, e=None, show_toast: bool = True):
        uid = (self.nikki_uid_input.value or "").strip() if self.nikki_uid_input is not None else ""
        file_path = (self.nikki_path_input.value or "").strip() if self.nikki_path_input is not None else ""

        if uid:
            db_rows = self.preference_db.get_gacha_records("nikki", uid)
            if db_rows:
                self.controller.logger.info("[抽卡分析] 无限暖暖 从数据库加载 UID %s 的 %d 条记录", uid, len(db_rows))
                try:
                    document = db_to_nikki_document(db_rows)
                    records_by_pool = document.get("records_by_pool") or {}
                    pearpal_by_pool = document.get("pearpal_by_pool") or {}
                    has_raw_records = any(bool(rows) for rows in records_by_pool.values())
                    has_pearpal_records = any(bool(rows) for rows in pearpal_by_pool.values())
                    self._render_nikki_analysis(document)
                    if has_raw_records:
                        overview = build_nikki_overview(records_by_pool)
                        if self.nikki_status_text is not None:
                            self.nikki_status_text.value = f"从数据库加载：共 {overview['total_records']} 条记录，来自 {overview['pool_count']} 个卡池。"
                            self.nikki_status_text.color = self.PALETTE["accent"]
                        if show_toast and self.current_view == "nikki":
                            self._toast("无限暖暖共鸣分析已从数据库加载。", self.PALETTE["accent"])
                    elif has_pearpal_records:
                        overview = build_nikki_pearpal_overview(pearpal_by_pool)
                        if self.nikki_status_text is not None:
                            self.nikki_status_text.value = (
                                f"从数据库加载：已按 pearpal 摘要模式估算 {overview['total_pulls']} 抽，覆盖 {overview['pool_count']} 个卡池。"
                            )
                            self.nikki_status_text.color = self.PALETTE["accent"]
                        if show_toast and self.current_view == "nikki":
                            self._toast("无限暖暖 pearpal 摘要分析已从数据库加载。", self.PALETTE["accent"])
                    elif self.nikki_status_text is not None:
                        self.nikki_status_text.value = "数据库中无有效共鸣记录。"
                        self.nikki_status_text.color = self.PALETTE["warning"]
                    self.page.update()
                    return
                except Exception as db_exc:
                    self.controller.logger.warning("[抽卡分析] 无限暖暖 从数据库加载失败：%s，回退到文件解析", str(db_exc))

        if not file_path:
            self.controller.logger.info("[抽卡分析] 无限暖暖 未设置 JSON 文件路径")
            if self.nikki_status_text is not None:
                self.nikki_status_text.value = "选择一个 JSON 文件，或先用 JWT / Momo Token 抓取共鸣记录。"
                self.nikki_status_text.color = self.PALETTE["muted"]
            self._render_nikki_analysis(None)
            self.page.update()
            return
        if not os.path.exists(file_path):
            self.controller.logger.info("[抽卡分析] 无限暖暖 JSON 文件不存在：%s", file_path)
            if self.nikki_status_text is not None:
                self.nikki_status_text.value = "文件路径无效，请检查路径是否正确。"
                self.nikki_status_text.color = self.PALETTE["danger"]
            self.page.update()
            return
        try:
            self.controller.logger.info("[抽卡分析] 开始分析无限暖暖 JSON：%s", file_path)
            document = load_nikki_resonance_document(file_path)
            records_by_pool = document.get("records_by_pool") or {}
            pearpal_by_pool = document.get("pearpal_by_pool") or {}
            has_raw_records = any(bool(rows) for rows in records_by_pool.values())
            has_pearpal_records = any(bool(rows) for rows in pearpal_by_pool.values())
            self._render_nikki_analysis(document)

            if uid:
                try:
                    db_records = nikki_to_db_records(document, uid)
                    if db_records:
                        import_id = self.preference_db.start_gacha_import({
                            "game_code": "nikki",
                            "uid": uid,
                            "source_path": file_path,
                            "source_hash": "",
                            "total_records": len(db_records),
                        })
                        self.preference_db.upsert_gacha_records("nikki", uid, import_id, db_records)
                        self.controller.logger.info("[抽卡分析] 无限暖暖记录已写入数据库：%d 条", len(db_records))
                except Exception as db_exc:
                    self.controller.logger.warning("[抽卡分析] 无限暖暖记录写入数据库失败：%s", str(db_exc))

            if has_raw_records:
                overview = build_nikki_overview(records_by_pool)
                if self.nikki_status_text is not None:
                    self.nikki_status_text.value = f"分析完成：共 {overview['total_records']} 条记录，来自 {overview['pool_count']} 个卡池。"
                    self.nikki_status_text.color = self.PALETTE["accent"]
                if show_toast and self.current_view == "nikki":
                    self._toast("无限暖暖共鸣分析已刷新。", self.PALETTE["accent"])
            elif has_pearpal_records:
                overview = build_nikki_pearpal_overview(pearpal_by_pool)
                if self.nikki_status_text is not None:
                    self.nikki_status_text.value = (
                        f"分析完成：已按 pearpal 摘要模式估算 {overview['total_pulls']} 抽，覆盖 {overview['pool_count']} 个卡池。"
                    )
                    self.nikki_status_text.color = self.PALETTE["accent"]
                if show_toast and self.current_view == "nikki":
                    self._toast("无限暖暖 pearpal 摘要分析已刷新。", self.PALETTE["accent"])
            elif self.nikki_status_text is not None:
                if document.get("source_format") == "site_backup":
                    label = document.get("profile_label") or "-"
                    self.nikki_status_text.value = f"已读取 nuan5 档案 {label}，但 pulls 与 pearpal 都为空，这个文件没有共鸣记录。"
                else:
                    self.nikki_status_text.value = "该文件中没有可识别的共鸣记录。"
                self.nikki_status_text.color = self.PALETTE["warning"]
                self.controller.logger.warning(
                    "[抽卡分析] 无限暖暖 JSON 解析完成后无共鸣记录：source_format=%s, pools=%d",
                    document.get("source_format"),
                    len(document.get("top_level_keys", [])),
                )
        except Exception as exc:
            self.controller.logger.error("[抽卡分析] 无限暖暖 JSON 分析失败：%s", str(exc), exc_info=True)
            if self.nikki_status_text is not None:
                self.nikki_status_text.value = f"分析失败：{str(exc)}"
                self.nikki_status_text.color = self.PALETTE["danger"]
            self._toast(f"无限暖暖分析失败：{str(exc)}", self.PALETTE["danger"])
        self.page.update()

    # ────────────────────────────
    # 终末地抽卡视图
    # ────────────────────────────
    def _build_endfield_view(self) -> ft.Container:
        self.endfield_path_input = ft.TextField(
            label="终末地抽卡 JSON 路径",
            value=DEFAULT_ENDFIELD_PATH if os.path.exists(DEFAULT_ENDFIELD_PATH) else "",
            hint_text="粘贴终末地抽卡 JSON 文件路径，或点击下方浏览 JSON。",
            border_color=self.PALETTE["line"],
            filled=True,
            fill_color=self.PALETTE["panel_alt"],
        )
        self.endfield_browse_button = ft.OutlinedButton("浏览 JSON", icon=FT_ICONS.FOLDER_OPEN, on_click=self._pick_endfield_file)
        self.endfield_analyze_button = ft.ElevatedButton("分析数据", on_click=self._reload_endfield_data)
        self.endfield_status_text = ft.Text("点击「分析数据」或切换到该页面即可加载统计。", size=13, color=self.PALETTE["muted"])

        self.endfield_overview_cards = ft.Row(wrap=True, spacing=10)
        self.endfield_pool_selector = ft.Row(wrap=True, spacing=8)
        self.endfield_pool_content = ft.Container(content=self._build_pool_empty_state("分析后会在这里显示卡池明细。"), height=400)
        self.endfield_pool_tabs = ft.Column(
            controls=[self.endfield_pool_selector, self.endfield_pool_content],
            spacing=12,
        )
        self.endfield_six_star_list = ft.Column(spacing=6, scroll=ft.ScrollMode.AUTO, height=400)
        self.endfield_char_list = ft.Column(spacing=6, scroll=ft.ScrollMode.AUTO, height=400)
        self.endfield_weapon_list = ft.Column(spacing=6, scroll=ft.ScrollMode.AUTO, height=400)
        self.endfield_pity_text = ft.Text("", size=13, color=self.PALETTE["muted"])

        import_panel = self._panel(
            ft.Column(
                controls=[
                    ft.Text("终末地抽卡分析", size=20, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"]),
                    ft.Text("直接读取终末地抽卡 JSON 文件，无需数据库导入。", size=13, color=self.PALETTE["muted"]),
                    self.endfield_path_input,
                    ft.Row(controls=[self.endfield_browse_button, self.endfield_analyze_button], spacing=8, wrap=True),
                    self.endfield_status_text,
                ],
                spacing=10,
            )
        )

        overview_panel = self._panel(
            ft.Column(
                controls=[
                    ft.Text("抽卡总览", size=20, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"]),
                    self.endfield_overview_cards,
                ],
                spacing=10,
            )
        )

        pool_panel = self._panel(
            ft.Column(
                controls=[
                    ft.Text("各卡池详细分析", size=20, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"]),
                    self.endfield_pool_tabs,
                ],
                spacing=10,
            )
        )

        six_star_panel = self._panel(
            ft.Column(
                controls=[
                    ft.Text("六星出货记录", size=20, weight=ft.FontWeight.BOLD, color="#d4a017"),
                    self.endfield_six_star_list,
                ],
                spacing=10,
            )
        )

        char_panel = self._panel(
            ft.Column(
                controls=[
                    ft.Text("角色统计", size=20, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"]),
                    self.endfield_char_list,
                ],
                spacing=10,
            )
        )

        weapon_panel = self._panel(
            ft.Column(
                controls=[
                    ft.Text("武器统计", size=20, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"]),
                    self.endfield_weapon_list,
                ],
                spacing=10,
            )
        )

        pity_panel = self._panel(
            ft.Column(
                controls=[
                    ft.Text("保底分布", size=20, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"]),
                    self.endfield_pity_text,
                ],
                spacing=10,
            )
        )

        return ft.Container(
            visible=False,
            padding=ft.Padding(18, 16, 18, 16),
            content=ft.Column(
                controls=[import_panel, overview_panel, pool_panel, six_star_panel, char_panel, weapon_panel, pity_panel],
                spacing=12,
                scroll=ft.ScrollMode.AUTO,
            ),
        )

    async def _pick_endfield_file(self, e):
        try:
            picker_kwargs = {"allow_multiple": False}
            file_picker_type = getattr(ft, "FilePickerFileType", None)
            if file_picker_type is not None and hasattr(file_picker_type, "CUSTOM"):
                picker_kwargs["file_type"] = file_picker_type.CUSTOM
                picker_kwargs["allowed_extensions"] = ["json"]
            files = await ft.FilePicker().pick_files(**picker_kwargs)
            if not files:
                return
            selected_path = getattr(files[0], "path", "") or getattr(files[0], "name", "")
            if self.endfield_path_input is not None and selected_path:
                self.endfield_path_input.value = selected_path
                self.page.update()
        except Exception as exc:
            self._toast(f"选择文件失败：{str(exc)}", self.PALETTE["danger"])

    def _reload_endfield_data(self, e=None, show_toast: bool = True):
        file_path = (self.endfield_path_input.value or "").strip() if self.endfield_path_input is not None else ""
        if not file_path or not os.path.exists(file_path):
            self.controller.logger.info("[抽卡分析] 终末地数据路径无效：%s", file_path or "<empty>")
            if self.endfield_status_text is not None:
                self.endfield_status_text.value = "文件路径无效，请检查路径是否正确。"
                self.endfield_status_text.color = self.PALETTE["danger"]
            self.page.update()
            return
        try:
            self.controller.logger.info("[抽卡分析] 开始分析终末地数据：%s", file_path)
            records = load_endfield_records(file_path)
            if not records:
                self.controller.logger.info("[抽卡分析] 终末地文件中没有抽卡记录：%s", file_path)
                if self.endfield_status_text is not None:
                    self.endfield_status_text.value = "该文件中没有抽卡记录。"
                    self.endfield_status_text.color = self.PALETTE["warning"]
                self.page.update()
                return
            self._render_endfield_analysis(records)
            self.controller.logger.info("[抽卡分析] 终末地分析完成：记录 %d 条", len(records))
            if self.endfield_status_text is not None:
                self.endfield_status_text.value = f"分析完成：共 {len(records)} 条记录。"
                self.endfield_status_text.color = self.PALETTE["accent"]
            if show_toast and self.current_view == "endfield":
                self._toast("终末地抽卡分析已刷新。", self.PALETTE["accent"])
        except Exception as exc:
            self.controller.logger.error("[抽卡分析] 终末地分析失败：%s", str(exc), exc_info=True)
            if self.endfield_status_text is not None:
                self.endfield_status_text.value = f"分析失败：{str(exc)}"
                self.endfield_status_text.color = self.PALETTE["danger"]
            self._toast(f"终末地分析失败：{str(exc)}", self.PALETTE["danger"])
        self.page.update()

    def _render_endfield_analysis(self, records):
        overview = build_endfield_overview(records)
        pool_analysis_result = build_pool_analysis(records)
        pity_analysis_result = build_pity_analysis(records)
        six_star_history = build_six_star_history(records)
        char_stats = build_endfield_character_stats(records)
        weapon_stats = build_endfield_weapon_stats(records)

        if self.endfield_overview_cards is not None:
            self.endfield_overview_cards.controls = [
                self._build_gacha_metric_card("总记录", str(overview["total_records"]), f"{overview['earliest_time']} ~ {overview['latest_time']}"),
                self._build_gacha_metric_card("六星", str(overview["six_star_count"]), "最高稀有度"),
                self._build_gacha_metric_card("五星", str(overview["five_star_count"]), "高稀有度"),
                self._build_gacha_metric_card("四星", str(overview["four_star_count"]), "基础稀有度"),
            ]

        pool_order = ["限定角色池", "常驻角色池", "新手池", "限定武器池", "常驻武器池"]
        self.endfield_pool_views = {}
        for pool_name in pool_order:
            if pool_name not in pool_analysis_result:
                continue
            p = pool_analysis_result[pool_name]
            if p["total"] == 0:
                continue
            self.endfield_pool_views[pool_name] = self._build_endfield_pool_tab(pool_name, p, pity_analysis_result.get(pool_name))
        self.endfield_selected_pool = self._refresh_pool_switcher(
            self.endfield_pool_selector,
            self.endfield_pool_content,
            self.endfield_pool_views,
            self.endfield_selected_pool,
            self._select_endfield_pool,
            "当前文件中没有可展示的卡池数据。",
        )

        if self.endfield_six_star_list is not None:
            self.endfield_six_star_list.controls.clear()
            for h in six_star_history[:50]:
                self.endfield_six_star_list.controls.append(
                    ft.Container(
                        bgcolor=self.PALETTE["panel_alt"],
                        border_radius=10,
                        padding=ft.Padding(10, 8, 10, 8),
                        content=ft.Row(
                            controls=[
                                ft.Text(h["time"], size=12, color=self.PALETTE["muted"], width=140),
                                ft.Text(h["name"], size=14, weight=ft.FontWeight.BOLD, color="#d4a017", width=100),
                                ft.Text(f"[{h['pool']}]", size=12, color=self.PALETTE["muted"], width=100),
                                ft.Text(f"第 {h['pity']} 抽", size=12, color=self.PALETTE["text"], width=80),
                            ],
                            spacing=8,
                        ),
                    )
                )

        if self.endfield_char_list is not None:
            self.endfield_char_list.controls.clear()
            current_rarity = None
            rarity_color = {6: "#d4a017", 5: "#8e44ad", 4: "#2980b9"}
            for c in char_stats:
                if current_rarity != c["rarity"]:
                    current_rarity = c["rarity"]
                    color = rarity_color.get(c["rarity"], self.PALETTE["text"])
                    self.endfield_char_list.controls.append(ft.Text(c["rarity_label"], size=16, weight=ft.FontWeight.BOLD, color=color))
                self.endfield_char_list.controls.append(
                    ft.Container(
                        bgcolor=self.PALETTE["panel_alt"],
                        border_radius=8,
                        padding=ft.Padding(8, 6, 8, 6),
                        content=ft.Row(
                            controls=[
                                ft.Text(c["name"], size=13, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"], width=120),
                                ft.Text(f"x{c['copies']}", size=13, color=self.PALETTE["muted"], width=50),
                                ft.Text(f"{c['first_time']} ~ {c['last_time']}", size=12, color=self.PALETTE["muted"]),
                            ],
                            spacing=8,
                        ),
                    )
                )

        if self.endfield_weapon_list is not None:
            self.endfield_weapon_list.controls.clear()
            current_rarity = None
            rarity_color = {6: "#d4a017", 5: "#8e44ad", 4: "#2980b9"}
            for w in weapon_stats:
                if current_rarity != w["rarity"]:
                    current_rarity = w["rarity"]
                    color = rarity_color.get(w["rarity"], self.PALETTE["text"])
                    self.endfield_weapon_list.controls.append(ft.Text(w["rarity_label"], size=16, weight=ft.FontWeight.BOLD, color=color))
                self.endfield_weapon_list.controls.append(
                    ft.Container(
                        bgcolor=self.PALETTE["panel_alt"],
                        border_radius=8,
                        padding=ft.Padding(8, 6, 8, 6),
                        content=ft.Row(
                            controls=[
                                ft.Text(w["name"], size=13, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"], width=180),
                                ft.Text(f"x{w['copies']}", size=13, color=self.PALETTE["muted"], width=50),
                                ft.Text(f"{w['first_time']} ~ {w['last_time']}", size=12, color=self.PALETTE["muted"]),
                            ],
                            spacing=8,
                        ),
                    )
                )

        if self.endfield_pity_text is not None:
            pity_lines = []
            for pool_name in pool_order:
                if pool_name not in pity_analysis_result:
                    continue
                pt = pity_analysis_result[pool_name]
                if pt["count"] == 0:
                    continue
                dist = sorted(pt["distribution"].items())
                dist_str = ", ".join(f"{pity}抽={count}次" for pity, count in dist)
                pity_lines.append(f"{pool_name}: 共 {pt['count']} 次六星, 平均 {pt['avg_pity']} 抽 (最少 {pt['min_pity']}, 最多 {pt['max_pity']})")
                pity_lines.append(f"  分布: {dist_str}")
            self.endfield_pity_text.value = "\n".join(pity_lines) if pity_lines else "暂无六星保底数据。"

    def _build_endfield_pool_tab(self, pool_name: str, p: Dict[str, Any], pity: Optional[Dict[str, Any]]) -> ft.Column:
        controls: List[ft.Control] = [
            ft.Row(
                controls=[
                    self._build_gacha_metric_card("总计", f"{p['total']} 抽", "该卡池总抽数"),
                    self._build_gacha_metric_card("六星", f"{p['six_star_count']} 个", f"{p['six_star_pct']}%"),
                    self._build_gacha_metric_card("五星", f"{p['five_star_count']} 个", "高稀有度"),
                    self._build_gacha_metric_card("四星", f"{p['four_star_count']} 个", "基础稀有度"),
                ],
                wrap=True,
                spacing=10,
            ),
            ft.Text(f"时间范围: {p['earliest_time']} ~ {p['latest_time']}", size=13, color=self.PALETTE["muted"]),
        ]

        if p.get("sub_pools") and len(p["sub_pools"]) > 1:
            sub_chips = ft.Row(wrap=True, spacing=8)
            for sub_name, sub_count in p["sub_pools"].items():
                sub_chips.controls.append(
                    ft.Container(
                        bgcolor=self.PALETTE["accent_soft"],
                        border_radius=999,
                        padding=ft.Padding(8, 4, 8, 4),
                        content=ft.Text(f"{sub_name}: {sub_count} 抽", size=12, color=self.PALETTE["accent"]),
                    )
                )
            controls.append(ft.Column([ft.Text("子池分布:", size=13, color=self.PALETTE["muted"]), sub_chips], spacing=6))

        if p.get("six_star_list"):
            six_star_row_controls = []
            for s in reversed(p["six_star_list"]):
                new_tag = " [NEW!]" if s.get("is_new") else ""
                six_star_row_controls.append(
                    ft.Container(
                        bgcolor=self.PALETTE["warning_soft"],
                        border_radius=10,
                        padding=ft.Padding(10, 8, 10, 8),
                        content=ft.Row(
                            controls=[
                                ft.Text(s["time"], size=12, color=self.PALETTE["muted"], width=140),
                                ft.Text(f"{s['name']}{new_tag}", size=14, weight=ft.FontWeight.BOLD, color="#d4a017"),
                                ft.Text(f"第 {s.get('pity', '?')} 抽", size=12, color=self.PALETTE["text"]),
                                ft.Text(f"池: {s.get('pool_name', '')}", size=12, color=self.PALETTE["muted"]),
                            ],
                            spacing=8,
                        ),
                    )
                )
            controls.append(ft.Column([ft.Text("六星出货记录:", size=14, weight=ft.FontWeight.BOLD, color="#d4a017"), *six_star_row_controls], spacing=6))

        if pity and pity["count"] > 0:
            controls.append(
                ft.Container(
                    bgcolor=self.PALETTE["info_soft"],
                    border_radius=12,
                    padding=ft.Padding(12, 10, 12, 10),
                    content=ft.Column(
                        controls=[
                            ft.Text("六星保底分析", size=14, weight=ft.FontWeight.BOLD, color=self.PALETTE["info"]),
                            ft.Text(f"共 {pity['count']} 次出货 | 平均 {pity['avg_pity']} 抽 | 最少 {pity['min_pity']} 抽 | 最多 {pity['max_pity']} 抽", size=13),
                            ft.Text(f"当前距上次六星已抽: {pity.get('current_pity_no_6s', 0)} 抽", size=13, color=self.PALETTE["muted"]),
                            ft.Text(f"最近出货: {', '.join(str(x) for x in pity.get('pities', [])[-10:])}", size=12, color=self.PALETTE["muted"]),
                        ],
                        spacing=4,
                    ),
                )
            )

        return ft.Column(controls=controls, spacing=12, scroll=ft.ScrollMode.AUTO)

    # ────────────────────────────
    # 鸣潮专项视图
    # ────────────────────────────
    def _build_wuwa_view(self) -> ft.Container:
        kujiequ_config = self._get_wuwa_kujiequ_config()
        saved_profile = wuwa_kujiequ_profile_from_dict(kujiequ_config.get("profile"))
        saved_source = str(kujiequ_config.get("source") or ("h5" if saved_profile and saved_profile.is_web else "android"))

        self.wuwa_kujiequ_phone_input = ft.TextField(
            label="手机号",
            value=str(kujiequ_config.get("phone") or ""),
            hint_text="用于手机号 + 验证码登录库街区。",
            border_color=self.PALETTE["line"],
            filled=True,
            fill_color=self.PALETTE["panel_alt"],
        )
        self.wuwa_kujiequ_code_input = ft.TextField(
            label="短信验证码",
            hint_text="当前版本未内置发送验证码，请先在库街区 App / 网页端获取。",
            border_color=self.PALETTE["line"],
            filled=True,
            fill_color=self.PALETTE["panel_alt"],
        )
        self.wuwa_kujiequ_token_input = ft.TextField(
            label="库街区 Token",
            value=saved_profile.token if saved_profile is not None else "",
            hint_text="已有 token 时可直接使用，无需再次短信登录。",
            password=True,
            can_reveal_password=True,
            border_color=self.PALETTE["line"],
            filled=True,
            fill_color=self.PALETTE["panel_alt"],
        )
        self.wuwa_kujiequ_did_input = ft.TextField(
            label="DID",
            value=saved_profile.did if saved_profile is not None else "",
            hint_text="使用现有 token 时可手动填写 DID；留空则沿用已保存值或自动生成。",
            password=True,
            can_reveal_password=True,
            border_color=self.PALETTE["line"],
            filled=True,
            fill_color=self.PALETTE["panel_alt"],
        )
        self.wuwa_kujiequ_source_dropdown = ft.Dropdown(
            label="来源模式",
            value=saved_source if saved_source in {"android", "h5"} else "android",
            options=[
                ft.dropdown.Option(key="android", text="APP / Android"),
                ft.dropdown.Option(key="h5", text="网页 / H5"),
            ],
            border_color=self.PALETTE["line"],
            filled=True,
            fill_color=self.PALETTE["panel_alt"],
            dense=True,
            content_padding=10,
        )
        self.wuwa_kujiequ_role_dropdown = ft.Dropdown(
            label="绑定角色",
            hint_text="登录或使用 token 后自动拉取。",
            options=[],
            on_select=self._on_wuwa_kujiequ_role_change,
            border_color=self.PALETTE["line"],
            filled=True,
            fill_color=self.PALETTE["panel_alt"],
            dense=True,
            content_padding=10,
        )
        self.wuwa_kujiequ_auto_sign_checkbox = ft.Checkbox(
            label="刷新数据 / 启动一条龙前自动签到",
            value=bool(kujiequ_config.get("auto_sign", False)),
            on_change=self._on_wuwa_kujiequ_auto_sign_change,
        )
        self.wuwa_kujiequ_login_button = ft.ElevatedButton("手机号 + 验证码登录", on_click=self._start_wuwa_kujiequ_login)
        self.wuwa_kujiequ_token_button = ft.OutlinedButton("使用现有 Token", on_click=self._start_wuwa_kujiequ_token_login)
        self.wuwa_kujiequ_refresh_button = ft.ElevatedButton("刷新库街区数据", on_click=self._start_wuwa_kujiequ_refresh)
        self.wuwa_kujiequ_sign_button = ft.OutlinedButton("立即签到", on_click=self._start_wuwa_kujiequ_sign)
        self.wuwa_kujiequ_clear_button = ft.TextButton("清空账号", on_click=self._clear_wuwa_kujiequ_account)
        self.wuwa_kujiequ_status_text = ft.Text(
            "先登录或填入现有 token，再刷新体力、活跃度、周本等数据。",
            size=13,
            color=self.PALETTE["muted"],
        )
        self.wuwa_kujiequ_account_text = ft.Text("未登录库街区。", size=13, color=self.PALETTE["muted"])
        self.wuwa_kujiequ_metrics_row = ft.Row(wrap=True, spacing=10)
        self.wuwa_kujiequ_daily_list = ft.Column(spacing=8)
        self.wuwa_kujiequ_base_list = ft.Column(spacing=8)
        self.wuwa_kujiequ_sign_list = ft.Column(spacing=8)

        self.wuwa_path_input = ft.TextField(
            label="鸣潮抽卡 XLSX 路径",
            value=DEFAULT_WUWA_PATH if os.path.exists(DEFAULT_WUWA_PATH) else "",
            hint_text="粘贴鸣潮抽卡 XLSX 文件路径，或点击下方浏览 XLSX。",
            border_color=self.PALETTE["line"],
            filled=True,
            fill_color=self.PALETTE["panel_alt"],
        )
        self.wuwa_browse_button = ft.OutlinedButton("浏览 XLSX", icon=FT_ICONS.FOLDER_OPEN, on_click=self._pick_wuwa_file)
        self.wuwa_analyze_button = ft.ElevatedButton("分析数据", on_click=self._reload_wuwa_data)
        self.wuwa_status_text = ft.Text("只有点击「分析数据」时才会导入并刷新鸣潮抽卡统计。", size=13, color=self.PALETTE["muted"])

        self.wuwa_overview_cards = ft.Row(wrap=True, spacing=10)
        self.wuwa_pool_selector = ft.Row(wrap=True, spacing=8)
        self.wuwa_pool_content = ft.Container(content=self._build_pool_empty_state("分析后会在这里显示卡池明细。"), height=400)
        self.wuwa_pool_tabs = ft.Column(
            controls=[self.wuwa_pool_selector, self.wuwa_pool_content],
            spacing=12,
        )
        self.wuwa_five_star_list = ft.Column(spacing=6, scroll=ft.ScrollMode.AUTO, height=400)
        self.wuwa_char_list = ft.Column(spacing=6, scroll=ft.ScrollMode.AUTO, height=400)
        self.wuwa_weapon_list = ft.Column(spacing=6, scroll=ft.ScrollMode.AUTO, height=400)
        self.wuwa_pity_text = ft.Text("", size=13, color=self.PALETTE["muted"])

        kujiequ_account_panel = self._panel(
            ft.Column(
                controls=[
                    ft.Text("库街区账号与角色", size=20, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"]),
                    ft.Text("支持手机号 + 验证码登录，也支持直接填入现有 token；若你手头已有固定 DID，也可以在这里手动填写。", size=13, color=self.PALETTE["muted"]),
                    ft.Row(controls=[self.wuwa_kujiequ_phone_input, self.wuwa_kujiequ_code_input], spacing=8, wrap=True),
                    ft.Row(controls=[self.wuwa_kujiequ_token_input, self.wuwa_kujiequ_did_input, self.wuwa_kujiequ_source_dropdown], spacing=8, wrap=True),
                    ft.Row(controls=[self.wuwa_kujiequ_role_dropdown], spacing=8, wrap=True),
                    ft.Row(
                        controls=[
                            self.wuwa_kujiequ_login_button,
                            self.wuwa_kujiequ_token_button,
                            self.wuwa_kujiequ_refresh_button,
                            self.wuwa_kujiequ_sign_button,
                            self.wuwa_kujiequ_clear_button,
                        ],
                        spacing=8,
                        wrap=True,
                    ),
                    self.wuwa_kujiequ_auto_sign_checkbox,
                    self.wuwa_kujiequ_status_text,
                ],
                spacing=10,
            )
        )

        kujiequ_overview_panel = self._panel(
            ft.Column(
                controls=[
                    ft.Text("库街区总览", size=20, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"]),
                    self.wuwa_kujiequ_account_text,
                    self.wuwa_kujiequ_metrics_row,
                ],
                spacing=10,
            )
        )

        kujiequ_daily_panel = self._panel(
            ft.Column(
                controls=[
                    ft.Text("每日数据", size=20, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"]),
                    self.wuwa_kujiequ_daily_list,
                ],
                spacing=10,
            )
        )

        kujiequ_base_panel = self._panel(
            ft.Column(
                controls=[
                    ft.Text("基础与周常数据", size=20, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"]),
                    self.wuwa_kujiequ_base_list,
                ],
                spacing=10,
            )
        )

        kujiequ_sign_panel = self._panel(
            ft.Column(
                controls=[
                    ft.Text("签到奖励预览", size=20, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"]),
                    self.wuwa_kujiequ_sign_list,
                ],
                spacing=10,
            )
        )

        import_panel = self._panel(
            ft.Column(
                controls=[
                    ft.Text("鸣潮抽卡分析（XLSX）", size=20, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"]),
                    ft.Text("库街区专项与抽卡分析互不影响；这里仍然直接读取鸣潮官方导出 XLSX。", size=13, color=self.PALETTE["muted"]),
                    self.wuwa_path_input,
                    ft.Row(controls=[self.wuwa_browse_button, self.wuwa_analyze_button], spacing=8, wrap=True),
                    self.wuwa_status_text,
                ],
                spacing=10,
            )
        )

        overview_panel = self._panel(
            ft.Column(
                controls=[
                    ft.Text("抽卡总览", size=20, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"]),
                    self.wuwa_overview_cards,
                ],
                spacing=10,
            )
        )

        pool_panel = self._panel(
            ft.Column(
                controls=[
                    ft.Text("各卡池详细分析", size=20, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"]),
                    self.wuwa_pool_tabs,
                ],
                spacing=10,
            )
        )

        five_star_panel = self._panel(
            ft.Column(
                controls=[
                    ft.Text("五星出货记录", size=20, weight=ft.FontWeight.BOLD, color="#d4a017"),
                    self.wuwa_five_star_list,
                ],
                spacing=10,
            )
        )

        char_panel = self._panel(
            ft.Column(
                controls=[
                    ft.Text("角色统计", size=20, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"]),
                    self.wuwa_char_list,
                ],
                spacing=10,
            )
        )

        weapon_panel = self._panel(
            ft.Column(
                controls=[
                    ft.Text("武器统计", size=20, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"]),
                    self.wuwa_weapon_list,
                ],
                spacing=10,
            )
        )

        pity_panel = self._panel(
            ft.Column(
                controls=[
                    ft.Text("保底分布", size=20, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"]),
                    self.wuwa_pity_text,
                ],
                spacing=10,
            )
        )

        view = ft.Container(
            visible=False,
            padding=ft.Padding(18, 16, 18, 16),
            content=ft.Column(
                controls=[
                    kujiequ_account_panel,
                    kujiequ_overview_panel,
                    kujiequ_daily_panel,
                    kujiequ_base_panel,
                    kujiequ_sign_panel,
                    import_panel,
                    overview_panel,
                    pool_panel,
                    five_star_panel,
                    char_panel,
                    weapon_panel,
                    pity_panel,
                ],
                spacing=12,
                scroll=ft.ScrollMode.AUTO,
            ),
        )
        self._apply_wuwa_kujiequ_config_to_controls()
        self._render_wuwa_kujiequ_dashboard(None)
        return view

    # ────────────────────────────
    # 塔吉多（异环社区）签到视图
    # ────────────────────────────
    def _build_tajiduo_view(self) -> ft.Container:
        cfg = self._get_tajiduo_config()

        self.tajiduo_phone_input = ft.TextField(
            label="手机号",
            value=str(cfg.get("phone") or ""),
            hint_text="老虎账号（+86 手机号）",
            border_color=self.PALETTE["line"],
            filled=True,
            fill_color=self.PALETTE["panel_alt"],
            dense=True,
        )
        self.tajiduo_password_input = ft.TextField(
            label="密码",
            password=True,
            can_reveal_password=True,
            hint_text="老虎账号密码，用于自动重登",
            border_color=self.PALETTE["line"],
            filled=True,
            fill_color=self.PALETTE["panel_alt"],
            dense=True,
        )
        self.tajiduo_sms_code_input = ft.TextField(
            label="短信验证码",
            hint_text="6 位验证码",
            width=150,
            border_color=self.PALETTE["line"],
            filled=True,
            fill_color=self.PALETTE["panel_alt"],
            dense=True,
        )
        self.tajiduo_token_input = ft.TextField(
            label="AccessToken",
            value=str(cfg.get("token") or ""),
            hint_text="已有 token 可直接填入；登录成功后也会自动写入",
            password=True,
            can_reveal_password=True,
            border_color=self.PALETTE["line"],
            filled=True,
            fill_color=self.PALETTE["panel_alt"],
            dense=True,
        )
        self.tajiduo_deviceid_input = ft.TextField(
            label="DeviceID",
            value=str(cfg.get("deviceid") or ""),
            hint_text="32 位十六进制设备 ID；留空则自动生成",
            border_color=self.PALETTE["line"],
            filled=True,
            fill_color=self.PALETTE["panel_alt"],
            dense=True,
        )
        self.tajiduo_uid_input = ft.TextField(
            label="UID",
            value=str(cfg.get("uid") or ""),
            hint_text="塔吉多用户 UID；登录成功后自动获取",
            width=160,
            border_color=self.PALETTE["line"],
            filled=True,
            fill_color=self.PALETTE["panel_alt"],
            dense=True,
        )
        self.tajiduo_bbs_checkbox = ft.Checkbox(
            label="BBS 签到 (+40 金币)",
            value=bool(cfg.get("do_bbs_signin", True)),
            on_change=self._on_tajiduo_option_change,
        )
        self.tajiduo_game_checkbox = ft.Checkbox(
            label="游戏签到",
            value=bool(cfg.get("do_game_signin", True)),
            on_change=self._on_tajiduo_option_change,
        )
        self.tajiduo_browse_checkbox = ft.Checkbox(
            label="浏览帖子",
            value=bool(cfg.get("do_browse", False)),
            on_change=self._on_tajiduo_option_change,
        )
        self.tajiduo_like_checkbox = ft.Checkbox(
            label="点赞帖子",
            value=bool(cfg.get("do_like", False)),
            on_change=self._on_tajiduo_option_change,
        )
        self.tajiduo_share_checkbox = ft.Checkbox(
            label="分享帖子",
            value=bool(cfg.get("do_share", False)),
            on_change=self._on_tajiduo_option_change,
        )
        self.tajiduo_startup_checkbox = ft.Checkbox(
            label="启动一条龙前自动签到",
            value=bool(cfg.get("startup_sign", False)),
            on_change=self._on_tajiduo_startup_change,
        )
        self.tajiduo_login_button = ft.ElevatedButton("密码登录", icon=FT_ICONS.LOGIN, on_click=self._start_tajiduo_password_login)
        self.tajiduo_sms_send_button = ft.OutlinedButton("发送验证码", icon=FT_ICONS.SEND, on_click=self._start_tajiduo_sms_captcha)
        self.tajiduo_sms_login_button = ft.ElevatedButton("验证码登录", icon=FT_ICONS.LOGIN, on_click=self._start_tajiduo_sms_login)
        self.tajiduo_token_button = ft.OutlinedButton("使用现有 Token", on_click=self._start_tajiduo_token_login)
        self.tajiduo_sign_button = ft.ElevatedButton("立即执行签到", icon=FT_ICONS.CHECK_CIRCLE_OUTLINE, on_click=self._start_tajiduo_sign)
        self.tajiduo_clear_button = ft.TextButton("清空配置", on_click=self._clear_tajiduo_config)
        self.tajiduo_status_text = ft.Text(
            "支持密码登录、短信验证码登录，或直接填入已有 Token。",
            size=13,
            color=self.PALETTE["muted"],
        )
        self.tajiduo_account_text = ft.Text("未登录塔吉多。", size=13, color=self.PALETTE["muted"])
        self.tajiduo_metrics_row = ft.Row(wrap=True, spacing=10)
        self.tajiduo_result_list = ft.Column(spacing=8)

        login_panel = self._panel(
            ft.Column(
                controls=[
                    ft.Container(
                        bgcolor=self.PALETTE["accent_soft"],
                        border_radius=18,
                        padding=ft.Padding(16, 14, 16, 14),
                        content=ft.Row(
                            controls=[
                                ft.Container(
                                    width=52,
                                    height=52,
                                    border_radius=16,
                                    bgcolor=self.PALETTE["card"],
                                    content=ft.Icon(FT_ICONS.AUTO_AWESOME, color=self.PALETTE["accent"], size=28),
                                    alignment=ft.Alignment(0, 0),
                                ),
                                ft.Column(
                                    controls=[
                                        ft.Text("塔吉多（异环社区）签到", size=20, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"]),
                                        ft.Text(
                                            "BBS 每日签到、游戏签到，以及可选的浏览/点赞/分享任务。",
                                            size=13,
                                            color=self.PALETTE["muted"],
                                        ),
                                    ],
                                    spacing=4,
                                    expand=True,
                                ),
                            ],
                            spacing=12,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                    ),
                    ft.Row(
                        controls=[self.tajiduo_phone_input, self.tajiduo_password_input, self.tajiduo_login_button],
                        spacing=8,
                        wrap=True,
                    ),
                    ft.Row(
                        controls=[
                            self.tajiduo_sms_send_button,
                            self.tajiduo_sms_code_input,
                            self.tajiduo_sms_login_button,
                        ],
                        spacing=8,
                        wrap=True,
                    ),
                    ft.Row(
                        controls=[self.tajiduo_token_input, self.tajiduo_deviceid_input, self.tajiduo_uid_input],
                        spacing=8,
                        wrap=True,
                    ),
                    ft.Row(
                        controls=[
                            self.tajiduo_token_button,
                            self.tajiduo_sign_button,
                            self.tajiduo_clear_button,
                        ],
                        spacing=8,
                        wrap=True,
                    ),
                    ft.Row(
                        controls=[
                            self.tajiduo_bbs_checkbox,
                            self.tajiduo_game_checkbox,
                            self.tajiduo_browse_checkbox,
                            self.tajiduo_like_checkbox,
                            self.tajiduo_share_checkbox,
                        ],
                        spacing=8,
                        wrap=True,
                    ),
                    self.tajiduo_startup_checkbox,
                    self.tajiduo_status_text,
                ],
                spacing=10,
            )
        )

        result_panel = self._panel(
            ft.Column(
                controls=[
                    ft.Text("签到结果", size=20, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"]),
                    self.tajiduo_account_text,
                    self.tajiduo_metrics_row,
                    self.tajiduo_result_list,
                ],
                spacing=10,
            )
        )

        view = ft.Container(
            visible=False,
            padding=ft.Padding(18, 16, 18, 16),
            content=ft.Column(
                controls=[login_panel, result_panel],
                spacing=12,
                scroll=ft.ScrollMode.AUTO,
            ),
        )
        self._apply_tajiduo_config_to_controls()
        self._render_tajiduo_sign_result(None)
        return view

    async def _pick_wuwa_file(self, e):
        try:
            picker_kwargs = {"allow_multiple": False}
            file_picker_type = getattr(ft, "FilePickerFileType", None)
            if file_picker_type is not None and hasattr(file_picker_type, "CUSTOM"):
                picker_kwargs["file_type"] = file_picker_type.CUSTOM
                picker_kwargs["allowed_extensions"] = ["xlsx"]
            files = await ft.FilePicker().pick_files(**picker_kwargs)
            if not files:
                return
            selected_path = getattr(files[0], "path", "") or getattr(files[0], "name", "")
            if self.wuwa_path_input is not None and selected_path:
                self.wuwa_path_input.value = selected_path
                self.page.update()
        except Exception as exc:
            self._toast(f"选择文件失败：{str(exc)}", self.PALETTE["danger"])

    def _reload_wuwa_data(self, e=None, show_toast: bool = True):
        file_path = (self.wuwa_path_input.value or "").strip() if self.wuwa_path_input is not None else ""

        profile = self._current_wuwa_kujiequ_profile()
        uid = (profile.role_id or profile.user_id or "").strip() if profile is not None else ""

        if uid:
            db_rows = self.preference_db.get_gacha_records("wuwa", uid)
            if db_rows:
                self.controller.logger.info("[抽卡分析] 鸣潮 从数据库加载 UID %s 的 %d 条记录", uid, len(db_rows))
                records = db_to_wuwa_records(db_rows)
                self._render_wuwa_analysis(records)
                if self.wuwa_status_text is not None:
                    self.wuwa_status_text.value = f"从数据库加载：共 {len(records)} 条记录，来自 {len(set(r.pool_display for r in records))} 个卡池。"
                    self.wuwa_status_text.color = self.PALETTE["accent"]
                if show_toast and self.current_view == "wuwa":
                    self._toast("鸣潮抽卡分析已从数据库加载。", self.PALETTE["accent"])
                self.page.update()
                return

        if not file_path or not os.path.exists(file_path):
            self.controller.logger.info("[抽卡分析] 鸣潮 XLSX 路径无效：%s", file_path or "<empty>")
            if self.wuwa_status_text is not None:
                if show_toast:
                    self.wuwa_status_text.value = "文件路径无效，请检查路径是否正确。"
                    self.wuwa_status_text.color = self.PALETTE["danger"]
                else:
                    self.wuwa_status_text.value = "如需抽卡分析，请选择一个 XLSX 文件；数据库中也无缓存记录。"
                    self.wuwa_status_text.color = self.PALETTE["muted"]
            self.page.update()
            return
        try:
            self.controller.logger.info("[抽卡分析] 开始分析鸣潮 XLSX：%s", file_path)
            all_records = load_all_wuwa_records(file_path)
            records_list = list(all_records)
            if not records_list:
                self.controller.logger.info("[抽卡分析] 鸣潮 XLSX 中没有抽卡记录：%s", file_path)
                if self.wuwa_status_text is not None:
                    self.wuwa_status_text.value = "该文件中没有抽卡记录。"
                    self.wuwa_status_text.color = self.PALETTE["warning"]
                self.page.update()
                return
            self._render_wuwa_analysis(records_list)
            self.controller.logger.info(
                "[抽卡分析] 鸣潮分析完成：记录 %d 条，卡池 %d 个",
                len(records_list),
                len(set(r.pool_display for r in all_records)),
            )

            if uid:
                try:
                    db_records = wuwa_to_db_records(records_list, uid)
                    import_id = self.preference_db.start_gacha_import({
                        "game_code": "wuwa",
                        "uid": uid,
                        "source_path": file_path,
                        "source_hash": "",
                        "total_records": len(db_records),
                    })
                    self.preference_db.upsert_gacha_records("wuwa", uid, import_id, db_records)
                    self.controller.logger.info("[抽卡分析] 鸣潮记录已写入数据库：%d 条", len(db_records))
                except Exception as db_exc:
                    self.controller.logger.warning("[抽卡分析] 鸣潮记录写入数据库失败：%s", str(db_exc))

            if self.wuwa_status_text is not None:
                self.wuwa_status_text.value = f"分析完成：共 {len(records_list)} 条记录，来自 {len(set(r.pool_display for r in all_records))} 个卡池。"
                self.wuwa_status_text.color = self.PALETTE["accent"]
            if show_toast and self.current_view == "wuwa":
                self._toast("鸣潮抽卡分析已刷新。", self.PALETTE["accent"])
        except Exception as exc:
            self.controller.logger.error("[抽卡分析] 鸣潮 XLSX 分析失败：%s", str(exc), exc_info=True)
            if self.wuwa_status_text is not None:
                self.wuwa_status_text.value = f"分析失败：{str(exc)}"
                self.wuwa_status_text.color = self.PALETTE["danger"]
            self._toast(f"鸣潮分析失败：{str(exc)}", self.PALETTE["danger"])
        self.page.update()

    def _render_wuwa_analysis(self, records):
        overview = build_wuwa_overview(records)
        pool_analysis_result = build_wuwa_pool_analysis(records)
        pity_analysis_result = build_wuwa_pity_analysis(records)
        five_star_history = build_wuwa_five_star_history(records)
        char_stats = build_wuwa_character_stats(records)
        weapon_stats = build_wuwa_weapon_stats(records)

        if self.wuwa_overview_cards is not None:
            self.wuwa_overview_cards.controls = [
                self._build_gacha_metric_card("总记录", str(overview["total_records"]), f"{overview['earliest_time']} ~ {overview['latest_time']}"),
                self._build_gacha_metric_card("五星", str(overview["five_star_count"]), "最高稀有度"),
                self._build_gacha_metric_card("四星", str(overview["four_star_count"]), "高稀有度"),
                self._build_gacha_metric_card("三星", str(overview["three_star_count"]), "基础稀有度"),
            ]

        pool_order = ["限定角色池", "常驻角色池", "新手池", "限定武器池", "常驻武器池"]
        self.wuwa_pool_views = {}
        for pool_name in pool_order:
            if pool_name not in pool_analysis_result:
                continue
            p = pool_analysis_result[pool_name]
            if p["total"] == 0:
                continue
            self.wuwa_pool_views[pool_name] = self._build_wuwa_pool_tab(pool_name, p, pity_analysis_result.get(pool_name))
        self.wuwa_selected_pool = self._refresh_pool_switcher(
            self.wuwa_pool_selector,
            self.wuwa_pool_content,
            self.wuwa_pool_views,
            self.wuwa_selected_pool,
            self._select_wuwa_pool,
            "当前文件中没有可展示的卡池数据。",
        )

        if self.wuwa_five_star_list is not None:
            self.wuwa_five_star_list.controls.clear()
            for h in five_star_history[:60]:
                type_tag = f"[{h.get('type', '')}]"
                self.wuwa_five_star_list.controls.append(
                    ft.Container(
                        bgcolor=self.PALETTE["panel_alt"],
                        border_radius=10,
                        padding=ft.Padding(10, 8, 10, 8),
                        content=ft.Row(
                            controls=[
                                ft.Text(h["time"], size=12, color=self.PALETTE["muted"], width=140),
                                ft.Text(h["name"], size=14, weight=ft.FontWeight.BOLD, color="#d4a017", width=120),
                                ft.Text(type_tag, size=12, color=self.PALETTE["muted"], width=60),
                                ft.Text(f"[{h['pool']}]", size=12, color=self.PALETTE["muted"], width=100),
                                ft.Text(f"第 {h['pity']} 抽", size=12, color=self.PALETTE["text"], width=80),
                            ],
                            spacing=6,
                        ),
                    )
                )

        if self.wuwa_char_list is not None:
            self.wuwa_char_list.controls.clear()
            current_rarity = None
            rarity_color = {5: "#d4a017", 4: "#8e44ad", 3: "#2980b9"}
            for c in char_stats:
                if current_rarity != c["rarity"]:
                    current_rarity = c["rarity"]
                    color = rarity_color.get(c["rarity"], self.PALETTE["text"])
                    self.wuwa_char_list.controls.append(ft.Text(c["rarity_label"], size=16, weight=ft.FontWeight.BOLD, color=color))
                self.wuwa_char_list.controls.append(
                    ft.Container(
                        bgcolor=self.PALETTE["panel_alt"],
                        border_radius=8,
                        padding=ft.Padding(8, 6, 8, 6),
                        content=ft.Row(
                            controls=[
                                ft.Text(c["name"], size=13, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"], width=120),
                                ft.Text(f"x{c['copies']}", size=13, color=self.PALETTE["muted"], width=50),
                                ft.Text(f"{c['first_time']} ~ {c['last_time']}", size=12, color=self.PALETTE["muted"]),
                            ],
                            spacing=8,
                        ),
                    )
                )

        if self.wuwa_weapon_list is not None:
            self.wuwa_weapon_list.controls.clear()
            current_rarity = None
            rarity_color = {5: "#d4a017", 4: "#8e44ad", 3: "#2980b9"}
            for w in weapon_stats:
                if current_rarity != w["rarity"]:
                    current_rarity = w["rarity"]
                    color = rarity_color.get(w["rarity"], self.PALETTE["text"])
                    self.wuwa_weapon_list.controls.append(ft.Text(w["rarity_label"], size=16, weight=ft.FontWeight.BOLD, color=color))
                self.wuwa_weapon_list.controls.append(
                    ft.Container(
                        bgcolor=self.PALETTE["panel_alt"],
                        border_radius=8,
                        padding=ft.Padding(8, 6, 8, 6),
                        content=ft.Row(
                            controls=[
                                ft.Text(w["name"], size=13, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"], width=180),
                                ft.Text(f"x{w['copies']}", size=13, color=self.PALETTE["muted"], width=50),
                                ft.Text(f"{w['first_time']} ~ {w['last_time']}", size=12, color=self.PALETTE["muted"]),
                            ],
                            spacing=8,
                        ),
                    )
                )

        if self.wuwa_pity_text is not None:
            pity_lines = []
            for pool_name in pool_order:
                if pool_name not in pity_analysis_result:
                    continue
                pt = pity_analysis_result[pool_name]
                if pt["count"] == 0:
                    continue
                dist = sorted(pt["distribution"].items())
                dist_str = ", ".join(f"{pity}抽={count}次" for pity, count in dist)
                pity_lines.append(f"{pool_name}: 共 {pt['count']} 次五星, 平均 {pt['avg_pity']} 抽 (最少 {pt['min_pity']}, 最多 {pt['max_pity']})")
                pity_lines.append(f"  分布: {dist_str}")
            self.wuwa_pity_text.value = "\n".join(pity_lines) if pity_lines else "暂无五星保底数据。"

    def _build_wuwa_pool_tab(self, pool_name: str, p: Dict[str, Any], pity: Optional[Dict[str, Any]]) -> ft.Column:
        controls: List[ft.Control] = [
            ft.Row(
                controls=[
                    self._build_gacha_metric_card("总计", f"{p['total']} 抽", f"角色{p['character_count']} / 武器{p['weapon_count']}"),
                    self._build_gacha_metric_card("五星", f"{p['five_star_count']} 个", f"{p['five_star_pct']}%"),
                    self._build_gacha_metric_card("四星", f"{p['four_star_count']} 个", f"{p['four_star_pct']}%"),
                    self._build_gacha_metric_card("三星", f"{p['three_star_count']} 个", f"{p['three_star_pct']}%"),
                ],
                wrap=True,
                spacing=10,
            ),
            ft.Text(f"时间范围: {p['earliest_time']} ~ {p['latest_time']}", size=13, color=self.PALETTE["muted"]),
        ]

        if p.get("sub_pools") and len(p["sub_pools"]) > 1:
            sub_chips = ft.Row(wrap=True, spacing=8)
            for sub_name, sub_count in p["sub_pools"].items():
                sub_chips.controls.append(
                    ft.Container(
                        bgcolor=self.PALETTE["accent_soft"],
                        border_radius=999,
                        padding=ft.Padding(8, 4, 8, 4),
                        content=ft.Text(f"{sub_name}: {sub_count} 抽", size=12, color=self.PALETTE["accent"]),
                    )
                )
            controls.append(ft.Column([ft.Text("子池分布:", size=13, color=self.PALETTE["muted"]), sub_chips], spacing=6))

        if p.get("five_star_list"):
            row_controls = []
            for s in reversed(p["five_star_list"]):
                type_tag = f" [{s.get('type', '')}]"
                row_controls.append(
                    ft.Container(
                        bgcolor=self.PALETTE["warning_soft"],
                        border_radius=10,
                        padding=ft.Padding(10, 8, 10, 8),
                        content=ft.Row(
                            controls=[
                                ft.Text(s["time"], size=12, color=self.PALETTE["muted"], width=140),
                                ft.Text(f"{s['name']}{type_tag}", size=14, weight=ft.FontWeight.BOLD, color="#d4a017"),
                                ft.Text(f"第 {s.get('pity', '?')} 抽", size=12, color=self.PALETTE["text"]),
                                ft.Text(f"池: {s.get('pool_name', '')}", size=12, color=self.PALETTE["muted"]),
                            ],
                            spacing=8,
                        ),
                    )
                )
            controls.append(ft.Column([ft.Text("五星出货记录:", size=14, weight=ft.FontWeight.BOLD, color="#d4a017"), *row_controls], spacing=6))

        if pity and pity["count"] > 0:
            controls.append(
                ft.Container(
                    bgcolor=self.PALETTE["info_soft"],
                    border_radius=12,
                    padding=ft.Padding(12, 10, 12, 10),
                    content=ft.Column(
                        controls=[
                            ft.Text("五星保底分析", size=14, weight=ft.FontWeight.BOLD, color=self.PALETTE["info"]),
                            ft.Text(f"共 {pity['count']} 次出货 | 平均 {pity['avg_pity']} 抽 | 最少 {pity['min_pity']} 抽 | 最多 {pity['max_pity']} 抽", size=13),
                            ft.Text(f"当前距上次五星已抽: {pity.get('current_pity_no_5s', 0)} 抽", size=13, color=self.PALETTE["muted"]),
                            ft.Text(f"最近出货: {', '.join(str(x) for x in pity.get('pities', [])[-10:])}", size=12, color=self.PALETTE["muted"]),
                        ],
                        spacing=4,
                    ),
                )
            )

        return ft.Column(controls=controls, spacing=12, scroll=ft.ScrollMode.AUTO)

    def _get_wuwa_kujiequ_config(self) -> Dict[str, Any]:
        raw = cm.load().get(WUWA_KUJIEQU_CONFIG_KEY, {})
        return raw if isinstance(raw, dict) else {}

    def _update_wuwa_kujiequ_config(self, **updates: Any) -> Dict[str, Any]:
        config = cm.load()
        current = config.get(WUWA_KUJIEQU_CONFIG_KEY)
        section = dict(current) if isinstance(current, dict) else {}
        section.update(updates)
        config[WUWA_KUJIEQU_CONFIG_KEY] = section
        cm.save(config)
        return section

    def _clear_wuwa_kujiequ_config(self):
        config = cm.load()
        if WUWA_KUJIEQU_CONFIG_KEY in config:
            config.pop(WUWA_KUJIEQU_CONFIG_KEY, None)
            cm.save(config)

    def _set_wuwa_kujiequ_busy(self, busy: bool):
        self.wuwa_kujiequ_busy = busy
        for control in (
            self.wuwa_kujiequ_phone_input,
            self.wuwa_kujiequ_code_input,
            self.wuwa_kujiequ_token_input,
            self.wuwa_kujiequ_did_input,
            self.wuwa_kujiequ_source_dropdown,
            self.wuwa_kujiequ_role_dropdown,
            self.wuwa_kujiequ_auto_sign_checkbox,
            self.wuwa_kujiequ_login_button,
            self.wuwa_kujiequ_token_button,
            self.wuwa_kujiequ_refresh_button,
            self.wuwa_kujiequ_sign_button,
            self.wuwa_kujiequ_clear_button,
        ):
            if control is not None:
                control.disabled = busy

    def _set_wuwa_kujiequ_role_options(self, roles: List[Dict[str, str]], selected_role_id: Optional[str] = None):
        sanitized_roles: List[Dict[str, str]] = []
        for role in roles or []:
            if not isinstance(role, dict):
                continue
            sanitized_roles.append(
                {
                    "user_id": str(role.get("user_id") or "").strip(),
                    "role_id": str(role.get("role_id") or "").strip(),
                    "role_name": str(role.get("role_name") or "").strip(),
                }
            )
        self.wuwa_kujiequ_roles = [role for role in sanitized_roles if role["user_id"] and role["role_id"]]
        if self.wuwa_kujiequ_role_dropdown is None:
            return
        self.wuwa_kujiequ_role_dropdown.options = [
            ft.dropdown.Option(
                key=role["role_id"],
                text=f"{role['role_name'] or role['role_id']} | UID {role['user_id']}",
            )
            for role in self.wuwa_kujiequ_roles
        ]
        chosen = str(selected_role_id or "").strip()
        if not chosen and self.wuwa_kujiequ_roles:
            chosen = self.wuwa_kujiequ_roles[0]["role_id"]
        if chosen and any(role["role_id"] == chosen for role in self.wuwa_kujiequ_roles):
            self.wuwa_kujiequ_role_dropdown.value = chosen
        else:
            self.wuwa_kujiequ_role_dropdown.value = None

    def _apply_wuwa_kujiequ_config_to_controls(self):
        config = self._get_wuwa_kujiequ_config()
        profile = wuwa_kujiequ_profile_from_dict(config.get("profile"))
        if self.wuwa_kujiequ_phone_input is not None:
            self.wuwa_kujiequ_phone_input.value = str(config.get("phone") or "")
        if self.wuwa_kujiequ_token_input is not None:
            self.wuwa_kujiequ_token_input.value = profile.token if profile is not None else ""
        if self.wuwa_kujiequ_did_input is not None:
            self.wuwa_kujiequ_did_input.value = profile.did if profile is not None else ""
        if self.wuwa_kujiequ_source_dropdown is not None:
            source_value = str(config.get("source") or ("h5" if profile and profile.is_web else "android"))
            self.wuwa_kujiequ_source_dropdown.value = source_value if source_value in {"android", "h5"} else "android"
        if self.wuwa_kujiequ_auto_sign_checkbox is not None:
            self.wuwa_kujiequ_auto_sign_checkbox.value = bool(config.get("auto_sign", False))
        self._set_wuwa_kujiequ_role_options(config.get("roles") or [], profile.role_id if profile is not None else None)
        if self.wuwa_kujiequ_account_text is not None:
            if profile is None:
                self.wuwa_kujiequ_account_text.value = "未登录库街区。"
                self.wuwa_kujiequ_account_text.color = self.PALETTE["muted"]
            else:
                source_label = "网页 H5" if profile.is_web else "APP / Android"
                role_label = profile.role_name or profile.role_id or "未选择角色"
                self.wuwa_kujiequ_account_text.value = (
                    f"已保存角色：{role_label} | UID {profile.user_id or '-'} | RoleID {profile.role_id or '-'} | 来源 {source_label}"
                )
                self.wuwa_kujiequ_account_text.color = self.PALETTE["text"]

    def _current_wuwa_kujiequ_profile(self):
        config = self._get_wuwa_kujiequ_config()
        profile = wuwa_kujiequ_profile_from_dict(config.get("profile"))
        if profile is None:
            return None
        selected_role_id = ""
        if self.wuwa_kujiequ_role_dropdown is not None:
            selected_role_id = str(self.wuwa_kujiequ_role_dropdown.value or "").strip()
        roles = self.wuwa_kujiequ_roles or list(config.get("roles") or [])
        if selected_role_id:
            for role in roles:
                if isinstance(role, dict) and str(role.get("role_id") or "").strip() == selected_role_id:
                    return profile.with_role(role)
        return profile

    def _on_wuwa_kujiequ_auto_sign_change(self, e):
        self.controller.logger.info("[鸣潮库街区] 自动签到开关已调整：%s", "开启" if bool(e.control.value) else "关闭")
        self._update_wuwa_kujiequ_config(auto_sign=bool(e.control.value))

    def _on_wuwa_kujiequ_role_change(self, e):
        profile = self._current_wuwa_kujiequ_profile()
        if profile is None:
            return
        self.controller.logger.info(
            "[鸣潮库街区] 已切换当前角色：%s | UID %s | RoleID %s",
            profile.role_name or "-",
            profile.user_id or "-",
            profile.role_id or "-",
        )
        self._update_wuwa_kujiequ_config(profile=wuwa_kujiequ_profile_to_dict(profile))
        if self.wuwa_kujiequ_account_text is not None:
            source_label = "网页 H5" if profile.is_web else "APP / Android"
            self.wuwa_kujiequ_account_text.value = (
                f"已保存角色：{profile.role_name or profile.role_id} | UID {profile.user_id or '-'} | RoleID {profile.role_id or '-'} | 来源 {source_label}"
            )
            self.wuwa_kujiequ_account_text.color = self.PALETTE["text"]
        self.page.update()

    def _clear_wuwa_kujiequ_account(self, e):
        if self.wuwa_kujiequ_busy:
            return
        self.controller.logger.info("[鸣潮库街区] 用户已清空保存的账号和角色信息")
        self._clear_wuwa_kujiequ_config()
        self.wuwa_kujiequ_roles = []
        if self.wuwa_kujiequ_phone_input is not None:
            self.wuwa_kujiequ_phone_input.value = ""
        if self.wuwa_kujiequ_code_input is not None:
            self.wuwa_kujiequ_code_input.value = ""
        if self.wuwa_kujiequ_token_input is not None:
            self.wuwa_kujiequ_token_input.value = ""
        if self.wuwa_kujiequ_did_input is not None:
            self.wuwa_kujiequ_did_input.value = ""
        if self.wuwa_kujiequ_source_dropdown is not None:
            self.wuwa_kujiequ_source_dropdown.value = "android"
        if self.wuwa_kujiequ_auto_sign_checkbox is not None:
            self.wuwa_kujiequ_auto_sign_checkbox.value = False
        self._set_wuwa_kujiequ_role_options([], None)
        if self.wuwa_kujiequ_status_text is not None:
            self.wuwa_kujiequ_status_text.value = "已清空库街区账号信息。"
            self.wuwa_kujiequ_status_text.color = self.PALETTE["accent"]
        self._render_wuwa_kujiequ_dashboard(None)
        self.page.update()

    def _start_wuwa_kujiequ_login(self, e):
        if self.wuwa_kujiequ_busy:
            return
        phone = (self.wuwa_kujiequ_phone_input.value or "").strip() if self.wuwa_kujiequ_phone_input is not None else ""
        code = (self.wuwa_kujiequ_code_input.value or "").strip() if self.wuwa_kujiequ_code_input is not None else ""
        if not phone or not code:
            if self.wuwa_kujiequ_status_text is not None:
                self.wuwa_kujiequ_status_text.value = "请先填写手机号和短信验证码。"
                self.wuwa_kujiequ_status_text.color = self.PALETTE["danger"]
            self.page.update()
            return
        is_web = bool(self.wuwa_kujiequ_source_dropdown and self.wuwa_kujiequ_source_dropdown.value == "h5")
        self.controller.logger.info("[鸣潮库街区] 用户发起手机号登录，来源：%s", "网页 H5" if is_web else "APP / Android")
        if self.wuwa_kujiequ_status_text is not None:
            self.wuwa_kujiequ_status_text.value = "正在登录库街区并读取绑定角色..."
            self.wuwa_kujiequ_status_text.color = self.PALETTE["accent"]
        self._set_wuwa_kujiequ_busy(True)
        self.page.update()
        threading.Thread(target=self._run_wuwa_kujiequ_login_backend, args=(phone, code, is_web), daemon=True).start()

    def _start_wuwa_kujiequ_token_login(self, e):
        if self.wuwa_kujiequ_busy:
            return
        token = (self.wuwa_kujiequ_token_input.value or "").strip() if self.wuwa_kujiequ_token_input is not None else ""
        if not token:
            if self.wuwa_kujiequ_status_text is not None:
                self.wuwa_kujiequ_status_text.value = "请先填写库街区 token。"
                self.wuwa_kujiequ_status_text.color = self.PALETTE["danger"]
            self.page.update()
            return
        is_web = bool(self.wuwa_kujiequ_source_dropdown and self.wuwa_kujiequ_source_dropdown.value == "h5")
        self.controller.logger.info("[鸣潮库街区] 用户发起 Token 登录，来源：%s", "网页 H5" if is_web else "APP / Android")
        saved_profile = wuwa_kujiequ_profile_from_dict(self._get_wuwa_kujiequ_config().get("profile"))
        did = (self.wuwa_kujiequ_did_input.value or "").strip() if self.wuwa_kujiequ_did_input is not None else ""
        if not did:
            did = saved_profile.did if saved_profile is not None else None
        selected_role_id = str(self.wuwa_kujiequ_role_dropdown.value or "").strip() if self.wuwa_kujiequ_role_dropdown is not None else None
        if self.wuwa_kujiequ_status_text is not None:
            self.wuwa_kujiequ_status_text.value = "正在校验 token 并读取绑定角色..."
            self.wuwa_kujiequ_status_text.color = self.PALETTE["accent"]
        self._set_wuwa_kujiequ_busy(True)
        self.page.update()
        threading.Thread(
            target=self._run_wuwa_kujiequ_token_backend,
            args=(token, is_web, did, selected_role_id),
            daemon=True,
        ).start()

    def _start_wuwa_kujiequ_refresh(self, e=None, show_toast: bool = True):
        if self.wuwa_kujiequ_busy:
            return
        profile = self._current_wuwa_kujiequ_profile()
        if profile is None:
            if self.wuwa_kujiequ_status_text is not None:
                self.wuwa_kujiequ_status_text.value = "请先登录库街区，或先使用现有 token。"
                self.wuwa_kujiequ_status_text.color = self.PALETTE["danger"]
            self.page.update()
            return
        auto_sign = bool(self.wuwa_kujiequ_auto_sign_checkbox.value) if self.wuwa_kujiequ_auto_sign_checkbox is not None else False
        self._update_wuwa_kujiequ_config(
            auto_sign=auto_sign,
            phone=(self.wuwa_kujiequ_phone_input.value or "").strip() if self.wuwa_kujiequ_phone_input is not None else "",
            source="h5" if profile.is_web else "android",
            profile=wuwa_kujiequ_profile_to_dict(profile),
        )
        self.controller.logger.info(
            "[鸣潮库街区] 用户发起数据刷新：角色 %s | UID %s | 自动签到 %s",
            profile.role_name or profile.role_id or "-",
            profile.user_id or "-",
            "开启" if auto_sign else "关闭",
        )
        if self.wuwa_kujiequ_status_text is not None:
            self.wuwa_kujiequ_status_text.value = "正在刷新库街区每日数据..."
            self.wuwa_kujiequ_status_text.color = self.PALETTE["accent"]
        self._set_wuwa_kujiequ_busy(True)
        self.page.update()
        threading.Thread(
            target=self._run_wuwa_kujiequ_refresh_backend,
            args=(wuwa_kujiequ_profile_to_dict(profile), auto_sign, show_toast, "库街区数据已刷新。"),
            daemon=True,
        ).start()

    def _start_wuwa_kujiequ_sign(self, e):
        if self.wuwa_kujiequ_busy:
            return
        profile = self._current_wuwa_kujiequ_profile()
        if profile is None:
            if self.wuwa_kujiequ_status_text is not None:
                self.wuwa_kujiequ_status_text.value = "请先登录库街区，或先使用现有 token。"
                self.wuwa_kujiequ_status_text.color = self.PALETTE["danger"]
            self.page.update()
            return
        self.controller.logger.info(
            "[鸣潮库街区] 用户发起立即签到：角色 %s | UID %s",
            profile.role_name or profile.role_id or "-",
            profile.user_id or "-",
        )
        if self.wuwa_kujiequ_status_text is not None:
            self.wuwa_kujiequ_status_text.value = "正在执行今日签到..."
            self.wuwa_kujiequ_status_text.color = self.PALETTE["accent"]
        self._set_wuwa_kujiequ_busy(True)
        self.page.update()
        threading.Thread(
            target=self._run_wuwa_kujiequ_sign_backend,
            args=(wuwa_kujiequ_profile_to_dict(profile),),
            daemon=True,
        ).start()

    def _run_wuwa_kujiequ_login_backend(self, phone: str, code: str, is_web: bool):
        try:
            self.controller.logger.info("[鸣潮库街区] 正在请求登录并读取绑定角色")
            profile, roles = login_wuwa_kujiequ_with_code(phone, code, is_web=is_web)
            self._emit(
                "wuwa_kujiequ_login_success",
                phone=phone,
                source="h5" if is_web else "android",
                profile=wuwa_kujiequ_profile_to_dict(profile),
                roles=roles,
            )
        except Exception as exc:
            self._emit("wuwa_kujiequ_failed", stage="库街区登录", message=str(exc))

    def _run_wuwa_kujiequ_token_backend(
        self,
        token: str,
        is_web: bool,
        did: Optional[str],
        selected_role_id: Optional[str],
    ):
        try:
            self.controller.logger.info("[鸣潮库街区] 正在校验 Token 并读取绑定角色")
            profile, roles = login_wuwa_kujiequ_with_token(
                token,
                is_web=is_web,
                did=did,
                selected_role_id=selected_role_id,
            )
            self._emit(
                "wuwa_kujiequ_login_success",
                phone=(self.wuwa_kujiequ_phone_input.value or "").strip() if self.wuwa_kujiequ_phone_input is not None else "",
                source="h5" if is_web else "android",
                profile=wuwa_kujiequ_profile_to_dict(profile),
                roles=roles,
            )
        except Exception as exc:
            self._emit("wuwa_kujiequ_failed", stage="Token 校验", message=str(exc))

    def _run_wuwa_kujiequ_refresh_backend(
        self,
        profile_data: Dict[str, Any],
        auto_sign: bool,
        show_toast: bool,
        message: str,
    ):
        try:
            profile = wuwa_kujiequ_profile_from_dict(profile_data)
            if profile is None:
                raise RuntimeError("当前没有可用的库街区登录状态。")
            self.controller.logger.info("[鸣潮库街区] 开始拉取仪表盘数据")
            dashboard = fetch_wuwa_kujiequ_dashboard(profile, auto_sign=auto_sign)
            self._emit(
                "wuwa_kujiequ_dashboard_success",
                dashboard=dashboard,
                message=message,
                show_toast=show_toast,
            )
        except Exception as exc:
            self._emit("wuwa_kujiequ_failed", stage="刷新库街区数据", message=str(exc))

    def _run_wuwa_kujiequ_sign_backend(self, profile_data: Dict[str, Any]):
        try:
            profile = wuwa_kujiequ_profile_from_dict(profile_data)
            if profile is None:
                raise RuntimeError("当前没有可用的库街区登录状态。")
            self.controller.logger.info("[鸣潮库街区] 开始执行立即签到并回刷仪表盘")
            sign_result = perform_wuwa_kujiequ_sign(profile)
            dashboard = fetch_wuwa_kujiequ_dashboard(profile, auto_sign=False)
            dashboard["sign_result"] = sign_result
            self._emit(
                "wuwa_kujiequ_dashboard_success",
                dashboard=dashboard,
                message=sign_result.get("message") or "签到完成。",
                show_toast=True,
            )
        except Exception as exc:
            self._emit("wuwa_kujiequ_failed", stage="执行签到", message=str(exc))

    def _format_wuwa_kujiequ_time(self, timestamp: Any) -> str:
        try:
            ts = int(timestamp or 0)
        except (TypeError, ValueError):
            return ""
        if ts <= 0:
            return ""
        if ts > 10**12:
            ts = ts // 1000
        return time.strftime("%m-%d %H:%M", time.localtime(ts))

    def _format_wuwa_kujiequ_detail_value(self, detail: Optional[Dict[str, Any]], fallback: str = "-") -> str:
        if not detail:
            return fallback
        value = str(detail.get("value") or "").strip()
        if value:
            return value
        cur = detail.get("cur")
        total = detail.get("total")
        if cur is not None and total is not None:
            return f"{cur}/{total}"
        return fallback

    def _summarize_wuwa_kujiequ_boxes(self, items: List[Dict[str, Any]]) -> str:
        if not items:
            return ""
        parts = []
        for item in items[:4]:
            name = str(item.get("name") or "").strip()
            num = item.get("num", 0)
            if name:
                parts.append(f"{name} {num}")
        if not parts:
            return ""
        return " / ".join(parts)

    def _build_wuwa_kujiequ_info_line(self, title: str, value: str, note: str = "", tone: Optional[str] = None) -> ft.Container:
        body_controls: List[ft.Control] = [
            ft.Text(value, size=14, weight=ft.FontWeight.BOLD, color=tone or self.PALETTE["text"])
        ]
        if note:
            body_controls.append(ft.Text(note, size=12, color=self.PALETTE["muted"]))
        return ft.Container(
            bgcolor=self.PALETTE["panel_alt"],
            border_radius=12,
            padding=ft.Padding(12, 10, 12, 10),
            content=ft.Row(
                controls=[
                    ft.Text(title, size=13, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"], width=140),
                    ft.Column(body_controls, spacing=2, expand=True),
                ],
                spacing=10,
                vertical_alignment=ft.CrossAxisAlignment.START,
            ),
        )

    def _render_wuwa_kujiequ_dashboard(self, dashboard: Optional[Dict[str, Any]]):
        if self.wuwa_kujiequ_metrics_row is None:
            return
        if self.wuwa_kujiequ_daily_list is None or self.wuwa_kujiequ_base_list is None or self.wuwa_kujiequ_sign_list is None:
            return

        current_profile = self._current_wuwa_kujiequ_profile()
        if not dashboard:
            auto_sign_enabled = bool(self.wuwa_kujiequ_auto_sign_checkbox and self.wuwa_kujiequ_auto_sign_checkbox.value)
            if self.wuwa_kujiequ_account_text is not None:
                if current_profile is None:
                    self.wuwa_kujiequ_account_text.value = "未登录库街区。"
                    self.wuwa_kujiequ_account_text.color = self.PALETTE["muted"]
                else:
                    source_label = "网页 H5" if current_profile.is_web else "APP / Android"
                    self.wuwa_kujiequ_account_text.value = (
                        f"已保存角色：{current_profile.role_name or current_profile.role_id or '未选择角色'} | "
                        f"UID {current_profile.user_id or '-'} | RoleID {current_profile.role_id or '-'} | 来源 {source_label}"
                    )
                    self.wuwa_kujiequ_account_text.color = self.PALETTE["text"]
            self.wuwa_kujiequ_metrics_row.controls = [
                self._build_gacha_metric_card("当前状态", "未刷新", "登录后即可读取体力、活跃度、周本与签到。"),
                self._build_gacha_metric_card("自动签到", "已开启" if auto_sign_enabled else "未开启", "刷新库街区数据时自动尝试签到。"),
            ]
            self._set_column_placeholder(self.wuwa_kujiequ_daily_list, "登录后会在这里显示体力、活跃度、深塔和海墟等每日数据。")
            self._set_column_placeholder(self.wuwa_kujiequ_base_list, "登录后会在这里显示周本次数、宝箱、成就、活跃天数等基础数据。")
            self._set_column_placeholder(self.wuwa_kujiequ_sign_list, "登录后会在这里显示当月签到奖励和当前签到状态。")
            return

        profile_data = dashboard.get("profile") or {}
        daily = dashboard.get("daily") or {}
        base = dashboard.get("base") or {}
        sign = dashboard.get("sign") or {}
        errors = [str(item) for item in dashboard.get("errors") or [] if item]

        role_name = str(daily.get("role_name") or base.get("name") or profile_data.get("role_name") or "-")
        source_label = "网页 H5" if profile_data.get("is_web") else "APP / Android"
        if self.wuwa_kujiequ_account_text is not None:
            self.wuwa_kujiequ_account_text.value = (
                f"当前角色：{role_name} | UID {profile_data.get('user_id', '-') or '-'} | "
                f"RoleID {profile_data.get('role_id', '-') or '-'} | 来源 {source_label}"
            )
            self.wuwa_kujiequ_account_text.color = self.PALETTE["text"]

        energy = daily.get("energy") or {}
        liveness = daily.get("liveness") or {}
        weekly = daily.get("weekly") or {}
        store_energy = daily.get("store_energy") or {}
        tower = daily.get("tower") or {}
        slash_tower = daily.get("slash_tower") or {}
        weekly_count = base.get("weekly_inst_count")
        weekly_limit = base.get("weekly_inst_limit")
        weekly_value = self._format_wuwa_kujiequ_detail_value(weekly, fallback=f"{weekly_count or 0}/{weekly_limit or 0}")
        sign_state = "已签到" if sign.get("is_signed") else "未签到"

        self.wuwa_kujiequ_metrics_row.controls = [
            self._build_gacha_metric_card(
                "当前体力",
                self._format_wuwa_kujiequ_detail_value(
                    energy,
                    fallback=f"{base.get('energy', 0)}/{base.get('max_energy', 0)}" if base else "-",
                ),
                str(energy.get("name") or "每日体力"),
            ),
            self._build_gacha_metric_card(
                "活跃度",
                self._format_wuwa_kujiequ_detail_value(
                    liveness,
                    fallback=f"{base.get('liveness', 0)}/{base.get('liveness_max', 0)}" if base else "-",
                ),
                str(liveness.get("name") or "今日活跃"),
            ),
            self._build_gacha_metric_card(
                "周本",
                weekly_value,
                f"库街区周本次数 {weekly_count or 0}/{weekly_limit or 0}",
            ),
            self._build_gacha_metric_card(
                "结晶单质",
                self._format_wuwa_kujiequ_detail_value(
                    store_energy,
                    fallback=f"{base.get('store_energy', 0)}/{base.get('store_energy_limit', 0)}" if base else "-",
                ),
                str(store_energy.get("name") or "库存资源"),
            ),
            self._build_gacha_metric_card(
                "角色等级",
                f"{base.get('level', 0)} 级 / 世界 {base.get('world_level', 0)}" if base else "-",
                f"活跃 {base.get('active_days', 0)} 天" if base else "基础数据暂不可用",
            ),
            self._build_gacha_metric_card(
                "今日签到",
                sign_state,
                f"本月已签 {sign.get('signed_count', 0)} 天",
            ),
        ]

        daily_controls: List[ft.Control] = []
        daily_entries = [
            ("当前体力", energy),
            ("今日活跃", liveness),
            ("周常进度", weekly),
            ("结晶单质", store_energy),
            ("逆境深塔", tower),
            ("冥歌海墟", slash_tower),
        ]
        for label, detail in daily_entries:
            if not detail:
                continue
            refresh_text = self._format_wuwa_kujiequ_time(detail.get("refresh_time") or detail.get("expire_time"))
            note = str(detail.get("name") or detail.get("key") or "").strip()
            if refresh_text:
                note = f"{note} | 刷新 {refresh_text}" if note else f"刷新 {refresh_text}"
            daily_controls.append(
                self._build_wuwa_kujiequ_info_line(
                    label,
                    self._format_wuwa_kujiequ_detail_value(detail),
                    note,
                )
            )
        for item in daily.get("battle_pass") or []:
            note = ""
            refresh_text = self._format_wuwa_kujiequ_time(item.get("refresh_time") or item.get("expire_time"))
            if refresh_text:
                note = f"刷新 {refresh_text}"
            daily_controls.append(
                self._build_wuwa_kujiequ_info_line(
                    f"活跃奖励 {item.get('name') or item.get('key') or ''}",
                    self._format_wuwa_kujiequ_detail_value(item),
                    note,
                )
            )
        if daily_controls:
            self.wuwa_kujiequ_daily_list.controls = daily_controls
        else:
            self._set_column_placeholder(self.wuwa_kujiequ_daily_list, "当前没有可展示的每日数据。")

        base_controls: List[ft.Control] = []
        if base:
            base_controls.extend(
                [
                    self._build_wuwa_kujiequ_info_line(
                        "账号进度",
                        f"{base.get('level', 0)} 级 / 世界 {base.get('world_level', 0)}",
                        f"活跃 {base.get('active_days', 0)} 天 | 声匣 {base.get('sound_box', 0)}",
                    ),
                    self._build_wuwa_kujiequ_info_line(
                        "周本次数",
                        f"{base.get('weekly_inst_count', 0)}/{base.get('weekly_inst_limit', 0)}",
                        "roleBox 基础数据",
                    ),
                    self._build_wuwa_kujiequ_info_line(
                        "结晶单质库存",
                        f"{base.get('store_energy', 0)}/{base.get('store_energy_limit', 0)}",
                        base.get('rouge_title') or "库存资源",
                    ),
                    self._build_wuwa_kujiequ_info_line(
                        "成就与收集",
                        f"成就 {base.get('achievement_count', 0)} / 星数 {base.get('achievement_star', 0)}",
                        f"角色 {base.get('role_num', 0)} | 大宝箱 {base.get('big_count', 0)} | 小宝箱 {base.get('small_count', 0)}",
                    ),
                ]
            )
            box_note = self._summarize_wuwa_kujiequ_boxes(base.get("box_list") or [])
            if box_note:
                base_controls.append(self._build_wuwa_kujiequ_info_line("探索宝箱", box_note, "区域分布摘要"))
            treasure_note = self._summarize_wuwa_kujiequ_boxes(base.get("treasure_box_list") or [])
            if treasure_note:
                base_controls.append(self._build_wuwa_kujiequ_info_line("收集箱", treasure_note, "珍藏宝箱摘要"))
            phantom_note = self._summarize_wuwa_kujiequ_boxes(base.get("phantom_box_list") or [])
            if phantom_note:
                base_controls.append(self._build_wuwa_kujiequ_info_line("声骸箱", phantom_note, "声骸相关箱体摘要"))
        if base_controls:
            self.wuwa_kujiequ_base_list.controls = base_controls
        else:
            self._set_column_placeholder(self.wuwa_kujiequ_base_list, "当前没有可展示的基础数据。")

        sign_controls: List[ft.Control] = []
        if sign:
            sign_controls.append(
                self._build_wuwa_kujiequ_info_line(
                    "签到状态",
                    sign_state,
                    f"本月已签 {sign.get('signed_count', 0)} 天",
                    tone=self.PALETTE["accent"] if sign.get("is_signed") else self.PALETTE["warning"],
                )
            )
            for good in (sign.get("goods") or [])[:10]:
                note_parts = [f"第 {good.get('serial_num', 0)} 天"]
                if good.get("is_signed"):
                    note_parts.append("已计入签到进度")
                elif good.get("is_gain"):
                    note_parts.append("已领取")
                sign_controls.append(
                    self._build_wuwa_kujiequ_info_line(
                        good.get("goods_name") or "奖励",
                        f"x{good.get('goods_num', 0)}",
                        " | ".join(note_parts),
                    )
                )
        sign_result = dashboard.get("sign_result") or {}
        if sign_result:
            sign_controls.insert(
                0,
                self._build_wuwa_kujiequ_info_line(
                    "最近签到结果",
                    sign_result.get("message") or "签到完成",
                    "已自动刷新签到状态",
                    tone=self.PALETTE["accent"],
                ),
            )
        if errors:
            sign_controls.append(
                self._build_wuwa_kujiequ_info_line(
                    "部分接口异常",
                    "；".join(errors),
                    "已展示当前能成功读取的数据。",
                    tone=self.PALETTE["warning"],
                )
            )
        if sign_controls:
            self.wuwa_kujiequ_sign_list.controls = sign_controls
        else:
            self._set_column_placeholder(self.wuwa_kujiequ_sign_list, "当前没有可展示的签到数据。")

    def _set_status(self, title: str, body: str, tone: str = "info"):
        tone_map = {
            "info": ("待命中", self.PALETTE["accent_soft"], self.PALETTE["accent"]),
            "running": ("进行中", self.PALETTE["info_soft"], self.PALETTE["info"]),
            "success": ("已完成", self.PALETTE["accent_soft"], self.PALETTE["accent"]),
            "warning": ("请留意", self.PALETTE["warning_soft"], self.PALETTE["warning"]),
            "danger": ("已中断", self.PALETTE["danger_soft"], self.PALETTE["danger"]),
        }
        badge_text, badge_bg, badge_fg = tone_map.get(tone, tone_map["info"])
        self.status_chip.bgcolor = badge_bg
        self.status_chip_text.value = badge_text
        self.status_chip_text.color = badge_fg
        self.status_title.value = title
        self.status_body.value = body
        self.progress_ring.visible = tone == "running"

    def _update_banner(self):
        if not self.dep_report:
            self.banner_box.visible = False
            return

        messages = list(getattr(self.dep_report, "errors", [])) + list(getattr(self.dep_report, "warnings", []))
        if not messages:
            self.banner_box.visible = False
            return

        preview = "；".join(messages[:2])
        if len(messages) > 2:
            preview += f"；另有 {len(messages) - 2} 条提醒"
        self.banner_text.value = f"启动检查提醒：{preview}"
        self.banner_box.visible = True

    def _append_log(self, message: str):
        self.log_lines.append(message)
        if len(self.log_lines) > 400:
            self.log_lines = self.log_lines[-400:]
        self.log_box.value = "\n".join(self.log_lines)

    # ────────────────────────────
    # 森空岛终末地签到事件处理
    # ────────────────────────────
    def _get_skendfield_config(self) -> Dict[str, Any]:
        raw = cm.load().get(SKENDFIELD_SIGN_CONFIG_KEY, {})
        return raw if isinstance(raw, dict) else {}

    def _update_skendfield_config(self, **updates: Any) -> Dict[str, Any]:
        config = cm.load()
        section = config.get(SKENDFIELD_SIGN_CONFIG_KEY)
        data = dict(section) if isinstance(section, dict) else {}
        data.update(updates)
        config[SKENDFIELD_SIGN_CONFIG_KEY] = data
        cm.save(config)
        return data

    def _apply_skendfield_config_to_controls(self):
        cfg = self._get_skendfield_config()
        if self.skendfield_token_input is not None:
            self.skendfield_token_input.value = str(cfg.get("token") or "")
        if self.skendfield_server_dropdown is not None:
            self.skendfield_server_dropdown.value = str(cfg.get("server") or "cn")
        if self.skendfield_startup_checkbox is not None:
            self.skendfield_startup_checkbox.value = bool(cfg.get("startup_sign", False))

    def _set_skendfield_busy(self, busy: bool):
        self.skendfield_busy = busy
        for ctrl in (self.skendfield_token_input, self.skendfield_server_dropdown,
                     self.skendfield_sign_button, self.skendfield_startup_checkbox):
            if ctrl is not None:
                ctrl.disabled = busy

    def _start_skendfield_sign(self, e=None):
        self.controller.logger.info("[森空岛] 点击签到按钮 (busy=%s)", self.skendfield_busy)
        if self.skendfield_busy:
            self.controller.logger.warning("[森空岛] 签到被忽略：正在忙碌中")
            return
        token = (self.skendfield_token_input.value or "").strip() if self.skendfield_token_input is not None else ""
        server = str(self.skendfield_server_dropdown.value or "cn").strip() if self.skendfield_server_dropdown is not None else "cn"
        if not token:
            self.controller.logger.warning("[森空岛] 签到被忽略：未填写 Token")
            self._toast("请先填写 Token", self.PALETTE["warning"])
            return
        self._update_skendfield_config(token=token, server=server)
        startup = bool(self.skendfield_startup_checkbox.value) if self.skendfield_startup_checkbox is not None else False
        self._update_skendfield_config(token=token, server=server, startup_sign=startup)
        self._set_skendfield_busy(True)
        if self.skendfield_status_text is not None:
            self.skendfield_status_text.value = "正在执行森空岛签到..."
            self.skendfield_status_text.color = self.PALETTE["accent"]
        self.skendfield_status_text.update() if self.skendfield_status_text else None
        self.controller.logger.info("[森空岛] 用户发起签到 (%s)，token %s...", server, token[:8])
        threading.Thread(target=self._run_skendfield_sign_backend, args=(token, server), daemon=True).start()

    def _run_skendfield_sign_backend(self, token: str, server: str):
        try:
            result = perform_skendfield_sign(token=token, server=server)
            self._emit("skendfield_sign_finished", result=result)
        except Exception as exc:
            self.controller.logger.error("[森空岛] 签到失败: %s", str(exc), exc_info=True)
            self._emit("skendfield_sign_finished", result={"status": "failed", "message": str(exc)})

    def _build_endfield_center_view(self) -> ft.Container:
        cfg = self._get_endfield_protocol_config()

        self.endfield_center_api_key_input = ft.TextField(
            label="API Key",
            value=str(cfg.get("api_key") or ""),
            hint_text="从 https://end.shallow.ink/ 获取",
            password=True,
            can_reveal_password=True,
            border_color=self.PALETTE["line"],
            filled=True, fill_color=self.PALETTE["panel_alt"], dense=True,
        )
        self.endfield_center_framework_token_input = ft.TextField(
            label="Framework Token",
            value=str(cfg.get("framework_token") or ""),
            hint_text="登录绑定后自动获取并保存",
            password=True,
            can_reveal_password=True,
            border_color=self.PALETTE["line"],
            filled=True, fill_color=self.PALETTE["panel_alt"], dense=True,
        )
        self.endfield_center_role_id_input = ft.TextField(
            label="角色 UID (roleId)",
            value=str(cfg.get("role_id") or ""),
            hint_text="绑定终末地角色后自动获取",
            width=200,
            border_color=self.PALETTE["line"],
            filled=True, fill_color=self.PALETTE["panel_alt"], dense=True,
        )
        self.endfield_center_server_id_input = ft.TextField(
            label="服务器 ID (serverId)",
            value=str(cfg.get("server_id") or "1"),
            hint_text="官服=1, B服=2, 国际服=10",
            width=150,
            border_color=self.PALETTE["line"],
            filled=True, fill_color=self.PALETTE["panel_alt"], dense=True,
        )
        self.endfield_center_startup_checkbox = ft.Checkbox(
            label="启动前自动签到（协议终端）",
            value=bool(cfg.get("startup_sign", False)),
        )
        self.endfield_center_status_text = ft.Text(
            "配置 API Key 可使用协议终端查询便笺/理智/干员/建设/抽卡/成就。下方直接签到无需 API Key。",
            size=13, color=self.PALETTE["muted"],
        )

        self.endfield_center_result_area = ft.Column(spacing=8, scroll=ft.ScrollMode.AUTO, height=500)

        query_buttons = ft.Row(
            controls=[
                ft.ElevatedButton("查询便笺", icon=FT_ICONS.SEARCH, on_click=lambda e: self._endfield_query("note")),
                ft.ElevatedButton("查询理智", icon=FT_ICONS.SEARCH, on_click=lambda e: self._endfield_query("stamina")),
                ft.ElevatedButton("查询干员", icon=FT_ICONS.SEARCH, on_click=lambda e: self._endfield_query("search_chars")),
                ft.ElevatedButton("帝江号建设", icon=FT_ICONS.SEARCH, on_click=lambda e: self._endfield_query("spaceship")),
                ft.ElevatedButton("地区建设", icon=FT_ICONS.SEARCH, on_click=lambda e: self._endfield_query("domain")),
                ft.ElevatedButton("抽卡统计", icon=FT_ICONS.SEARCH, on_click=lambda e: self._endfield_query("gacha_stats")),
                ft.ElevatedButton("抽卡记录", icon=FT_ICONS.SEARCH, on_click=lambda e: self._endfield_query("gacha_records")),
                ft.ElevatedButton("成就", icon=FT_ICONS.SEARCH, on_click=lambda e: self._endfield_query("achieve")),
                ft.OutlinedButton("清空结果", on_click=lambda e: self._clear_endfield_result()),
            ],
            spacing=6, wrap=True,
        )

        skg_cfg = self._get_skendfield_config()
        self.skendfield_token_input = ft.TextField(
            label="森空岛 Token（直接签到）",
            value=str(skg_cfg.get("token") or ""),
            hint_text="粘贴森空岛网页获取的 token（支持 {'data':{'content':'...'}} 格式）",
            password=True,
            can_reveal_password=True,
            border_color=self.PALETTE["line"],
            filled=True, fill_color=self.PALETTE["panel_alt"], dense=True,
        )
        self.skendfield_server_dropdown = ft.Dropdown(
            label="服务器",
            value=str(skg_cfg.get("server") or "cn"),
            options=[
                ft.dropdown.Option("cn", "国服"),
                ft.dropdown.Option("global", "国际服"),
            ],
            border_color=self.PALETTE["line"],
            filled=True, fill_color=self.PALETTE["panel_alt"],
        )
        self.skendfield_startup_checkbox = ft.Checkbox(
            label="启动前自动签到",
            value=bool(skg_cfg.get("startup_sign", False)),
        )
        self.skendfield_sign_button = ft.ElevatedButton("立即签到", icon=FT_ICONS.CHECK_CIRCLE_OUTLINE,
                                                        on_click=self._start_skendfield_sign)
        self.skendfield_clear_button = ft.TextButton("清空配置", on_click=self._clear_skendfield_config)
        self.skendfield_status_text = ft.Text(
            "填写森空岛 Token 后点击签到。Token 长期有效，无需 API Key。",
            size=13, color=self.PALETTE["muted"],
        )
        self.skendfield_metrics_row = ft.Row(wrap=True, spacing=10)
        self.skendfield_result_list = ft.Column(spacing=8)

        skg_sign_panel = self._panel(
            ft.Column(
                controls=[
                    ft.Row(
                        controls=[
                            ft.Container(width=48, height=48, border_radius=14,
                                bgcolor=self.PALETTE["accent_soft"],
                                content=ft.Icon(FT_ICONS.SHIELD, color=self.PALETTE["accent"], size=26),
                                alignment=ft.Alignment(0, 0)),
                            ft.Column(controls=[
                                ft.Text("森空岛直接签到", size=18, weight=ft.FontWeight.W_500, color=self.PALETTE["text"]),
                                ft.Text("无需 API Key，可直接使用森空岛账号 Token 签到。", size=13, color=self.PALETTE["muted"]),
                            ], spacing=4, expand=True),
                        ], spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    self.skendfield_token_input,
                    self.skendfield_server_dropdown,
                    ft.Row(
                        controls=[self.skendfield_sign_button, self.skendfield_clear_button, self.skendfield_startup_checkbox],
                        spacing=10, wrap=True,
                    ),
                    self.skendfield_status_text,
                    self.skendfield_metrics_row,
                    self.skendfield_result_list,
                ], spacing=10,
            )
        )

        login_panel = self._panel(
            ft.Column(
                controls=[
                    ft.Row(
                        controls=[
                            ft.Container(width=48, height=48, border_radius=14,
                                bgcolor=self.PALETTE["accent_soft"],
                                content=ft.Icon(FT_ICONS.HUB, color=self.PALETTE["accent"], size=26),
                                alignment=ft.Alignment(0, 0)),
                            ft.Column(controls=[
                                ft.Text("终末地数据中心", size=20, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"]),
                                ft.Text("基于协议终端 (end.shallow.ink)，需先获取 API Key。", size=13, color=self.PALETTE["muted"]),
                            ], spacing=4, expand=True),
                        ], spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    self.endfield_center_api_key_input,
                    self.endfield_center_framework_token_input,
                    ft.Row([self.endfield_center_role_id_input, self.endfield_center_server_id_input], spacing=10, wrap=True),
                    ft.Row([self.endfield_center_startup_checkbox]),
                    self.endfield_center_status_text,
                    ft.Divider(color=self.PALETTE["line"]),
                    query_buttons,
                ], spacing=10,
            )
        )

        self.endfield_center_gacha_overview = ft.Row(wrap=True, spacing=10)
        self.endfield_center_gacha_list = ft.Column(spacing=6, scroll=ft.ScrollMode.AUTO, height=300)
        gacha_panel = self._panel(
            ft.Column(
                controls=[
                    ft.Text("本地抽卡数据", size=20, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"]),
                    ft.Text("已导入的终末地 UIGF 抽卡记录总览。", size=13, color=self.PALETTE["muted"]),
                    self.endfield_center_gacha_overview,
                    ft.Text("最近六星记录", size=14, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"]),
                    self.endfield_center_gacha_list,
                ], spacing=10,
            )
        )

        result_panel = self._panel(
            ft.Column(
                controls=[
                    ft.Text("查询结果", size=20, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"]),
                    self.endfield_center_result_area,
                ], spacing=10,
            )
        )

        view = ft.Container(
            visible=False,
            padding=ft.Padding(18, 16, 18, 16),
            content=ft.Column(
                controls=[login_panel, skg_sign_panel, gacha_panel, result_panel],
                spacing=12, scroll=ft.ScrollMode.AUTO,
            ),
        )
        self._apply_endfield_protocol_config_to_controls()
        self._apply_skendfield_config_to_controls()
        self._render_skendfield_sign_result(None)
        self._render_endfield_center_gacha()
        return view

    def _get_endfield_protocol_config(self) -> Dict[str, Any]:
        raw = cm.load().get(ENDFIELD_PROTOCOL_CONFIG_KEY, {})
        return raw if isinstance(raw, dict) else {}

    def _update_endfield_protocol_config(self, **updates: Any) -> Dict[str, Any]:
        config = cm.load()
        section = config.get(ENDFIELD_PROTOCOL_CONFIG_KEY)
        data = dict(section) if isinstance(section, dict) else {}
        data.update(updates)
        config[ENDFIELD_PROTOCOL_CONFIG_KEY] = data
        cm.save(config)
        return data

    def _apply_endfield_protocol_config_to_controls(self):
        cfg = self._get_endfield_protocol_config()
        if self.endfield_center_api_key_input is not None:
            self.endfield_center_api_key_input.value = str(cfg.get("api_key") or "")
        if self.endfield_center_framework_token_input is not None:
            self.endfield_center_framework_token_input.value = str(cfg.get("framework_token") or "")
        if self.endfield_center_role_id_input is not None:
            self.endfield_center_role_id_input.value = str(cfg.get("role_id") or "")
        if self.endfield_center_server_id_input is not None:
            self.endfield_center_server_id_input.value = str(cfg.get("server_id") or "1")
        if self.endfield_center_startup_checkbox is not None:
            self.endfield_center_startup_checkbox.value = bool(cfg.get("startup_sign", False))

    def _set_endfield_center_busy(self, busy: bool):
        self.endfield_center_busy = busy

    def _endfield_query(self, query_type: str):
        self.controller.logger.info("[终末地数据中心] 点击查询按钮: %s (busy=%s)", query_type, self.endfield_center_busy)
        if self.endfield_center_busy:
            self.controller.logger.warning("[终末地数据中心] 查询被忽略：正在忙碌中")
            return
        api_key = (self.endfield_center_api_key_input.value or "").strip() if self.endfield_center_api_key_input is not None else ""
        framework_token = (self.endfield_center_framework_token_input.value or "").strip() if self.endfield_center_framework_token_input is not None else ""
        role_id = (self.endfield_center_role_id_input.value or "").strip() if self.endfield_center_role_id_input is not None else ""
        server_id = (self.endfield_center_server_id_input.value or "").strip() if self.endfield_center_server_id_input is not None else ""
        self._update_endfield_protocol_config(
            api_key=api_key, framework_token=framework_token, role_id=role_id, server_id=server_id,
        )
        self._set_endfield_center_busy(True)
        if self.endfield_center_status_text is not None:
            self.endfield_center_status_text.value = f"正在查询 {query_type}..."
            self.endfield_center_status_text.color = self.PALETTE["accent"]
            self.endfield_center_status_text.update()
        self.controller.logger.info("[终末地数据中心] 查询 %s", query_type)
        threading.Thread(
            target=self._run_endfield_query_backend,
            args=(api_key, framework_token, role_id, server_id, query_type),
            daemon=True,
        ).start()

    def _run_endfield_query_backend(self, api_key, framework_token, role_id, server_id, query_type):
        try:
            result = perform_endfield_query(
                api_key=api_key, framework_token=framework_token,
                role_id=role_id, server_id=server_id, query_type=query_type,
            )
            self._emit("endfield_center_query_finished", query_type=query_type, result=result)
        except Exception as exc:
            self.controller.logger.error("[终末地数据中心] 查询失败: %s", str(exc), exc_info=True)
            self._emit("endfield_center_query_finished", query_type=query_type, result=None, error=str(exc))

    def _clear_endfield_result(self):
        if hasattr(self, 'endfield_center_result_area') and self.endfield_center_result_area is not None:
            self.endfield_center_result_area.controls = [self._build_pool_empty_state("点击上方按钮查询数据。")]
            self.endfield_center_result_area.update()

    def _clear_skendfield_config(self, e=None):
        if self.skendfield_busy:
            return
        self.controller.logger.info("[森空岛] 用户已清空配置")
        config = cm.load()
        if SKENDFIELD_SIGN_CONFIG_KEY in config:
            config.pop(SKENDFIELD_SIGN_CONFIG_KEY, None)
            cm.save(config)
        for ctrl in (self.skendfield_token_input,):
            if ctrl is not None:
                ctrl.value = ""
        if self.skendfield_server_dropdown is not None:
            self.skendfield_server_dropdown.value = "cn"
        if self.skendfield_startup_checkbox is not None:
            self.skendfield_startup_checkbox.value = False
        if self.skendfield_status_text is not None:
            self.skendfield_status_text.value = "配置已清空。"
            self.skendfield_status_text.color = self.PALETTE["muted"]
        self._render_skendfield_sign_result(None)

    def _render_endfield_query_result(self, query_type: str, result, error: str):
        if self.endfield_center_result_area is None:
            return
        self._set_endfield_center_busy(False)
        if error:
            self.endfield_center_result_area.controls = [ft.Container(
                bgcolor=self.PALETTE["danger_soft"], border_radius=14,
                padding=ft.Padding(14, 12, 14, 12),
                content=ft.Text(f"查询失败: {error}", size=14, color=self.PALETTE["danger"]),
            )]
            if self.endfield_center_status_text is not None:
                self.endfield_center_status_text.value = f"查询 {query_type} 失败: {error}"
                self.endfield_center_status_text.color = self.PALETTE["danger"]
            self.page.update()
            return
        if result is None:
            self.endfield_center_result_area.controls = [ft.Container(
                bgcolor=self.PALETTE["warning_soft"], border_radius=14,
                padding=ft.Padding(14, 12, 14, 12),
                content=ft.Text(f"查询 {query_type} 返回为空（可能缺少参数或 token 过期）", size=14, color=self.PALETTE["warning"]),
            )]
            if self.endfield_center_status_text is not None:
                self.endfield_center_status_text.value = f"查询 {query_type} 返回为空"
                self.endfield_center_status_text.color = self.PALETTE["warning"]
            self.page.update()
            return

        if self.endfield_center_status_text is not None:
            self.endfield_center_status_text.value = f"查询 {query_type} 成功。"
            self.endfield_center_status_text.color = self.PALETTE["accent"]

        data = result.get("data", result)
        summary_controls = self._format_endfield_query_result(query_type, data)
        self.endfield_center_result_area.controls = summary_controls

        # 理智只是资源状态；只有便笺里的每日活跃计数能写入每日进度。
        if query_type == "note" and self.daily_progress_tracker is not None:
            dailies = (
                self._ef_get_dict(data, "dailyMission", "daily_mission")
                or self._ef_get_dict(data, "dailies", "daily_quest", "daily_quests", "daily_task",
                                     "daily_tasks", "daily", "quests")
            )
            if dailies:
                def _safe_int(value, default=0):
                    try:
                        return int(value)
                    except (TypeError, ValueError):
                        return default

                completed = _safe_int(self._ef_get_val(dailies, "activation", "finished", "completed", "done",
                                                       "completed_count", "current", "cur") or 0)
                total = _safe_int(self._ef_get_val(dailies, "maxActivation", "max_activation",
                                                   "total", "all", "total_count", "max") or 0)
                cfg = self._get_endfield_protocol_config()
                role_id_str = str(cfg.get("role_id", ""))
                self.daily_progress_tracker.record_task(
                    game_key="endfield",
                    game_name="终末地",
                    task_key="日活跃",
                    task_name="日活跃",
                    status="success" if total and completed >= total else "pending",
                    source="community",
                    message=f"{completed}/{total}",
                    uid=role_id_str,
                    extra={"current": completed, "total": total},
                )
            else:
                self.controller.logger.debug("[终末地数据中心] note 返回不含每日活跃计数，不写入每日进度")

        self.page.update()

    @staticmethod
    def _ef_get_val(data, *keys):
        for k in keys:
            v = data.get(k)
            if v is not None and v != "":
                return v
        return None

    @staticmethod
    def _ef_get_dict(data, *keys):
        for k in keys:
            v = data.get(k)
            if isinstance(v, dict):
                return v
        return None

    @staticmethod
    def _ef_get_list(data, *keys):
        for k in keys:
            v = data.get(k)
            if isinstance(v, list):
                return v
        return None

    def _format_endfield_query_result(self, query_type: str, data) -> list:
        controls = []
        if not isinstance(data, dict):
            return controls
        gv = self._ef_get_val
        gd = self._ef_get_dict
        gl = self._ef_get_list

        if query_type == "note":
            controls.append(ft.Text("便笺总览", size=16, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"]))
            rec = gd(data, "recovery", "stamina", "energy", "stamina_info")
            if rec:
                cur = gv(rec, "current", "cur", "value", "stamina")
                mx = gv(rec, "max", "max_value", "limit", "max_stamina", "maxStamina")
                rt = gv(rec, "recover_time", "recoverTime", "restore_time")
                controls.append(ft.Text(
                    f"理智: {cur or '?'}/{mx or '?'}  (恢复: {rt or '?'})",
                    size=14, color=self.PALETTE["accent"]))
            dailies = gd(data, "dailies", "daily_quest", "daily_quests", "daily_task", "daily_tasks", "daily", "quests")
            daily_mission = gd(data, "dailyMission", "daily_mission")
            if daily_mission:
                c = gv(daily_mission, "activation", "current", "cur")
                t = gv(daily_mission, "maxActivation", "max_activation", "total", "max")
                controls.append(ft.Text(
                    f"日活跃: {c if c is not None else '?'}/{t if t is not None else '?'}",
                    size=14, color=self.PALETTE["text"]))
            if dailies:
                c = gv(dailies, "finished", "completed", "done", "completed_count", "current", "cur")
                t = gv(dailies, "total", "all", "total_count", "max")
                controls.append(ft.Text(
                    f"每日任务: {c or '?'}/{t or '?'}",
                    size=14, color=self.PALETTE["text"]))
            weekly = gd(data, "weekly", "weekly_quest", "weekly_quests", "weekly_task")
            if weekly:
                wc = gv(weekly, "finished", "completed", "done")
                wt = gv(weekly, "total", "all")
                if wc is not None and wt is not None:
                    controls.append(ft.Text(f"周常任务: {wc}/{wt}", size=14, color=self.PALETTE["text"]))
                else:
                    is_done = gv(weekly, "completed", "is_completed")
                    controls.append(ft.Text(
                        f"周常任务: {'已完成' if is_done else '未完成'}",
                        size=14, color=self.PALETTE["text"]))

        elif query_type == "stamina":
            controls.append(ft.Text("理智状态", size=16, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"]))
            rec = gd(data, "recovery", "stamina", "energy", "stamina_info")
            src = rec if rec else data
            cur = gv(src, "current", "cur", "value", "stamina") or "?"
            mx = gv(src, "max", "max_value", "limit", "max_stamina", "maxStamina") or "?"
            rt = gv(src, "recover_time", "recoverTime", "restore_time") or ""
            controls.append(ft.Text(f"当前理智: {cur}/{mx}", size=18, weight=ft.FontWeight.BOLD, color=self.PALETTE["accent"]))
            if rt:
                controls.append(ft.Text(f"完全恢复时间: {rt}", size=13, color=self.PALETTE["muted"]))

        elif query_type == "search_chars":
            controls.append(ft.Text("干员列表", size=16, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"]))
            chars = gl(data, "chars", "characters", "list", "operators")
            if isinstance(chars, list):
                controls.append(ft.Text(f"共 {len(chars)} 位干员", size=13, color=self.PALETTE["muted"]))
                for ch in chars[:30]:
                    name = gv(ch, "name", "char_name", "operator_name") or "?"
                    rarity = gv(ch, "rarity", "star", "star_level") or "?"
                    controls.append(ft.Container(
                        bgcolor=self.PALETTE["panel_alt"], border_radius=8,
                        padding=ft.Padding(8, 6, 8, 6),
                        content=ft.Row([
                            ft.Text(str(name), size=13, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"], width=160),
                            ft.Text(f"★{rarity}", size=12, color="#d4a017"),
                        ], spacing=8),
                    ))

        elif query_type == "spaceship":
            controls.append(ft.Text("帝江号建设", size=16, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"]))
            rooms = gl(data, "rooms", "buildings", "areas", "room_list", "construction", "data")
            if not rooms:
                ss = gd(data, "data", "spaceship", "info", "detail")
                if ss:
                    rooms = gl(ss, "rooms", "buildings", "areas", "room_list", "construction")
            if isinstance(rooms, list) and rooms:
                for room in rooms[:20]:
                    name = gv(room, "name", "room_name", "building_name") or "?"
                    level = gv(room, "level", "build_level", "lv") or "?"
                    controls.append(ft.Container(
                        bgcolor=self.PALETTE["panel_alt"], border_radius=8,
                        padding=ft.Padding(8, 6, 8, 6),
                        content=ft.Row([
                            ft.Text(str(name), size=13, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"], expand=True),
                            ft.Text(f"Lv.{level}", size=13, color=self.PALETTE["accent"]),
                        ], spacing=8),
                    ))
            else:
                controls.append(ft.Text("暂无建设数据", size=13, color=self.PALETTE["muted"]))

        elif query_type == "domain":
            controls.append(ft.Text("地区建设", size=16, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"]))
            domains = gl(data, "domains", "areas", "buildings", "regions", "zones", "construction", "data")
            if not domains:
                dm_root = gd(data, "data", "domain", "detail")
                if dm_root:
                    domains = gl(dm_root, "domains", "areas", "buildings", "regions", "zones", "construction")
            if isinstance(domains, list) and domains:
                for dm in domains[:20]:
                    name = gv(dm, "name", "domain_name", "area_name") or "?"
                    info = gv(dm, "info", "desc", "description", "progress") or ""
                    controls.append(ft.Container(
                        bgcolor=self.PALETTE["panel_alt"], border_radius=8,
                        padding=ft.Padding(8, 6, 8, 6),
                        content=ft.Column([
                            ft.Text(str(name), size=13, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"]),
                            ft.Text(str(info)[:80], size=12, color=self.PALETTE["muted"]),
                        ], spacing=2),
                    ))
            else:
                controls.append(ft.Text("暂无地区建设数据", size=13, color=self.PALETTE["muted"]))

        elif query_type == "gacha_stats":
            controls.append(ft.Text("抽卡统计", size=16, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"]))
            total = gv(data, "total_pulls", "total", "total_count", "totalGacha")
            six = gv(data, "six_star", "count_6", "sixStar", "legendary")
            five = gv(data, "five_star", "count_5", "fiveStar", "epic")
            controls.append(ft.Text(f"总抽数: {total}   六星: {six}   五星: {five}", size=14, color=self.PALETTE["text"]))

        elif query_type == "gacha_records":
            controls.append(ft.Text("抽卡记录", size=16, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"]))
            records = gl(data, "records", "data", "list", "items")
            if not records and isinstance(data, list):
                records = data
            if isinstance(records, list):
                controls.append(ft.Text(f"共 {len(records)} 条", size=13, color=self.PALETTE["muted"]))
                for rec in records[:25]:
                    name = gv(rec, "name", "item_name", "char_name") or "?"
                    rarity = gv(rec, "rarity", "star") or "?"
                    pool = gv(rec, "pool_name", "pool", "gacha_name") or ""
                    ts = gv(rec, "time", "gacha_time", "timestamp", "ts") or ""
                    controls.append(ft.Container(
                        bgcolor=self.PALETTE["panel_alt"], border_radius=8,
                        padding=ft.Padding(8, 6, 8, 6),
                        content=ft.Row([
                            ft.Text(str(name), size=13, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"], width=140),
                            ft.Text(f"★{rarity}", size=12, color="#d4a017", width=40),
                            ft.Text(str(pool)[:20], size=11, color=self.PALETTE["muted"], width=100),
                            ft.Text(str(ts)[:19], size=11, color=self.PALETTE["muted"]),
                        ], spacing=6),
                    ))

        elif query_type == "achieve":
            controls.append(ft.Text("成就总览", size=16, weight=ft.FontWeight.BOLD, color=self.PALETTE["text"]))
            total = gv(data, "total", "total_achieve", "total_count", "all")
            completed = gv(data, "completed", "finished", "done", "completed_count")
            controls.append(ft.Text(f"成就进度: {completed or '?'}/{total or '?'}", size=18, weight=ft.FontWeight.BOLD, color=self.PALETTE["accent"]))

        return controls

    def _render_endfield_center_gacha(self):
        if self.endfield_center_gacha_overview is None or self.endfield_center_gacha_list is None:
            return
        try:
            db_rows = self.preference_db.get_gacha_records("endfield", "")
            if not db_rows:
                self.endfield_center_gacha_overview.controls = [
                    self._build_gacha_metric_card("本地抽卡", "暂无", "请先在「抽卡分析」页面导入终末地 UIGF 文件。"),
                ]
                self._set_column_placeholder(self.endfield_center_gacha_list, "导入终末地 UIGF 文件后，这里会显示最近六星记录。")
                return
            records = [r for r in db_rows if isinstance(r, dict)]
            self.controller.logger.info("[终末地数据中心] 本地抽卡数据: 记录数=%d", len(records))
            if not records:
                self.endfield_center_gacha_overview.controls = [
                    self._build_gacha_metric_card("本地抽卡", "暂无", "还没有导入终末地 UIGF 抽卡数据。"),
                ]
                self._set_column_placeholder(self.endfield_center_gacha_list, "导入终末地 UIGF 文件后，这里会显示最近六星记录。")
                return

            overview = build_endfield_overview(records)
            six_star_history = build_six_star_history(records)

            self.endfield_center_gacha_overview.controls = [
                self._build_gacha_metric_card("总记录", str(overview.get("total_records", "?")),
                    f"{overview.get('earliest_time', '?')} ~ {overview.get('latest_time', '?')}"),
                self._build_gacha_metric_card("六星", str(overview.get("six_star_count", "?")), "最高稀有度",
                    color=self.PALETTE["accent"]),
                self._build_gacha_metric_card("五星", str(overview.get("five_star_count", "?")), "高稀有度"),
                self._build_gacha_metric_card("四星", str(overview.get("four_star_count", "?")), "基础稀有度"),
            ]

            self.endfield_center_gacha_list.controls = []
            for h in six_star_history[:20]:
                self.endfield_center_gacha_list.controls.append(
                    ft.Container(
                        bgcolor=self.PALETTE["accent_soft"],
                        border_radius=8,
                        padding=ft.Padding(8, 6, 8, 6),
                        content=ft.Row([
                            ft.Text(h.get("time", ""), size=12, color=self.PALETTE["muted"], width=140),
                            ft.Text(h.get("name", "?"), size=13, weight=ft.FontWeight.BOLD, color="#d4a017", width=120),
                            ft.Text(f"[{h.get('pool', '')}]", size=12, color=self.PALETTE["muted"], width=100),
                            ft.Text(f"第 {h.get('pity', '?')} 抽", size=12, color=self.PALETTE["text"]),
                        ], spacing=6),
                    )
                )
        except Exception as exc:
            self.controller.logger.error("[终末地数据中心] 加载本地抽卡数据失败: %s", exc, exc_info=True)
        self.page.update()

    def _render_skendfield_sign_result(self, result: Optional[Dict[str, Any]]):
        if self.skendfield_metrics_row is None or self.skendfield_result_list is None:
            return
        self._set_skendfield_busy(False)
        if result is None:
            self.skendfield_metrics_row.controls = [self._build_gacha_metric_card("待签到", "-", "填写 Token 后点击签到。")]
            self._set_column_placeholder(self.skendfield_result_list, "暂无签到记录。")
            if self.skendfield_status_text is not None:
                self.skendfield_status_text.value = "填写 Token 后点击签到，会自动保存配置以便下次使用。"
                self.skendfield_status_text.color = self.PALETTE["muted"]
            return

        server_name = result.get("server_name", result.get("server", ""))
        status = result.get("status", "")
        role = result.get("role", "")
        channel = result.get("channel", "")
        msg = result.get("message", "")

        is_success = status in ("success", "already")
        tone = self.PALETTE["accent"] if is_success else self.PALETTE["danger"]
        status_text = "签到成功" if status == "success" else (
            "已签到" if status == "already" else "失败"
        )

        if self.skendfield_status_text is not None:
            self.skendfield_status_text.value = f"{server_name} {role or ''}: {msg}"
            self.skendfield_status_text.color = tone

        self.skendfield_metrics_row.controls = [
            self._build_gacha_metric_card("服务器", server_name, ""),
            self._build_gacha_metric_card("角色", role or "-", channel or ""),
            self._build_gacha_metric_card("状态", status_text, msg, color=tone),
        ]

        if is_success:
            awards = result.get("awards", [])
            if awards:
                award_items = [ft.Text(f"  {a}", size=14, color=self.PALETTE["accent"]) for a in awards]
                card = ft.Container(
                    bgcolor=self.PALETTE["accent_soft"],
                    border_radius=14,
                    padding=ft.Padding(14, 12, 14, 12),
                    content=ft.Column(
                        controls=[
                            ft.Text(f"签到奖励 - {role}({channel})", size=14, weight=ft.FontWeight.BOLD, color=self.PALETTE["accent"]),
                            *award_items,
                        ],
                        spacing=4,
                    ),
                )
            else:
                card = ft.Container(
                    bgcolor=self.PALETTE["accent_soft"],
                    border_radius=14,
                    padding=ft.Padding(14, 12, 14, 12),
                    content=ft.Text(f"{status_text} - {role or server_name}", size=14, color=self.PALETTE["accent"]),
                )
            self.skendfield_result_list.controls = [card]
        else:
            self.skendfield_result_list.controls = [
                ft.Container(
                    bgcolor=self.PALETTE["danger_soft"],
                    border_radius=14,
                    padding=ft.Padding(14, 12, 14, 12),
                    content=ft.Text(f"签到失败: {msg}", size=14, color=self.PALETTE["danger"]),
                )
            ]

    # ────────────────────────────
    # 塔吉多事件处理
    # ────────────────────────────
    def _get_tajiduo_config(self) -> Dict[str, Any]:
        raw = cm.load().get(TAJIDUO_SIGN_CONFIG_KEY, {})
        return raw if isinstance(raw, dict) else {}

    def _update_tajiduo_config(self, **updates: Any) -> Dict[str, Any]:
        config = cm.load()
        section = config.get(TAJIDUO_SIGN_CONFIG_KEY)
        data = dict(section) if isinstance(section, dict) else {}
        data.update(updates)
        config[TAJIDUO_SIGN_CONFIG_KEY] = data
        cm.save(config)
        return data

    def _apply_tajiduo_config_to_controls(self):
        cfg = self._get_tajiduo_config()
        if self.tajiduo_token_input is not None:
            self.tajiduo_token_input.value = str(cfg.get("token") or "")
        if self.tajiduo_deviceid_input is not None:
            self.tajiduo_deviceid_input.value = str(cfg.get("deviceid") or "")
        if self.tajiduo_uid_input is not None:
            self.tajiduo_uid_input.value = str(cfg.get("uid") or "")
        if self.tajiduo_phone_input is not None:
            self.tajiduo_phone_input.value = str(cfg.get("phone") or "")
        if self.tajiduo_bbs_checkbox is not None:
            self.tajiduo_bbs_checkbox.value = bool(cfg.get("do_bbs_signin", True))
        if self.tajiduo_game_checkbox is not None:
            self.tajiduo_game_checkbox.value = bool(cfg.get("do_game_signin", True))
        if self.tajiduo_browse_checkbox is not None:
            self.tajiduo_browse_checkbox.value = bool(cfg.get("do_browse", False))
        if self.tajiduo_like_checkbox is not None:
            self.tajiduo_like_checkbox.value = bool(cfg.get("do_like", False))
        if self.tajiduo_share_checkbox is not None:
            self.tajiduo_share_checkbox.value = bool(cfg.get("do_share", False))
        if self.tajiduo_startup_checkbox is not None:
            self.tajiduo_startup_checkbox.value = bool(cfg.get("startup_sign", False))

    def _set_tajiduo_busy(self, busy: bool):
        self.tajiduo_busy = busy
        for control in (
            self.tajiduo_phone_input,
            self.tajiduo_password_input,
            self.tajiduo_sms_code_input,
            self.tajiduo_token_input,
            self.tajiduo_deviceid_input,
            self.tajiduo_uid_input,
            self.tajiduo_bbs_checkbox,
            self.tajiduo_game_checkbox,
            self.tajiduo_browse_checkbox,
            self.tajiduo_like_checkbox,
            self.tajiduo_share_checkbox,
            self.tajiduo_startup_checkbox,
            self.tajiduo_login_button,
            self.tajiduo_sms_send_button,
            self.tajiduo_sms_login_button,
            self.tajiduo_token_button,
            self.tajiduo_sign_button,
            self.tajiduo_clear_button,
        ):
            if control is not None:
                control.disabled = busy

    def _on_tajiduo_option_change(self, e):
        self._update_tajiduo_config(
            do_bbs_signin=bool(self.tajiduo_bbs_checkbox and self.tajiduo_bbs_checkbox.value),
            do_game_signin=bool(self.tajiduo_game_checkbox and self.tajiduo_game_checkbox.value),
            do_browse=bool(self.tajiduo_browse_checkbox and self.tajiduo_browse_checkbox.value),
            do_like=bool(self.tajiduo_like_checkbox and self.tajiduo_like_checkbox.value),
            do_share=bool(self.tajiduo_share_checkbox and self.tajiduo_share_checkbox.value),
        )

    def _on_tajiduo_startup_change(self, e):
        self._update_tajiduo_config(startup_sign=bool(e.control.value))

    def _start_tajiduo_password_login(self, e=None):
        if self.tajiduo_busy:
            return
        phone = str(self.tajiduo_phone_input.value or "").strip() if self.tajiduo_phone_input is not None else ""
        password = str(self.tajiduo_password_input.value or "").strip() if self.tajiduo_password_input is not None else ""
        if not phone or len(phone) != 11:
            if self.tajiduo_status_text is not None:
                self.tajiduo_status_text.value = "请输入正确的 11 位手机号。"
                self.tajiduo_status_text.color = self.PALETTE["danger"]
            self.page.update()
            return
        if not password:
            if self.tajiduo_status_text is not None:
                self.tajiduo_status_text.value = "请输入密码。"
                self.tajiduo_status_text.color = self.PALETTE["danger"]
            self.page.update()
            return
        deviceid = str(self.tajiduo_deviceid_input.value or "").strip() if self.tajiduo_deviceid_input is not None else ""
        if not deviceid:
            fp = gen_tajiduo_fingerprint()
            deviceid = fp["deviceid"]
            if self.tajiduo_deviceid_input is not None:
                self.tajiduo_deviceid_input.value = deviceid
        self.controller.logger.info("[塔吉多] 用户发起密码登录，手机号 %s", phone[:3] + "****" + phone[7:])
        if self.tajiduo_status_text is not None:
            self.tajiduo_status_text.value = "正在使用密码登录塔吉多..."
            self.tajiduo_status_text.color = self.PALETTE["accent"]
        self._set_tajiduo_busy(True)
        self.page.update()
        threading.Thread(target=self._run_tajiduo_password_login_backend, args=(phone, password, deviceid), daemon=True).start()

    def _run_tajiduo_password_login_backend(self, phone: str, password: str, deviceid: str):
        try:
            fp = gen_tajiduo_fingerprint()
            result = complete_tajiduo_password_login(phone, password, deviceid, fp["openudid"], fp["vendorid"])
            self._emit("tajiduo_login_finished", result=result, phone=phone)
        except Exception as exc:
            self._emit("tajiduo_failed", stage="密码登录", message=str(exc))

    def _start_tajiduo_sms_captcha(self, e):
        if self.tajiduo_busy:
            return
        phone = str(self.tajiduo_phone_input.value or "").strip() if self.tajiduo_phone_input is not None else ""
        if not phone or len(phone) != 11:
            if self.tajiduo_status_text is not None:
                self.tajiduo_status_text.value = "请输入正确的 11 位手机号。"
                self.tajiduo_status_text.color = self.PALETTE["danger"]
            self.page.update()
            return
        deviceid = str(self.tajiduo_deviceid_input.value or "").strip() if self.tajiduo_deviceid_input is not None else ""
        if not deviceid:
            fp = gen_tajiduo_fingerprint()
            deviceid = fp["deviceid"]
            if self.tajiduo_deviceid_input is not None:
                self.tajiduo_deviceid_input.value = deviceid
        self.controller.logger.info("[塔吉多] 用户发起短信验证码发送，手机号 %s", phone[:3] + "****" + phone[7:])
        if self.tajiduo_status_text is not None:
            self.tajiduo_status_text.value = f"正在向 {phone[:3]}****{phone[7:]} 发送验证码..."
            self.tajiduo_status_text.color = self.PALETTE["accent"]
        self._set_tajiduo_busy(True)
        if self.tajiduo_sms_send_button is not None:
            self.tajiduo_sms_send_button.disabled = True
        self.page.update()
        threading.Thread(target=self._run_tajiduo_sms_captcha_backend, args=(phone, deviceid), daemon=True).start()

    def _run_tajiduo_sms_captcha_backend(self, phone: str, deviceid: str):
        try:
            fp = gen_tajiduo_fingerprint()
            code, j = send_tajiduo_captcha(phone, deviceid, fp["openudid"], fp["vendorid"])
            if isinstance(j, dict) and j.get("code") == 0:
                self._emit("tajiduo_sms_sent", phone=phone)
            else:
                msg = str(j.get("message", j)) if isinstance(j, dict) else str(j)
                self._emit("tajiduo_failed", stage="发送验证码", message=msg)
        except Exception as exc:
            self._emit("tajiduo_failed", stage="发送验证码", message=str(exc))

    def _start_tajiduo_sms_login(self, e):
        if self.tajiduo_busy:
            return
        phone = str(self.tajiduo_phone_input.value or "").strip() if self.tajiduo_phone_input is not None else ""
        sms = str(self.tajiduo_sms_code_input.value or "").strip() if self.tajiduo_sms_code_input is not None else ""
        if not phone or len(phone) != 11:
            if self.tajiduo_status_text is not None:
                self.tajiduo_status_text.value = "请先输入手机号并发送验证码。"
                self.tajiduo_status_text.color = self.PALETTE["danger"]
            self.page.update()
            return
        if not sms or len(sms) < 4:
            if self.tajiduo_status_text is not None:
                self.tajiduo_status_text.value = "请输入收到的短信验证码。"
                self.tajiduo_status_text.color = self.PALETTE["danger"]
            self.page.update()
            return
        deviceid = str(self.tajiduo_deviceid_input.value or "").strip() if self.tajiduo_deviceid_input is not None else ""
        if not deviceid:
            fp = gen_tajiduo_fingerprint()
            deviceid = fp["deviceid"]
            if self.tajiduo_deviceid_input is not None:
                self.tajiduo_deviceid_input.value = deviceid
        self.controller.logger.info("[塔吉多] 用户发起短信验证码登录")
        if self.tajiduo_status_text is not None:
            self.tajiduo_status_text.value = "正在使用短信验证码登录塔吉多..."
            self.tajiduo_status_text.color = self.PALETTE["accent"]
        self._set_tajiduo_busy(True)
        self.page.update()
        threading.Thread(target=self._run_tajiduo_sms_login_backend, args=(phone, sms, deviceid), daemon=True).start()

    def _run_tajiduo_sms_login_backend(self, phone: str, sms: str, deviceid: str):
        try:
            fp = gen_tajiduo_fingerprint()
            result = complete_tajiduo_sms_login(phone, sms, deviceid, fp["openudid"], fp["vendorid"])
            self._emit("tajiduo_login_finished", result=result, phone=phone)
        except Exception as exc:
            self._emit("tajiduo_failed", stage="短信验证码登录", message=str(exc))

    def _start_tajiduo_token_login(self, e):
        if self.tajiduo_busy:
            return
        token = str(self.tajiduo_token_input.value or "").strip() if self.tajiduo_token_input is not None else ""
        if not token:
            if self.tajiduo_status_text is not None:
                self.tajiduo_status_text.value = "请先填写 AccessToken。"
                self.tajiduo_status_text.color = self.PALETTE["danger"]
            self.page.update()
            return
        deviceid = str(self.tajiduo_deviceid_input.value or "").strip() if self.tajiduo_deviceid_input is not None else ""
        uid = str(self.tajiduo_uid_input.value or "").strip() if self.tajiduo_uid_input is not None else "0"
        if not deviceid:
            fp = gen_tajiduo_fingerprint()
            deviceid = fp["deviceid"]
            if self.tajiduo_deviceid_input is not None:
                self.tajiduo_deviceid_input.value = deviceid
        self.controller.logger.info("[塔吉多] 用户发起 Token 登录")
        if self.tajiduo_status_text is not None:
            self.tajiduo_status_text.value = "已保存当前 Token 配置。"
            self.tajiduo_status_text.color = self.PALETTE["accent"]
        self._update_tajiduo_config(token=token, deviceid=deviceid, uid=uid)
        if self.tajiduo_account_text is not None:
            self.tajiduo_account_text.value = f"已使用现有 Token | UID {uid or '未设置'}"
            self.tajiduo_account_text.color = self.PALETTE["text"]
        self._render_tajiduo_sign_result(None)
        self.page.update()

    def _start_tajiduo_sign(self, e=None, show_toast: bool = True):
        if self.tajiduo_busy:
            return
        token = str(self.tajiduo_token_input.value or "").strip() if self.tajiduo_token_input is not None else ""
        deviceid = str(self.tajiduo_deviceid_input.value or "").strip() if self.tajiduo_deviceid_input is not None else ""
        uid = str(self.tajiduo_uid_input.value or "").strip() if self.tajiduo_uid_input is not None else "0"
        if not token:
            if self.tajiduo_status_text is not None:
                self.tajiduo_status_text.value = "请先登录或填入 AccessToken。"
                self.tajiduo_status_text.color = self.PALETTE["danger"]
            self.page.update()
            return
        if not deviceid:
            fp = gen_tajiduo_fingerprint()
            deviceid = fp["deviceid"]
            if self.tajiduo_deviceid_input is not None:
                self.tajiduo_deviceid_input.value = deviceid
        do_bbs = bool(self.tajiduo_bbs_checkbox and self.tajiduo_bbs_checkbox.value)
        do_game = bool(self.tajiduo_game_checkbox and self.tajiduo_game_checkbox.value)
        do_browse = bool(self.tajiduo_browse_checkbox and self.tajiduo_browse_checkbox.value)
        do_like = bool(self.tajiduo_like_checkbox and self.tajiduo_like_checkbox.value)
        do_share = bool(self.tajiduo_share_checkbox and self.tajiduo_share_checkbox.value)
        phone = str(self.tajiduo_phone_input.value or "").strip() if self.tajiduo_phone_input is not None else ""
        password = str(self.tajiduo_password_input.value or "").strip() if self.tajiduo_password_input is not None else ""
        self.controller.logger.info("[塔吉多] 用户发起手动签到")
        self._update_tajiduo_config(
            token=token,
            deviceid=deviceid,
            uid=uid,
            do_bbs_signin=do_bbs,
            do_game_signin=do_game,
            do_browse=do_browse,
            do_like=do_like,
            do_share=do_share,
            startup_sign=bool(self.tajiduo_startup_checkbox and self.tajiduo_startup_checkbox.value),
        )
        if self.tajiduo_status_text is not None:
            self.tajiduo_status_text.value = "正在执行塔吉多签到..."
            self.tajiduo_status_text.color = self.PALETTE["accent"]
        self._set_tajiduo_busy(True)
        self.page.update()
        threading.Thread(
            target=self._run_tajiduo_sign_backend,
            args=(token, deviceid, uid, do_bbs, do_game, do_browse, do_like, do_share, phone, password, show_toast),
            daemon=True,
        ).start()

    def _run_tajiduo_sign_backend(
        self,
        token: str,
        deviceid: str,
        uid: str,
        do_bbs: bool,
        do_game: bool,
        do_browse: bool,
        do_like: bool,
        do_share: bool,
        phone: str,
        password: str,
        show_toast: bool,
    ):
        try:
            result = perform_tajiduo_sign(
                token=token,
                deviceid=deviceid,
                uid=uid,
                do_bbs_signin=do_bbs,
                do_game_signin=do_game,
                do_browse=do_browse,
                do_like=do_like,
                do_share=do_share,
                phone=phone,
                password=password,
            )
            self._emit("tajiduo_sign_finished", result=result, show_toast=show_toast)
        except Exception as exc:
            self._emit("tajiduo_failed", stage="签到", message=str(exc))

    def _clear_tajiduo_config(self, e):
        if self.tajiduo_busy:
            return
        self.controller.logger.info("[塔吉多] 用户已清空配置")
        config = cm.load()
        if TAJIDUO_SIGN_CONFIG_KEY in config:
            config.pop(TAJIDUO_SIGN_CONFIG_KEY, None)
            cm.save(config)
        for control in (
            self.tajiduo_phone_input,
            self.tajiduo_password_input,
            self.tajiduo_sms_code_input,
            self.tajiduo_token_input,
            self.tajiduo_deviceid_input,
            self.tajiduo_uid_input,
        ):
            if control is not None:
                control.value = ""
        if self.tajiduo_bbs_checkbox is not None:
            self.tajiduo_bbs_checkbox.value = True
        if self.tajiduo_game_checkbox is not None:
            self.tajiduo_game_checkbox.value = True
        if self.tajiduo_browse_checkbox is not None:
            self.tajiduo_browse_checkbox.value = False
        if self.tajiduo_like_checkbox is not None:
            self.tajiduo_like_checkbox.value = False
        if self.tajiduo_share_checkbox is not None:
            self.tajiduo_share_checkbox.value = False
        if self.tajiduo_startup_checkbox is not None:
            self.tajiduo_startup_checkbox.value = False
        if self.tajiduo_status_text is not None:
            self.tajiduo_status_text.value = "已清空塔吉多配置。"
            self.tajiduo_status_text.color = self.PALETTE["accent"]
        if self.tajiduo_account_text is not None:
            self.tajiduo_account_text.value = "未登录塔吉多。"
            self.tajiduo_account_text.color = self.PALETTE["muted"]
        self._render_tajiduo_sign_result(None)
        self.page.update()

    def _render_tajiduo_sign_result(self, result: Optional[Dict[str, Any]]):
        if self.tajiduo_metrics_row is None or self.tajiduo_result_list is None:
            return
        cfg = self._get_tajiduo_config()
        token = str(self.tajiduo_token_input.value or "").strip() if self.tajiduo_token_input is not None else str(cfg.get("token") or "")
        uid = str(self.tajiduo_uid_input.value or "").strip() if self.tajiduo_uid_input is not None else str(cfg.get("uid") or "")
        startup = bool(self.tajiduo_startup_checkbox and self.tajiduo_startup_checkbox.value)
        has_auth = bool(token)

        if self.tajiduo_account_text is not None:
            if has_auth:
                self.tajiduo_account_text.value = f"已配置 Token | UID {uid or '未设置'}"
                self.tajiduo_account_text.color = self.PALETTE["text"]
            else:
                self.tajiduo_account_text.value = "未登录塔吉多。"
                self.tajiduo_account_text.color = self.PALETTE["muted"]

        if result is None:
            self.tajiduo_metrics_row.controls = [
                self._build_gacha_metric_card("登录状态", "已配置" if has_auth else "未配置", "支持密码/短信登录或手动填入 Token。"),
                self._build_gacha_metric_card("启动前签到", "已开启" if startup else "未开启", "若本轮包含异环，则在启动前先执行签到。"),
            ]
            self._set_column_placeholder(self.tajiduo_result_list, "签到结果会显示在这里；配置 Token 后点击「立即执行签到」即可。")
            return

        lines = format_tajiduo_sign_report(result)
        bbs = result.get("bbs") or {}
        game_results = result.get("game") or []
        success_count = sum(1 for g in game_results if g.get("status") == "success")
        already_count = sum(1 for g in game_results if g.get("status") == "already")
        failed_count = sum(1 for g in game_results if g.get("status") == "failed")
        bbs_status = bbs.get("status", "")
        if bbs_status == "success":
            success_count += 1
        elif bbs_status == "already":
            already_count += 1
        elif bbs_status == "failed":
            failed_count += 1

        self.tajiduo_metrics_row.controls = [
            self._build_gacha_metric_card("BBS 签到", "成功" if bbs_status == "success" else ("已签" if bbs_status == "already" else ("失败" if bbs_status == "failed" else "未执行")), bbs.get("message", "")),
            self._build_gacha_metric_card("游戏签到成功", str(success_count), "本次实际新签到的游戏数"),
            self._build_gacha_metric_card("游戏已签", str(already_count), "今天已经完成过签到的游戏数"),
            self._build_gacha_metric_card("游戏失败", str(failed_count), "绑定异常或接口错误的游戏数"),
        ]
        self.tajiduo_result_list.controls = [
            self._build_wuwa_kujiequ_info_line("结果", line, "") for line in lines
        ] or [self._build_pool_empty_state("本次没有可展示的签到结果。")]

    def _set_results(self, report: List[str], warnings: List[str]):
        lines = report[:] if report else ["本轮没有可展示的结果。"]
        if warnings:
            lines.append("")
            lines.append("额外提醒：")
            lines.extend(f"- {item}" for item in warnings)
        self.results_lines = lines
        self.result_box.value = "\n".join(lines)

    async def _pump_ui_events(self):
        while True:
            changed = False
            while True:
                try:
                    kind, payload = self.ui_queue.get_nowait()
                except queue.Empty:
                    break
                changed = True
                self._handle_ui_event(kind, payload)
            if changed:
                self.page.update()
            await asyncio.sleep(0.15)

    def _handle_ui_event(self, kind: str, payload: dict):
        if kind == "log":
            self._append_log(payload.get("message", ""))
            return
        if kind == "status":
            self._set_status(payload["title"], payload["body"], payload.get("tone", "info"))
            return
        if kind == "queue_state":
            self.queue_state[payload["app_key"]] = payload["state"]
            self._refresh_summary_and_queue()
            return
        if kind == "daily_check_finished":
            self._set_daily_check_busy(False)
            result = payload.get("result")
            daily_result = result if isinstance(result, DailyCheckResult) else DailyCheckResult()
            self._render_daily_check_result(daily_result)
            if payload.get("show_toast") and self.current_view == "daily":
                self._toast(
                    "每日进度已刷新。",
                    self.PALETTE["accent"] if daily_result.all_completed and not any(game.errors for game in daily_result.games) else self.PALETTE["warning"],
                )
            return
        if kind == "daily_check_failed":
            self._set_daily_check_busy(False)
            message = str(payload.get("message") or "未知错误")
            if self.daily_status_text is not None:
                self.daily_status_text.value = f"每日进度刷新失败：{message}"
                self.daily_status_text.color = self.PALETTE["danger"]
            if payload.get("show_toast") and self.current_view == "daily":
                self._toast(f"每日进度刷新失败：{message}", self.PALETTE["danger"])
            return
        if kind == "gacha_imported":
            self._set_gacha_import_busy(False)
            if self.gacha_import_summary is not None:
                self.gacha_import_summary.value = self._summarize_gacha_import(payload.get("summaries", []))
                self.gacha_import_summary.color = self.PALETTE["accent"]
            selected_uid = None
            selected_game_code = payload.get("selected_game_code")
            summaries = payload.get("summaries", [])
            if summaries:
                selected_uid = str(summaries[0].get("uid", "") or "") or None
                if not selected_game_code:
                    selected_game_code = str(summaries[0].get("game_code", "") or "") or None
            self.current_view = "gacha"
            self._refresh_view_nav()
            self._reload_gacha_data(selected_uid=selected_uid, selected_game_code=selected_game_code, show_toast=False)
            toast_game_name = get_game_display_name(selected_game_code or GENSHIN_GAME_CODE)
            self._toast(f"{toast_game_name} 抽卡数据已导入并刷新统计。", self.PALETTE["accent"])
            return
        if kind == "gacha_import_failed":
            self._set_gacha_import_busy(False)
            if self.gacha_import_summary is not None:
                self.gacha_import_summary.value = f"导入失败：{payload.get('message', '未知错误')}"
                self.gacha_import_summary.color = self.PALETTE["danger"]
            self._toast(f"导入失败：{payload.get('message', '未知错误')}", self.PALETTE["danger"])
            return
        if kind == "nikki_fetch_progress":
            if self.nikki_status_text is not None:
                self.nikki_status_text.value = payload.get("message", "")
                self.nikki_status_text.color = self.PALETTE["muted"]
            return
        if kind == "nikki_fetch_finished":
            self._set_nikki_fetch_busy(False)
            output_path = str(payload.get("output_path", "") or "")
            summary = payload.get("summary", {}) or {}
            source_mode = str(summary.get("source_mode") or "query")
            auth_mode_label = "JWT" if summary.get("auth_mode") == "jwt" else "Momo Token"
            if self.nikki_output_input is not None:
                self.nikki_output_input.value = output_path
            if self.nikki_path_input is not None:
                self.nikki_path_input.value = output_path
            if self.nikki_status_text is not None:
                if source_mode == "pearpal_notebook":
                    self.nikki_status_text.value = (
                        f"Pearpal 摘要导入完成：UID {summary.get('uid', '-')}，"
                        f"{summary.get('event_count', 0)} 条 4/5 星事件，估算 {summary.get('estimated_total_pulls', 0)} 抽，"
                        f"{summary.get('pool_count', 0)} 个卡池，已保存到 {output_path}"
                    )
                else:
                    self.nikki_status_text.value = (
                        f"抓取完成：UID {summary.get('uid', '-')}，共 {summary.get('total_records', 0)} 条记录，"
                        f"{summary.get('pool_count', 0)} 个卡池，认证方式 {auth_mode_label}，已保存到 {output_path}"
                    )
                self.nikki_status_text.color = self.PALETTE["accent"]
            self.current_view = "nikki"
            self._refresh_view_nav()
            self._reload_nikki_data(show_toast=False)
            if source_mode == "pearpal_notebook":
                self._toast("无限暖暖 Pearpal 摘要已导入并刷新。", self.PALETTE["accent"])
            else:
                self._toast("无限暖暖共鸣记录已抓取并刷新。", self.PALETTE["accent"])
            return
        if kind == "nikki_fetch_failed":
            self._set_nikki_fetch_busy(False)
            if self.nikki_status_text is not None:
                self.nikki_status_text.value = f"抓取失败：{payload.get('message', '未知错误')}"
                self.nikki_status_text.color = self.PALETTE["danger"]
            self._toast(f"无限暖暖抓取失败：{payload.get('message', '未知错误')}", self.PALETTE["danger"])
            return
        if kind == "mihoyo_qr_ready":
            qr_base64 = str(payload.get("qr_base64") or "")
            qr_url = str(payload.get("url") or "")
            self.controller.logger.info("[米游社] 扫码二维码已生成，等待 App 确认")
            self._show_mihoyo_qr_prompt(
                "请在 3 分钟内使用米游社 App 扫描下方二维码并确认登录；成功后会自动写入可查询战绩的 Cookie。",
                qr_base64=qr_base64,
                url=qr_url,
            )
            if self.mihoyo_status_text is not None:
                self.mihoyo_status_text.value = "二维码已生成，请在手机米游社 App 中扫码确认登录。"
                self.mihoyo_status_text.color = self.PALETTE["accent"]
            return
        if kind == "mihoyo_cookie_loaded":
            self._set_mihoyo_sign_busy(False)
            self._reset_mihoyo_qr_prompt()
            result = payload.get("result", {}) or {}
            cookie = tidy_mihoyo_cookie(str(result.get("cookie") or ""))
            self.mihoyo_cookie_draft = cookie
            browser = str(payload.get("browser") or result.get("browser") or "edge")
            self._update_mihoyo_sign_config(
                cookie=cookie,
                stoken_cookie=tidy_mihoyo_cookie(str(result.get("stoken_cookie") or "")) or self._get_current_mihoyo_stoken_cookie_text(),
                browser=browser,
                enabled_games={
                    "genshin": bool(self.mihoyo_genshin_checkbox and self.mihoyo_genshin_checkbox.value),
                    "honkai_sr": bool(self.mihoyo_hsr_checkbox and self.mihoyo_hsr_checkbox.value),
                    "zzz": bool(self.mihoyo_zzz_checkbox and self.mihoyo_zzz_checkbox.value),
                },
                startup_sign=bool(self.mihoyo_startup_checkbox and self.mihoyo_startup_checkbox.value),
            )
            if self.mihoyo_cookie_input is not None:
                self.mihoyo_cookie_input.value = cookie
            fields = extract_mihoyo_cookie_fields(cookie)
            account_id = fields.get("account_id_v2") or fields.get("account_id") or fields.get("ltuid_v2") or fields.get("ltuid") or fields.get("stuid_v2") or fields.get("stuid") or "-"
            record_ready = bool(result.get("record_ready") or fields.get("stoken") or fields.get("stoken_v2"))
            self.controller.logger.info("[米游社] 浏览器 Cookie 已载入，浏览器：%s，账号：%s", browser, account_id)
            if self.mihoyo_status_text is not None:
                if record_ready:
                    self.mihoyo_status_text.value = f"已从 {browser} 读取米游社 Cookie，并补齐战绩查询字段。当前账号 ID：{account_id}，现在会自动刷新一次每日进度。"
                    self.mihoyo_status_text.color = self.PALETTE["accent"]
                else:
                    self.mihoyo_status_text.value = f"已从 {browser} 读取米游社 Cookie，可直接执行签到。当前账号 ID：{account_id}；若要查星铁/绝区零便笺，建议改用 App 扫码登录。"
                    self.mihoyo_status_text.color = self.PALETTE["warning"]
            self._render_mihoyo_sign_result(None)
            self._render_daily_check_result(self.daily_result)
            if record_ready and self._selected_daily_game_keys() and not self.daily_check_busy:
                self._start_daily_check(show_toast=False)
            self._toast("米游社 Cookie 已从浏览器载入。", self.PALETTE["accent"] if record_ready else self.PALETTE["warning"])
            return
        if kind == "mihoyo_qr_login_finished":
            self._set_mihoyo_sign_busy(False)
            self._reset_mihoyo_qr_prompt()
            result = payload.get("result", {}) or {}
            cookie = tidy_mihoyo_cookie(str(result.get("cookie") or ""))
            self.mihoyo_cookie_draft = cookie
            browser = str(self.mihoyo_browser_dropdown.value or "edge") if self.mihoyo_browser_dropdown is not None else "edge"
            self._update_mihoyo_sign_config(
                cookie=cookie,
                stoken_cookie=tidy_mihoyo_cookie(str(result.get("stoken_cookie") or "")),
                browser=browser,
                enabled_games={
                    "genshin": bool(self.mihoyo_genshin_checkbox and self.mihoyo_genshin_checkbox.value),
                    "honkai_sr": bool(self.mihoyo_hsr_checkbox and self.mihoyo_hsr_checkbox.value),
                    "zzz": bool(self.mihoyo_zzz_checkbox and self.mihoyo_zzz_checkbox.value),
                },
                startup_sign=bool(self.mihoyo_startup_checkbox and self.mihoyo_startup_checkbox.value),
            )
            if self.mihoyo_cookie_input is not None:
                self.mihoyo_cookie_input.value = cookie
            fields = extract_mihoyo_cookie_fields(cookie)
            account_id = fields.get("account_id_v2") or fields.get("account_id") or fields.get("ltuid_v2") or fields.get("ltuid") or fields.get("stuid_v2") or fields.get("stuid") or "-"
            record_ready = bool(result.get("record_ready") or fields.get("stoken") or fields.get("stoken_v2"))
            self.controller.logger.info("[米游社] App 扫码登录完成：账号 %s | 便笺查询 %s", account_id, "就绪" if record_ready else "未就绪")
            if self.mihoyo_status_text is not None:
                self.mihoyo_status_text.value = (
                    f"已通过米游社 App 扫码登录写入 Cookie。当前账号 ID：{account_id}；"
                    f"{'已补齐战绩查询字段，正在自动刷新每日进度。' if record_ready else '已登录，但仍未补齐战绩查询字段。'}"
                )
                self.mihoyo_status_text.color = self.PALETTE["accent"] if record_ready else self.PALETTE["warning"]
            self._render_mihoyo_sign_result(None)
            self._render_daily_check_result(self.daily_result)
            if self._selected_daily_game_keys() and not self.daily_check_busy:
                self._start_daily_check(show_toast=False)
            self._toast("米游社扫码登录已完成。", self.PALETTE["accent"] if record_ready else self.PALETTE["warning"])
            return
        if kind == "mihoyo_sms_sent":
            self._set_mihoyo_sign_busy(False)
            if self.mihoyo_send_sms_button is not None:
                self.mihoyo_send_sms_button.disabled = False
            if self.mihoyo_status_text is not None:
                self.mihoyo_status_text.value = "验证码已发送，请在手机上查看短信并输入下方验证码后点「验证码登录」。"
                self.mihoyo_status_text.color = self.PALETTE["accent"]
            if self.mihoyo_sms_code_input is not None:
                self.mihoyo_sms_code_input.value = ""
            self.page.update()
            return
        if kind == "mihoyo_sms_login_finished":
            self._set_mihoyo_sign_busy(False)
            if self.mihoyo_send_sms_button is not None:
                self.mihoyo_send_sms_button.disabled = False
            self._reset_mihoyo_qr_prompt()
            result = payload.get("result", {}) or {}
            cookie = tidy_mihoyo_cookie(str(result.get("cookie") or ""))
            self.mihoyo_cookie_draft = cookie
            browser = str(self.mihoyo_browser_dropdown.value or "edge") if self.mihoyo_browser_dropdown is not None else "edge"
            self._update_mihoyo_sign_config(
                cookie=cookie,
                stoken_cookie=tidy_mihoyo_cookie(str(result.get("stoken_cookie") or "")),
                browser=browser,
                enabled_games={
                    "genshin": bool(self.mihoyo_genshin_checkbox and self.mihoyo_genshin_checkbox.value),
                    "honkai_sr": bool(self.mihoyo_hsr_checkbox and self.mihoyo_hsr_checkbox.value),
                    "zzz": bool(self.mihoyo_zzz_checkbox and self.mihoyo_zzz_checkbox.value),
                },
                startup_sign=bool(self.mihoyo_startup_checkbox and self.mihoyo_startup_checkbox.value),
            )
            if self.mihoyo_cookie_input is not None:
                self.mihoyo_cookie_input.value = cookie
            if self.mihoyo_sms_code_input is not None:
                self.mihoyo_sms_code_input.value = ""
            fields = extract_mihoyo_cookie_fields(cookie)
            account_id = fields.get("account_id_v2") or fields.get("account_id") or fields.get("ltuid_v2") or fields.get("ltuid") or fields.get("stuid_v2") or fields.get("stuid") or "-"
            record_ready = bool(result.get("record_ready") or fields.get("stoken") or fields.get("stoken_v2"))
            self.controller.logger.info("[米游社] 手机验证码登录完成：账号 %s | 便笺查询 %s", account_id, "就绪" if record_ready else "未就绪")
            if self.mihoyo_status_text is not None:
                self.mihoyo_status_text.value = (
                    f"已通过手机验证码登录米游社。当前账号 ID：{account_id}；"
                    f"{'已补齐战绩查询字段，正在自动刷新每日进度。' if record_ready else '已登录，但仍未补齐战绩查询字段。'}"
                )
                self.mihoyo_status_text.color = self.PALETTE["accent"] if record_ready else self.PALETTE["warning"]
            self._render_mihoyo_sign_result(None)
            self._render_daily_check_result(self.daily_result)
            if self._selected_daily_game_keys() and not self.daily_check_busy:
                self._start_daily_check(show_toast=False)
            self._toast("米游社手机验证码登录已完成。", self.PALETTE["accent"] if record_ready else self.PALETTE["warning"])
            return
        if kind == "mihoyo_password_login_finished":
            self._set_mihoyo_sign_busy(False)
            self._reset_mihoyo_qr_prompt()
            result = payload.get("result", {}) or {}
            cookie = tidy_mihoyo_cookie(str(result.get("cookie") or ""))
            self.mihoyo_cookie_draft = cookie
            browser = str(self.mihoyo_browser_dropdown.value or "edge") if self.mihoyo_browser_dropdown is not None else "edge"
            self._update_mihoyo_sign_config(
                cookie=cookie,
                stoken_cookie=tidy_mihoyo_cookie(str(result.get("stoken_cookie") or "")),
                browser=browser,
                enabled_games={
                    "genshin": bool(self.mihoyo_genshin_checkbox and self.mihoyo_genshin_checkbox.value),
                    "honkai_sr": bool(self.mihoyo_hsr_checkbox and self.mihoyo_hsr_checkbox.value),
                    "zzz": bool(self.mihoyo_zzz_checkbox and self.mihoyo_zzz_checkbox.value),
                },
                startup_sign=bool(self.mihoyo_startup_checkbox and self.mihoyo_startup_checkbox.value),
            )
            if self.mihoyo_cookie_input is not None:
                self.mihoyo_cookie_input.value = cookie
            fields = extract_mihoyo_cookie_fields(cookie)
            account_id = fields.get("account_id_v2") or fields.get("account_id") or fields.get("ltuid_v2") or fields.get("ltuid") or fields.get("stuid_v2") or fields.get("stuid") or "-"
            record_ready = bool(result.get("record_ready") or fields.get("stoken") or fields.get("stoken_v2"))
            self.controller.logger.info("[米游社] 密码登录完成：账号 %s | 便笺查询 %s", account_id, "已就绪" if record_ready else "未就绪")
            if self.mihoyo_status_text is not None:
                self.mihoyo_status_text.value = (
                    f"已通过密码登录米游社。当前账号 ID：{account_id}；"
                    f"{'已获取 stoken，正在自动刷新每日进度。' if record_ready else '已登录，但仍未获 stoken。'}"
                )
                self.mihoyo_status_text.color = self.PALETTE["accent"] if record_ready else self.PALETTE["warning"]
            self._render_mihoyo_sign_result(None)
            self._render_daily_check_result(self.daily_result)
            if self._selected_daily_game_keys() and not self.daily_check_busy:
                self._start_daily_check(show_toast=False)
            self._toast("米游社密码登录已完成。", self.PALETTE["accent"] if record_ready else self.PALETTE["warning"])
            return
        if kind == "mihoyo_sign_finished":
            self._set_mihoyo_sign_busy(False)
            result = payload.get("result", {}) or {}
            game_results = result.get("games") or []
            self.controller.logger.info(
                "[米游社] 手动签到完成：成功 %d，已签 %d，失败 %d",
                sum(int(item.get("success_count") or 0) for item in game_results),
                sum(int(item.get("already_count") or 0) for item in game_results),
                sum(int(item.get("failed_count") or 0) for item in game_results),
            )
            if self.mihoyo_status_text is not None:
                self.mihoyo_status_text.value = "米游社签到已完成。"
                self.mihoyo_status_text.color = self.PALETTE["accent"]
            self._render_mihoyo_sign_result(result)
            if payload.get("show_toast", True):
                self._toast("米游社签到已执行完成。", self.PALETTE["accent"])
            return
        if kind == "mihoyo_sign_failed":
            self._set_mihoyo_sign_busy(False)
            stage = str(payload.get("stage") or "米游社操作")
            message = str(payload.get("message") or "未知错误")
            self.controller.logger.error("[米游社] %s失败：%s", stage, message)
            if "扫码" in stage:
                self._reset_mihoyo_qr_prompt()
            if self.mihoyo_status_text is not None:
                self.mihoyo_status_text.value = f"{stage}失败：{message}"
                self.mihoyo_status_text.color = self.PALETTE["danger"]
            if self.current_view == "gacha":
                self._toast(f"{stage}失败：{message}", self.PALETTE["danger"])
            self._render_mihoyo_sign_result(None)
            return
        if kind == "wuwa_kujiequ_login_success":
            self._set_wuwa_kujiequ_busy(False)
            profile_data = payload.get("profile", {}) or {}
            source = str(payload.get("source") or ("h5" if profile_data.get("is_web") else "android"))
            self.controller.logger.info(
                "[鸣潮库街区] 登录成功：角色数 %d，当前角色 %s | UID %s",
                len(payload.get("roles", []) or []),
                profile_data.get("role_name") or profile_data.get("role_id") or "-",
                profile_data.get("user_id") or "-",
            )
            self._update_wuwa_kujiequ_config(
                phone=str(payload.get("phone") or ""),
                source=source,
                roles=payload.get("roles", []) or [],
                profile=profile_data,
                auto_sign=bool(self.wuwa_kujiequ_auto_sign_checkbox and self.wuwa_kujiequ_auto_sign_checkbox.value),
            )
            self._apply_wuwa_kujiequ_config_to_controls()
            if self.wuwa_kujiequ_code_input is not None:
                self.wuwa_kujiequ_code_input.value = ""
            if self.wuwa_kujiequ_status_text is not None:
                self.wuwa_kujiequ_status_text.value = f"登录成功，已找到 {len(payload.get('roles', []) or [])} 个绑定角色，正在刷新库街区数据..."
                self.wuwa_kujiequ_status_text.color = self.PALETTE["accent"]
            self.page.update()
            self._start_wuwa_kujiequ_refresh(show_toast=False)
            return
        if kind == "wuwa_kujiequ_dashboard_success":
            self._set_wuwa_kujiequ_busy(False)
            dashboard = payload.get("dashboard", {}) or {}
            profile_data = dashboard.get("profile", {}) or {}
            self.controller.logger.info(
                "[鸣潮库街区] 数据刷新完成：角色 %s | UID %s | 错误数 %d",
                profile_data.get("role_name") or profile_data.get("role_id") or "-",
                profile_data.get("user_id") or "-",
                len([item for item in dashboard.get("errors") or [] if item]),
            )
            self._update_wuwa_kujiequ_config(
                phone=(self.wuwa_kujiequ_phone_input.value or "").strip() if self.wuwa_kujiequ_phone_input is not None else "",
                source="h5" if profile_data.get("is_web") else "android",
                profile=profile_data,
                auto_sign=bool(self.wuwa_kujiequ_auto_sign_checkbox and self.wuwa_kujiequ_auto_sign_checkbox.value),
            )
            self._apply_wuwa_kujiequ_config_to_controls()
            self._render_wuwa_kujiequ_dashboard(dashboard)
            message = str(payload.get("message") or "库街区数据已刷新。")
            errors = [str(item) for item in dashboard.get("errors") or [] if item]
            if self.wuwa_kujiequ_status_text is not None:
                self.wuwa_kujiequ_status_text.value = message if not errors else f"{message} 部分接口未返回：{'；'.join(errors)}"
                self.wuwa_kujiequ_status_text.color = self.PALETTE["warning"] if errors else self.PALETTE["accent"]
            if payload.get("show_toast") and self.current_view == "wuwa":
                self._toast("鸣潮库街区数据已刷新。", self.PALETTE["accent"] if not errors else self.PALETTE["warning"])
            self.page.update()
            return
        if kind == "wuwa_kujiequ_failed":
            self._set_wuwa_kujiequ_busy(False)
            stage = str(payload.get("stage") or "库街区操作")
            message = str(payload.get("message") or "未知错误")
            self.controller.logger.error("[鸣潮库街区] %s失败：%s", stage, message)
            if self.wuwa_kujiequ_status_text is not None:
                self.wuwa_kujiequ_status_text.value = f"{stage}失败：{message}"
                self.wuwa_kujiequ_status_text.color = self.PALETTE["danger"]
            if self.current_view == "wuwa":
                self._toast(f"{stage}失败：{message}", self.PALETTE["danger"])
            self.page.update()
            return
        if kind == "tajiduo_login_finished":
            self._set_tajiduo_busy(False)
            result = payload.get("result", {}) or {}
            phone = str(payload.get("phone") or "")
            token = str(result.get("token") or "")
            deviceid = str(result.get("deviceid") or "")
            uid = str(result.get("uid") or "0")
            self.controller.logger.info("[塔吉多] 登录成功：UID %s", uid)
            self._update_tajiduo_config(
                token=token,
                deviceid=deviceid,
                uid=uid,
                phone=phone,
                password=str(result.get("password") or ""),
                do_bbs_signin=bool(self.tajiduo_bbs_checkbox and self.tajiduo_bbs_checkbox.value),
                do_game_signin=bool(self.tajiduo_game_checkbox and self.tajiduo_game_checkbox.value),
                do_browse=bool(self.tajiduo_browse_checkbox and self.tajiduo_browse_checkbox.value),
                do_like=bool(self.tajiduo_like_checkbox and self.tajiduo_like_checkbox.value),
                do_share=bool(self.tajiduo_share_checkbox and self.tajiduo_share_checkbox.value),
                startup_sign=bool(self.tajiduo_startup_checkbox and self.tajiduo_startup_checkbox.value),
            )
            self._apply_tajiduo_config_to_controls()
            if self.tajiduo_sms_code_input is not None:
                self.tajiduo_sms_code_input.value = ""
            if self.tajiduo_status_text is not None:
                self.tajiduo_status_text.value = f"登录成功，UID {uid}，正在执行签到..."
                self.tajiduo_status_text.color = self.PALETTE["accent"]
            if self.tajiduo_account_text is not None:
                self.tajiduo_account_text.value = f"已登录 | UID {uid}"
                self.tajiduo_account_text.color = self.PALETTE["text"]
            self.page.update()
            self._start_tajiduo_sign(show_toast=False)
            return
        if kind == "tajiduo_sms_sent":
            self._set_tajiduo_busy(False)
            if self.tajiduo_sms_send_button is not None:
                self.tajiduo_sms_send_button.disabled = False
            if self.tajiduo_status_text is not None:
                self.tajiduo_status_text.value = "验证码已发送，请在手机上查看短信并输入下方验证码后点「验证码登录」。"
                self.tajiduo_status_text.color = self.PALETTE["accent"]
            if self.tajiduo_sms_code_input is not None:
                self.tajiduo_sms_code_input.value = ""
            self.page.update()
            return
        if kind == "tajiduo_sign_finished":
            self._set_tajiduo_busy(False)
            result = payload.get("result", {}) or {}
            lines = format_tajiduo_sign_report(result)
            self.controller.logger.info("[塔吉多] 签到完成：%s", " | ".join(lines))
            if self.tajiduo_status_text is not None:
                self.tajiduo_status_text.value = "塔吉多签到已完成。"
                self.tajiduo_status_text.color = self.PALETTE["accent"]
            self._render_tajiduo_sign_result(result)
            if payload.get("show_toast", True):
                self._toast("塔吉多签到已执行完成。", self.PALETTE["accent"])
            return
        if kind == "tajiduo_failed":
            self._set_tajiduo_busy(False)
            stage = str(payload.get("stage") or "塔吉多操作")
            message = str(payload.get("message") or "未知错误")
            self.controller.logger.error("[塔吉多] %s失败：%s", stage, message)
            if self.tajiduo_status_text is not None:
                self.tajiduo_status_text.value = f"{stage}失败：{message}"
                self.tajiduo_status_text.color = self.PALETTE["danger"]
            if self.current_view == "tajiduo":
                self._toast(f"{stage}失败：{message}", self.PALETTE["danger"])
            self._render_tajiduo_sign_result(None)
            self.page.update()
            return
        if kind == "endfield_center_query_finished":
            self._set_endfield_center_busy(False)
            query_type = str(payload.get("query_type", ""))
            result = payload.get("result")
            error = str(payload.get("error", "")) if payload.get("error") else ""
            self._render_endfield_query_result(query_type, result, error)
            return
        if kind == "skendfield_sign_finished":
            self._set_skendfield_busy(False)
            result = payload.get("result", {}) or {}
            lines = format_skendfield_sign_report(result)
            self.controller.logger.info("[森空岛] 签到完成：%s", " | ".join(lines))
            self._render_skendfield_sign_result(result)
            if payload.get("show_toast", True):
                self._toast("森空岛签到已执行完成。", self.PALETTE["accent"])
            return
        if kind == "finished":
            self._record_automation_progress()
            self.is_running = False
            self.current_queue = []
            self._set_results(payload.get("report", []), payload.get("warnings", []))
            self._set_status(payload["title"], payload["body"], payload.get("tone", "info"))
            self._refresh_summary_and_queue()
            self._toast(payload.get("toast", "本轮任务已结束。"), self.PALETTE["accent"])
            return

    _INTERNAL_APP_KEYS = frozenset({"XMFlow", "CJFlow"})

    def _record_automation_progress(self):
        if self.daily_progress_tracker is None:
            return
        for app_key, script_choice in self.current_queue:
            if app_key in self._INTERNAL_APP_KEYS:
                continue
            display_name = get_app_display_name(app_key)
            script_name = script_choice or display_name
            state = self.queue_state.get(app_key, "unknown")
            if state == "成功":
                self.daily_progress_tracker.record_script_run(
                    app_key, display_name, script_name, "success", "一条龙执行完成",
                )
            elif state == "失败":
                self.daily_progress_tracker.record_script_run(
                    app_key, display_name, script_name, "failed", "一条龙执行失败",
                )
            elif state == "已停止":
                self.daily_progress_tracker.record_script_run(
                    app_key, display_name, script_name, "skipped", "用户手动停止",
                )
            elif state in ("待执行", "运行中"):
                self.daily_progress_tracker.record_script_run(
                    app_key, display_name, script_name, "skipped", "本轮未完成",
                )
            else:
                self.daily_progress_tracker.record_script_run(
                    app_key, display_name, script_name, "failed", f"状态异常: {state}",
                )

    def _emit(self, kind: str, **payload):
        self.ui_queue.put((kind, payload))

    def _toggle_tools(self, e):
        self.tools_body.visible = not self.tools_body.visible
        self.tools_toggle.text = "收起" if self.tools_body.visible else "展开"
        self.page.update()

    def _toggle_log(self, e):
        self.log_container.visible = not self.log_container.visible
        self.log_toggle.text = "收起日志" if self.log_container.visible else "展开日志"
        self.page.update()

    def _toast(self, message: str, color: str):
        self.page.snack_bar = ft.SnackBar(content=ft.Text(message), bgcolor=color)
        self.page.snack_bar.open = True
        self.page.update()


def run_flet_gui(dep_report=None):
    try:
        if not ctypes.windll.shell32.IsUserAnAdmin():
            ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, " ".join(sys.argv), None, 1)
            sys.exit(0)
    except Exception:
        pass

    def main(page: ft.Page):
        FletGameAutoApp(page, dep_report=dep_report)

    app_view = getattr(ft, "AppView", None)
    app_kwargs = {}
    if app_view is not None and hasattr(app_view, "FLET_APP"):
        app_kwargs["view"] = app_view.FLET_APP

    if hasattr(ft, "run"):
        ft.run(main, **app_kwargs)
    else:
        ft.app(target=main, **app_kwargs)


if __name__ == "__main__":
    run_flet_gui()
