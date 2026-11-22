# 工作流、依赖、日志、注释与工程基线

> 由 AGENTS.md 拆分；改动相关代码前先读本篇。

## Execution Workflow

- Before modifying code, adding a module, or changing a runtime flow, write a brief plan first. The plan should name the target file or symbol, the local hypothesis, and the first validation step.
- Prefer the smallest grounded edit. If behavior is still uncertain, make a reversible local change instead of widening scope early.
- After the first substantive edit, immediately run the narrowest validation available for the touched slice before doing more edits. For this repo that usually means diagnostics on touched files, plus a focused import or config check when interfaces changed.
- Before finishing, proactively inspect new errors in every touched file and resolve issues introduced by the change instead of stopping at a successful patch.
- When a task changes Python imports, module boundaries, or startup/runtime wiring, do at least one focused static validation beyond visual inspection.
## Dependency Rules

- When adding a new Python dependency, update **requirements.txt only**（唯一人工清单，2026-10-29 起）。
- dependency_manager.py 运行时解析 requirements.txt 生成 import 名映射（`REQUIRED_PYTHON_PACKAGES` 是推导值，不要手改）；只有「包名与 import 名不同」的例外才登记 `IMPORT_NAME_BY_PACKAGE`。启动**只检查不安装**，安装统一走 setup_deps.bat——详见下文「工程化基线」，旧的双登记/自动安装口径已废弃。
## Logging Rules

- New UI flows and new data/task flows should write logs to the GameAutomation logger.
- Prefer logs at user action boundaries and IO boundaries: start, key step, success summary, failure reason.
- Do not write full secrets such as cookies or tokens into logs. Use masked account identifiers or short summaries instead.
- **日志长期保留，不自动删除**：`logs/auto_runner.log` 按天轮转，`log.backup_count=0` 表示永久保留历史文件；禁止把默认值改回正数或新增日志清理逻辑。看门狗 dump 超过 5MB 只归档为带时间戳的副本，不覆盖旧内容。
- **禁止无说明的 `except Exception: pass`**：预期异常用窄类型并写明原因；非预期异常至少记 `logger.debug`；只有日志/信号基础设施自身失败才允许静默，且必须注释说明（2026-10-28 已清理 123 处）。
## Scope Notes

- Startup sign behavior should be shared through startup_sign_runner.py rather than duplicated separately in QML and CLI flows.
- Keep project-visible notes like this file concise and focused on repeated engineering pitfalls.

## 配置写入规则（2026-11-09 审计 P1-1 起）

- **app_config.json 的所有读-改-写必须走 `settings.manager` 锁内入口**：单键/单段写用 `update_*` 系列，任意段的复合读改写用 `update_config(mutator)`（mutator 直接改传入 dict）。模块自带线程锁，`load()` 返回深拷贝（改返回值不影响缓存）。
- **禁止** `cfg = load(); cfg[...] = ...; save(cfg)` 两段式（两次持锁之间会放锁，跨线程后写者用旧快照整文档覆盖先写者）；**禁止**绕过 manager 直接 `atomic_write_json(APP_CONFIG_PATH, ...)` / 自管加解密链——startup_sign_runner / daily_checker / email_reporter / webui.auth / scheduler 曾各自绕行，2026-11-09 已全部收敛，新代码别开新洞。
- `flow_configs.json` 的读-改-写（GUI `_save_wool_flow_field` / WebUI `api/wool.toggle`）全程持 `settings.paths.FLOW_CONFIG_LOCK`，新写方同样要持锁。
- 跨进程（`maintenance/settings_gui.py`）保存前必须 `cm.load(force=True)` 从盘重读，否则本进程陈旧快照整文档回滚主 GUI 的写入；进程级互斥（msvcrt 文件锁）尚未实现，settings_gui 只改 qbittorrent/log/global 段，别扩到 scheduled_tasks/凭据段。
- **维护执行器必须取互斥锁**（2026-11-10 起）：`generic_workflow_runner` / `screen_task_runner` 入口调 `services.instance_lock.acquire_aux_lock("maintenance")`，拿不到即拒绝执行——两边都是实点击抢前台，与生产 GUI 批处理并发会互相把对方窗口切到前台、点击落进对方流程。新增同类"实操作"维护脚本一律先取锁。
## 代码注释规范（2026-10-24 起分阶段补全）

分阶段给自有代码补详细注释，**只加注释不改任何逻辑**，每文件完成后 py_compile 验证。
- 语言一律中文；风格对齐 AGENTS.md：讲"为什么/约束/坑"，不复述代码字面意思。
- 模块 docstring：职责、数据流（从哪来到哪去）、与其他模块的关系、不变量与坑。
- 公共函数 docstring：契约（参数/返回/副作用/失败模式）。
- 行内注释只标非直观处：魔法数字来源、顺序约束、竞态窗口、踩过的坑。
- **排除**：`flet/`（内嵌第三方框架）、`ui/flet_app.py` + `ui/flet/`（已停维护）、
  `data/reference/temp_project/`（参考项目副本，随 data/ 忽略）、`artifacts/`（测试已带注释）。
- 阶段：①判定与数据核心（tracker/task_batch_runner/discovery/解析器）
  ②任务执行层（controller/flow_runner/ocr_tester/click_sequence/process_supervisor）
  ③客户端 API 层 ④启动器与诊断 ⑤bridge/workflow_db/webui。
### 阶段进度（2026-10-24）

- **阶段① 已完成**：daily_progress_tracker（模块数据流/UPSERT 语义/判定契约/
  is_all_success 陷阱）、discovery（四层瀑布/包装器识别/缓存回读/安全边界）、
  task_batch_runner（模块总览/run_task_batch/_run_single_task 分支地图/三个
  数据结构/三个 verdict 助手）、7 个解析器（_today_key/查找函数一行注释；
  模块 docstring 原本就齐）。全部 py_compile 通过，done_today/nte_dayvalue
  回归 ALL PASS。
- 踩坑：批量插 docstring 的脚本必须按 def 的实际缩进插入（类方法 8 空格、
  嵌套函数更深）——已两次修错缩进，后续阶段（bridge 大量类方法）务必注意。
- 阶段②起从 automation_controller.py 开始（执行层与 batch_runner 互为表里）。

--------------------------------------------------------------------------------
## 工程化基线（2026-10-29 起）

- **测试目录**：纯逻辑回归套件统一在 `tests/suite_*.py`（从 `artifacts/test_*.py`
  迁入，文件名日期后缀不变；本文旧段里出现的 `artifacts/test_xxx_日期.py` 按此映射到
  `tests/suite_xxx_日期.py`）。`tests/test_suites.py` 逐个子进程跑，隔离各套件的
  monkeypatch；新增套件按 `suite_*.py` 命名即自动纳入，并必须登记 `tests/ci_suites.txt`。
  - **套件禁写生产库（2026-11-22 起）**：套件用假 hwnd（`1`/`1234`）调流程时，
    失败取证要自己打桩（`mock.patch.object(模块, "_record_failure")`）；代码侧另有
    兜底——`is_test_process() and is_production_data_path(目标路径)` 同时成立才
    拒写（目标改成临时库照写，套件自建库测 DB 是合法用法）。已铺三处：
    `flow_diagnostics.record_failure`（不截图不写库）、`DailyProgressTracker.record_task`
    （假进度：`wuwa/script_script` 就是这么进日报的）、`WorkflowDB.set_task_meta`
    （月度/周常门控）。起因：白荆套件往生产库写了 138 条 hwnd 假的"没有截图的
    失败"，用户对着 GUI「最近失败记录」问「这是什么情况，也不留证据」。
  - **沙箱临时目录垫片（2026-11-22 起）**：DSH workspace-write 沙箱下
    `tempfile.mkdtemp/TemporaryDirectory` 的 mode=0700 会建出受保护 DACL，
    受限令牌写不进自己建的目录（WinError 5，约 40 套件假红；默认权限
    `os.mkdir` 正常——不是工作区 ACL 缺陷）。`tests/sandbox_temp.py` 把
    `tempfile.mkdtemp` 换成默认权限版；**新套件用到 tempfile 目录 API 时**，
    在 `sys.path.insert` 后加 `from sandbox_temp import install; install()`
    （conftest 已覆盖 pytest 路径，直跑路径靠套件自己装）。临时库/夹具一律
    放 `.temp` 或系统 temp，**别放 `data/`**（生产目录，会被"测试进程不写
    生产库"守卫拦，见 05-daily-progress）。

    **实测矩阵（2026-11-22，受限令牌、工作区内，venv 3.13 与内置 3.12 双解释器
    同结果；.temp / data/cache / 系统 temp 三处位置同结果）**：

    | 建目录方式 | 建目录 | 往里写文件 | 能读它的权限 |
    |---|---|---|---|
    | `os.makedirs(p)` / `os.mkdir(p)` / `os.mkdir(p, 0o755)` | ✅ | ✅ | ✅ |
    | `os.mkdir(p, 0o700)` | ✅ | ❌ PermissionError | ❌ 连 icacls 都 Access is denied |
    | `tempfile.mkdtemp()` / `TemporaryDirectory()` | ✅ | ❌ WinError 5 | ❌ |

    机制：带 mode=0700 建目录写出的是**受保护 DACL**（丢掉继承），沙箱受限
    令牌（CodexSandboxUsers / S-1-4-* 能力组）拿不到任何授权——目录建完自己
    立刻进不去，也删不掉；不带 mode 则继承父目录授权，一切正常。文件类
    `tempfile.mkstemp / NamedTemporaryFile` 不受影响（实测可写）。
    **推论**：① 沙箱里建目录永远别带 mode；② 0700 目录一旦建出来就是死目录，
    沙箱内无解，只能沙箱外 `rmdir /s /q`；③ 这不是工作区 ACL 缺陷，跑 ACL
    修复脚本没用（diagnose-windows-sandbox-acl 流程已走过，审批无应答未执行，
    结论以本矩阵为准）。
  - **默认快回归（2026-11-16 起）**：`.venv\Scripts\python.exe -m pytest tests -q`
    跑 `GAMEAUTO_TEST_TIER=fast`，即 ci 清单减去 `LOCAL_HEAVY_SUITES`（真机 OCR /
    >30s / 依赖会被改写的本机文件），约 2 分钟；结尾打印最慢 10 个套件耗时。
  - 需要完整验证时显式 `GAMEAUTO_TEST_TIER=full ...`（含真机套件，可能十几分钟）；
    CI 用 `GAMEAUTO_TEST_TIER=ci ...`（跑清单全部 include）。被跳过的套件在 pytest
    结果里以 skip + 原因呈现，不计为通过。
- **测试禁锁实现写法**（2026-11-13 起）：套件里禁止「读源码文本断言某段代码存在」
  的写法锁——注释、日志文案、类型注解、精确调用格式（含参数排版）、「旧代码已
  删除」墓碑都不许锁；改一处措辞就红、重构必误报，且基本都有行为等价物。允许的
  例外只有**接线锚点**：巨型入口函数无法在测试里真调用时，允许用
  `inspect.getsource(具体函数)` 或源码子串检查**稳定标识符**（函数名/内建动作名/
  配置键），优先 `hasattr`；函数存在性一律 `hasattr` 不做源码子串。QML 结构用
  离屏实例化 + `findChild` 验证，不 grep `.qml` 文本。
- **artifacts/ 留什么**：截图证据 + 依赖本机日志/游戏配置/真实屏幕的真机脚本
  （如 wool_claim_logs、wool_flow_fixes、stamina_configs、genshin/starrail_launcher 的
  app_config 断言）。纯逻辑测试不要再往 artifacts 放；**套件夹具尤其不许写
  `data/artifacts`**——2026-11-22 审计在这里捡到一个套件留下的 0 字节
  `_minimax_fake_api.log`，夹具该写 `data/cache/` 或临时目录，收尾删掉。
- **依赖单一来源**：`requirements.txt` 是唯一人工清单，`services/dependency_manager.py`
  运行时解析它（import 名例外表 `IMPORT_NAME_BY_PACKAGE`），**不要再硬编码第二份包列表**。
  新增依赖只改 requirements.txt，然后在本机验证并运行
  `.venv\Scripts\python.exe tools\refresh_requirements_lock.py` 刷新 `requirements.lock`。
- **锁文件**：`requirements.lock` 是已验证环境的精确快照（含 `onnxruntime-gpu`，
  **绝不含 CPU 版 onnxruntime**——会让 onnxocr 静默降级）。安装必须 `--no-deps` 回放。
- **安装入口**：统一 `setup_deps.bat`（普通权限；检测到管理员会用 explorer 降权重启）。
  GUI/CLI 启动**只检查不安装**，缺包提示运行 setup_deps.bat。**禁止**恢复启动时提权
  pip install（`run_full_dependency_check` 已无 auto_install 参数）。
- **静态检查**：`pyproject.toml` 配 ruff 窄规则（E9/F821/F822/F823/E722/W292/W293/F541，2026-11-18 扩入后三个零噪音类），
  开发工具在 `requirements-dev.txt`；`pyproject.toml` 不放运行时依赖，避免第三份清单。
- **Bridge 拆分**：`ui/bridge.py` 只保留核心状态、Qt 属性/信号与事件分发；各视图的
  `@Slot` 实现在 `ui/bridge_views/`（click_sequence / setup / automation / scheduler /
  daily / gacha / disk_manager / tool_manager 八个 mixin，PySide6 会把 mixin 里的 Slot/Property/Signal 注册进最终
  QObject），后台领域逻辑仍在 `ui/bridge_backends/`。**新逻辑按视图放对应 mixin 或 backend，
  不要再往 bridge.py 堆**；单文件目标 <1000 行。
- **run_flow 拆分**：内建动作分发 `_dispatch_step_action`、失败取证
  `_collect_failure_evidence` 已抽成模块函数；动作清单统一用 `_BUILTIN_ACTIONS`
  （新增内建动作只改这一处）。
## 运行时数据目录（2026-10-29 起）

- 所有运行时数据统一放 `data/`：
  - `data/logs/`：auto_runner.log、看门狗 dump；
  - `data/artifacts/`：失败截图、白荆验证图、本地真机脚本；
  - `data/cache/`：第三方配置备份、各接口缓存、月度重跑队列；
  - `data/workflow.sqlite3`：本机偏好与运行数据库（含 WAL 文件）。
- 包代码统一在 `src/game_auto/`（src 布局）；根目录只保留 `src/`、`assets/`（模板与点击序列）、
  `scripts/`（启动脚本）、`data/`、`docs/`、`tests/`、`tools/` 与配置。
  运行时导入需把 `src/` 加进 `PYTHONPATH`（启动脚本已自动处理；pytest 由 `pythonpath=src` 处理）。
  资源路径统一由 `settings/paths.py` 的 `ASSETS_DIR` / `TEMPLATES_DIR` / `CLICK_SEQUENCES_DIR`
  给出；新增模板写到 `assets/templates/`，flow 配置里写 `assets/templates/xxx.png` 相对路径。
- 一次性迁移脚本：`tools/migrate_data_layout_20261029.py`（关闭 GUI/CLI 后运行；
  会同步把 app_config 的 `log.dir` 改成 `data/logs`，并修正本地 artifacts 脚本的相对路径）。
- 本文旧段落里出现的 `logs/…`、`artifacts/…`、`storage/cache/…`、根目录
  `workflow.sqlite3` 一律按新的 `data/` 路径理解。`storage/` 目录现在只放
  SQLite 访问层代码，不再存缓存。

## 工作区卫生（2026-11-17 起，用户拍板）

**根目录禁止堆临时产物，用完即删。** 适用于所有 AI 会话（Claude Code / Trae /
Kilo / Qoder / WorkBuddy / 星辰等）与人工操作：

- 一次性脚本、探针、中间产物放**系统临时目录**或 `.temp/`（已 gitignore），
  **绝不落在仓库根**；根目录出现 `_tmp_*`、`tmp*`、`*_tmp*`、`pytest-of-*`、
  `ga_*` 等一律视为未清理的垃圾。
- 任务收尾动作：删除本任务产生的临时文件/目录/截图/探针脚本——**收尾即清理，
  不等用户提醒**。pytest/mkdtemp 的临时目录在套件 finally 里回收。
- 并行会话正在写入的产物（按 mtime 判断，几分钟内活跃）不删，等其收尾；
  已在 changelog 里宣告收尾的会话遗留物可清。
- 有保留价值的产物进正式目录：截图/工件 `data/artifacts/`，抓取数据
  `data/<name>_capture/`，脚本入 `tools/`，套件入 `tests/`——**不放根目录**。
- **套件/脚本自建临时目录必须显式 `dir=` 钉到 `data/cache/`**（gitignored）：
  本机 Git Bash 的 `TEMP=/tmp` 不存在，绕过 pytest 直跑 python/套件时
  tempfile 一路回退 **cwd（仓库根）**——11-16 conftest 修过 pytest 侧，
  2026-11-18 又在 version_check/teleagent/codearts 四个直跑套件复发
  （根目录 ga_git_ver_×15 + tmp×9）。sqlite 的 `order by` 排序临时文件
  同理报 unable to open，连接上 `PRAGMA temp_store = MEMORY`。

## F 盘写入拦截（2026-11-18 提权探针实锤）

**非管理员进程一律禁写 F 盘**（任何路径）：python 直写、`cmd /c copy`、
PowerShell `Copy-Item` 全部拒绝访问，`robocopy` 挂死，msys `cp.exe` 从
python 派生直接 NtCreateFile 崩——按进程镜像/谱系拦截，与内容无关。
**管理员 python 直写可用**（GUI 经 `start_gui_admin.bat` 自提权，直接
shutil 即可）；AI 对话史串链（`services/ai_history_refresh.py`）依赖此规则。
写 F: 的非 GUI 脚本走「暂存到工作区 → 管理员拷回」两步，或整段以管理员
运行。读 F: 不受限制。探针记录见 `data/cache/aihist_admin_probe.json`
（已清，结论同 changelog 2026-11-18）。
- `app_config.json.bak*` 是程序自动备份，不属于垃圾，不要顺手清。

## 变更记录约定

- 每次改动在 [docs/changelog.md](../changelog.md) 对应日期下**追加一行**，
  只写"做了什么 / 为什么"；实现细节与踩坑写进本目录对应分篇，不写进 changelog。
- 新增游戏：在 `games/` 加一个 `GameProfile` 模块并 `register()`，不要再手改
  `settings/defaults.py`、`game_update_calendar.py`、`task_batch_runner.py` 的映射表。

## 隐私脱敏（2026-11-18 起，用户拍板；仓库为 public）

changelog / conventions 分篇 / 套件夹具里**不写 AI 对话的标题原文、正文片段、
检索验证用的 query 原文**——尤其健康/私人类查询。写 AI 对话史相关改动时用
「某来源会话（N 条 / M 字）」这类规模表述；举例需要文本时用编造的中性文案
（如「示例问题」），不用真实对话内容。检索验证截图/日志同样不外引。
套件夹具（make_db / convert_session 测试数据）也用编造文案，不镜像真实会话。
RAG/总库数据本体在 gitignored 区不受此约束（本机私有），约束的是**会推送到
public 仓库的文本**。

> AI生成
