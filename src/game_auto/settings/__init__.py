import copy
from typing import Dict, Optional

from . import manager as cm
from .discovery import apply_runtime_discovery
from .defaults import (
    APP_KEY_ALIASES,
    DEFAULT_APPS,
    DEFAULT_APP_ORDER,
    DEFAULT_QBITTORRENT,
    DEFAULT_SCRIPT_TOOLS,
    GAME_KEYS,
    APP_KEY_TO_GAME_KEY,
    GAME_KEY_TO_SIGN_SECTION,
)
from .paths import LOG_DIR as PROJECT_LOG_DIR, LOG_FILE as PROJECT_LOG_FILE, WORKFLOW_DB_PATH as PROJECT_WORKFLOW_DB_PATH
from ..services.launch_task_catalog import merge_launch_apps


LOG_DIR = str(PROJECT_LOG_DIR)
LOG_FILE = str(PROJECT_LOG_FILE)
WORKFLOW_DB_PATH = str(PROJECT_WORKFLOW_DB_PATH)

MIN_RUN_TIME = 300
CHECK_INTERVAL = 10
# 游戏收到关闭指令后，进程仍在优雅关闭（存盘/清理）的宽限秒数。
# 超过该宽限仍无可见窗口，则强制结束游戏与脚本进程树，避免进程在后台残留过久。
GAME_CLOSE_GRACE_SEC = 20
# 游戏关闭后某些游戏会重新弹出原生启动器/更新窗，该进程不在游戏进程树内，
# 若残留会抢焦点/挡点击干扰下一个脚本。收尾阶段扫杀它的轮询等待秒数。
POST_CLOSE_LAUNCHER_WAIT = 4
DEBUG_MODE = True
# 0 = 永久保留历史日志（TimedRotatingFileHandler 在 backupCount<=0 时不删除旧文件）
BACKUP_COUNT = 0

APP_ORDER = list(DEFAULT_APP_ORDER)
QBITTORRENT: Dict[str, str] = copy.deepcopy(DEFAULT_QBITTORRENT)
SCRIPT_TOOLS: Dict = copy.deepcopy(DEFAULT_SCRIPT_TOOLS)
APPS: Dict[str, Dict] = copy.deepcopy(DEFAULT_APPS)


def apply_runtime_config(config: Optional[Dict] = None) -> Dict:
    runtime_config = copy.deepcopy(config or cm.load())
    runtime_config = apply_runtime_discovery(runtime_config)
    cfg_order = list(runtime_config.get("app_order") or DEFAULT_APP_ORDER)
    cfg_apps = copy.deepcopy(runtime_config.get("apps") or DEFAULT_APPS)
    # 补入 DEFAULT_APPS 中存在但 app_config.json 中缺失的新游戏，
    # 这样新增游戏无需手动修改 app_config.json 即可出现在 GUI 列表
    for app_key, app_cfg in DEFAULT_APPS.items():
        if app_key not in cfg_apps:
            cfg_apps[app_key] = copy.deepcopy(app_cfg)
        if app_key not in cfg_order:
            cfg_order.append(app_key)
    # 补入新游戏后，再补每个应用 launcher_options 里新增的选项（2026-11-07
    # OneDragonDirect 教训）：_merge_defaults 对 list 是整体覆盖，旧配置文件
    # 里已有的 launcher_options 会整体替换模板的新列表，导致模板新增的选项
    # 在老配置上「左右脑不一致」（GUI 下拉看不到）。按 name 合并：模板有、
    # 配置无的补进去；配置已有的原样保留（用户填过的路径不丢）。
    for app_key, app_cfg in DEFAULT_APPS.items():
        tmpl_opts = app_cfg.get("launcher_options")
        if not tmpl_opts:
            continue
        cfg_opts = cfg_apps.setdefault(app_key, {}).setdefault("launcher_options", [])
        cfg_names = {o.get("name") for o in cfg_opts if isinstance(o, dict)}
        for tmpl_opt in tmpl_opts:
            if isinstance(tmpl_opt, dict) and tmpl_opt.get("name") not in cfg_names:
                cfg_opts.append(copy.deepcopy(tmpl_opt))
    app_order, apps = merge_launch_apps(cfg_order, cfg_apps)
    # 已下线任务：彻底移除归龙潮，避免旧配置残留仍显示
    apps.pop("GuiLongChao", None)
    if "GuiLongChao" in app_order:
        app_order = [k for k in app_order if k != "GuiLongChao"]
    qbittorrent = runtime_config.get("qbittorrent") or DEFAULT_QBITTORRENT
    script_tools = runtime_config.get("script_tools") or DEFAULT_SCRIPT_TOOLS

    runtime_config["app_order"] = list(app_order)
    runtime_config["apps"] = copy.deepcopy(apps)

    APP_ORDER[:] = list(app_order)

    # deepcopy 先行、clear+update 相邻：模块全局 dict 是被各处 `from ... import
    # APPS` 持引用消费的，不能整体换引用，只能原地替换。把耗时的深拷贝放
    # clear 之前，把"读者可能看到空/半新 dict"的窗口压到两条相邻 C 级 dict
    # 操作之间（2026-11-09 审计 P2；此前 deepcopy 在 clear 之后，空窗秒级）。
    new_apps = copy.deepcopy(apps)
    new_qbt = copy.deepcopy(qbittorrent)
    new_st = copy.deepcopy(script_tools)

    QBITTORRENT.clear()
    QBITTORRENT.update(new_qbt)

    SCRIPT_TOOLS.clear()
    SCRIPT_TOOLS.update(new_st)

    APPS.clear()
    APPS.update(new_apps)
    return runtime_config


def refresh_runtime_config(force: bool = False) -> Dict:
    return apply_runtime_config(cm.load(force=force))


refresh_runtime_config()


def get_app_display_name(app_key: str) -> str:
    app_key = resolve_app_key(app_key)
    cfg = APPS.get(app_key, {})
    return cfg.get("display_name", app_key)


def get_script_display_name(app_key: str, script_choice: Optional[str] = None) -> str:
    app_key = resolve_app_key(app_key)
    cfg = APPS.get(app_key, {})
    if script_choice and "launcher_options" in cfg:
        opt = next((o for o in cfg["launcher_options"] if o["name"] == script_choice), None)
        if opt:
            return opt.get("display_name", opt["name"])
    return cfg.get("script_name", "默认脚本")


def resolve_app_key(app_key: str) -> str:
    """
    将历史 key / 大小写变体统一解析为当前标准 key。
    """
    if app_key in APPS:
        return app_key
    if app_key in APP_KEY_ALIASES:
        return APP_KEY_ALIASES[app_key]
    lowered = str(app_key).lower()
    if lowered in APP_KEY_ALIASES:
        return APP_KEY_ALIASES[lowered]
    return app_key


def resolve_game_key(any_key: str) -> str:
    """
    将任意 key（大驼峰 app_key、小写 game_key、别名）统一解析为小写式 game_key。

    例：
      resolve_game_key("GenshinImpact") → "genshin"
      resolve_game_key("genshin")       → "genshin"
      resolve_game_key("InfinityNikki") → "nikki"
      resolve_game_key("nikki")         → "nikki"
    """
    if any_key in GAME_KEYS:
        return any_key
    if any_key in APP_KEY_TO_GAME_KEY:
        return APP_KEY_TO_GAME_KEY[any_key]
    # 通过 app_key 别名间接转换
    resolved_app_key = resolve_app_key(any_key)
    if resolved_app_key in APP_KEY_TO_GAME_KEY:
        return APP_KEY_TO_GAME_KEY[resolved_app_key]
    return any_key