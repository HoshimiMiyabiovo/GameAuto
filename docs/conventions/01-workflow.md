# 工作流、依赖、日志、注释与工程基线

> 由 AGENTS.md 拆分；改动相关代码前先读本篇。

## Execution Workflow

- Before modifying code, adding a module, or changing a runtime flow, write a brief plan first. The plan should name the target file or symbol, the local hypothesis, and the first validation step.
- Prefer the smallest grounded edit. If behavior is still uncertain, make a reversible local change instead of widening scope early.
- After the first substantive edit, immediately run the narrowest validation available for the touched slice before doing more edits. For this repo that usually means diagnostics on touched files, plus a focused import or config check when interfaces changed.
- Before finishing, proactively inspect new errors in every touched file and resolve issues introduced by the change instead of stopping at a successful patch.
- When a task changes Python imports, module boundaries, or startup/runtime wiring, do at least one focused static validation beyond visual inspection.
## Dependency Rules

- When adding a new Python dependency, update both requirements.txt and dependency_manager.py.
- Specifically, dependency_manager.py -> REQUIRED_PYTHON_PACKAGES must include the import name to pip package mapping, otherwise startup dependency checks will not auto-install the package.
## Logging Rules

- New UI flows and new data/task flows should write logs to the GameAutomation logger.
- Prefer logs at user action boundaries and IO boundaries: start, key step, success summary, failure reason.
- Do not write full secrets such as cookies or tokens into logs. Use masked account identifiers or short summaries instead.
- **日志长期保留，不自动删除**：`logs/auto_runner.log` 按天轮转，`log.backup_count=0` 表示永久保留历史文件；禁止把默认值改回正数或新增日志清理逻辑。看门狗 dump 超过 5MB 只归档为带时间戳的副本，不覆盖旧内容。
- **禁止无说明的 `except Exception: pass`**：预期异常用窄类型并写明原因；非预期异常至少记 `logger.debug`；只有日志/信号基础设施自身失败才允许静默，且必须注释说明（2026-10-28 已清理 123 处）。
## Scope Notes

- Startup sign behavior should be shared through startup_sign_runner.py rather than duplicated separately in QML and CLI flows.
- Keep project-visible notes like this file concise and focused on repeated engineering pitfalls.
## 代码注释规范（2026-10-24 起分阶段补全）

分阶段给自有代码补详细注释，**只加注释不改任何逻辑**，每文件完成后 py_compile 验证。
- 语言一律中文；风格对齐 AGENTS.md：讲"为什么/约束/坑"，不复述代码字面意思。
- 模块 docstring：职责、数据流（从哪来到哪去）、与其他模块的关系、不变量与坑。
- 公共函数 docstring：契约（参数/返回/副作用/失败模式）。
- 行内注释只标非直观处：魔法数字来源、顺序约束、竞态窗口、踩过的坑。
- **排除**：`flet/`（内嵌第三方框架）、`ui/flet_app.py` + `ui/flet/`（已停维护）、
  `%TEMP%/`（参考项目）、`artifacts/`（测试已带注释）。
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

- **测试目录**：纯逻辑回归套件统一在 `tests/suite_*.py`（26 个，从 `artifacts/test_*.py`
  迁入，文件名日期后缀不变；本文旧段里出现的 `artifacts/test_xxx_日期.py` 按此映射到
  `tests/suite_xxx_日期.py`）。一键执行：
  `.venv\Scripts\python.exe -m pytest tests -q`（`tests/test_suites.py` 逐个子进程跑，
  隔离各套件的 monkeypatch；新增套件按 `suite_*.py` 命名即自动纳入）。
- **artifacts/ 留什么**：截图证据 + 依赖本机日志/游戏配置/真实屏幕的真机脚本
  （如 wool_claim_logs、wool_flow_fixes、stamina_configs、genshin/starrail_launcher 的
  app_config 断言）。纯逻辑测试不要再往 artifacts 放。
- **依赖单一来源**：`requirements.txt` 是唯一人工清单，`services/dependency_manager.py`
  运行时解析它（import 名例外表 `IMPORT_NAME_BY_PACKAGE`），**不要再硬编码第二份包列表**。
  新增依赖只改 requirements.txt，然后在本机验证并运行
  `.venv\Scripts\python.exe tools\refresh_requirements_lock.py` 刷新 `requirements.lock`。
- **锁文件**：`requirements.lock` 是已验证环境的精确快照（含 `onnxruntime-gpu`，
  **绝不含 CPU 版 onnxruntime**——会让 RapidOCR 静默降级）。安装必须 `--no-deps` 回放。
- **安装入口**：统一 `setup_deps.bat`（普通权限；检测到管理员会用 explorer 降权重启）。
  GUI/CLI 启动**只检查不安装**，缺包提示运行 setup_deps.bat。**禁止**恢复启动时提权
  pip install（`run_full_dependency_check` 已无 auto_install 参数）。
- **静态检查**：`pyproject.toml` 配 ruff 窄规则（E9/F821/F822/F823/E722），
  开发工具在 `requirements-dev.txt`；`pyproject.toml` 不放运行时依赖，避免第三份清单。
- **Bridge 拆分**：`ui/bridge.py` 只保留核心状态、Qt 属性/信号与事件分发；各视图的
  `@Slot` 实现在 `ui/bridge_views/`（click_sequence / setup / automation / scheduler /
  daily / gacha 六个 mixin，PySide6 会把 mixin 里的 Slot/Property/Signal 注册进最终
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
- 根目录只保留源码、资源（`模板/`、`click_sequences/`）、配置与文档。
- 一次性迁移脚本：`tools/migrate_data_layout_20261029.py`（关闭 GUI/CLI 后运行；
  会同步把 app_config 的 `log.dir` 改成 `data/logs`，并修正本地 artifacts 脚本的相对路径）。
- 本文旧段落里出现的 `logs/…`、`artifacts/…`、`storage/cache/…`、根目录
  `workflow.sqlite3` 一律按新的 `data/` 路径理解。`storage/` 目录现在只放
  SQLite 访问层代码，不再存缓存。

## 变更记录约定

- 每次改动在 [docs/changelog.md](../changelog.md) 对应日期下**追加一行**，
  只写"做了什么 / 为什么"；实现细节与踩坑写进本目录对应分篇，不写进 changelog。
- 新增游戏：在 `games/` 加一个 `GameProfile` 模块并 `register()`，不要再手改
  `settings/defaults.py`、`game_update_calendar.py`、`task_batch_runner.py` 的映射表。
