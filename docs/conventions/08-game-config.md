# 第三方脚本刷本配置与 flow_configs

> 由 AGENTS.md 拆分；改动相关代码前先读本篇。

## 第三方脚本体力刷本配置（2026-10-28 第一期）

- **页面**：`ui/qml/views/StaminaConfigView.qml`，Main.qml 侧栏「脚本刷本配置」→
  `stamina_config`（已加入 `UiBridge.switchView` 白名单）；bridge 槽
  `getStaminaConfigs()` / `saveStaminaConfig(game_key, json)` /
  `openStaminaConfigFile(game_key)`。
- **适配器**：`services/stamina_configs/`，JSON 五款：原神 BetterGI（`User/OneDragon/默认配置.json`）、
  鸣潮 ok-ww（`DailyTask.json`）、终末地 ok-ef（`DailyBattleTask.json` + `DailyTask.json` 刷体力开关）、
  异环 ok-nte（`DailyRoutineTaskConfigs.json` daily_anomaly）、暖暖 whimbox
  （`configs/config.json` OneDragon）。配置路径从 `app_config.json` 的脚本安装路径
  推导，不写死盘符。
- **下拉来源**：`FieldSpec.options` 有值且非 list 时页面用 `HusSelect`；选项分别来自
  ok-ww 源码 support_tasks/material_option_list、ok-ef 的 `world_map.json`
  stages_dict、ok-nte AnomalyTask 常量、whimbox `setting_options.json`、
  BetterGI `tp.json` MasteryDomain + AutoBoss Pathing 文件名。没有稳定枚举行的
  （如 BetterGI BOSS 队伍名、whimbox 激化材料、周本多选列表）保留输入框；
  可选字段为空时下拉带「(空/保持当前)」。
- **安全规则**：只改白名单字段；写前复制到
  `storage/cache/config_backups/<game>/<file>.<时间>.bak`；同目录临时文件 +
  `os.replace` 原子替换；脚本/游戏进程运行中拒绝写入；未知字段/越界值拒绝。
  JSON 会按 indent=2 重排，但只影响被写的文件。
- **第二期（2026-10-29 已完成，见文末「刷本配置第二期」）**：崩铁 March7th
  `config.yaml`、绝区零 ZZZ OneDragon `config/01/one_dragon/charge_plan.yml`
  + `config/01/team.yml`。YAML 必须用 `ruamel.yaml` 保留注释/顺序，
  **禁止用 PyYAML dump**（会重排 45KB 配置并丢注释）。
- **回归**：`artifacts/test_stamina_configs_20261028.py`（5 个真实配置临时副本读写、
  备份、非法值/未知字段拒绝、运行中拒绝、bridge 槽接线、Main/QML 接线）；
  `StaminaConfigView.qml` 已用 offscreen + stub bridge 实例化验证。
- **QML 列表角色坑（2026-10-28 用户截图实锤）**：`fieldModel` 不能只用动态
  `value` 角色——第一行是 bool 时 ListModel 会把后续 int/string 全部强转成 bool，
  界面出现「地脉花次数 = false」「BOSS 队伍 = false」。必须显式 `valueText`(string)
  + `boolValue`(bool) 两个角色，保存时按 kind 取对应角色。
- **HusSelect 的 model 会遮蔽外层 delegate 的 model（同日实锤）**：在
  `HusSelect` 内部写 `model.valueText` / `model.allowEmpty` 读到的是它自己的
  选项 ListModel（恒为空），导致当前值匹配不到、回显永远落到第一项「不做…」。
  正确做法：在外层 delegate 定义 `currentValue` / `allowEmpty` 属性，select 用
  `property string sourceValue: fieldCell.currentValue` 读取，`onSourceValueChanged`
  + `Qt.callLater(syncSelection)` 同步 currentIndex。文件里的值本身没坏，是纯回显坑。
- **字段布局**：字段区用 `GridLayout` 双列（窗口 <1000px 自动单列），布尔开关与
  标签同一行、非布尔字段标签在控件上方，避免整行横跨导致“串行”观感。
- **快照 / 双副本坑（2026-10-28 用户截图）**：页面只在打开或点「刷新」时读文件，
  脚本自己的 UI 改配置后不会自动同步；保存只提交 `dirty` 字段，未触碰字段不会
  被旧快照覆盖；卡片显示「有 N 项未保存」。whimbox 有两份安装/配置
  （`whimbox_app` 与 `whimbox_launcher`），**配置路径只跟 `app_config` 里
  InfinityNikki 的 launcher**：whibox 的 bat 会 `cd /d %~dp0`，所以就是
  `<launcher 所在目录>/configs/config.json`。不要按 UI 进程或运行副本切换——UI
  开哪份不影响一条龙实际跑的那份；要换副本就改 `app_config` 的 launcher。
## flow_configs 本地/示例拆分（2026-10-28）

- **背景**：`flow_configs.json` 同时含共享流程定义和 GUI 写入的个人开关
  （`enabled` / `close_process_on_finish`），之前提交个人设置会污染仓库。
- **拆分**：仓库只跟踪 `flow_configs.example.json`（默认流程定义 + 安全默认值，
  WorkBuddy/Qoder 关闭开关为 false）；`flow_configs.json` 加入 `.gitignore`，
  作为每台机器的本地文件，GUI/WebUI 只写它。
- **首次运行**：`settings.paths.ensure_flow_config_path()` 在本地文件缺失时从
  example 复制；调用点：`foreground_flow_runner._load_flow_config`、
  `ui/bridge._save_wool_flow_field`、`ui/webui/api/wool.toggle`。
- **升级注意**：共享流程定义更新只进 example；已有本地 `flow_configs.json` 不会
  自动合并，需要手动把新流程/字段补进本地文件（或删本地让 example 重新复制，
  会丢个人开关）。这是拆分换来的代价，别再把个人开关提交回仓库。
- **回归**：`artifacts/test_flow_config_split_20261028.py`（缺失复制/已有不覆盖/
  加载器回退/GUI 只写本地/example 安全默认/接线）。
## 刷本配置第二期（YAML：崩铁 March7th / 绝区零 OneDragon，2026-10-29）

- **适配器**：`services/stamina_configs/march7th.py`（`game_key=honkai_sr`）与
  `zzz_one_dragon.py`（`game_key=zzz`），注册后刷本配置页自动出 7 张卡
  （第一期 5 个 JSON + 本期 2 个 YAML）。
- **YAML 读写**：`yaml_adapter.py` 基于 **ruamel.yaml round-trip**，固定
  `mapping=2 / sequence=2 / offset=0`（这两个脚本是"顶层列表不缩进"风格）。
  **禁止用 PyYAML dump**——注释会丢、整份文件会重排。实测：charge_plan.yml
  无改动往返 0 行差异；March7th config.yaml 仅一条折叠 URL 合并为单行。
- **March7th 字段**（`<March7th 根>/config.yaml`；根目录从
  `apps.HonkaiStarRail.launcher_options` 中含 March7th 的启动器推导）：
  power_enable / power_plan_keep / instance_type（5 种副本）/ 每种类型的
  副本名与连续次数（`depends_on` 只显示选中类型，路径按类型固定，避免
  "改类型同时改名"写错槽位）/ 自动切队伍与队伍编号 / 历战余响开关与开始星期 /
  燃料与后备开拓力 / 沉浸器合成上限。**不暴露 `power_plan` 三元组列表**
  （结构复杂，改坏会中断一条龙计划）。
- **OneDragon 字段**（`<OD 根>/config/01/one_dragon/charge_plan.yml`；
  **仅支持单账户，固定 `config/01`，不支持多账户**；根目录优先取
  `apps.ZenlessZoneZero._discovery.monitors.*.log_dir` 的父目录，兜底解析
  ScriptChainer 的 `config/script_chain/*.yml` 的 script_path）：
  use_coupon / 首个计划的 **plan_target（刷本目标，可直接改）** / plan_times /
  predefined_team_idx（下拉显示 `config/01/team.yml` 的编队名）/
  auto_battle_config / level / card_num / notorious_hunt_buff_num。
  2026-10-31 起旧只读 `plan_summary` 换成 `plan_target` 下拉：选项来自
  `<OD 根>/assets/game_data/compendium_data.yml` 的「训练」页签，值是
  `分类|任务类型|关卡`，标签保留原名并附图鉴显示名（如「升温测试（火属性）」）；
  保存时拆回 `tab_name=训练 / category_name / mission_type_name / mission_name`，
  无任务类型/关卡的分类（合成电池、专业挑战室 boss）写空/None。图鉴文件缺失时
  自动退回只读 `plan_summary`，不暴露可手填的复合值。**不改 history_list**。
- **安全**：写入仍走"运行中拒绝（process_names 含脚本和游戏）→ 备份到
  `storage/cache/config_backups/<game>/` → 临时文件 + os.replace"；
  `FieldSpec.options` 非空时 str 值必须命中（`_coerce` 新增校验），
  未知字段/越界 int 一律 ConfigError。
- **依赖**：`ruamel.yaml>=0.18` 已进 requirements.txt 与 requirements.lock
  （import 映射 `ruamel.yaml → ruamel`，dependency_manager 自动识别）。
- **回归**：`tests/suite_stamina_configs_phase2_20261029.py`（合成 YAML：
  读写/注释保留/只改目标值/备份/运行中拒绝/非法枚举/越界/空计划/
  两种根目录解析/往返零差异/注册表/`plan_target` 下拉与回显/图鉴外目标拒绝/
  合成电池写空/图鉴缺失退回只读/单账户固定 config01）。实机只读验证：
  March7th 24 字段、OneDragon 7 字段均能从真实配置读出；真实图鉴生成 77 个
  刷本目标选项，当前计划回显为「实战模拟室 / 代理人技能 / 升温测试（火属性）」。
### 刷本配置页性能与编辑草稿（2026-10-31）

- `ui/qml/views/StaminaConfigView.qml` 的卡片容器必须是 **ListView**，且 model
  只绑定 `root.visibleConfigs`（当前游戏一张卡）；上方 7 个游戏按钮切换。
  不能退回 `Flickable + ColumnLayout + Repeater` 一次铺 7 张卡：离屏实测首帧
  约 2.3 秒（创建 1.38s），改单卡 ListView 后约 0.22 秒（创建 0.2s）。
- ListView delegate 必须 `reuseItems: false`：每张卡有自己的 `fieldModel`/dirty
  状态，复用会串卡。切换游戏/滚出屏幕会销毁 delegate，所以未保存编辑放在页面级
  `root.fieldDrafts`（键 `gameKey + \u0001 + fieldKey`）：bool/select/input 三个
  输入回调都必须调 `setFieldDraft`，卡片 `Component.onCompleted` 灌回草稿，保存
  成功调 `clearGameDrafts`。新增输入类型时漏掉这一步会导致切换回来编辑丢失。
- 刷新配置时先清空 `fieldDrafts` 再替换 `configs`，并按 game_key 保留当前选中
  游戏，避免旧草稿灌进新数据。
- 回归：`tests/suite_stamina_view_perf_20261031.py`（QML 离屏加载、7 个游戏按钮、
  只渲染当前 1 张卡、ListView 参数、草稿写入/读取/清理）；QML 结构改动后必须跑。
