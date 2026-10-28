"""原神 BetterGI 一条龙体力配置（JSON）。

配置文件：<BetterGI 根>/User/OneDragon/默认配置.json
一条龙任务开关在 TaskEnabledList（UUID → bool），任务名在 TaskDefinitions
（UUID → 名称）；这里按名称找 UUID，避免把 UUID 写死。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .base import FieldSpec, JsonConfigAdapter, script_root

# 与体力/刷本直接相关的一条龙任务
STAMINA_TASKS = ("每日地脉花", "自动地脉花", "自动秘境", "自动幽境危战")


class BetterGIConfigAdapter(JsonConfigAdapter):
    game_key = "genshin"
    display_name = "原神 / BetterGI"
    app_key = "GenshinImpact"
    primary_file = "onedragon"
    extra_process_names = ("BetterGI.exe", "YuanShen.exe")

    def _resolve_files(self) -> Dict[str, Path]:
        root = script_root(self.app_config, self.app_key)
        return {"onedragon": root / "User" / "OneDragon" / "默认配置.json"}

    def _boss_options(self) -> Tuple[str, ...]:
        """从 BetterGI 的 AutoBoss 寻路资源文件名提取 BOSS 名；缺失时用已知列表。"""
        root = self._files["onedragon"].parent.parent.parent
        path = root / "GameTask" / "AutoBoss" / "Assets" / "Pathing"
        names: List[str] = []
        try:
            for file in sorted(path.glob("*.json")):
                name = file.stem
                for suffix in ("战斗后快速前往", "强制传送", "键鼠前往", "前往"):
                    if name.endswith(suffix):
                        name = name[: -len(suffix)]
                        break
                if name and name not in names:
                    names.append(name)
        except Exception:
            pass
        if names:
            return tuple(names)
        return (
            "无相之雷", "无相之火", "无相之水", "无相之草", "无相之岩",
            "急冻树", "爆炎树", "掣电树", "纯水精灵", "魔偶剑鬼",
            "古岩龙蜥", "遗迹巨蛇", "雷音权现", "风蚀沙虫", "深罪浸礼者",
        )

    def _domain_options(self) -> Tuple[str, ...]:
        """从 BetterGI tp.json 的 MasteryDomain 提取秘境名。"""
        root = self._files["onedragon"].parent.parent.parent
        path = root / "GameTask" / "AutoTrackPath" / "Assets" / "tp.json"
        names: List[str] = []

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                if node.get("type") == "MasteryDomain" and node.get("name"):
                    name = str(node["name"])
                    if name not in names:
                        names.append(name)
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        try:
            walk(json.loads(path.read_text(encoding="utf-8-sig")))
        except Exception:
            pass
        if names:
            return tuple(sorted(names))
        return ("荒坠的圣迹", "无光的深都", "山风的荆冕")

    def _fields(self, data: Dict[str, Any]) -> List[FieldSpec]:
        onedragon = data.get("onedragon") or {}
        definitions = onedragon.get("TaskDefinitions") or {}
        fields: List[FieldSpec] = []
        if isinstance(definitions, dict):
            for task_name in STAMINA_TASKS:
                for uid, name in definitions.items():
                    if str(name) == task_name:
                        fields.append(
                            FieldSpec(
                                key=f"task:{uid}",
                                label=f"启用「{task_name}」",
                                kind="bool",
                                file="onedragon",
                                path=("TaskEnabledList", str(uid)),
                                help="BetterGI 一条龙任务开关",
                            )
                        )
                        break
        fields += [
            FieldSpec(
                "domain_name", "秘境名称", "str", "onedragon", ("DomainName",),
                help="自动秘境 / 每日秘境使用的副本名",
                options=self._domain_options(),
            ),
            FieldSpec(
                "weekly_domain_enabled", "启用每周秘境", "bool", "onedragon",
                ("WeeklyDomainEnabled",),
            ),
            FieldSpec(
                "ley_line_run_count", "地脉花次数", "int", "onedragon",
                ("LeyLineRunCount",), min_value=0, max_value=99,
                help="0 表示按树脂耗尽/脚本默认逻辑",
            ),
            FieldSpec(
                "ley_line_resin_exhaustion", "地脉花刷到树脂耗尽", "bool",
                "onedragon", ("LeyLineResinExhaustionMode",),
            ),
            FieldSpec(
                "min_resin_keep", "保留树脂下限", "int", "onedragon",
                ("MinResinToKeep",), min_value=0, max_value=2000,
            ),
            FieldSpec(
                "auto_boss_name", "自动 BOSS 名称", "str", "onedragon",
                ("AutoBossName",), help="BetterGI AutoBoss 支持的 BOSS 名",
                options=self._boss_options(),
            ),
            FieldSpec(
                "auto_boss_team", "自动 BOSS 队伍", "str", "onedragon",
                ("AutoBossTeamName",), help="留空表示使用脚本当前配置",
            ),
            FieldSpec(
                "auto_boss_specify_count", "指定 BOSS 次数", "bool", "onedragon",
                ("AutoBossSpecifyRunCount",),
            ),
            FieldSpec(
                "auto_boss_run_count", "BOSS 次数", "int", "onedragon",
                ("AutoBossRunCount",), min_value=0, max_value=99,
            ),
            FieldSpec(
                "auto_boss_use_transient", "BOSS 使用须臾树脂", "bool",
                "onedragon", ("AutoBossUseTransientResin",),
            ),
            FieldSpec(
                "auto_boss_use_fragile", "BOSS 使用脆弱树脂", "bool",
                "onedragon", ("AutoBossUseFragileResin",),
            ),
        ]
        return fields
