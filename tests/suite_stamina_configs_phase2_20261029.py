# -*- coding: utf-8 -*-
"""刷本配置第二期（YAML：March7th / OneDragon）回归（2026-10-29）。

全部用临时目录里的合成配置，不碰真实脚本配置。锁定：
1. ruamel round-trip：注释/键顺序/行内注释保留，只改白名单字段；
2. 白名单/取值校验：未知字段、越界 int、不在 options 的枚举一律拒绝；
3. 运行中拒绝写入；写前备份；写入失败不改原文件；
4. 路径解析：March7th 走 launcher_options，OneDragon 走 monitors.log_dir
   与 ScriptChainer 的 script_chain 兜底，均不写死盘符；
5. 注册表包含两个新适配器且 game_key 正确。

跑法：.venv\\Scripts\\python.exe tests\\suite_stamina_configs_phase2_20261029.py
"""

import json
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT.parent))

from game_auto.services.stamina_configs import base as stamina_base  # noqa: E402
from game_auto.services.stamina_configs.base import ConfigError  # noqa: E402
from game_auto.services.stamina_configs.march7th import (  # noqa: E402
    March7thConfigAdapter,
    find_march7th_root,
)
from game_auto.services.stamina_configs.registry import ADAPTER_CLASSES  # noqa: E402
from game_auto.services.stamina_configs.zzz_one_dragon import (  # noqa: E402
    ZzzOneDragonConfigAdapter,
    find_one_dragon_root,
)

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILURES.append(name)


M7_YAML = """\
# 副本设置
power_enable: true # 清体力总开关
instance_type: 侵蚀隧洞    # 副本类型
instance_names:
  拟造花萼（金）: 无
  拟造花萼（赤）: 「纷争荒墟」悬锋城
  凝滞虚影: 溟簇之形
  侵蚀隧洞: 密伶之径
  饰品提取: 鎏金追忆
  历战余响: 坏灭的喜剧 # 历战注释
instance_names_challenge_count:
  拟造花萼（金）: 24
  拟造花萼（赤）: 24
  凝滞虚影: 8
  侵蚀隧洞: 6
  饰品提取: 6
  历战余响: 3
tp_before_instance: false # 进本前传送
# 队伍配置
instance_team_enable: true
instance_team_number: '6'
echo_of_war_enable: true
echo_of_war_start_day_of_week: 1
use_fuel: false
use_reserved_trailblaze_power: false
merge_immersifier: false
merge_immersifier_limit: '8'
"""

CHARGE_YAML = """\
use_coupon: true # 使用快捷手册卡券
plan_list:
- tab_name: 训练
  category_name: 区域巡防
  mission_type_name: 锐牙与钝斧
  mission_name: 异常演练
  level: 默认等级
  auto_battle_config: 全配队通用
  run_times: 0
  plan_times: 1
  card_num: '3'
  predefined_team_idx: 0
  notorious_hunt_buff_num: 1
history_list:
- tab_name: 训练
  mission_name: 历史记录
"""

TEAM_YAML = """\
team_list:
- name: 编队1
  auto_battle: 全配队通用
  agent_id_list:
  - a
- name: 编队2
  auto_battle: 速刷
  agent_id_list:
  - b
"""

# OneDragon 图鉴数据（只用于生成刷本目标下拉，测试里刻意只保留少量条目）
COMPENDIUM_YAML = """\
- tab_name: "训练"
  category_list:
    - category_name: "实战模拟室"
      mission_type_list:
        - mission_type_name: "基础材料"
          mission_list:
            - mission_name: "调查专项"
              mission_name_display: "代理人经验"
        - mission_type_name: "代理人技能"
          mission_list:
            - mission_name: "升温测试"
              mission_name_display: "火属性"
    - category_name: "区域巡防"
      mission_type_list:
        - mission_type_name: "锐牙与钝斧"
          mission_type_name_display: "自由蓝调 极地重金属"
    - category_name: "合成电池"
"""


MENU_JSON = {
    "侵蚀隧洞": {"无": "跳过", "密伶之径": "伶人套 / 禁果套", "霜风之径": "冰套 / 风套"},
    "历战余响": {"无": "跳过", "坏灭的喜剧": "万色返空主"},
}


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_march7th(tmp: Path):
    print("[1] March7th 读取/写入/校验")
    config = _write(tmp / "m7" / "config.yaml", M7_YAML)
    adapter = March7thConfigAdapter(app_config={}, files={"config": config})
    # 单测不依赖本机游戏是否在运行：写入前的进程检查固定返回“未运行”
    adapter.running_reason = lambda: ""
    obj = adapter.read()
    values = obj["values"]
    check("读取 power_enable", values.get("power_enable") is True)
    check("读取 instance_type", values.get("instance_type") == "侵蚀隧洞")
    check("读取副本名", values.get("name:侵蚀隧洞") == "密伶之径", values.get("name:侵蚀隧洞"))
    check("读取连续次数", values.get("count:侵蚀隧洞") == 6)
    check("读取队伍编号", values.get("instance_team_number") == "6")
    check("读取沉浸器上限", values.get("merge_immersifier_limit") == "8")
    team_field = next(f for f in obj["fields"] if f["key"] == "instance_team_number")
    check("队伍编号是下拉", team_field["options"] and team_field["option_labels"], team_field)

    restored_dir = tmp / "backups"
    stamina_base.CONFIG_BACKUP_DIR = restored_dir
    result = adapter.write({
        "power_enable": False,
        "instance_type": "饰品提取",
        "name:饰品提取": "新饰品",
        "count:饰品提取": 3,
    })
    text = config.read_text(encoding="utf-8")
    check("写入成功", result.get("ok") is True)
    check("已备份", bool(result.get("backups")) and Path(result["backups"][0]).is_file())
    check("注释保留（清体力总开关）", "# 清体力总开关" in text)
    check("注释保留（副本类型）", "# 副本类型" in text)
    check("行内注释保留（历战）", "# 历战注释" in text)
    check("只改目标值", "power_enable: false" in text and "饰品提取: 新饰品" in text)
    check("无关字段保持原样", "tp_before_instance: false # 进本前传送" in text)
    check("键顺序不变", text.index("power_enable") < text.index("instance_names") < text.index("tp_before_instance"))

    try:
        adapter.write({"instance_type": "不存在的副本"})
        check("非法枚举被拒绝", False)
    except ConfigError:
        check("非法枚举被拒绝", True)

    adapter.running_reason = lambda: "March7th Assistant.exe 正在运行"
    before = config.read_text(encoding="utf-8")
    try:
        adapter.write({"power_enable": True})
        check("运行中拒绝写入", False)
    except ConfigError:
        check("运行中拒绝写入", config.read_text(encoding="utf-8") == before)

    try:
        March7thConfigAdapter(app_config={}, files={"config": tmp / "missing.yaml"}).read()
        check("缺失文件报错", False)
    except ConfigError:
        check("缺失文件报错", True)

    print("[2] March7th 路径解析")
    app_cfg = {"apps": {"HonkaiStarRail": {"launcher_options": [
        {"name": "March7th", "launcher": str(tmp / "m7" / "March7th Assistant.exe")},
    ]}}}
    check("launcher_options 解析根目录", find_march7th_root(app_cfg) == (tmp / "m7"))
    try:
        find_march7th_root({"apps": {"HonkaiStarRail": {}}})
        check("无路径时报错", False)
    except ConfigError:
        check("无路径时报错", True)


def test_one_dragon(tmp: Path):
    print("[3] OneDragon 读取/写入/校验")
    root = tmp / "od"
    charge = _write(root / "config" / "01" / "one_dragon" / "charge_plan.yml", CHARGE_YAML)
    _write(root / "config" / "01" / "team.yml", TEAM_YAML)
    _write(root / "assets" / "game_data" / "compendium_data.yml", COMPENDIUM_YAML)
    (root / ".log").mkdir()
    app_cfg = {"apps": {"ZenlessZoneZero": {"_discovery": {
        "monitors": {"zzz_daily_log_monitor": {"log_dir": str(root / ".log")}},
    }}}}
    check("log_dir 解析根目录", find_one_dragon_root(app_cfg) == root)

    adapter = ZzzOneDragonConfigAdapter(app_config=app_cfg)
    adapter.running_reason = lambda: ""
    obj = adapter.read()
    values = obj["values"]
    check("读取 use_coupon", values.get("use_coupon") is True)
    check("读取 plan_times", values.get("plan_times") == 1)
    check("读取编队索引", values.get("predefined_team_idx") == 0)
    check("读取战斗配置", values.get("auto_battle_config") == "全配队通用")
    team_field = next(f for f in obj["fields"] if f["key"] == "predefined_team_idx")
    check("编队下拉来自 team.yml", team_field["option_labels"] == ["编队1", "编队2"], team_field)
    battle_field = next(f for f in obj["fields"] if f["key"] == "auto_battle_config")
    check("战斗配置选项去重", set(battle_field["options"]) == {"全配队通用", "速刷"})
    check("计划快照", adapter.plan_snapshot().get("mission_name") == "异常演练")

    target_field = next(f for f in obj["fields"] if f["key"] == "plan_target")
    check("刷本目标可直接修改", target_field["readonly"] is False)
    check("刷本目标下拉来自图鉴", len(target_field["options"]) >= 4, target_field["options"])
    check("当前计划目标回显",
          values.get("plan_target") == "区域巡防|锐牙与钝斧|异常演练", values.get("plan_target"))
    check("图鉴外的当前目标也保留可回显",
          values.get("plan_target") in target_field["options"], target_field["options"])
    check("下拉标签带图鉴显示名",
          any("升温测试（火属性）" in label for label in target_field["option_labels"]),
          target_field["option_labels"])
    check("配置路径固定 config/01",
          "01" in Path(obj["files"]["charge"]).parts, obj["files"]["charge"])

    stamina_base.CONFIG_BACKUP_DIR = tmp / "backups"
    result = adapter.write({
        "plan_times": 2,
        "predefined_team_idx": 1,
        "auto_battle_config": "速刷",
        "plan_target": "实战模拟室|代理人技能|升温测试",
    })
    text = charge.read_text(encoding="utf-8")
    check("写入成功", result.get("ok") is True)
    check("注释保留", "# 使用快捷手册卡券" in text)
    check("只改目标值", "plan_times: 2" in text and "predefined_team_idx: 1" in text)
    check("刷本目标写入分类", "category_name: 实战模拟室" in text, text[:400])
    check("刷本目标写入任务类型", "mission_type_name: 代理人技能" in text)
    check("刷本目标写入关卡", "mission_name: 升温测试" in text)
    check("刷本目标固定训练页签", "tab_name: 训练" in text)
    check("history_list 未动", "mission_name: 历史记录" in text)
    check("文本顺序不变", text.index("use_coupon") < text.index("plan_list") < text.index("history_list"))

    # 无任务类型/关卡的分类（合成电池）要写空，OneDragon 校验要求如此
    adapter.write({"plan_target": "合成电池||"})
    saved_plan = adapter._read_files()["charge"]["plan_list"][0]
    check("合成电池任务类型为空", saved_plan.get("mission_type_name") == "", saved_plan)
    check("合成电池关卡为空", saved_plan.get("mission_name") is None, saved_plan)

    try:
        adapter.write({"plan_times": -1})
        check("越界 int 被拒绝", False)
    except ConfigError:
        check("越界 int 被拒绝", True)
    try:
        adapter.write({"not_a_field": 1})
        check("未知字段被拒绝", False)
    except ConfigError:
        check("未知字段被拒绝", True)
    try:
        adapter.write({"plan_target": "不存在的分类||"})
        check("图鉴外的刷本目标被拒绝", False)
    except ConfigError:
        check("图鉴外的刷本目标被拒绝", True)

    empty = _write(tmp / "od_empty" / "config" / "01" / "one_dragon" / "charge_plan.yml",
                   "use_coupon: false\nplan_list: []\n")
    empty_adapter = ZzzOneDragonConfigAdapter(app_config={}, files={"charge": empty})
    empty_obj = empty_adapter.read()
    check("空计划只剩 use_coupon 与只读摘要",
          [f["key"] for f in empty_obj["fields"]] == ["use_coupon", "plan_summary"])
    check("空计划摘要提示不刷本",
          "不会刷本" in (empty_obj["values"].get("plan_summary") or ""))

    # 图鉴文件缺失：退回只读摘要，不把复合值暴露成可手填文本框
    plain_root = tmp / "od_plain"
    plain_charge = _write(plain_root / "config" / "01" / "one_dragon" / "charge_plan.yml", CHARGE_YAML)
    plain_cfg = {"apps": {"ZenlessZoneZero": {"_discovery": {
        "monitors": {"zzz_daily_log_monitor": {"log_dir": str(plain_root / ".log")}},
    }}}}
    plain_adapter = ZzzOneDragonConfigAdapter(app_config=plain_cfg)
    plain_adapter.running_reason = lambda: ""
    plain_obj = plain_adapter.read()
    plain_fields = {f["key"]: f for f in plain_obj["fields"]}
    check("图鉴缺失时退回只读摘要", "plan_summary" in plain_fields and "plan_target" not in plain_fields,
          list(plain_fields))
    check("图鉴缺失时摘要仍可读",
          "异常演练" in (plain_obj["values"].get("plan_summary") or ""),
          plain_obj["values"].get("plan_summary"))

    print("[4] OneDragon 根目录兜底（ScriptChainer 链）")
    chain_root = tmp / "chain"
    _write(chain_root / "config" / "script_chain" / "1.yml",
           "script_list:\n- script_path: %s\n" % (tmp / "od" / "OneDragon-Launcher.exe"))
    chain_cfg = {"apps": {"ZenlessZoneZero": {"launcher_options": [
        {"name": "OneDragonScriptChainer", "launcher": str(chain_root / "Runner.exe")},
    ]}}}
    check("script_chain 解析根目录", find_one_dragon_root(chain_cfg) == (tmp / "od"))


def test_roundtrip_and_registry(tmp: Path):
    print("[5] ruamel 往返与注册表")
    for name, text in (("m7.yaml", M7_YAML), ("od.yaml", CHARGE_YAML)):
        path = _write(tmp / "rt" / name, text)
        adapter = March7thConfigAdapter(app_config={}, files={"config": path})
        data = adapter._read_files()
        import io
        buf = io.StringIO()
        adapter._yaml().dump(data["config"], buf)
        check(f"{name} 无改动往返零差异", buf.getvalue() == text)

    source = (PROJECT_ROOT / "services" / "stamina_configs" / "yaml_adapter.py").read_text(encoding="utf-8")
    check("YAML 适配器使用 ruamel", "from ruamel.yaml import YAML" in source)
    check("未使用 PyYAML dump", "import yaml" not in source and "yaml.dump(" not in source)

    zzz_source = (PROJECT_ROOT / "services" / "stamina_configs" / "zzz_one_dragon.py").read_text(encoding="utf-8")
    check("OneDragon 固定单账户 config/01", 'ACCOUNT_SUBDIR = "01"' in zzz_source)
    check("OneDragon 文档手写不支持多账户", "不支持多账户" in zzz_source)

    keys = {cls.game_key for cls in ADAPTER_CLASSES}
    check("注册表含 7 个适配器", len(ADAPTER_CLASSES) == 7, str(keys))
    check("注册 honkai_sr", "honkai_sr" in keys)
    check("注册 zzz", "zzz" in keys)


def test_instance_menu(tmp: Path):
    print("[6] March7th 副本名下拉（读脚本名单）")
    root = tmp / "m7menu"
    config = _write(root / "config.yaml", M7_YAML)
    _write(root / "assets" / "config" / "instance_names.json",
           json.dumps(MENU_JSON, ensure_ascii=False))
    adapter = March7thConfigAdapter(app_config={}, files={"config": config})
    adapter.running_reason = lambda: ""
    fields = {f["key"]: f for f in adapter.read()["fields"]}
    tunnel = fields["name:侵蚀隧洞"]
    check("侵蚀隧洞下拉含当前值", "密伶之径" in tunnel["options"], tunnel["options"])
    check("下拉标签带产出说明", any("伶人套" in label for label in tunnel["option_labels"]))
    check("历战余响下拉含副本", "坏灭的喜剧" in fields["name:历战余响"]["options"])

    plain = _write(tmp / "m7plain" / "config.yaml", M7_YAML)
    plain_fields = {f["key"]: f for f in March7thConfigAdapter(
        app_config={}, files={"config": plain}).read()["fields"]}
    check("名单文件缺失时退回手填", plain_fields["name:侵蚀隧洞"]["options"] == [])

    old = _write(root / "config_old.yaml",
                 M7_YAML.replace("侵蚀隧洞: 密伶之径", "侵蚀隧洞: 旧副本"))
    old_fields = {f["key"]: f for f in March7thConfigAdapter(
        app_config={}, files={"config": old}).read()["fields"]}
    check("名单外旧值补进下拉", "旧副本" in old_fields["name:侵蚀隧洞"]["options"])
    check("旧值标签标注当前配置",
          any("当前配置" in label for label in old_fields["name:侵蚀隧洞"]["option_labels"]))

    stamina_base.CONFIG_BACKUP_DIR = tmp / "backups_menu"
    result = adapter.write({"name:侵蚀隧洞": "霜风之径"})
    check("下拉值可保存",
          result.get("ok") is True and "侵蚀隧洞: 霜风之径" in config.read_text(encoding="utf-8"))
    try:
        adapter.write({"name:侵蚀隧洞": "不存在的径"})
        check("下拉外的手填值被拒绝", False)
    except ConfigError:
        check("下拉外的手填值被拒绝", True)


def main():
    with tempfile.TemporaryDirectory() as temp:
        tmp = Path(temp)
        test_march7th(tmp)
        test_one_dragon(tmp)
        test_roundtrip_and_registry(tmp)
        test_instance_menu(tmp)
    print("-" * 60)
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} -> {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
