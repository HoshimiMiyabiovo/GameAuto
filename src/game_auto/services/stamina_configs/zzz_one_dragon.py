"""绝区零 OneDragon 体力刷本配置（charge_plan.yml，YAML 保注释）。

文件：
- <OneDragon 根>/config/01/one_dragon/charge_plan.yml —— 计划（首个 plan 生效）
- <OneDragon 根>/config/01/team.yml —— 编队名/战斗配置（只读，用于下拉选项）
- <OneDragon 根>/assets/game_data/compendium_data.yml —— 图鉴数据（只读，生成刷本目标选项）

**仅支持单账户（不支持多账户）**：固定使用 `config/01`，不扫描 `config/02`、`config/03`
等其他账户目录。本项目没有多账户需求，不要为多账户增加目录枚举或账户选择器。

根目录解析（不写死盘符）：
1. app_config.apps.ZenlessZoneZero._discovery.monitors 里的 log_dir（指向 .log 目录）取父目录；
2. 兜底：ScriptChainer 的 config/script_chain/*.yml 里 script_path 指向
   OneDragon-Launcher.exe，取其所在目录。

白名单只改首个计划的刷本目标（页签/分类/关卡）、次数/队伍/战斗配置/难度/卡券/鸣徽数量
与 use_coupon；刷本目标的合法组合来自 OneDragon 图鉴数据，不能手填任意字符串。
不碰 history_list（历史记录）。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ruamel.yaml import YAML

from .base import LOGGER, ConfigError, FieldSpec
from .yaml_adapter import YamlConfigAdapter

# 仅支持单账户：固定 config/01（OneDragon 的多账户目录不扫描）
ACCOUNT_SUBDIR = "01"
PLAN_TARGET_SEP = "|"
NO_PLAN_SUMMARY = "未配置计划（不会刷本）"


def _plan_target_value(plan: Dict[str, Any]) -> str:
    """把计划的分类/任务类型/关卡拼成下拉值；空段保留，便于区分「无该级」。"""
    return PLAN_TARGET_SEP.join(
        str(plan.get(key) or "").strip()
        for key in ("category_name", "mission_type_name", "mission_name")
    )


def _split_plan_target(value: str) -> Tuple[str, str, str]:
    parts = str(value or "").split(PLAN_TARGET_SEP)
    if len(parts) != 3:
        raise ConfigError("刷本目标格式不正确")
    category, mission_type, mission_name = (part.strip() for part in parts)
    return category, mission_type, mission_name


def _join_plan_label(*parts: str) -> str:
    """拼接下拉标签，连续重复段（如「特训目标 / 特训目标」）只显示一次。"""
    labels: List[str] = []
    for part in parts:
        text = str(part or "").strip()
        if text and (not labels or labels[-1] != text):
            labels.append(text)
    return " / ".join(labels)


def _label_with_raw(raw: str, display: str) -> str:
    """图鉴显示名与配置里的原始名不同时，两个都保留：升温测试（火属性）。"""
    raw = str(raw or "").strip()
    display = str(display or "").strip() or raw
    return f"{raw}（{display}）" if raw and display != raw else (display or raw)


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
        files = {
            "charge": root / "config" / ACCOUNT_SUBDIR / "one_dragon" / "charge_plan.yml"
        }
        team = root / "config" / ACCOUNT_SUBDIR / "team.yml"
        if team.is_file():
            files["team"] = team
        return files

    def _compendium_path(self) -> Path:
        return find_one_dragon_root(self.app_config) / "assets" / "game_data" / "compendium_data.yml"

    def _compendium_categories(self) -> List[Dict[str, Any]]:
        """读取 OneDragon 图鉴里「训练」页签的分类树；读不到返回空（UI 退回只读摘要）。"""
        try:
            path = self._compendium_path()
        except ConfigError:
            return []
        if not path.is_file():
            return []
        try:
            data = YAML(typ="safe").load(path.read_text(encoding="utf-8")) or []
        except Exception as exc:
            LOGGER.warning("[刷本配置] 读取 OneDragon 图鉴数据失败 %s: %s", path, exc)
            return []
        if not isinstance(data, list):
            return []
        for tab in data:
            if not isinstance(tab, dict):
                continue
            if str(tab.get("tab_name") or "").strip() != "训练":
                continue
            categories = tab.get("category_list")
            return [item for item in categories if isinstance(item, dict)] if isinstance(categories, list) else []
        return []

    def _plan_target_entries(self, data: Dict[str, Any]) -> Tuple[List[str], List[str]]:
        """生成「刷本目标」下拉的 (值, 标签)。

        值格式：`分类|任务类型|关卡`；没有任务类型/关卡时对应段为空。
        标签用图鉴的显示名（如 升温测试 → 火属性），并在括号里保留原名以免歧义。
        当前计划若不在图鉴里（OneDragon 版本较旧），也追加为「（当前）」选项，
        保证能回显、能原样保存。
        """
        values: List[str] = []
        labels: List[str] = []
        seen = set()

        def add(value: str, label: str) -> None:
            if not value or value in seen:
                return
            seen.add(value)
            values.append(value)
            labels.append(label)

        for category in self._compendium_categories():
            category_name = str(category.get("category_name") or "").strip()
            if not category_name:
                continue
            # 体力计划里的「恶名狩猎」只跑深度追猎（普通狩猎走独立 app）
            category_display = "恶名狩猎（深度追猎）" if category_name == "恶名狩猎" else category_name
            mission_types = category.get("mission_type_list")
            if not isinstance(mission_types, list) or not mission_types:
                add(_plan_target_value({"category_name": category_name}), category_display)
                continue
            for mission_type in mission_types:
                if not isinstance(mission_type, dict):
                    continue
                type_name = str(mission_type.get("mission_type_name") or "").strip()
                if not type_name:
                    continue
                type_display = str(mission_type.get("mission_type_name_display") or type_name).strip() or type_name
                missions = mission_type.get("mission_list")
                if not isinstance(missions, list) or not missions:
                    add(
                        _plan_target_value({
                            "category_name": category_name,
                            "mission_type_name": type_name,
                        }),
                        _join_plan_label(category_display, _label_with_raw(type_name, type_display)),
                    )
                    continue
                for mission in missions:
                    if not isinstance(mission, dict):
                        continue
                    mission_name = str(mission.get("mission_name") or "").strip()
                    if not mission_name:
                        continue
                    mission_display = str(mission.get("mission_name_display") or mission_name).strip() or mission_name
                    add(
                        _plan_target_value({
                            "category_name": category_name,
                            "mission_type_name": type_name,
                            "mission_name": mission_name,
                        }),
                        _join_plan_label(
                            category_display,
                            _label_with_raw(type_name, type_display),
                            _label_with_raw(mission_name, mission_display),
                        ),
                    )

        # 图鉴整个读不到时不生成「只有当前一项」的假下拉，退回只读摘要更诚实
        if not values:
            return values, labels

        plans = (data.get("charge") or {}).get("plan_list") or []
        if plans and isinstance(plans[0], dict):
            current = _plan_target_value(plans[0])
            if current and current not in seen:
                add(
                    current,
                    "（当前）" + _join_plan_label(
                        str(plans[0].get("category_name") or ""),
                        str(plans[0].get("mission_type_name") or ""),
                        str(plans[0].get("mission_name") or ""),
                    ),
                )
        return values, labels

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
            fields.append(FieldSpec(
                "plan_summary", "当前刷本计划（只读）", "str", "charge", ("plan_list", "0"),
                readonly=True, help="未配置计划（不会刷本）；不支持多账户，固定 config/01"))
            return fields

        target_values, target_labels = self._plan_target_entries(data)
        if target_values:
            # 直接编辑刷本目标：值来自图鉴数据的合法组合，保存时拆回四个身份字段
            fields.append(FieldSpec(
                "plan_target", "刷本目标（可直接修改）", "str", "charge",
                ("plan_list", "0", "mission_name"),
                options=tuple(target_values), option_labels=tuple(target_labels),
                allow_empty=False,
                help="选项来自 OneDragon 图鉴数据；保存只改当前计划（plan_list[0]）的页签/分类/关卡，"
                     "不碰历史记录。不支持多账户，固定 config/01。"))
        else:
            # 图鉴文件缺失/损坏时退回旧行为，至少能看到当前计划
            fields.append(FieldSpec(
                "plan_summary", "当前刷本计划（只读）", "str", "charge", ("plan_list", "0"),
                readonly=True,
                help="找不到 OneDragon 图鉴数据 compendium_data.yml，只能只读展示。"
                     "不支持多账户，固定 config/01。"))

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

    def _read_value(self, data: Dict[str, Any], spec: FieldSpec) -> Any:
        if spec.key == "plan_summary":
            plans = (data.get("charge") or {}).get("plan_list") or []
            parts: List[str] = []
            for index, plan in enumerate(plans, 1):
                if not isinstance(plan, dict):
                    continue
                title = " / ".join(
                    x for x in (str(plan.get(k) or "").strip()
                                for k in ("tab_name", "category_name", "mission_type_name", "mission_name"))
                    if x)
                meta = []
                if plan.get("level"):
                    meta.append(str(plan["level"]))
                if plan.get("plan_times") is not None:
                    meta.append(f"{plan['plan_times']} 轮")
                suffix = f"（{'，'.join(meta)}）" if meta else ""
                parts.append(f"{index}. {title or '未命名计划'}{suffix}")
            return "；".join(parts) or NO_PLAN_SUMMARY
        if spec.key == "plan_target":
            plans = (data.get("charge") or {}).get("plan_list") or []
            if not plans or not isinstance(plans[0], dict):
                return ""
            return _plan_target_value(plans[0])
        return super()._read_value(data, spec)

    def _apply(self, data: Dict[str, Any], spec: FieldSpec, value: Any) -> None:
        """plan_target 是复合字段：拆回 OneDragon 的四个计划身份键。"""
        if spec.key == "plan_target":
            category, mission_type, mission_name = _split_plan_target(str(value))
            if not category:
                raise ConfigError("刷本目标必须选择分类")
            plans = (data.get(spec.file) or {}).get("plan_list") or []
            if not plans or not isinstance(plans[0], dict):
                raise ConfigError("当前配置没有可编辑的计划")
            plan = plans[0]
            # 体力计划固定挂在「训练」页签；分类/任务类型/关卡来自图鉴合法组合。
            # 无任务类型/关卡时写空（合成电池 / 专业挑战室等），OneDragon 校验要求如此。
            plan["tab_name"] = "训练"
            plan["category_name"] = category
            plan["mission_type_name"] = mission_type
            plan["mission_name"] = mission_name or None
            return
        super()._apply(data, spec, value)

    def plan_snapshot(self) -> Optional[Dict[str, Any]]:
        """只读快照：当前计划的身份字段，供 UI/日志核对改的是哪个计划。"""
        data = self._read_files()
        plans = (data.get("charge") or {}).get("plan_list") or []
        if not plans or not isinstance(plans[0], dict):
            return None
        plan = plans[0]
        return {key: plan.get(key) for key in ("tab_name", "category_name", "mission_type_name", "mission_name")}
