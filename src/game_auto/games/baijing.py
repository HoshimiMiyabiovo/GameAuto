"""白荆回廊（内置模板流程，无外部日志源）。"""

from .base import GameProfile
from .registry import register

PROFILE = GameProfile(
    game_key="baijing",
    app_key="BaiJing",
    display_name="白荆回廊",
    order=7,
    aliases=("bai_jing",),
    # 没有任何外部脚本日志：永远注入占位卡片，执行结果由内置流程写库
    always_placeholder=True,
    empty_daily="game_auto.games.baijing:build_empty_daily",
    hint="白荆回廊使用内置模板流程（启动器 + 进入游戏 + 日常点击序列）。",
    default_app={
        "display_name": "白荆回廊",
        "type": "normal",
        "launcher": "D:\\Program Files\\Game\\Seed_OB_Prod\\1.1.1.7\\Seed_OB_Prod.exe",
        "launcher_params": [],
        "game": {"process_name": "GateMK-Win64-Shipping.exe"},
        "timeouts": {"launch": 300, "monitor": 1800},
        "initial_wait": 5,
        "post_launch_click_game": True,
        "post_launch_click_timeout": 8,
        "post_launch_ingame_flow": "baijing",
    },
)


def build_empty_daily():
    """今日无记录时注入占位，避免每日进度里看不到白荆卡片。"""
    return {
        "game_key": "baijing",
        "game_name": "白荆回廊",
        "uid": "",
        "nickname": "",
        "is_all_success": False,
        "has_failed": False,
        "progress_percent": 0,
        "tasks": [
            {"task_key": "enter_game", "task_name": "进入游戏", "status": "pending", "message": "待一条龙执行", "current": None, "total": None},
            {"task_key": "daily", "task_name": "日常任务", "status": "pending", "message": "待执行", "current": None, "total": None},
            {"task_key": "verify", "task_name": "验证", "status": "pending", "message": "待验证截图", "current": None, "total": None, "verify_image": None, "verify_image_url": None, "extra": {}},
            {"task_key": "weekly", "task_name": "每周商店购买", "status": "pending", "message": "待周常（每周一次）", "current": None, "total": None},
            {"task_key": "fitting_weekly", "task_name": "拟合回归（每周）", "status": "pending", "message": "待每周活动（外勤→拟合回归→挂机→领奖）", "current": None, "total": None},
            {"task_key": "monthly", "task_name": "每月有猫兑换", "status": "pending", "message": "待月度（每月一次，购买清单待定）", "current": None, "total": None},
        ],
        "errors": [],
    }


register(PROFILE)
