"""崩铁 March7th Assistant 体力刷本配置（config.yaml，YAML 保注释）。

配置文件：<March7th 根>/config.yaml
路径来源：app_config.apps.HonkaiStarRail.launcher_options 里名字/路径含 March7th
的启动器（_discovery.launcher_options 兜底），不写死盘符。

白名单只覆盖清体力相关键：
- 总开关 / 执行后保留计划 / 副本类型 / 各类型副本名与连续次数；
- 自动切队伍、历战余响、燃料/后备开拓力、沉浸器合成。
**不暴露 power_plan（[类型, 名称, 次数] 三元组列表）**：结构复杂，改坏会
让一条龙计划执行异常；需要时在脚本自己的 UI 里配，或用 power_plan_keep 控制保留。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Tuple

from .base import ConfigError, FieldSpec
from .yaml_adapter import YamlConfigAdapter

INSTANCE_TYPES: Tuple[str, ...] = ("拟造花萼（金）", "拟造花萼（赤）", "凝滞虚影", "侵蚀隧洞", "饰品提取")
ECHO_TYPE = "历战余响"
DAY_OPTIONS = tuple(str(i) for i in range(1, 8))
DAY_LABELS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
TEAM_OPTIONS = tuple(str(i) for i in range(1, 11))
TEAM_LABELS = tuple(f"队伍 {i}" for i in range(1, 11))
IMMERSIFIER_LIMIT_OPTIONS = tuple(str(i) for i in range(0, 9))


def find_march7th_root(app_config: Dict[str, Any]) -> Path:
    """从 app_config 的启动器选项里找 March7th 安装目录。"""
    apps = app_config.get("apps") if isinstance(app_config, dict) else {}
    app = (apps or {}).get("HonkaiStarRail")
    if not isinstance(app, dict):
        raise ConfigError("app_config.json 缺少 apps.HonkaiStarRail")
    candidates: List[Any] = list(app.get("launcher_options") or [])
    discovery = app.get("_discovery") if isinstance(app.get("_discovery"), dict) else {}
    options = discovery.get("launcher_options")
    if isinstance(options, dict):
        candidates.extend(options.values())
    elif isinstance(options, list):
        candidates.extend(options)
    for opt in candidates:
        if not isinstance(opt, dict):
            continue
        launcher = str(opt.get("launcher") or opt.get("script_exe_path") or "")
        if "march7th" not in f"{opt.get('name') or ''}{launcher}".lower() or not launcher:
            continue
        path = Path(launcher)
        return path.parent if path.suffix else path
    raise ConfigError("app_config.json 的 apps.HonkaiStarRail 没有 March7th 启动器路径")


class March7thConfigAdapter(YamlConfigAdapter):
    game_key = "honkai_sr"
    display_name = "崩铁 / March7th"
    app_key = "HonkaiStarRail"
    primary_file = "config"
    extra_process_names = ("March7th Assistant.exe", "StarRail.exe")

    def _resolve_files(self) -> Dict[str, Path]:
        return {"config": find_march7th_root(self.app_config) / "config.yaml"}

    def _fields(self, data: Dict[str, Any]) -> List[FieldSpec]:
        fields: List[FieldSpec] = [
            FieldSpec("power_enable", "启用清体力", "bool", "config", ("power_enable",),
                      help="关闭后一条龙的清体力与历战余响会跳过"),
            FieldSpec("power_plan_keep", "执行后保留体力计划", "bool", "config", ("power_plan_keep",),
                      help="关闭时按剩余次数更新并删除已完成计划"),
            FieldSpec("instance_type", "副本类型", "str", "config", ("instance_type",),
                      allow_empty=False, options=INSTANCE_TYPES,
                      help="决定下面哪一组副本名/次数生效"),
        ]
        for instance in INSTANCE_TYPES:
            fields.append(FieldSpec(
                f"name:{instance}", f"{instance} 副本名", "str", "config",
                ("instance_names", instance),
                help="与脚本内副本名一致；「无」表示不刷该类型",
                depends_on=("instance_type", (instance,)),
            ))
            fields.append(FieldSpec(
                f"count:{instance}", f"{instance} 连续次数", "int", "config",
                ("instance_names_challenge_count", instance),
                min_value=0, max_value=99,
                help="每轮连续挑战次数",
                depends_on=("instance_type", (instance,)),
            ))
        fields += [
            FieldSpec("tp_before_instance", "进本前先传送", "bool", "config", ("tp_before_instance",),
                      help="开启后先传送到任意锚点再进本"),
            FieldSpec("instance_team_enable", "自动切换队伍", "bool", "config", ("instance_team_enable",)),
            FieldSpec("instance_team_number", "队伍编号", "str", "config", ("instance_team_number",),
                      allow_empty=False, options=TEAM_OPTIONS, option_labels=TEAM_LABELS,
                      depends_on=("instance_team_enable", ("True", "true", "1"))),
            FieldSpec("echo_of_war_enable", "启用历战余响", "bool", "config", ("echo_of_war_enable",),
                      help="体力优先完成 3 次历战余响"),
            FieldSpec("echo_of_war_start_day_of_week", "历战余响开始日", "int", "config",
                      ("echo_of_war_start_day_of_week",), min_value=1, max_value=7,
                      options=DAY_OPTIONS, option_labels=DAY_LABELS,
                      help="该天起的一周内执行历战余响"),
            FieldSpec(f"name:{ECHO_TYPE}", "历战余响副本名", "str", "config",
                      ("instance_names", ECHO_TYPE)),
            FieldSpec(f"count:{ECHO_TYPE}", "历战余响次数", "int", "config",
                      ("instance_names_challenge_count", ECHO_TYPE), min_value=0, max_value=99),
            FieldSpec("use_fuel", "使用燃料", "bool", "config", ("use_fuel",),
                      help="清体力时是否消耗燃料补充开拓力"),
            FieldSpec("use_reserved_trailblaze_power", "使用后备开拓力", "bool", "config",
                      ("use_reserved_trailblaze_power",)),
            FieldSpec("merge_immersifier", "优先合成沉浸器", "bool", "config", ("merge_immersifier",)),
            FieldSpec("merge_immersifier_limit", "沉浸器上限", "str", "config",
                      ("merge_immersifier_limit",), options=IMMERSIFIER_LIMIT_OPTIONS,
                      help="合成沉浸器时的保留上限"),
        ]
        return fields
