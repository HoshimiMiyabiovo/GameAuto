# Game Auto —— 游戏自动化助手

一键启动多款二次元游戏的第三方自动化脚本，支持日常签到、自动化运行、错误监控与自动恢复，以及抽卡（跃迁/祈愿）记录分析与可视化。

## 支持的游戏与自动化方案

| 游戏 | 自动化脚本 |
|---|---|
| 原神 (Genshin Impact) | BetterGI |
| 崩坏：星穹铁道 (Honkai: Star Rail) | StarRail OneDragon / March7th Assistant |
| 绝区零 (Zenless Zone Zero) | OneDragon Bat / ScriptChainer |
| 鸣潮 (Wuthering Waves) | ok-ww |
| 明日方舟：终末地 (Arknights: Endfield) | ok-ef |
| 异环 (Neverness to Everness) | ok-nte |
| 无限暖暖 (Infinity Nikki) | whimbox (OneDragon) |

此外还支持两款传统游戏的图像模板自动化：赛尔号脱机 (XM Seer)、重聚脱机 (CJ)。

> **注意：** 本项目仅负责**编排和监控**这些第三方自动化脚本，你需要自行安装对应游戏的自动化工具。

## 功能特性

- **GUI & CLI 双入口**：默认 GUI 基于 PySide6 + QML + HuskarUI，老的 Flet 版保留作回退
- **每日签到**：启动前自动跑米游社（原神/星铁/绝区零）、库街区（鸣潮）、塔吉多（异环 BBS+游戏）、森空岛（终末地）+ 终末地协议终端。米游社 cookie_token 过期会用 stoken 自动刷新；塔吉多 token 失效可用配置的手机号+密码自动重登
- **自动编排**：按配置顺序依次启动游戏及对应脚本，自动聚焦游戏窗口
- **日志监控**：实时监控脚本日志，检测报错或卡死，支持自动重试
- **抽卡分析**：支持米哈游系 UIGF 格式、终末地 JSON、鸣潮 Excel、无限暖暖 API 多种数据源
- **前置流程**：支持异环等需要先启动启动器再点"开始游戏"的前置流程
- **邮件通知**：运行结束可发每日报告邮件，邮箱密码经 Windows DPAPI 加密落盘

## 环境要求

- Windows 10/11 操作系统（大量使用 Win32 API）
- Python 3.10+（当前 `flet` 与 `Pillow` 依赖都已要求 >= 3.10）
- Tesseract OCR 5.x（项目已内置在 `tools/tesseract/`）

## 快速开始

```bash
# 1. 克隆项目
git clone <repo-url>
cd game_auto

# 2. 创建虚拟环境并安装依赖
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 3. 启动
# GUI 模式（推荐）：
双击 00_START_HERE_GUI_ADMIN.bat

# 或使用 start 目录里的入口：
双击 start\00_START_GUI_ADMIN.bat

# CLI 模式：
双击 start\00_START_CLI_ADMIN.bat
```

首次启动时会自动检查并安装缺失的 Python 依赖包。

## 项目结构

```
game_auto/
├── 00_START_HERE_GUI_ADMIN.bat  # 根目录推荐 GUI 入口
├── app/                         # CLI / GUI 包入口
├── ui/                          # Flet GUI 视图与自动化页面
├── settings/                    # 配置运行视图、默认值、路径与 JSON 持久化
├── services/                    # 通用服务、共享工具与任务目录
├── clients/                     # 米游社 / 库街区等外部客户端
├── gacha/                       # 抽卡导入、解析与分析
├── storage/                     # SQLite 数据访问层
├── start/                       # 用户双击入口（GUI / CLI / 打开项目目录）
├── launchers/                   # 内部提权与包模式启动脚本
├── maintenance/                 # 维护脚本与一次性工具脚本
├── runtime/                     # 运行执行链：控制器、流程执行器、监控与 OCR 工具
├── app_config.json              # 用户覆盖配置与执行顺序
├── flow_configs.json            # 图像模板流程定义
├── tools/                       # 内置工具（Tesseract OCR 等）
└── workflow.sqlite3             # 本地偏好与运行数据数据库
```

现在根目录不再放项目源码 `.py` 文件；源码按职责拆进上面的子包里，方便查找与维护。

`maintenance/` 目前集中放这类不属于主运行链的脚本：

- `settings_gui.py`：旧的 Tkinter 配置编辑器
- `seed_xm_templates.py`：一次性写入 XM 模板数据
- `workflow_bootstrap.py`：初始化演示工作流
- `screen_task_runner.py`：固定坐标点击工具
- `generic_workflow_runner.py`：数据库驱动工作流执行器（当前未接入主运行链）

`runtime/` 目前集中放主运行链相关模块：

- `automation_controller.py`：核心编排器
- `foreground_flow_runner.py`：图像模板/OCR 流程执行器
- `task_batch_runner.py`：CLI / GUI 共用任务批处理层
- `process_supervisor.py`、`window_detector.py`：进程与窗口检测
- `bettergi_log_monitor.py`、`ok_ww_log_monitor.py`、`whimbox_log_monitor.py`：运行期日志监控
- `ocr_click_tester.py`：运行链复用的 OCR / 点击辅助工具

`app/`、`ui/`、`settings/`、`services/`、`clients/`、`gacha/`、`storage/` 分工如下：

- `app/`：CLI / GUI 入口模块，供 `python -m game_auto.app.cli` 与 `python -m game_auto.app.gui` 调用
- `ui/`：QML 视图（`ui/qml/`）、PySide6↔QML 桥接（`ui/bridge.py`）、主题令牌（`ui/theme.py`），以及保留的 Flet 界面（`ui/flet_app.py`，暂停维护）
- `settings/`：运行时配置视图、默认配置、配置文件读写（含原子写入 `atomic_write_json`）与项目路径常量
- `services/`：依赖检查、启动前签到调度（`startup_sign_runner.py`）、每日进度追踪（`daily_progress_tracker.py`）、邮件报告（`email_reporter.py`）、各游戏日志解析器与共享工具函数
- `clients/`：米游社、库街区、塔吉多、森空岛、终末地协议终端等外部接口客户端
- `gacha/`：米哈游 / 终末地 / 鸣潮 / 无限暖暖的抽卡导入与分析逻辑
- `storage/`：SQLite 工作流与偏好数据库访问层

## 配置说明

配置现在分成两层：

- `app_config.json`：项目级共享配置，适合手工编辑或跟随项目一起迁移。
- `workflow.sqlite3`：本机运行偏好和运行数据，主要由 GUI 自动维护，不建议手工改表。

### app_config.json 负责什么

关键字段：

- `app_order` — 游戏执行顺序
- `apps` — 各游戏的启动路径、参数、进程名、超时时间等
- `mihoyo_sign` — 米游社签到 Cookie
- `wuwa_kujiequ` — 库街区签到 Token
- `global.min_run_time` — 最短运行时间
- `log` — 日志目录与保留份数

GUI 里这几类操作会写回 `app_config.json`：

- 调整任务执行顺序
- 修改各游戏或各启动方案的路径
- 更新项目级签到配置、日志配置等共享设置

### workflow.sqlite3 负责什么

主要保存：

- `app_launch_preferences`：每个任务是否启用、默认脚本方案等 GUI 启动偏好
- 抽卡导入记录和分析数据缓存
- 其他运行期偏好与历史数据

GUI 里这几类操作会写回 `workflow.sqlite3`：

- 勾选本轮默认启用哪些任务
- 记录每个任务默认使用哪套脚本方案
- 保存抽卡导入后的本地数据库数据

简单说：

- 想改“项目应该怎么跑”，看 `app_config.json`
- 想改“这台电脑上我平时怎么用”，看 `workflow.sqlite3`

## 依赖项

主要 Python 包：

- `psutil` — 进程管理
- `pyautogui` — 屏幕自动化
- `pytesseract` — OCR 文字识别
- `pywin32` — Windows API，也用于 DPAPI 密码加密
- `opencv-python` — 图像模板匹配
- `PySide6` — Qt 6 + QML，默认 GUI 框架（需锁定 >=6.8,<6.10 兼容 PyHuskarUI）
- `PyHuskarUI` — QML 控件库
- `flet` — 旧 GUI 框架，保留作回退
- `openpyxl` — Excel 解析（鸣潮抽卡记录）
- `rapidocr-onnxruntime` — 备选 OCR 引擎

## 免责声明

本项目仅用于**学习和个人便利**目的。使用者应自行遵守相关游戏的服务条款，作者不对因使用本工具导致的账号封禁或其他后果负责。
