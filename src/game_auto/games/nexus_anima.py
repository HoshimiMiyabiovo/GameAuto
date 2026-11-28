"""因缘精灵（Nexus Anima，米哈游 HoYoPlay 系）。

2026-11-25 接入启动配置；2026-11-28 接入 ok-hna（独立 ok-script 3.0.13 工程）：
ok-hna 自带 LauncherTask（启动→登录→主世界）与 DailyActivityTask（日常活跃→
消耗精力作战→福利领取），game_auto 用 headless module runner 驱动
DailyActivityTask，按 runner 结果判定，不再裸启动游戏干等 30 分钟监控超时。

模块模式默认关闭；本机 `app_config.apps.NexusAnima.module_mode` 启用并填
ok-hna 的 `.venv` / runtime_root（机器路径不进仓库）。ok-hna 要求管理员权限：
游戏是管理员进程，游戏内 UI 只接受 Pynput 真实输入，GameAuto/runner 需要同级
完整性；非管理员启动时 ok-hna 会明确报「需要管理员权限」而不是假成功。

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
    hint="因缘精灵使用 ok-hna（启动→日常活跃→福利一条龙），需管理员权限。",
    default_app={
        "display_name": "因缘精灵",
        "script_name": "ok-hna",
        "type": "normal",
        # 模板不带机器路径：本机真实 launcher 写在 app_config.apps.NexusAnima
        # （GUI 路径配置页可改），换机/换盘只改配置不动代码
        "launcher": "",
        "launcher_params": [],
        "game": {"process_name": "NexusAnima.exe"},
        "timeouts": {"launch": 300, "monitor": 1800},
        "initial_wait": 5,
        # 阶段 1：headless ok-script runner + ok-hna DailyActivityTask。
        # 默认关闭；本机 app_config 启用并填 runtime_root/python_exe。
        # timeout=2400s 是 controller 硬上限，runner 内部同值任务超时先报；
        # stall_log_sec=720s 高于 LauncherTask 最长 600s 的静默等待，
        # 真卡死（日志 12 分钟无新增）先熔断，不会无限等。
        "module_mode": {
            "enabled": False,
            "runtime_name": "ok-hna",
            "task": "DailyActivityTask",
            "timeout": 2400,
            "close_game": True,
            "stall_log_sec": 720,
            "require_admin": True,
            "runtime_root": "",
            "python_exe": "",
            "humanize_config": "",
        },
    },
)

register(PROFILE)
