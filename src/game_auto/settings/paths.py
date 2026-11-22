from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path

LOGGER = logging.getLogger("GameAutomation")


# src 布局：settings/paths.py → settings → game_auto → src → 仓库根
BASE_DIR = Path(__file__).resolve().parents[3]
APP_CONFIG_PATH = BASE_DIR / "app_config.json"
# 运行时数据统一放 data/（日志/失败截图/数据库/缓存），根目录只留源码与资源。
# 旧版本把 logs/artifacts/workflow.sqlite3/storage/cache 直接放在根目录，
# 迁移脚本：tools/migrate_data_layout_20261029.py（一次性，迁移后本注释仍有效）。
DATA_DIR = BASE_DIR / "data"


# ── 测试进程 / 生产数据识别（2026-11-22）──────────────────────────────
# 起因：套件用假 hwnd 跑流程，把假失败写进 flow_failures（138 条没有截图的
# "失败"）、把假脚本进度写进 daily_progress（日报里凭空多出 wuwa/script_script）。
# 单元套件里凡是"写生产状态"的入口都按这两句判：
#     if is_test_process() and is_production_data_path(目标路径): 跳过写
# 放 paths 是因为 runtime / services / storage 三层都要用，而 paths 是三者唯一
# 的共同下游（services 不能反向依赖 runtime）。
_TEST_MODE_ENV_KEYS = ("GAMEAUTO_TEST_MODE", "GAME_AUTO_TEST_MODE")
_TEST_TRUE_VALUES = ("1", "true", "yes", "on")


def is_test_process() -> bool:
    """当前进程是不是单元套件 / pytest（测试桩的写入不该进生产库）。

    识别三路，任一命中即视为测试进程：
      ① pytest 用例内（PYTEST_CURRENT_TEST，pytest 自己维护的变量）；
      ② 显式 GAMEAUTO_TEST_MODE=1（CI / test_suites 子进程可统一设置）；
      ③ 直接跑套件（argv[0] 在 tests/ 下且文件名以 suite_ / test_ 开头）。

    生产的 GUI / 定时任务 argv[0] 是启动脚本或 exe，不会命中；宁可测试期
    少记一次，也不让桩数据混进生产库（真机写入由正式进程照常落）。
    """
    try:
        if os.environ.get("PYTEST_CURRENT_TEST"):
            return True
        for key in _TEST_MODE_ENV_KEYS:
            if str(os.environ.get(key, "")).strip().lower() in _TEST_TRUE_VALUES:
                return True
        argv0 = str((sys.argv or [""])[0] or "")
        if argv0:
            path = os.path.abspath(argv0)
            parent = os.path.basename(os.path.dirname(path))
            name = os.path.basename(path).lower()
            if parent == "tests" and (name.startswith("suite_") or name.startswith("test_")):
                return True
    except Exception:
        return False
    return False


def is_production_data_path(path) -> bool:
    """path 是否落在生产数据目录（<仓库>/data）下。

    套件把目标路径改写成 tmp 时返回 False —— 那正是我们允许测试写库的方式：
    "测试进程写**临时**库"合法，"测试进程写**生产**库"一律拦。
    """
    try:
        return Path(str(path)).resolve().is_relative_to(DATA_DIR.resolve())
    except Exception:
        return False

LOG_DIR = DATA_DIR / "logs"
LOG_FILE = LOG_DIR / "auto_runner.log"
# 看门狗栈转储按故障类型分文件（原先两类硬故障混在一个 watchdog.log，事后没法区分）
# 主线程无心跳（GUI 卡死）：hang_watchdog 写这个
WATCHDOG_MAIN_LOG_FILE = LOG_DIR / "watchdog_main_thread.log"
# 任务线程无心跳（任务卡死）：task_heartbeat_watchdog 写这个
WATCHDOG_TASK_LOG_FILE = LOG_DIR / "watchdog_task_heartbeat.log"
TOOLS_DIR = BASE_DIR / "tools"
TESSERACT_DIR = TOOLS_DIR / "tesseract"
TESSERACT_EXE = TESSERACT_DIR / "tesseract.exe"
TESSDATA_DIR = TESSERACT_DIR / "tessdata"
# 资源目录：图像模板与固定点击序列（相对代码，随仓库迁移）
ASSETS_DIR = BASE_DIR / "assets"
TEMPLATES_DIR = ASSETS_DIR / "templates"
ARTIFACTS_DIR = DATA_DIR / "artifacts"
OCR_DEBUG_DIR = ARTIFACTS_DIR / "ocr_debug"
FLOW_CONFIG_PATH = BASE_DIR / "flow_configs.json"
# 仓库里只提交默认流程定义；flow_configs.json 是每台机器的本地配置（GUI 会写
# 薅羊毛开关/完成后关闭等个人字段），已加入 .gitignore。首次运行没有本地文件时
# 由 ensure_flow_config_path() 从 example 复制一份。
FLOW_CONFIG_EXAMPLE_PATH = BASE_DIR / "flow_configs.example.json"


def ensure_flow_config_path() -> Path:
    """返回本地 flow_configs.json 路径；缺失时从 example 复制，不覆盖已有文件。"""
    if FLOW_CONFIG_PATH.is_file():
        return FLOW_CONFIG_PATH
    if FLOW_CONFIG_EXAMPLE_PATH.is_file():
        try:
            shutil.copy2(FLOW_CONFIG_EXAMPLE_PATH, FLOW_CONFIG_PATH)
        except Exception as exc:
            LOGGER.warning("[Paths] flow_configs.json 初始化复制失败: %s", exc)
    return FLOW_CONFIG_PATH


# flow_configs.json 的读-改-写互斥锁：GUI（bridge_views/automation.
# _save_wool_flow_field）与 WebUI（api/wool.toggle）并发写时后写者整文档
# 覆盖先写者，读改写全程必须持锁（2026-11-09 审计 P2-1 同族）。
# app_config.json 的对应锁在 settings.manager（_CONFIG_LOCK），别混用。
import threading as _threading  # noqa: E402  - 放在路径常量旁便于两个写方共同引用
FLOW_CONFIG_LOCK = _threading.Lock()

WORKFLOW_DB_PATH = DATA_DIR / "workflow.sqlite3"
# 固定点击序列（录制/回放）存储目录
CLICK_SEQUENCES_DIR = ASSETS_DIR / "click_sequences"
# 已集成进一条龙的序列，禁止在 GUI 误删（白荆回廊日常/验证/周常）
PROTECTED_CLICK_SEQUENCES = frozenset({
    "白荆回廊-日常",
    "白荆回廊-验证",
    "白荆回廊-每周商店购买",
})
# 受保护序列的备份目录（用于误删后恢复）
PROTECTED_BACKUP_DIR = CLICK_SEQUENCES_DIR / ".protected_backup"
CACHE_DIR = DATA_DIR / "cache"
# 第三方脚本自动安装的默认根目录（app_config.script_tools.install_root 为空时）
SCRIPT_TOOLS_DEFAULT_DIR = DATA_DIR / "script_tools"
# 第三方脚本配置修改前的备份（每次写入前复制，不覆盖历史）
CONFIG_BACKUP_DIR = CACHE_DIR / "config_backups"
# 终末地森空岛查询结果的本地缓存（进页面即时展示，过期后后台刷新）
ENDFIELD_SKLAND_CACHE = CACHE_DIR / "endfield_skland.json"
# 鸣潮库街区每日数据的本地缓存（进页面即时展示，过期后后台刷新）
WUWA_DAILY_CACHE = CACHE_DIR / "wuwa_daily.json"
# 白荆回廊验证截图目录（WGC 截取，仅保留最近 7 天）
BAIJING_VERIFY_DIR = ARTIFACTS_DIR / "baijing_verify"
# 失败证据目录（失败当刻的窗口截图，配套 workflow.sqlite3 的 flow_failures 表）
FLOW_FAILURE_DIR = ARTIFACTS_DIR / "flow_failures"
# 失败截图保留天数（写入时顺手按 mtime 清理，避免无限堆积）
FLOW_FAILURE_KEEP_DAYS = 14
# 每月商店采购证据（采购前/后各一张 + index.jsonl），长期保留不自动清理：
# 每月只跑一次、单张 40~80KB，"看不到商品算已买过"的判定要靠它事后复核。
MONTHLY_SHOP_EVIDENCE_DIR = ARTIFACTS_DIR / "monthly_shop"
# 全局热键游戏截图（services/game_shot_service.py）：用户手动触发、长期保留。
# 不要复用 FLOW_FAILURE_DIR——那边按 14 天滚动清理，会删掉用户自己截的图。
GAME_SHOT_DIR = ARTIFACTS_DIR / "game_shots"
ASSETS_DIR = BASE_DIR / "assets"
FLET_EXAMPLES_DIR = TOOLS_DIR / "flet" / "sdk" / "python" / "examples"
