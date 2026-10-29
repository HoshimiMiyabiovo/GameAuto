"""绝区零 OneDragon 体力刷本配置（charge_plan.yml，YAML 保注释）。

文件：
- <OneDragon 根>/config/01/one_dragon/charge_plan.yml —— 计划（首个 plan 生效）
- <OneDragon 根>/config/01/team.yml —— 编队名/战斗配置（只读，用于下拉选项）

根目录解析（不写死盘符）：
1. app_config.apps.ZenlessZoneZero._discovery.monitors 里的 log_dir（指向 .log 目录）取父目录；
2. 兜底：ScriptChainer 的 config/script_chain/*.yml 里 script_path 指向
   OneDragon-Launcher.exe，取其所在目录。

白名单只改首个计划的次数/队伍/战斗配置/难度/卡券/鸣徽数量与 use_coupon；
**不改 mission/tab/category**（计划身份，改错会对不上游戏内副本），
也不碰 history_list（历史记录）。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .base import ConfigError, FieldSpec
from .yaml_adapter import YamlConfigAdapter


def _candidate_launchers(app_config: Dict[str, Any]) -> List[Dict[str, Any]]:
    apps = app_config.get("apps") if isinstance(app_config, dict) else {}
    app = (apps or {}).get("ZenlessZoneZero")
    if not isinstance(app, dict):
        return []
    out: List[Dict[str, Any]] = [o for o in (app.get("launcher_options") or []) if isinstance(o, dict)]
    discovery = app.get("_discovery") if isinstance(app.get("_discovery"), dict) else {}
    options = discovery.get("launcher_options")
    if isinstance(options, dict):
        out.extend(o for o in options.values() if isinstance(o, dict))
    elif isinstance(options, list):
        out.extend(o for o in options if isinstance(o, dict))
    return out


def find_one_dragon_root(app_config: Dict[str, Any]) -> Path:
    apps = app_config.get("apps") if isinstance(app_config, dict) else {}
    app = (apps or {}).get("ZenlessZoneZero")
    if not isinstance(app, dict):
        raise ConfigError("app_config.json 缺少 apps.ZenlessZoneZero")
    discovery = app.get("_discovery") if isinstance(app.get("_discovery"), dict) else {}
    monitors = discovery.get("monitors") if isinstance(discovery.get("monitors"), dict) else {}
    for monitor in monitors.values():
        if not isinstance(monitor, dict):
            continue
        log_dir = str(monitor.get("log_dir") or "")
        if log_dir:
            path = Path(log_dir)
            return path.parent if path.name.startswith(".") else path
    for launcher in _candidate_launchers(app_config):
        exe = str(launcher.get("launcher") or launcher.get("script_exe_path") or "")
        if not exe:
            continue
        chain_dir = Path(exe).parent / "config" / "script_chain"
        for chain in sorted(chain_dir.glob("*.yml")) if chain_dir.is_dir() else []:
            try:
                match = re.search(r"(?m)^\s*(?:-\s*)?script_path:\s*(.+?)\s*$", chain.read_text(encoding="utf-8"))
            except OSError:
                continue
            if not match:
                continue
            target = Path(match.group(1).strip().strip('"'))
            return target.parent
    raise ConfigError("app_config.json 中找不到 OneDragon 根目录（log_dir 或 ScriptChainer 链路）")


class ZzzOneDragonConfigAdapter(YamlConfigAdapter):
    game_key = "zzz"
    display_name = "绝区零 / OneDragon"
    app_key = "ZenlessZoneZero"
    primary_file = "charge"
    extra_process_names = (
        "OneDragon ScriptChainer Runner.exe",
        "OneDragon-Launcher.exe",
        "ZenlessZoneZero.exe",
    )

    def _resolve_files(self) -> Dict[str, Path]:
        root = find_one_dragon_root(self.app_config)
        files = {"charge": root / "config" / "01" / "one_dragon" / "charge_plan.yml"}
        team = root / "config" / "01" / "team.yml"
        if team.is_file():
            files["team"] = team
        return files

    def _team_options(self, data: Dict[str, Any]) -> Tuple[Tuple[str, ...], Tuple[str, ...], Tuple[str, ...]]:
        team = data.get("team") or {}
        teams = team.get("team_list") if isinstance(team, dict) else None
        names: List[str] = []
        battles: List[str] = []
        for item in teams or []:
            if not isinstance(item, dict):
                continue
            if item.get("name"):
                names.append(str(item["name"]))
            if item.get("auto_battle"):
                battles.append(str(item["auto_battle"]))
        return (
            tuple(str(i) for i in range(len(names))),
            tuple(names),
            tuple(sorted(set(battles))),
        )

    def _fields(self, data: Dict[str, Any]) -> List[FieldSpec]:
        fields: List[FieldSpec] = [
            FieldSpec("use_coupon", "使用快捷手册卡券", "bool", "charge", ("use_coupon",),
                      help="有卡券时优先消耗"),
        ]
        charge = data.get("charge") or {}
        plans = charge.get("plan_list") if isinstance(charge, dict) else None
        if not plans:
            return fields
        team_options, team_labels, battle_options = self._team_options(data)
        fields += [
            FieldSpec("plan_times", "计划执行次数", "int", "charge", ("plan_list", "0", "plan_times"),
                      min_value=0, max_value=99, help="当前首个计划的执行轮数"),
            FieldSpec("predefined_team_idx", "编队编号", "int", "charge",
                      ("plan_list", "0", "predefined_team_idx"), min_value=0, max_value=9,
                      options=team_options, option_labels=team_labels,
                      help="从 team.yml 的编队列表选择（下拉显示编队名）"),
            FieldSpec("auto_battle_config", "自动战斗配置", "str", "charge",
                      ("plan_list", "0", "auto_battle_config"), options=battle_options,
                      help="选项来自 team.yml 各编队的 auto_battle"),
            FieldSpec("level", "难度", "str", "charge", ("plan_list", "0", "level"),
                      allow_empty=False, help="如「默认等级」，需与游戏内难度名一致"),
            FieldSpec("card_num", "快捷手册卡券数", "str", "charge", ("plan_list", "0", "card_num"),
                      help="该计划使用的卡券数量"),
            FieldSpec("notorious_hunt_buff_num", "鸣徽数量", "int", "charge",
                      ("plan_list", "0", "notorious_hunt_buff_num"), min_value=0, max_value=9,
                      help="区域巡防等玩法的鸣徽加成数量"),
        ]
        return fields

    def plan_snapshot(self) -> Optional[Dict[str, Any]]:
        """只读快照：当前计划的身份字段，供 UI/日志核对改的是哪个计划。"""
        data = self._read_files()
        plans = (data.get("charge") or {}).get("plan_list") or []
        if not plans or not isinstance(plans[0], dict):
            return None
        plan = plans[0]
        return {key: plan.get(key) for key in ("tab_name", "category_name", "mission_type_name", "mission_name")}
