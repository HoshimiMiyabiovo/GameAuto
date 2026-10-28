from __future__ import annotations

from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
APP_CONFIG_PATH = BASE_DIR / "app_config.json"
LOG_DIR = BASE_DIR / "logs"
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
TEMPLATES_DIR = BASE_DIR / "模板"
ARTIFACTS_DIR = BASE_DIR / "artifacts"
OCR_DEBUG_DIR = ARTIFACTS_DIR / "ocr_debug"
FLOW_CONFIG_PATH = BASE_DIR / "flow_configs.json"
WORKFLOW_DB_PATH = BASE_DIR / "workflow.sqlite3"
# 固定点击序列（录制/回放）存储目录
CLICK_SEQUENCES_DIR = BASE_DIR / "click_sequences"
# 已集成进一条龙的序列，禁止在 GUI 误删（白荆回廊日常/验证/周常）
PROTECTED_CLICK_SEQUENCES = frozenset({
    "白荆回廊-日常",
    "白荆回廊-验证",
    "白荆回廊-每周商店购买",
})
# 受保护序列的备份目录（用于误删后恢复）
PROTECTED_BACKUP_DIR = CLICK_SEQUENCES_DIR / ".protected_backup"
CACHE_DIR = BASE_DIR / "storage" / "cache"
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
ASSETS_DIR = BASE_DIR / "assets"
FLET_EXAMPLES_DIR = TOOLS_DIR / "flet" / "sdk" / "python" / "examples"