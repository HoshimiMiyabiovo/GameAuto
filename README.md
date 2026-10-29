# Game Auto —— 游戏自动化助手

一键启动多款二次元游戏的第三方自动化脚本，支持日常签到、自动化运行、错误监控与自动恢复，以及抽卡（跃迁/祈愿）记录分析与可视化。

## 支持的游戏与自动化方案

| 游戏 | 自动化方案 | 签到/数据源 |
|---|---|---|
| 原神 (Genshin Impact) | BetterGI | 米游社 |
| 崩坏：星穹铁道 (Honkai: Star Rail) | StarRail OneDragon / March7th Assistant | 米游社 |
| 绝区零 (Zenless Zone Zero) | OneDragon Bat / ScriptChainer | 米游社 |
| 鸣潮 (Wuthering Waves) | ok-ww | 库街区 |
| 明日方舟：终末地 (Arknights: Endfield) | ok-ef | 森空岛签到 + 协议终端/森空岛官方数据 |
| 异环 (Neverness to Everness) | ok-nte + 前置流程 | 塔吉多（BBS + 游戏） |
| 无限暖暖 (Infinity Nikki) | whimbox (OneDragon) | — |
| 归龙潮 | 内置模板流程（`runtime/guilongchao_flow.py`） | — |
| 白荆回廊 | 内置模板流程（`runtime/baijing_flow.py`） | — |

另有两款传统游戏的图像模板自动化：赛尔号脱机（XM）、重聚脱机（CJ），可作为独立虚拟任务插入执行顺序。

> **注意：** 本项目仅负责**编排和监控**这些第三方自动化脚本，你需要自行安装对应游戏的自动化工具。

## 功能特性

- **GUI & CLI 双入口**：默认 GUI 基于 PySide6 + QML + HuskarUI（`app/gui.py`），首次启动有配置向导（`SetupWizardView`）；老的 Flet 版保留作回退（`ui/flet_app.py`）
- **每日签到**：启动前自动跑米游社（原神/星铁/绝区零）、库街区（鸣潮）、塔吉多（异环 BBS+游戏）、森空岛（终末地）。米游社 cookie_token 过期会用 stoken 自动刷新；塔吉多 token 失效可用配置的手机号+密码自动重登
- **每日进度**：汇总各游戏当日日常完成度（体力/活跃度/周本等），鸣潮每日数据卡片在专项页可直接刷新
- **终末地数据中心**：支持第三方协议终端（`end-api.shallow.ink`）与森空岛官方数据两条查询链路（后者复用签到 token，无需额外注册）
- **自动编排**：按配置顺序依次启动游戏及对应脚本，自动聚焦游戏窗口；任务顺序可在 GUI 里 ↑/↓ 调整并持久化
- **日志监控**：实时监控脚本日志，检测报错或卡死，支持自动重试
- **抽卡分析**：米哈游系 UIGF 格式、终末地 JSON（含 token 同步抓取）、鸣潮 Excel / ADB 抓取、无限暖暖共鸣目录等多种数据源，数据统一落 SQLite
- **前置流程**：支持异环、归龙潮、白荆回廊等需要先启动启动器再点"开始游戏"的图像模板流程
- **邮件通知**：运行结束可发每日报告邮件，邮箱密码经 Windows DPAPI 加密落盘

## 环境要求

- Windows 10/11 操作系统（大量使用 Win32 API）
- Python 3.10+
- Tesseract OCR 5.x（项目已内置在 `tools/tesseract/`）

## 快速开始

```bash
# 1. 克隆项目
git clone <repo-url>
cd game_auto

# 2. 以普通权限安装依赖（uv + requirements.lock 精确复现；不要用管理员运行）
setup_deps.bat

# 3. 启动（启动时只检查依赖，缺包会提示运行 setup_deps.bat；随后进入配置向导）
```

| 入口 | 说明 |
|---|---|
| `00_START_HERE_GUI_ADMIN.bat` | 根目录推荐入口 → 图形界面 |
| `start\00_START_GUI_ADMIN.bat` | GUI 入口原始位置 |
| `start\00_START_CLI_ADMIN.bat` | 命令行模式 |
| `start\00_START_FLET_ADMIN.bat`（含根目录 `00_START_HERE_FLET_ADMIN.bat`） | 旧版 Flet 界面（回退用） |
| `start\10_OPEN_PROJECT_HOME.bat` | 直接打开项目目录 |

`start\` 只是用户入口壳，实际启动逻辑在 `launchers\`（自动提权 + `python -m game_auto...` 包模式启动）。

## 项目结构

```
game_auto/
├── app/            # CLI / GUI 包入口（app/cli.py、app/gui.py）
├── ui/             # QML 视图（ui/qml/）、PySide6↔QML 桥接（ui/bridge.py）、主题（ui/theme.py）
│                   #   及保留的 Flet 界面（ui/flet_app.py，暂停维护）
├── runtime/        # 运行执行链：编排器、流程执行器、日志监控、OCR 工具
├── services/       # 依赖检查、启动前签到调度、每日进度、邮件报告、各游戏日志解析器
├── clients/        # 米游社 / 库街区 / 塔吉多 / 森空岛等外部接口客户端
├── gacha/          # 各游戏抽卡导入、解析与分析
├── settings/       # 默认配置、路径常量、配置读写（含原子写入）
├── storage/        # SQLite 数据访问层（workflow_db.py；不再存放缓存）
├── data/           # 运行时数据（不入库）：logs/、artifacts/、cache/、workflow.sqlite3
├── maintenance/    # 维护脚本与一次性工具（旧配置编辑器、模板种子等）
├── start/          # 用户双击入口
├── launchers/      # 内部提权与包模式启动脚本
├── tools/          # 内置工具（Tesseract OCR、QML 布局 smoke 测试等）
├── 模板/            # 图像模板流程用的截图模板
├── app_config.json      # 用户覆盖配置与执行顺序（不入库）
└── flow_configs.json    # 图像模板流程定义（XM / CJ / NTE；本地文件，不入库）
```

### runtime/ 主要模块

- `automation_controller.py`：核心编排器
- `task_batch_runner.py`：CLI / GUI 共用任务批处理层
- `foreground_flow_runner.py`：图像模板 / OCR 流程执行器
- `guilongchao_flow.py`、`baijing_flow.py`：归龙潮、白荆回廊游戏内流程
- `process_supervisor.py`、`window_detector.py`：进程与窗口检测
- `bettergi_log_monitor.py`、`ok_ww_log_monitor.py`、`whimbox_log_monitor.py`：运行期日志监控
- `ocr_click_tester.py`：OCR / 点击辅助工具

### clients/ 主要模块

- `mihoyo_sign.py`：米游社签到（原神/星铁/绝区零，stoken 自动刷新）
- `wuwa_kujiequ.py`：库街区签到 + 每日数据；`wuwa_kujiequ_adb.py`：通过 ADB + WebView CDP 从库街区 APP 抓 token
- `tajiduo_sign.py` / `tajiduo_login.py`：塔吉多签到与账密重登
- `skendfield_sign.py`：森空岛签到；`skendfield_data.py`：森空岛官方终末地数据查询
- `endfield_protocol.py`：第三方协议终端数据查询；`endfield_gacha.py`：终末地抽卡记录 token 同步抓取
- `daily_checker.py`：多游戏每日进度汇总

### gacha/ 主要模块

- `hoyo_importer.py` / `hoyo_analysis.py` / `genshin_importer.py` / `genshin_analysis.py`：米哈游系抽卡
- `endfield_analysis.py`：终末地抽卡
- `wuwa_analysis.py`：鸣潮抽卡
- `nikki_resonance_fetch.py` / `nikki_resonance_analysis.py` / `nikki_item_catalog.py`：无限暖暖共鸣（抽卡）

## 配置说明

配置分两层：

- `app_config.json`：项目级共享配置，适合手工编辑或跟随项目迁移（字段参考 `app_config.example.json`）
- `workflow.sqlite3`：本机运行偏好和运行数据，主要由 GUI 自动维护，不建议手工改表

`app_config.json` 关键字段：

- `app_order` — 游戏执行顺序
- `apps` — 各游戏的启动路径、参数、进程名、超时时间等
- `mihoyo_sign` — 米游社签到 Cookie；可选 `device` 段填入真机设备指纹（copy-device-info APK 复制的字段），用于降低便笺查询触发米游社风控（1034/10041）的概率
- `wuwa_kujiequ` — 库街区签到 Token 与角色信息
- `tajiduo_sign` — 塔吉多凭据（含重登手机号+密码）
- `skendfield_sign` — 森空岛凭据
- `endfield_protocol` — 终末地协议终端 api_key / role_id 等
- `nikki` — 无限暖暖相关配置
- `log` — 日志目录与保留份数

简单说：想改"项目应该怎么跑"看 `app_config.json`；想改"这台电脑上我平时怎么用"看 `workflow.sqlite3`（任务勾选、默认脚本方案、抽卡导入记录等）。

## 依赖项

主要 Python 包（完整列表见 `requirements.txt`；`requirements.lock` 是已验证环境的精确快照，安装统一走 `setup_deps.bat`。启动时 `services/dependency_manager.py` 只检查、不安装）：

- `PySide6`（锁定 `>=6.8,<6.9`，兼容 PyHuskarUI）+ `pyhuskarui` — 默认 GUI 框架与 QML 控件库
- `flet` — 旧 GUI 框架，保留作回退
- `psutil` — 进程管理；`pywin32` — Windows API 与 DPAPI 密码加密
- `pyautogui` / `opencv-python` / `Pillow` — 屏幕自动化与图像模板匹配
- `pytesseract` — OCR 文字识别；`rapidocr-onnxruntime` + `onnxruntime-gpu` — 主 OCR 引擎（必须 GPU 版，CPU 版会静默降级）
- `windows-capture` — 窗口截屏；`numpy` — 图像处理
- `genshin` — 米哈游协议（签到、便笺、stoken 换取）
- `browser-cookie3` — 浏览器 Cookie 提取；`qrcode` — 扫码登录二维码
- `rsa` / `pycryptodome` — 库街区 / 塔吉多接口签名加密
- `openpyxl` — Excel 解析（鸣潮抽卡记录）
- `ruamel.yaml` — 刷本配置（崩铁 March7th / 绝区零 OneDragon）YAML 读写，保留注释与键顺序

## 开发注意

重复踩坑的工程约定（依赖新增规则、日志规则、QML 兼容性避坑、米哈游协议已验证事实等）集中记录在 [AGENTS.md](AGENTS.md)，改代码前先看它。

```bash
# 纯逻辑回归套件（tests/suite_*.py，逐个子进程隔离）
.venv\Scripts\python.exe -m pytest tests -q

# 静态检查（E9 / F821 / E722 窄规则集）
.venv\Scripts\ruff.exe check .

# 依赖变更后刷新锁文件（先在本机验证，再执行）
.venv\Scripts\python.exe tools\refresh_requirements_lock.py
```

## 免责声明

本项目仅用于**学习和个人便利**目的。使用者应自行遵守相关游戏的服务条款，作者不对因使用本工具导致的账号封禁或其他后果负责。
