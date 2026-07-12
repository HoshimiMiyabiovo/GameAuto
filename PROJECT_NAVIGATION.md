# 项目目录导航

这个仓库仍然使用 `game_auto` 作为包根目录，但根目录本身已经不再放源码 `.py` 文件。
现在目录大致分成几层：`start/` 放用户入口，`launchers/` 放内部启动脚本，`app/` 与 `ui/` 放入口和界面，`runtime/` 放运行执行链，`gacha/` 放抽卡逻辑，`settings/` / `services/` / `clients/` / `storage/` 放基础支撑层，`maintenance/` 放维护脚本。

## 推荐从这里启动

- `00_START_HERE_GUI_ADMIN.bat`：根目录推荐入口，启动图形界面。
- `start/00_START_GUI_ADMIN.bat`：GUI 用户入口原始位置。
- `start/00_START_CLI_ADMIN.bat`：命令行模式入口。
- `start/10_OPEN_PROJECT_HOME.bat`：直接打开项目目录。
- `launchers/`：内部启动脚本目录，负责提权和 `python -m game_auto...` 包模式启动。

## 根目录里主要看这几类

- GUI 直接入口：`00_START_HERE_GUI_ADMIN.bat`
- 用户入口：`start/`
- 应用入口：`app/`
- 图形界面：`ui/`
- 配置与基础服务：`settings/`、`services/`、`clients/`、`storage/`
- 抽卡相关：`gacha/`
- 运行执行链：`runtime/`
- 维护脚本：`maintenance/`
- 运行配置：`app_config.json`、`flow_configs.json`
- 数据库与运行数据：`workflow.sqlite3`、`logs/`、`artifacts/`
- 模板与第三方工具：`模板/`、`tools/`
- 根目录源码：已清空，不再直接放 `.py`

## start 目录说明

- `start/00_START_GUI_ADMIN.bat`：GUI 用户入口
- `start/00_START_CLI_ADMIN.bat`：CLI 用户入口
- `start/10_OPEN_PROJECT_HOME.bat`：打开项目目录

## launchers 目录说明

- `launchers/start_gui_admin.bat`：GUI 启动主脚本
- `launchers/start_cli_admin.bat`：CLI 启动主脚本
- `launchers/open_project_home.bat`：直接打开项目目录

## maintenance 目录说明

- `maintenance/settings_gui.py`：旧 Tkinter 配置编辑器
- `maintenance/seed_xm_templates.py`：XM 模板初始化脚本
- `maintenance/workflow_bootstrap.py`：演示工作流初始化脚本
- `maintenance/screen_task_runner.py`：固定坐标点击工具
- `maintenance/generic_workflow_runner.py`：数据库驱动工作流执行器

## runtime 目录说明

- `runtime/automation_controller.py`：核心编排器
- `runtime/task_batch_runner.py`：CLI / GUI 共用任务调度
- `runtime/foreground_flow_runner.py`：模板/OCR 流程执行器
- `runtime/process_supervisor.py`、`runtime/window_detector.py`：运行时系统探测
- `runtime/bettergi_log_monitor.py`、`runtime/ok_ww_log_monitor.py`、`runtime/whimbox_log_monitor.py`：运行期日志监控
- `runtime/ocr_click_tester.py`：运行链复用的 OCR / 点击辅助工具

## 其他源码目录说明

- `app/cli.py`、`app/gui.py`：CLI / GUI 启动入口
- `ui/`：Flet 界面总装、自动化页与共享 UI 常量
- `settings/`：默认配置、运行时配置视图、项目路径与配置持久化
- `services/`：依赖检查、启动前签到、任务目录与通用工具
- `clients/`：米游社与库街区接口客户端
- `gacha/`：各游戏抽卡导入、抓取与分析
- `storage/workflow_db.py`：工作流与偏好数据库访问层

## 后续如果要继续整理

下一步如果还要继续收结构，更合适的是按游戏或数据源继续细分 `gacha/` 和 `clients/`；
但不建议短期内再重命名入口层和运行链包名，否则会增加后续合并与排错成本。