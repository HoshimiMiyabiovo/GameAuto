"""因缘精灵（Nexus Anima，米哈游 HoYoPlay 系；暂只接入启动配置，不接脚本）。

2026-11-25 接入：登记身份、启动配置（游戏本体 exe）与磁盘扫描根；日常/刷本
脚本未接，故不配 daily_tool / daily_parser / script_tools，也不注入每日进度
占位卡片——没有日志源时占位卡会一直停在「待执行」，反而误导。

启动方式与崩铁/绝区零同形态：HoYo 系游戏本体 exe 可直接拉起（本机已登录时
无需经过 launcher 的「开始游戏」）。若哪天直启被拒绝，把 app_config 里的
apps.NexusAnima.launcher 改成安装根下的官方 launcher.exe 即可。
"""

from .base import GameProfile
from .registry import register

PROFILE = GameProfile(
    game_key="nexus_anima",
    app_key="NexusAnima",
    display_name="因缘精灵",
    order=8,
    # exe 在 <安装根>/games/NexusAnima Game/ 下，上溯 2 层到安装根
    # （H:\Program Files\Game\Nexus Anima，官方 launcher.exe 也在这层）
    disk_root_up_levels=2,
    aliases=("崩坏：因缘精灵", "Nexus Anima"),
    hint="因缘精灵当前只接入启动配置（游戏本体 + 磁盘扫描），日常脚本尚未接入。",
    default_app={
        "display_name": "因缘精灵",
        "type": "normal",
        # 模板不带机器路径：本机真实 launcher 写在 app_config.apps.NexusAnima
        # （GUI 路径配置页可改），换机/换盘只改配置不动代码
        "launcher": "",
        "launcher_params": [],
        "game": {"process_name": "NexusAnima.exe"},
        "timeouts": {"launch": 300, "monitor": 1800},
        "initial_wait": 20,
    },
)

register(PROFILE)
