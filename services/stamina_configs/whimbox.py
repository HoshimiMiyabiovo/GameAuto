"""无限暖暖 whimbox 一条龙体力/幻境配置（JSON）。

配置文件：<whimbox_app>/configs/config.json
OneDragon 段的值都是 {"value": ..., "description": ...} 包装，只改 value，
description 原样保留。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from .base import FieldSpec, JsonConfigAdapter, script_root


class WhimboxConfigAdapter(JsonConfigAdapter):
    game_key = "nikki"
    display_name = "无限暖暖 / whimbox"
    app_key = "InfinityNikki"
    primary_file = "config"
    extra_process_names = (
        "whimbox.exe",
        "whimbox_launcher.exe",
        "X6Game-Win64-Shipping.exe",
    )

    def _resolve_files(self) -> Dict[str, Path]:
        root = script_root(self.app_config, self.app_key)
        return {"config": root / "configs" / "config.json"}

    def _fields(self, data: Dict[str, Any]) -> List[FieldSpec]:
        base = ("OneDragon",)

        def spec(key: str, label: str, kind: str = "str", help_text: str = "") -> FieldSpec:
            return FieldSpec(
                key, label, kind, "config", base + (key, "value"),
                help=help_text, allow_empty=False if kind == "str" else True,
            )

        return [
            spec("energy_cost", "消耗体力的幻境",
                 help_text="如 素材激化幻境 / 祝福闪光幻境 / 魔物试炼幻境"),
            spec("jihua_target", "激化幻境兑换目标"),
            spec("jihua_cost", "激化幻境消耗材料"),
            spec("jihua_cost_2", "备选材料 1"),
            spec("jihua_cost_3", "备选材料 2"),
            spec("monster_target", "魔物试炼目标"),
            spec("bless_target", "祝福闪光目标"),
            spec("realm_target", "周本目标", kind="list",
                 help_text="逗号分隔，如 奇格格达, 卷卷"),
        ]
