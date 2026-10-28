"""无限暖暖 whimbox 一条龙体力/幻境配置（JSON）。

配置文件：<whimbox_app>/configs/config.json
OneDragon 段的值都是 {"value": ..., "description": ...} 包装，只改 value，
description 原样保留。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

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
        # 一条龙路径由 app_config 的 launcher 固定（whibox 的 bat 会 cd 到自己目录），
        # 因此永远编辑 GameAuto 实际启动的那份；UI 开哪份副本不影响这里。
        root = script_root(self.app_config, self.app_key)
        return {"config": root / "configs" / "config.json"}

    def _setting_options(self) -> Dict[str, List[str]]:
        """从 whimbox 自带 setting_options.json 读取下拉选项；缺失时用当前已知值兜底。"""
        path = (
            self._files["config"].parent.parent
            / "python-embedded"
            / "Lib"
            / "site-packages"
            / "whimbox"
            / "assets"
            / "setting_options.json"
        )
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            return {k: [str(x) for x in v] for k, v in data.items() if isinstance(v, list)}
        except Exception:
            return {}

    def _fields(self, data: Dict[str, Any]) -> List[FieldSpec]:
        base = ("OneDragon",)
        opts = self._setting_options()

        def spec(
            key: str,
            label: str,
            kind: str = "str",
            help_text: str = "",
            option_key: str = "",
        ) -> FieldSpec:
            options: Tuple[str, ...] = ()
            if option_key:
                options = tuple(opts.get(option_key, ()))
            return FieldSpec(
                key, label, kind, "config", base + (key, "value"),
                help=help_text,
                allow_empty=False if kind == "str" else True,
                options=options,
            )

        return [
            spec("energy_cost", "消耗体力的幻境",
                 help_text="一条龙优先消耗体力的幻境",
                 option_key="energy_cost"),
            spec("jihua_target", "激化幻境兑换目标",
                 help_text="素材激化幻境里换什么",
                 option_key="jihua_target"),
            spec("jihua_cost", "激化幻境消耗材料"),
            spec("jihua_cost_2", "备选材料 1"),
            spec("jihua_cost_3", "备选材料 2"),
            spec("monster_target", "魔物试炼目标",
                 option_key="monster_target"),
            spec("bless_target", "祝福闪光目标",
                 option_key="bless_target"),
            spec("realm_target", "周本目标", kind="list",
                 help_text="逗号分隔，可多选",
                 option_key="realm_target"),
        ]
