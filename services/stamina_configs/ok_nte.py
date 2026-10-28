"""异环 ok-nte 每日活跃/体力任务配置（JSON）。

配置文件：data/apps/ok-nte/working/configs/DailyRoutineTaskConfigs.json
一条龙 DailyRoutineTask 使用这里的 daily_anomaly 配置耗尽体力目标。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from .base import FieldSpec, JsonConfigAdapter, script_root


class OkNteConfigAdapter(JsonConfigAdapter):
    game_key = "nte"
    display_name = "异环 / ok-nte"
    app_key = "NevernessToEverness"
    primary_file = "routine"
    extra_process_names = ("ok-nte.exe", "HTGame.exe")

    def _resolve_files(self) -> Dict[str, Path]:
        root = script_root(self.app_config, self.app_key)
        return {
            "routine": root
            / "data"
            / "apps"
            / "ok-nte"
            / "working"
            / "configs"
            / "DailyRoutineTaskConfigs.json"
        }

    def _fields(self, data: Dict[str, Any]) -> List[FieldSpec]:
        prefix = ("daily_anomaly",)
        return [
            FieldSpec(
                "stamina_cost", "目标消耗体力", "int", "routine",
                prefix + ("目标消耗体力",), min_value=0, max_value=999,
            ),
            FieldSpec(
                "task_type", "任务类型", "str", "routine",
                prefix + ("任务类型",), help="如 经验与甲硬币", allow_empty=False,
            ),
            FieldSpec(
                "reward_target", "具体奖励目标", "str", "routine",
                prefix + ("具体奖励目标",), help="如 角色经验", allow_empty=False,
            ),
            FieldSpec(
                "material_index", "异能材料序号", "int", "routine",
                prefix + ("异能材料序号",), min_value=0, max_value=99,
            ),
            FieldSpec(
                "disc_index", "弧盘材料序号", "int", "routine",
                prefix + ("弧盘材料序号",), min_value=0, max_value=99,
            ),
            FieldSpec(
                "hollow_index", "空幕序号", "int", "routine",
                prefix + ("空幕序号",), min_value=0, max_value=99,
            ),
        ]
