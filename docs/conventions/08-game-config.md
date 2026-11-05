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

## 第三方脚本版本检测与更新提示（2026-11-03）

- **插件契约**：`games/base.py` 的 `ScriptToolSpec(label, repo, option_name)` +
  `GameProfile.script_tools`。**新增游戏有第三方脚本时在这里填元数据**，不要另立
  映射表：`label` 展示名；`repo` 填 GitHub `owner/repo`，**留空 = 只查本地版本**
  （上游仓库不明的，如绝区零 ScriptChainer）；多方案游戏（崩铁/绝区零）用
  `option_name` 匹配 `launcher_options[*].name`，单方案留空走顶层 `launcher`。
- **检测链路**：`services/script_version_check.py`，安装路径解析（正式字段 →
  `_discovery` 回落）→ 本地版本（按 spec 策略，见下表）→ GitHub
  `releases/latest` 取 tag → 数字版本元组比较；本地未知只展示不判"有更新"。
- **本地版本真值表（2026-11-03 实盘查证，`artifacts/probe_version_sources.py`）**：
  软件原地自更新**不会改安装目录名**，目录名只对"无自更新 + 按版本号命名
  zip 分发"的工具可信。逐工具：
  | 工具 | 本地版本来源 | 查证依据 |
  |---|---|---|
  | BetterGI | exe ProductVersion（`trust_exe_version`） | 目录无版本；更新器替换 exe，元数据跟真版本 |
  | March7th | `assets/config/version.txt`（`version_file`） | 目录名 v2.2.0.3 已过期，version.txt 实际 v2026.10.31 |
  | ok-ww/ok-ef/ok-nte | `data/apps/<exe名>/app.json` 的 `current_version`（`pyappify`） | pyappify 安装器自更新清单；exe 元数据是框架版本（1.2.3/0.1.0/1.2.5 全是假），目录名是初次安装版本 |
  | ScriptChainer | 目录名正则（`trust_dir_version`） | 按版本号命名 zip 分发、无自更新 |
  | OneDragon 一条龙系 / whimbox | 无（显示"本地版本未知"） | OneDragon 版本跟 git 走；whimbox 在 Electron asar 内，不值得解析 |
  新增/换工具时先实盘确认真值来源再填 spec，**不要默认目录名可信**——
  首版实现吃过亏：4 个"有更新"里 3 个是过期目录名误报。

## 脚本管理页：自动下载安装（2026-11-03）

- **页面**：侧边栏「脚本管理」（`tool_manager`），`views/ToolManagerView.qml` +
  bridge mixin `ui/bridge_views/tool_manager.py`；配置向导页版本卡片对
  可下载且未安装的工具有「一键安装」按钮（首启可选自动下载，不强制）。
- **下载范围**（逐仓库实查 release 资产后定，别凭印象扩）：
  ok-ww/ok-ef/ok-nte（pyappify 在线包 zip ~5MB，首启自补全运行时）、
  March7th（7z 474MB 优先、zip 719MB 兜底）、BetterGI（7z 465MB）。
  OneDragon 系/whimbox/ScriptChainer **不支持自动安装**（包结构复杂/只有
  whl/无上游），页面只给「打开发布页」。资产正则必须写窄：ok-nte 另有
  full/body 两个 300MB+ zip，宽正则会下错包。
- **安装器**：`services/script_tool_installer.py`。流式下载（256KB 分片、
  分片级 stop_check、.part 原子改名）→ ok 系 sha256 清单对账 → 解压
  （zip stdlib + **zip-slip 成员校验**；7z py7zr + 成员名检查）→ 单一顶层
  目录剥离 → 按名定位启动器（限深 3）→ 换血落位 → `register_launcher_path`
  双写 apps + `_discovery` 并 refresh_runtime_config（同 saveSetupPaths 写法）。
- **安全规则**：脚本/游戏进程运行中拒绝（script_process_name +
  game.process_name + cleanup_cmd_patterns 三路查，pyappify 引擎必须查
  命令行）；**托管目录外的已有安装拒绝改动**（自动安装只管理
  `data/script_tools/<tool_key>` 或 overrides 指定目录）；磁盘余量 ≥ 2.5×
  包体积；更新 = 旧目录改名 `.old` → staging 就位 → 删 `.old`（失败留
  `.old` 手工恢复，日志写明）。
- **配置**：`app_config.json` 的 `script_tools` 段
  （`install_root` 空 = `paths.SCRIPT_TOOLS_DEFAULT_DIR`；`overrides` 按
  `ScriptToolSpec.tool_key` 覆盖单工具目录），defaults/manager/runtime 三处
  按 qbittorrent 范本接。新工具接入 checklist：实查 release 资产 →
  spec 填 `tool_key/asset_patterns/launcher_name` → 确认解压后
  launcher_name 能定位 → 实装一次验证路径注册与版本识别。
- **依赖**：`py7zr>=0.21`（BetterGI/March7th 只有 7z 包），import 名同为
  py7zr。
- **网络现实（2026-11-04 实测）**：GitHub API（api.github.com）与下载 CDN
  （objects.githubusercontent.com）可达性是两回事——本机 API 稳定、CDN 被
  RST/握手超时。下载失败已包装成带"稍后重试或发布页手动下载"指引的
  RuntimeError；Mirror酱/代理源在"明确不做"清单里，要做另起需求。
- **回归**：`tests/suite_tool_manager_20261103.py`（资产正则/zip-slip/7z
  往返/安装编排全流程 mock/换血/守卫/取消清理/下载与 sha/桥接与 QML 冒烟）。
- **缓存与提醒**：`data/cache/script_versions.json`，每仓库 TTL 24h，网络失败
  沿用旧值并标 `stale`；`notified` 记录实现"同版本只 toast 一次"（手动检查
  也会记账但不弹 toast；页面缓存加载不联网、不消费提醒额度）。未认证 GitHub
  API 限速 60 次/时，9 个仓库一天一查。
- **UI**：配置向导页"脚本版本 / 更新"卡片；bridge 数据推送走
  `script_versions` 事件分支 + `scriptVersionsJson` / `scriptCheckRunning`
  属性；启动 5s 静默检查（`bridge.__init__` 的 `QTimer.singleShot`）。
- **回归**：`tests/suite_script_version_check_20261103.py`（版本解析/路径提取/
  配置解析/TTL/stale 回退/notified 去重/本地未知不误报/桥接与 QML 接线/
  离屏 stub-bridge 冒烟）。仓库 slug 变更（脚本换家）只改 games 档案里的 repo。

## ok-ww v3.7.13 上游锁定与工坊域名（2026-11-04）

- **主程序锁 v3.7.13 不升级**（上游 #1824 零氪封号 + v3.7.16/17 砍角色代码/多账号
  tab），`data/apps/ok-ww/app.json` 的 `update_method` 已写死 `MANUAL_UPDATE`。
  **没有任何静默重开路径**：应用源码不调 `update_app_config`，壳每次启动从盘读
  app.json，pyappify 里两处 AUTO 只是读盘前的内存初值。会回到 AUTO 的只有三种
  **显式动作**：手改 app.json、壳 UI 里切换、删 app.json 重装（缺省即 AUTO）——
  换机部署用 `D:\backup\ok-ww\ok-ww_v3.7.13_full_20261104.zip`（内层 app.json 同样
  已是 MANUAL_UPDATE）。
- **工坊域名现状**：`cfokwwchar.ok-script.com` 被上游删 DNS（NXDOMAIN），数据本体
  在 `okwwcharcode.ok-script.com`（= 公网仓库 `ok-oldking/ok-ww-char-code` 的
  GitHub Pages；`/teams.json`、`/teams/{slug}.json`、`/codes/*.zip` 路径与 JSON
  结构和 v3.7.13 解析器完全兼容，49 队/69 轴，仍在活跃更新）。
- **本地补丁**：v3.7.13 安装内 `src/gui/CharacterCodeTab.py` 的
  `WORKSHOP_TEAMS_ALL_URL` / `WORKSHOP_TEAM_URL` / `WORKSHOP_ARCHIVE_HOSTS` 三个
  常量已改指新域名（`repo/` 与 `working/` 两份同改，改前各有
  `CharacterCodeTab.py.bak_20261104`；`_import_workshop_code` 的旧域名重写对
  新域名是恒等映射，保留不动）。**重装/更新冲掉补丁后两份都要重打**；离线兜底
  是本地整仓镜像重导（重导只丢行尾，`Hsin_h2_r2_gap_0.5.patch` 可机械恢复）。
- **版本检测口径**：`games/wuwa.py` 的 ScriptTool repo 仍指向
  `ok-oldking/ok-wuthering-waves`，本地 v3.7.13 vs 上游最新（2026-11-04 当日
  三连发至 v3.7.18，全是收缩性变更：砍 tab/下载/赞助、ok-script 3.x、改为
  游戏退出后才查更新），脚本管理页会一直显示「有新版本」——这是**有意保留的
  观测窗口**（盯上游动向），不是误报；勿据此升级，也勿为消红把 repo 置空。
