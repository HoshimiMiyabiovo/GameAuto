"""终末地 ok-ef 体力刷本配置（JSON）。

配置文件：
- data/apps/ok-ef/working/configs/DailyBattleTask.json（刷哪个本/档位/次数/队伍）
- data/apps/ok-ef/working/configs/DailyTask.json（战斗任务里是否启用「刷体力」）
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from .base import FieldSpec, JsonConfigAdapter, script_root

STAMINA_TASK_NAME = "刷体力"


class OkEfConfigAdapter(JsonConfigAdapter):
    game_key = "endfield"
    display_name = "终末地 / ok-ef"
    app_key = "ArknightsEndfield"
    primary_file = "battle"
    extra_process_names = ("ok-ef.exe", "Endfield.exe")

    def _resolve_files(self) -> Dict[str, Path]:
        root = script_root(self.app_config, self.app_key)
        base = root / "data" / "apps" / "ok-ef" / "working" / "configs"
        return {
            "battle": base / "DailyBattleTask.json",
            "daily": base / "DailyTask.json",
        }

    def _fields(self, data: Dict[str, Any]) -> List[FieldSpec]:
        return [
            FieldSpec(
                "enable_stamina", "启用「刷体力」任务", "bool", "daily",
                ("战斗任务",), help="对应 DailyTask.json 战斗任务列表里的「刷体力」",
            ),
            FieldSpec(
                "stamina_instance", "体力本", "str", "battle", ("体力本",),
                help="必须是 ok-ef 里的副本名原文", allow_empty=False,
            ),
            FieldSpec(
                "stamina_tier", "奖励档位", "str", "battle",
                ("体力本奖励档位",), help="如 高阶", allow_empty=False,
            ),
            FieldSpec(
                "use_limited_stamina", "消耗限时体力药", "bool", "battle",
                ("消耗限时体力药",),
            ),
            FieldSpec(
                "extra_farm_count", "刷完后继续次数", "int", "battle",
                ("体力刷完后继续刷取次数",), min_value=0, max_value=999,
            ),
            FieldSpec(
                "team", "指定队伍编号", "str", "battle",
                ("指定的队伍编号",), help="如 不换队伍 / 1 / 2", allow_empty=False,
            ),
        ]

    def _read_value(self, data: Dict[str, Any], spec: FieldSpec) -> Any:
        if spec.key == "enable_stamina":
            tasks = (data.get("daily") or {}).get("战斗任务") or []
            return STAMINA_TASK_NAME in [str(x) for x in tasks]
        return super()._read_value(data, spec)

    def _apply(self, data: Dict[str, Any], spec: FieldSpec, value: Any) -> None:
        if spec.key == "enable_stamina":
            daily = data.get("daily")
            if not isinstance(daily, dict):
                raise KeyError("DailyTask.json 根节点不是对象")
            tasks = daily.setdefault("战斗任务", [])
            if not isinstance(tasks, list):
                raise TypeError("战斗任务 不是列表")
            tasks[:] = [x for x in tasks if str(x) != STAMINA_TASK_NAME]
            if value:
                tasks.append(STAMINA_TASK_NAME)
            return
        super()._apply(data, spec, value)
