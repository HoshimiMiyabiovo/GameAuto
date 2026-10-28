"""鸣潮 ok-ww 每日体力任务配置（JSON）。

配置文件：<ok-ww 根>/data/apps/ok-ww/working/configs/DailyTask.json
只暴露 DailyTask 里的刷本目标/序号/材料/梦魇巢穴开关；其余任务配置不动。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from .base import FieldSpec, JsonConfigAdapter, script_root


class OkWwConfigAdapter(JsonConfigAdapter):
    game_key = "wuwa"
    display_name = "鸣潮 / ok-ww"
    app_key = "WutheringWaves"
    primary_file = "daily"
    extra_process_names = ("ok-ww.exe", "Wuthering Waves.exe")

    def _resolve_files(self) -> Dict[str, Path]:
        root = script_root(self.app_config, self.app_key)
        return {
            "daily": root / "data" / "apps" / "ok-ww" / "working" / "configs" / "DailyTask.json"
        }

    def _fields(self, data: Dict[str, Any]) -> List[FieldSpec]:
        return [
            FieldSpec(
                "which_to_farm", "刷什么", "str", "daily", ("Which to Farm",),
                help="脚本里的原文，如 Tacet Suppression / Forgery Challenge / Simulation Challenge",
                allow_empty=False,
            ),
            FieldSpec(
                "tacet_index", "无音区序号", "int", "daily",
                ("Which Tacet Suppression to Farm",), min_value=0, max_value=99,
            ),
            FieldSpec(
                "forgery_index", "锻造挑战序号", "int", "daily",
                ("Which Forgery Challenge to Farm",), min_value=0, max_value=99,
            ),
            FieldSpec(
                "material", "模拟领域材料", "str", "daily",
                ("Material Selection",), allow_empty=False,
            ),
            FieldSpec(
                "nightmare_nest", "刷梦魇巢穴", "bool", "daily",
                ("Farm Nightmare Nest for Daily Echo",),
            ),
        ]
