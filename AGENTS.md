# Game Auto Agent Notes

This file mirrors important repository-level development notes inside the project so they are visible in the repo itself.

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

## 凭据 DPAPI 加密（2026-10-25 起含 phone）

- `settings/manager.py` 的 `_SENSITIVE_SUBSTRINGS` 决定哪些键落盘自动 DPAPI 加密（token / cookie / password / secret / api_key / **phone**）。登录手机号是 PII（塔吉多、库街区都靠它自动重登），曾以明文落盘，2026-10-25 已纳入清单并自动迁移为密文。
- `load()` 内置自愈迁移：检出盘上明文敏感值 → `save()` 重写为密文，读取方无需兼容分支（`secure_store.resolve` 对无前缀明文原样返回）。新增敏感字段直接让键名命中清单即可，**不要自建加密逻辑**，也不要把新键加进 `_SENSITIVE_EXCLUDE`。
- 注意：运行中的旧 GUI 进程保存配置会把新纳入清单的字段写回明文——重启 GUI 后首次 load 自动再加密，属自愈场景，但改完清单应重启 GUI。

## Mihoyo Cookie Notes

- browser_cookie3 access to Edge cookies may require administrator privileges on this Windows environment.
- Do not swallow RequiresAdminError and then report it as "cookie not found". Surface the real permission problem.
- QML GUI (Flet 已暂停维护) should elevate before attempting Edge cookie extraction.

## UI Framework Notes

- 项目有两套 UI：**PySide6+QML + HuskarUI**（`ui/qml/`，**当前默认入口**，`app/gui.py` → `_launch_qml_ui`）和 **Flet**（`ui/flet_app.py`，暂停维护，不再被 `start_gui_admin.bat` 调用，run_flet_gui 无任何 caller，保留仅作回退方案）。
- HuskarUI 来源：`https://github.com/mengps/HuskarUI`，QML 控件前缀 `Hus`（HusCard / HusButton / HusText / HusSelect / HusCheckBox / HusTag / HusSpin / HusEmpty / HusMenu / HusNotification / HusInput / HusIcon 等）。
- QML 视图文件在 `ui/qml/views/`，可复用组件在 `ui/qml/components/`，主窗口在 `ui/qml/Main.qml`。
- Python ↔ QML 桥接在 `ui/bridge.py`（UiBridge），主题令牌在 `ui/theme.py`。
- **凭据输入必须用 `components/CredentialInput.qml`**（默认 `echoMode: TextInput.Password`，自带"显示/隐藏"按钮）。**禁止**直接用 `HusInput` 接收 token / cookie / api_key，会明码显示。

### PyHuskarUI 兼容性避坑 (重要)

1. **PySide6 版本锁定**: PyHuskarUI 0.6.1 的 QML 文件内 `ParallelAnimation` 直接放在 `Rectangle` 的 `data` 子节点中，在 Qt 6.11 中会报错 `Cannot assign object of type "QQuickParallelAnimation" to list property "data"; expected "QObject"`。必须降级 PySide6: `pip install "PySide6>=6.8,<6.10"`，涉及 PyHuskarUI 库内 7 个文件（HusButton/HusBadge/HusButtonBlock/HusCheckBox/HusRadio/HusRadioBlock/HusSwitch）。

2. **不要同时启用 qrc 资源和文件系统 QML 路径**: PyHuskarUI 的 QML 同时存在于编译资源 (`qml_rc.py` 注册的 `qrc:`) 和文件系统，只能二选一。使用默认的编译资源即可，**禁止**调用 `engine.addImportPath(str(pyhuskarui_qml_dir))`。

3. **QML 初始化不要跨模块导入**: `gui.py → main_window.py → bridge` 的导入链在 `python -m` 模式下会导致 QML 加载失败。正确做法是将 QApplication + HusApp + Engine + Bridge 的初始化**内联**写在 `app/gui.py` 的 `main()` 中，不走 `from ..ui.main_window import run_huskar_ui`。

4. **QML 磁盘缓存**: Qt 的 QML 编译缓存按 organizationName/appName 分区，位于 `%LOCALAPPDATA%/GameAuto/`。切换 PySide6 版本后需清掉旧缓存（`*.qmlc`），否则可能加载过期字节码。

5. **禁止硬编码文件路径**: 所有数据路径必须通过 `settings/paths.py` 或 `app_config.json` 配置，**禁止**在代码中写死绝对路径（如 `D:\Program Files\...`）。抽卡数据必须写入 SQLite 数据库（`gacha_records` 表），从数据库读取，不能依赖本地文件系统中的固定文件。`ui/shared.py` 中的 `DEFAULT_ENDFIELD_PATH` 是反面教材，新代码不得效仿。

### QML 签到按钮 + 桥接 (重要)

- **"执行签到"按钮必须**调 `bridge.run*Sign()` 才能真正签到，只调 `bridge.saveCredential(...)` 只会把 token 存进 config，**不会触发签到**。各 view 对应关系：
  - `EndfieldCenterView.qml` → `bridge.runSkendfieldSign()`（森空岛）
  - `WuwaView.qml` → `bridge.runWuwaKujiequSign()`（库街区）
  - `TajiduoView.qml` → `bridge.runTajiduoSign()`（塔吉多/异环）
- 对应 backend 都在 `ui/bridge.py`：`_run_skendfield_sign_backend` / `_run_wuwa_kujiequ_sign_backend` / `_run_tajiduo_sign_backend`。每个起 daemon 线程从 `app_config.json` 读凭据、调 `clients/*.perform_sign(...)`、用 `format_sign_report` 写日志、最后 `emit_event("toast", ...)` 推回 QML 弹通知。
- 按钮加 `enabled: !bridge.isRunning` 防双击重入。
- 视图加 `onVisibleChanged: { if (visible) bridge.loadCredentials() }` 确保切回来时凭据一定从 config 重灌。

### 签到返回状态码 (已验证)

| 客户端 | `status` 字段值 | 含义 |
|--------|----------------|------|
| `clients/skendfield_sign.py` | `"success"` / `"already"` / `"failed"` / `"login_failed"` | 签到 OK / 已签到 / 失败 / 登录失败 |
| `clients/tajiduo_sign.py` (每项) | `"success"` / `"already"` / `"failed"` / `"skipped"` | 同上，但分 `bbs` 和 `game[]` 两块 |
| `clients/wuwa_kujiequ.py` | 顶层 `code` + `message` 字段 | `code=200` 且 msg 含"请求成功"/"请勿重复签到"才算成功 |

判定是否成功的 helper 在 bridge backend 里，**不要**写 `result.get("already_signed")`（错，实际是 `"already"`），也不要写 `result.get("success")` 期望 True（skendfield 用 `status="success"`，tajiduo 用 `bbs.get("status")=="success"`）。

### 绝区零双链路 + 主界面检测 (重要, 2026-10-21)

**两条链路互不相干，别混**：
- **链路 A（每月一次）**：`ZzzMonthlyFlow` 独立任务 → `run_zzz_monthly_background` → `run_flow("ZZZ_MONTHLY")`（flow_configs.json）。exe 走 `exe_from_app: "ZenlessZoneZero"` 从 app_config 取游戏本体。
- **链路 B（每日一条龙）**：`execute_task("ZenlessZoneZero")` 直接拉 `OneDragon ScriptChainer Runner.exe --chain 1`，**不经过 ZZZ_MONTHLY**，进主界面由 OneDragon 自己的「进入游戏序章」节点完成；本项目只监控 `D:\down\ZZZ\ZenlessZoneZero-OneDragon\.log\log.txt` 并用 `services/zzz_daily_parser.py` 出卡片。

**「点击进入游戏」≠ 进入主界面**：ZZZ_MONTHLY 第一步（OCR 点"点击进入游戏"）只过客户端登录界面，之后还要 loading（OneDragon 的状态文本：加载配置数据中/版本校对中/登录游戏服务器中/登录成功，最长各有时限）。2026-10-21 已在第一步后加第二步「等待进入主界面」：`wait_template` 等大世界特征——模板 `模板\绝区零-主界面-电台.png`（52x52 右侧电台图标，**复制自 OneDragon `assets/template/normal_world/map/raw.png`，GPL-3.0 本地自用**）+ OCR 兜底「星期」（OneDragon 同画面文字特征，pc_rect 201,38-270,86）；timeout 600s、`on_fail: retry_prev`（客户端内可能还有第二层进入游戏按钮，回退上一步重点）、最多 3 轮。

**踩坑记录**：项目匹配器是 `cv2.matchTemplate` TM_CCOEFF_NORMED 灰度单尺度取全局最大值，**不支持 mask**——直接套 OneDragon 带背景的 raw.png 即可（图标图案对比强烈，1080p 自匹配 1.0），但阈值别高于 0.8；match_scales 已补 1.5/0.67 覆盖 2K/低分辨率渲染。验证脚本：`artifacts/test_zzz_monthly_main_screen_20261021.py`（配置静态）、`artifacts/test_zzz_main_screen_match_20261021.py`（合成图多尺度匹配 + 导入链）。主界面检测失败时 `record_failure` 会自动截图+OCR 快照落库，先看 `tools/flow_failure_report.py` 聚合再改。

#### 后勤商店采购（2026-10-21，ZZZ_MONTHLY 第 3-6 步）

主界面之后链路：**点右下角"···"（OneDragon message 模板）→ 丽都菜单点「后勤商店」→ 等「哨站后勤」页签出现 → `zzz_shop_buy` 采购**。采购清单（哨站后勤：原装母带×2/日常业绩×10/邦布券×5/调律校音器×1；协会物资：日常业绩×10/邦布券×5/调律校音器×1/仓鼠笼访问器×1）全在 flow_configs.json 的 `shop_groups`，交互逻辑在 `runtime/zzz_monthly_shop.py`。

- **交互模型**：点商品图标 → 弹「兑换确认」→ 点「确认」；弹窗默认 ×1，买 N 个 = N 轮循环，**不操作数量控件**。已达上限点商品不出弹窗 → 跳过剩余次数且不计失败；确认后弹窗不关（如余额不足）→ 该商品记失败；模板与 OCR 都定位不到 → 跳过并记失败。
- **OCR 兜底**：item 配 `ocr_text` 时，模板缺失/未命中自动走 OCR 文字定位（点文字中心，商品卡整卡热区）。仓鼠笼访问器暂无模板图即用此路。项目 OCR 主引擎是 **RapidOCR**（tesseract 仅备选），实测商店截图商品名 conf 0.66-0.79、位置偏差 ≤3px。
- 新 action `zzz_shop_buy`：foreground_flow_runner.py 三处小改（匹配循环特例 break、matched 判定、action 分发到 `buy_shop_groups`）；`_normalize_flow_paths` 已扩展锚定 `confirm_template`/`shop_groups` 内路径。
- **商品图标模板只裁"图标+×N 角标"区**（避开卡名与限购文字，上限后卡面变灰不影响匹配），按用户截图 1919x1052 精确裁；第一行图标区 y 190-410、第二行 y 610-830，卡中心 x ≈ 548/843/1138/1428/1723。真实截图反向验证：确认按钮在无弹窗商店界面仅 0.614（阈值 0.8 不误点），商品图标在被弹窗遮挡的背景上 ≤0.57。
- **音擎残芯悬案**：用户 2026-10-21 明确不是「音擎能源模块」（EXP 卡那个）且本月已购，原误配项已删；待用户提供该商品截图/描述后再加项，勿再凭截图猜。
- 回归：`artifacts/test_zzz_monthly_shop_20261021.py`（monkeypatch 界面状态机 24 项：正常/OCR 兜底/上限/确认卡住/OCR 也未命中/停止，点击次数精确断言）、`artifacts/test_zzz_shop_templates_20261021.py`（真实截图 12 项）、`artifacts/test_zzz_shop_ocr_fallback_20261021.py`（mock grab_screen 跑 RapidOCR 全链 4 项——注意 ocr_window_content 截的是实时屏幕，测图片必须 mock grab_screen，v1 直接传 region 截的是桌面所以零命中）。

#### 商店兑换每月采购：信号残响 / 余烬兑换 / 星尘兑换（2026-10-25，清单待定）

用户 2026-10-25 需求：绝区零 主界面 ESC → 商城(24 HOUR) → 信源清单 → **信号残响**；崩铁 主界面 ESC → 商店 → 兑换商店 → **余烬兑换**；原神 主界面 ESC → 商城 → 尘辉兑换 → **星尘兑换**。三家都每月买点小东西，**具体买什么下个月才告诉**；用户反复强调「一定要进入余烬兑换/信号残响」「一定要确认在星尘兑换（不能看到宗室长剑/宗室大剑/宗室猎枪/宗室秘法录/宗室长弓）」。通用实现是 `runtime/monthly_exchange.py`（action=`monthly_exchange`），三游戏差异全在 flow_configs 的 step 参数里；旧的 `zzz_signal_exchange.py` 已并入此模块（改名，不要再单建一份）。

- **接线位置**：ZZZ_MONTHLY 第 3 步（`等待进入主界面` 之后、`打开功能菜单` 之前）；HSR_MONTHLY 第 2 步，第 1 步同时改成 `starrail_enter_main`；新流程 `GENSHIN_MONTHLY`（第 1 步 `genshin_enter_main`、第 2 步星尘兑换）+ 独立任务 `GenshinMonthlyFlow`（catalog/STANDALONE_TASK_ORDER/`run_genshin_monthly_background`/task_batch_runner 四处接线）。
- **清单位置**：`flow_configs.json → <流程>.steps[每月兑换].items`，项写法 `{"name", "template"?, "ocr_text"?, "count"?}`；没给模板/ocr_text 时用商品名做 OCR 定位。
- **空清单语义（当前状态）**：items 为空 → 只记一条「待定」日志并直接返回成功，**不按 ESC、不点开商店**；绝区零后面的后勤商店采购照常跑。别把空清单改成报失败，否则每月流程白挂。
- **页签强校验（对应「一定」）**：
  - 页签模板一律裁「未选中态」；点完必须等模板消失（= 变选中态）才算进入成功，点两次仍在 → 整步失败，**不进入购买**。
  - 进来时模板就不命中（上次已停在该页签 / 模板失效）：用「页签中心亮度」确认选中态（实测 1919x1079：绝区零选中 145 vs 未选中 78；崩铁 188 vs 53，阈值默认 110；**原神 186 vs 未选中 104/114 → 必须用 150**，原神页签栏底色本来就亮，用 110 会把「原石购买」误判成已选中）。不够就点一次再测；亮度读不到或仍不够 → 失败，**绝不放行到购买**。
  - 崩铁「余烬兑换」会被 RapidOCR 认成「余煨兑换」（conf 0.60，实测）→ 目标文字 OCR 不可靠，step 配 `tab_between_texts=["星芒兑换","星尘之礼"]`，认错时取两者中点（(441,132)/(889,132) → (665,132) 正好是余烬兑换）；原神同理配 `["星辉兑换","原石购买"]`（中点 (898,127) = 星尘兑换）。
- **错页签拦截（绝区零信号余波商品 / 崩铁五星光锥 / 原神宗室武器）**：`forbidden_texts` 在进入页签后、购买前做一次全窗 OCR，命中任意词即失败不买。
  - 绝区零：信号余波的「硫磺石 / 燃狱齿轮 / 嵌合编译器 / 啜泣摇篮」（用户 2026-10-25 要求屏蔽）。**RapidOCR 把「啜泣摇篮」认成「嚼泣摇篮」**（conf 0.61，实测），配置里两个写法都列，别只留正确名导致拦不住。
  - 崩铁：星芒兑换的五星光锥「银河铁道之夜 / 时节不居 / 但战斗还未结束 / 无可取代的东西 / 以世界之名」**一定要消失**（用户 2026-10-25 原话），实测这五个名字在 1919x1079 截图上 conf 0.74-0.87 全部可识别；截图里还能看到「制胜的瞬间 / 如泥酣眠」，用户只点名五件，**别自行加词**（要加先问）。
  - 原神：宗室长剑/大剑/猎枪/秘法录/长弓（星辉兑换页实测 conf 0.71-0.81 全部可识别）。
  - OCR 失败只告警不拦截（页签模板/亮度已是第一道闸）；拦截只在「有商品要买」时执行，空清单不打开商店。
- **导航定位**：绝区零商城入口走模板 `模板/绝区零-商城入口.png`（24HOUR 图标，主菜单 1.000 / 商城页 0.476）+ OCR「商城」（0.67）；「信源清单」纯 OCR（黄色选中行与其它导航行同款，模板区分度不够，选中态 OCR 0.73）；页签模板 `绝区零-信号残响页签.png`（1.000 / 主菜单 0.555）+ OCR「信号残响」（0.80）。
- **崩铁左侧导航必须限定区域**：「兑换商店」同时出现在顶层面包屑 (153,80) 和左侧导航行 (170,382)，全窗 OCR 先命中的是面包屑（点了无效），所以配 `nav_region_frac=[0.0,0.18,0.2,0.95]`。商店入口只有 OCR「商店」（菜单格 (1322,408)，0.67），没裁模板。
- **原神定位**：商城入口 OCR「商城」（ESC 菜单 (196,455)，0.67，没裁模板——左侧面板背景固定，OCR 够用）；左侧「尘辉兑换」OCR（(130,434)，0.74，只有选中态截图，不做模板）；页签模板 `模板/原神-星尘兑换页签.png`（1.000 / ESC 菜单 0.579）+ OCR「星尘兑换」（0.80）。
- **原神启动路径（exe_from_game）**：`apps.GenshinImpact.launcher` 是 BetterGI（一条龙脚本），**不能给每月流程用 exe_from_app**（会启动 BetterGI 而不是游戏）。新增 flow 字段 `exe_from_game`，从 `apps.<key>._discovery.game_exe_path` 取游戏本体（`_resolve_exe_path` 里实现），GENSHIN_MONTHLY 用 `exe_from_game: GenshinImpact` → YuanShen.exe。**不要**再走 GenshinLauncher 的 `exe_path` 硬编码，也不要把它加进「路径配置」的 launcher 列表（那会改坏 BetterGI 启动路径）。
- **购买交互未实跑**：复用后勤商店 `zzz_monthly_shop._buy_once`（新增 `label` 参数只为日志前缀），即「点商品 → 确认 → 确认按钮」。绝区零信号残响沿用 `模板/绝区零-商店-确认.png`；**崩铁余烬兑换与原神星尘兑换的 `confirm_template` 故意留空**（弹窗没截图，不凭猜），下月补清单时必须同时补弹窗截图再填，否则会以「确认按钮模板缺失」诚实失败。
- **退出**：买完先查 `exit_marker_template`（绝区零主世界「···」/ 崩铁主界面图标 / 原神派蒙图标），看不到才按 ESC，最多 3 次；先查再按是防止多按一次又把主世界切回菜单。未确认回到主界面只在摘要里标注，不单独判失败。
- **月度门控在流程层**（`background_template_runner._run_monthly_flow` 的 `monthly_flow:<KEY>`）：本月流程已跑过再补 items，需要手动重跑，不会自动补。
- 回归：`artifacts/test_monthly_exchange_20261025.py`（三游戏：空清单/完整链路/已在菜单/已达上限/页签缺失/商品定位失败/停止/回不到主世界/确认模板缺失/页签已选中/区域导航/页签未切换必须失败/亮度读不到必须失败/原神错页签拦截必须失败/原神 150 亮度阈值 + 配置与独立任务静态）、`artifacts/test_monthly_exchange_templates_20261025.py`（三游戏真图模板正反向 + RapidOCR + 面包屑陷阱 + 选中态亮度夹逼 + 原神宗室武器可识别，参考图 `artifacts/ref_{zzz,starrail,genshin}_exchange_{menu,shop}_20261025.png`）。插入新步骤后 `test_zzz_monthly_main_screen_20261021.py` 的步骤数/序号同步改为 7 步。

### 蓝屏复盘结论 + 任务鲁棒性三层防护 (重要, 2026-10-15)

**2026-10-15 蓝屏根因（微软官方实锤，非 USB 盘）**：BugCheck 0x133，WER 桶名 `0x133_ISR_dxgmms2!UnblockCpuWaiter`，cdb `!analyze -v` 栈回溯确认故障函数 `dxgmms2.sys+0x321bd`（DirectX 图形内核的 GPU 完成等待器），下方是 NVIDIA RTX 5070 驱动 nvlddmkm.sys（2026-07-02 版，注册表 Class\{4d36e968-...}\0000 实锤）——GPU 完成通知未按时回调，ISR 上下文高 IRQL 累计超时，看门狗主动蓝屏。当时在跑终末地自动化（9:21 启动，9:32 脚本异常，9:47:49 崩溃）。USB 盘抽风（UASPStor 129）是并行的另一个硬件问题，与蓝屏无因果。查显卡型号用注册表 `HKLM\SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}\000X`（DriverDesc/DriverVersion/MatchingDeviceId）；**dump 里的 AMDPCIDev.sys 是主板 AMD PCIe 驱动，不代表独显是 AMD**（本次误判教训）。分析工具：`D:\Windows Kits\10\Debuggers\x64\cdb.exe -z <dmp> -y C:\Symbols -sn -c "!analyze -v;q"`（-sn 禁网络符号防卡死；提权读 Minidump 用 ShellExecuteW runas）。

**蓝屏 dump 时间基准铁律（2026-10-15 用两次误判换来）**：
- **日志内容时间戳 = GetLocalTime 墙钟 = 真实时间**，永远可信；cdb 的 Debug session time（崩溃时刻）与崩溃前最后一条日志相差极少（本次 09:46:45 → 09:47:49 仅 1 分钟）
- **dump 文件 mtime / BugCheck 事件时间 ≠ 崩溃时刻**：完整 dump 由下次启动的 crashdmp.sys 补写，WER 事件也是开机后才记录。本次崩溃 09:47:49，但 dump mtime 16:16:28、事件 UTC 08:16:34（=本地 16:16:34）都是用户下午 16:16 开机后产生的
- 判断真实崩溃/重启时间用 `psutil.boot_time()`（LastBootUpTime）；别用 dump mtime 推崩溃时刻，别把"日志停在 X"当成"任务卡了很久"——先算 X 与崩溃时刻的距离
- wevtutil `/f:xml` 的 TimeCreated 带 Z 是 UTC；Kernel-Power 41/EventLog 6008/Kernel-Boot 簇是崩溃重启序列

**任务鲁棒性三层防护（任务线程卡死不再无声无息）**：
1. **日志队列化**（`automation_controller._init_logger`）：GameAutomation logger 只挂 QueueHandler，写盘/UI 转发由 QueueListener 消费线程执行。盘 IO 卡住只卡消费线程，业务线程 `logger.*` 永不阻塞（冒烟：10 条日志入队 0.3ms vs 同步写 20s）
2. **watcher 读超时**（`services.utils.read_file_with_timeout`）：两套日志监控（script_error_log_monitor / ok_ww_log_monitor）的 open/read 全部改带超时（默认 10s，配置键 `read_timeout`），超时放弃本轮、offset 不退，下轮重试；读线程 daemon 化，挂起不拖累进程退出
3. **任务线程心跳看门狗**（`services/task_heartbeat_watchdog.py`）：`_monitor_game` 循环每轮 `beat()`，超过 `task_heartbeat_timeout_sec`（默认 600s，远大于 CHECK_INTERVAL=10s）无心跳 → dump 全线程栈到 watchdog.log + 置 stop_current_task + 杀进程树。与 hang_watchdog（只盯主线程）互补——**主线程 QTimer 照常心跳时任务线程卡死的场景以前完全无感**
- 已验证：入队不阻塞 / 读超时返回 / 看门狗触发+栈落盘 / 持续 beat 不误触发，全部 PASS

### 脚本报错日志监控（ok- 系 pyappify 脚本）(重要)

- **pyappify 壳脚本有两套日志目录，监控必须指对**：
  - 外壳日志：`<app>/logs/app.*`（只有启动阶段内容，**python 子进程报错不在这里**）
  - python 子进程日志：`<app>/data/apps/<name>/working/logs/ok-script.log`（**常驻主日志，无日期后缀**，报错都在这里）
- watcher 生效需**三个条件同时满足**（2026-10-15 终末地实例全部缺失，导致脚本 09:32 异常后空转 7 小时到蓝屏）：
  1. `_discovery.monitors.<name>` 或顶层 `<name>` 段有 `abort_on_error: true`（`build_script_error_watchers` 只构建有该键的 watcher）
  2. `log_dir` 指向 `working/logs`，`file_pattern` 为 `ok-script*.log`
  3. `require_date_token: false`——`_matching_files` 默认要求文件名含当天日期，`ok-script.log` 无日期后缀会被排除。误判防护靠 watcher 构建时的 `_initialize_offsets` 记录当前偏移，只读启动后新增行
- `error_keywords` 与旧配置字段 `error_markers` 等价（build 已兼容两个名字），别再造第三种命名
- ok-ef 的 `ensure_main(time_out=600)` 超时会抛 `Please start in game world and in team!`，关键字必须包含它

### 签到状态双写铁律 (重要)

- **启动前签到（`services/startup_sign_runner.py`）签完后必须同时写两处**：① SQLite tracker（`record_sign_result`）② `app_config.json` 的 `{section}.last_sign_date/last_sign_status/last_sign_message`（helper `_write_sign_status_to_config`，与 bridge `_update_sign_status` 同字段）。DailyView 的签到行对 wuwa/endfield/nte **只读 last_sign_***，只写 tracker 会导致 UI 显示"今日尚未签到"（2026-10-12 实例：异环实际签到成功 +80金币，last_sign_date 却停在 3 个月前）。
- **nte 分支过滤签到任务禁止用 `endswith("签到")`**：`_inject_external_log_progress` 里只应精确丢弃 `task_name == "游戏签到"`（游戏内由 ok-nte 代表）。`BBS签到`（社区 API 成果）和 `异环签到`（community 源）必须保留，否则成功的签到记录被丢后 UI 只能走"今日尚未签到"兜底。
- 米游社三件套（genshin/honkai_sr/zzz）走 tracker `sign_` 前缀任务，不经 last_sign_*，两条路径不要混。

### 鸣潮启动器更新流程 (重要, 2026-10-16)

- 鸣潮官方启动器**有游戏更新时右下角按钮从「进入游戏」变为「更新」**（同位置青色渐变按钮，只文字不同）。`flow_configs.json` 的 `WuwaLauncher` 流程已加 4 步：①「游戏更新」click_template `模板\鸣潮-更新.png` + `optional_if_missing: true`（无更新跳过，timeout 8s）②「等待更新完成」wait_template `鸣潮-进入游戏.png`（timeout 3600s，下载完按钮变回进入游戏）③「进入游戏」④ wait_process
- 两个按钮模板**灰度互不混淆**（同场景匹配 1.0，交叉 0.625 < 阈值 0.8），裁模板用 HSV 青色分割定位右下角按钮
- `optional_if_missing` 的语义是 **timeout 到点才跳过**（循环内搜到即 break），所以"检测某状态不存在"的步骤 timeout 要短（8s），否则无更新时每次空等
- 下载中按钮若变「更新中 xx%」导致更新模板失配 → optional 跳过 → wait_template 等「进入游戏」回来即可，流程不会卡死

### OCR 防护两条铁律 (重要, 2026-10-17)

**背景事故**：2026-10-17 09:59-10:03 XM 流程整体失败。窗口不可用 → OCR 安静回退全屏 → 记事本里开着的 `auto_runner.log` 被拍进截图 → OCR 把日志文字当成「进入脱机」假命中（0.87）→ 点到记事本上 → 确认失败 → 步骤重试到耗尽。即注释里的"自己干扰自己"。

- **铁律 1：窗口不可用不许回退全屏**。`runtime/ocr_click_tester.py` 的 `ocr_window_content(logger, hwnd, region)`：传了 hwnd 但 `_ensure_window_region` 返回 None（最小化/已关闭）→ 先 `_restore_window_if_minimized()` 尝试 SW_RESTORE 还原；仍失败 → 打 ERROR（30s 节流）+ **返回 `[]`**。上层按"未匹配"重试，`run_flow` 的 `_refresh_flow_window` 会重新枚举窗口——比全屏瞎认正确。区域优先级不变：显式 `region` > hwnd 窗口区域 > 全屏（仅 hwnd=None 且 region=None 时才真全屏）。
- **铁律 2：日志污染硬拦截，不只告警**。`_check_log_contamination(logger, results) -> List[Dict]` 现在**返回剔除后的结果**：命中 `_LOG_SIGNATURE_PATTERNS` 的 OCR 条目一律剔除、禁止参与匹配。阈值策略：≥2 条才节流告警（防噪音）；**任何 1 条命中即剔除**（防漏网）。目标文字都是界面标签（进入脱机/登录/开始），不含日志特征模式，剔除不会误杀目标。
- 回归测试：`artifacts/test_ocr_guard_20261017.py`（7 个用例：干净零误剔 / 污染剔除 / 单条也剔 / 死窗口不抓屏 / 还原成功后继续 / 显式 region 优先）。改这块代码后必须跑。
- 模板匹配路径 `_locate_template_in_window` 本来就是 `rect=None → return None`，没有全屏洞，别改出洞来。

### 流程结束关闭进程 (重要)

- `runtime/foreground_flow_runner.py` 的 `close_flow_processes(logger, flow_key, pid, process_names)` 是统一收口：先按 PID 杀**整棵进程树**（`ProcessSupervisor.safe_kill_process`），再按 `window_process_names` **按名兜底**清残留。
  - Electron 类应用（TRAE SOLO / MiniMax Code / WorkBuddy）由启动器拉起子进程，只 `terminate()` 启动 PID 会留下真实窗口进程 —— 必须按名兜底。
  - **自身保护**：`_protected_pids()` = 当前进程 + 全部祖先 PID，绝不在清理时自杀（WORKBUDDY 流程与 GUI 宿主同名，靠这个保护）。
- `run_flow` 用 `try/finally` 保证收尾：`close_process_on_finish=true` 时成功/失败都关；`close_process_on_fail=false` 可只在失败时保留进程便于排查。
- **例外**：`stop_after_step` 提前返回（XM 后台流程跑到"运行中确认"）**不会**关闭进程 —— 挂机类流程要继续跑。
- 薅羊毛集合（`background_template_runner.run_wool_background`）对每个子流程额外兜底关闭，默认关闭、`close_process_on_finish=false` 才保留，日志尾缀 `（已关闭程序）` / `（程序保留）`。集合含 `WOOL_FLOW_KEYS = MINIMAX / WORKBUDDY / TRAE / QODER`。
- **假成功事故（2026-10-21 WorkBuddy，用户怒点）**：`optional_if_missing: true` 的步骤找不到元素时**直接跳过并报成功**——WorkBuddy 流程 11 秒没等到「立即领取」按钮（单实例应用，用户旧会话的加油站弹窗早已自动消失）→ 跳过 → 报 ✅ 实际没领。**规则：optional 的点击/等待步后面必须跟一个非 optional 的验证步**（确认达成态，如 MINIMAX 的「今日已签到」二步结构），否则弹窗不在时无法区分"已领取"和"没领到"。
- **绝不能杀用户正在用的软件（同日事故）**：WorkBuddy 是用户的日常聊天软件（GUI 还常从其进程树外启动，`_protected_pids` 护不住），`close_process_on_finish/fail` 必须 `false`。TRAE/QODER 是同类 IDE，用户可在 GUI 薅羊毛页自行勾选关闭，但**默认建议 false**；给正在使用的聊天/IDE 类软件开自动关闭前必须先确认。
- **弹窗是自动轮换的展示卡（2026-10-21 用户截图三连修正）**：WorkBuddy「Buddy加油站 每日可领 100 积分」弹窗为左下角**轮换 carousel**，至少三张卡（加油站→积分包购买→做任务赚积分），所以「立即领取」按钮**只有轮到的那个间隙可见**。展示一段时间后会整体退场，**悬停不唤回、二次启动不重弹**（单实例）。靠它领不可靠——用户提的"延迟太高点错"在轮换场景下是真实风险（匹配→点击间隙卡片转走），仅靠确认失败重试兜底不够。
- **常驻入口（2026-10-21 用户指点，流程已重构走这条）**：**左下角头像 → 菜单「Buddy加油站」→ 加油站页面**（可领时显示「立即领取」，已领显示「今日已领取」，积分余额在头像菜单里可见）。头像菜单任何时候都能打开，不依赖轮换弹窗。流程七步：点头像（`模板/hy4-左下角头像.png`，OCR 关）→ 点菜单行（`模板/hy4-Buddy加油站菜单.png`，OCR 兜底）→ 等「立即领取」（optional）→ 点领取+确认（optional）→ **再点头像（optional）→ 再点菜单行（optional）→ 确认「今日已领取」（非 optional，导航失败就老实 ❌）**。
- **领取后加油站页面会整个关闭（2026-10-27 实锤，假失败事故）**：10-27 流程点完「立即领取」（领取成功）后页面退回底下的聊天窗口，验证步在 74 秒里扫了 15+ 轮都找不到「今日已领取」→ 判 ❌，用户实看已领取。修复即在点击与验证之间插两个 optional 重新导航步——页面还开着时头像被盖住、超时跳过，验证照常命中；页面关了则重新进站再看。**不能**删掉非 optional 验证步来"修"这个问题（会退回 10-21 假成功事故）。
  头像模板是小图标、与窗口大小无关；用户换头像后模板失效（诚实失败，重裁即可）。回归 `artifacts/test_wool_workbuddy_20261021.py`（25 组，七步结构），参考截图固化在 `artifacts/ref_workbuddy_*_20261021.png`。
- **挂机类流程禁止开启关闭**：`CJ`（重聚脱机）是挂机流程，跑完必须继续运行，`close_process_on_finish` 与 `close_process_on_fail` 均为 `false`，配置里已加 `_comment` 标注，不要改。XM 走 `stop_after_step="运行中确认"` 提前返回，同样不关。

### 薅羊毛日志核验 (2026-10-24)

- **MiniMax**：`%APPDATA%/MiniMax/logs/api-MM-DD.log` 的
  `POST .../minimax-cloud/api/v1/signin/claim`，HTTP `status 200` 且
  `baseRespStatusCode:0` 才算成功。2026-10-24 12:18:10 日志实锤领取成功，
  但旧 UI「今日已签到」确认误报 ❌ —— **日志比 UI 准**。
- **TRAE**：`%APPDATA%/Trae CN/logs/<YYYYMMDD...>/main.log` 的
  `ttnet fetch response .../checkin_credits/claim 200 OK`。
- **WorkBuddy**：没有可用的本地领取日志（`%LOCALAPPDATA%/WorkBuddy/logs/main.log`
  停在 2026-09-03，`debug.log` 只有 crashpad 错误），继续用 UI「今日已领取」
  非 optional 确认；不要假装日志能覆盖它。
- `flow_configs.json` MINIMAX/TRAE 末步 `action=wool_verify_log`
  （`runtime/wool_claim_logs.py`）：只读当天日志，找不到领取记录就 ❌，
  不回退成假成功；MiniMax 额外用「今日已签到」模板兜底（已签到当天不会重复发 claim）。
- WorkBuddy `close_process_on_finish/fail` 必须是 false（2026-10-24 复查发现
  配置曾被设成 true，已改回）。回归 `artifacts/test_wool_claim_logs_20261024.py`。

### 薅羊毛 2026-10-25 三连 ❌：UI 确认比实际动作更脆弱

当天 12:40 结果 `MINIMAX ❌ WORKBUDDY ❌ TRAE ❌ QODER ✅`，三家根因都不是"没点"：

- **TRAE 是假失败**：12:36:34 日志已有 `checkin_credits/claim 200 OK`（领取成功），
  流程却卡在「等待签到参与抽奖弹窗」——该弹窗不一定出现，且原配置 max_attempts
  让它空等 3 分钟，没走到末步日志核验。修复：抽奖弹窗两步 `optional_if_missing`、
  等待 15s/1 次；成败仍由末步 `wool_verify_log`（非 optional）决定。
- **WORKBUDDY 是确认太早**：`confirm_text_change` 原实现只在 `wait_time`（≤3s）
  后查一次 OCR，`timeout_sec` 只作用于模板；而领取接口实测 ~6s 才回，
  4 次重试全部误报"未检测到文字变化"。修复：`confirm_text_change` 改每 2s 轮询
  到 timeout_sec；配置上点击步直接 `confirm_type=none`，由后续非 optional 的
  「今日已领取」验证（保留 10-21 规则：optional 点击后必须有非 optional 验证）。
- **MINIMAX 是真没领到**：`api-10-25.log` 只有 `signin/status`，没有 `signin/claim`
  （10-24 是有的）。旧侧边栏「签到得」入口已随 3.1.6 新 UI 失效，步骤1 的
  `confirm_type=disappear` 也必然超时。**新 UI 实测（用户截图）**：左下角昵称 →
  菜单「每日签到」→ 签到卡片（卡片显示「本轮已连续签到 N 天」/「今日已签到」）。
  流程已改为 5 步：点昵称 → **悬停**菜单「每日签到」（浮层卡片，不点击）→
  等卡片（OCR 连续签到）→ 可选点「立即签到」→ 末步日志核验；模板
  `minimax-昵称.png` / `minimax-每日签到菜单.png` / `minimax-今日已签到.png`，
  阈值必须 0.9（按钮模板在无关页面有 0.855 误命中，菜单行关闭时只有 0.50）。
  参考截图 `artifacts/ref_minimax_ui_*_20261025.png`。**未签到状态下的按钮文案
  还没有截图**，当前靠 OCR「立即签到」可选点击 + 日志核验；若次日仍无 claim，
  先补一张未签到卡片图。runner 新增 step 级 `hover: true`（只 moveTo 不点击，
  `_hover_window_local`，同样受 fail-safe 保护）。
- 回归 `artifacts/test_wool_flow_fixes_20261025.py`（33 项：配置约束 +
  text_change 轮询 + 日志解析 + MiniMax 新 UI 三模板在用户截图上的命中/误命中 +
  hover 坐标换算）；既有 confirm/wool/Qoder 4 套件 ALL PASS。
- 改 `flow_configs.json` / Python 后必须重启 GUI 才生效。

### Qoder 每日积分领取流程（2026-10-20 接入薅羊毛集合）

- **任务事实**：Qoder（AI IDE，`C:\Users\admin\AppData\Local\Qoder\Qoder Launcher\Qoder Launcher.exe` 启动，真实进程 `Qoder.exe`）每天 10:00（UTC+8）刷新 100 Credits，领取后 30 天有效。活动弹窗由 app 内嵌 webview（Surface）承载，标题「专属活动权益 立即查收」。
- **弹窗行为模型（日志实证，`%APPDATA%/com.qoder.app.stable/logs/<session>/main.log` 的 `[Campaign]` 行）**：
  - `claimable=true`（今日未领）**不保证启动自动弹**：2026-10-24 全天的会话日志都是
    `hasClaimableCampaign:true` + `shouldShowCampaignPopup:false`，新启动也没有
    `source:automatic` 的 Surface 事件。**不能把「启动自动弹窗」当前提**。
  - 手动入口确实存在：2026-10-24 15:40 用户从 `source:"settings_menu"` 打开了
    Surface（`活动 Surface 已打开 ... "claimable":true,"source":"settings_menu"`）。
    具体菜单项位置仍待截图补模板；不要再用「USAGE placement 礼品图标」的旧猜测。
  - 点「领取」→ 服务端 `claimStatus: CLAIMABLE → CLAIMED`；验证以 Qoder 日志为准。
- **流程设计**（`flow_configs.json` 的 `QODER`）：等待领取按钮（30s，optional）→
  点击领取（optional）→ **`qoder_verify_claim` 非 optional 日志核验**。
  核验读取最新 `main.log` 的 `[Campaign]` 状态：`CLAIMED/claimable=false` 才算成功，
  `CLAIMABLE/claimable=true` 或读不到状态一律失败。**禁止再靠三步全 optional 静默报成功**。
- **`close_process_on_finish/fail` 必须 false**：Qoder 是用户日常 IDE，绝不能关。
  `reuse_existing_window=true`：已在跑就复用窗口不新拉进程。
- **2026-10-24 事故（用户实锤）**：12:21（薅羊毛）和 12:53（QoderFlow）两次 QODER
  流程都没找到「领取」→ 三步 optional 全跳过 → 报 ✅，并且
  `close_process_on_finish: true` 把 Qoder 进程树杀了两次；当天 credits 一直未领。
  修复：关闭开关改回 false；末步换成日志核验 `runtime/qoder_claim.py`；
  回归 `artifacts/test_qoder_claim_20261024.py`。
- **常驻入口（2026-10-24 用户截图实锤）**：左下角头像 → 菜单「权益中心」
  （gift 图标带红点）→ 活动弹窗。`runtime/qoder_claim.open_campaign` 驱动：
  菜单行模板 `模板/qoder-权益中心.png`（185x36）；活动页只认
  `qoder-领取.png` / `qoder-已领取.png` 模板，**禁止用泛化「领取」OCR**。
- **2026-10-24 实跑假阳性教训**：Qoder 窗口被聊天窗口遮挡时，OCR 把聊天正文里的
  「领取」当成了弹窗按钮 → 等待步命中、点击步模板没命中却跳过，最后靠日志里的
  `hasClaimableCampaign=false` 误判成功。现在：打开/等待/点击全部走模板；
  窗口遮挡 ≥30% 先聚焦重试，仍遮挡直接诚实失败，绝不 OCR 其他窗口。
- **模板匹配阈值踩坑（2026-10-20 实测）**：`_match_template_cv2_once` 的生效阈值是 `min(match_confidences)`——列表写 `[0.88,0.84,0.8]` 实际按 0.8 匹配！QODER 的「领取」模板在 Qoder **设置页侧边栏**有 0.835 误命中（聊天页 0.595），0.8 档会放过关进而误点侧边栏。QODER 用 `[0.94,0.92,0.9]`（生效 0.9）：真实弹窗命中 1.000、误命中天花板 0.835，分离干净。**新建 Electron 应用流程务必先测误命中再定阈值**。
- 模板：`模板/qoder-领取.png`（黑底白字按钮，48x37）、`模板/qoder-已领取.png`（灰底白字，62x38）。点击步 `enable_ocr_fallback=false`（OCR 子串匹配会把「已领取」当「领取」命中，且描述行「领取后 30 天有效」也含"领取"）。
- **假失败事故（2026-10-21 10:03，服务端日志实锤）**：领取其实**成功了**（点击后 7.5s 服务端 `CLAIMABLE→CLAIMED`），但消失型确认连报 4 次"元素仍存在"→ 假 ❌。根因是 `confirm_disappear` **只在 +3s 单次检查**——Qoder 领取成功后弹窗要 **~30s 才关闭**（昨天实测：点击后 30s 弹窗消失；今天按钮持续可匹配 25s+）。
  修复：`confirm_disappear` 改为**每 2s 轮询直到 timeout_sec**（2026-10-21），QODER 点击步 `confirm_timeout_sec=45`。回归 `artifacts/test_confirm_disappear_poll_20261021.py`（8 组：延迟消失等到/永不消失超时/确认模板组合/立即消失快速返回）。
  阈值防线：QODER `match_confidences=[0.97,0.96,0.95]`（生效 0.95）——黑模板对灰「已领取」按钮真实交叉匹配只有 0.754（干净复测；昨天说的 0.922 是测量 bug：循环里覆盖了模板变量导致复合缩放，作废），真匹配 1.000，分离干净。
- OCR 对「领取」有子串误中风险（描述行含"领取"），仅等待步开 OCR 兜底、点击步纯模板。

### UIPI 权限护栏（2026-10-20 白荆实测，重要）

- **现象**：目标游戏以**管理员**运行时，普通权限进程的所有合成点击（pyautogui/SendInput/mouse_event）被 Windows **静默拦截**——鼠标会动、游戏没反应，流程表现为"每步匹配超时/未匹配模板"，极易误判成 UI 改版或坐标失效。
- **根因**：UIPI（User Interface Privilege Isolation）阻止低完整性进程向高完整性进程窗口发送输入。
- **解法**：本程序必须同样以管理员运行（`start_gui_admin.bat` / `00_START_HERE_GUI_ADMIN.bat` 自带 `Start-Process -Verb RunAs` 自提权）。GUI 设计上就是提权跑的；手动跑脚本验证时也要提权（UAC 弹窗需用户点"是"）。
- **护栏**：`foreground_flow_runner._uipi_block_reason(pid)`（`_process_is_elevated` 读 TokenElevation）在 `run_flow` 窗口就绪后预检——仅"本程序普通 + 目标管理员"才拦，报可行动的错并直接失败，不再让步骤匹配超时把问题带偏。回归：`artifacts/test_uipi_guard_20261020.py`（8 组权限组合）。
- **排查手法**：`psutil` + `OpenProcessToken`/`GetTokenInformation(TokenElevation=20)` 比对两进程权限；提权后同坐标点击立即生效即可实锤。
- **UIPI 只护 `run_flow` 路径，白荆 `execute_task` 路径没有预检（2026-10-25 实锤）**：用非提权驱动跑白荆（artifacts 单游戏驱动脚本），启动器「开始游戏」能点中（启动器是自己拉的非提权进程），但游戏本体（腾讯反作弊 TQMCenter 全家桶，提权）之后**所有点击/ESC 全被静默拦截**——"已点击关闭公告"打了日志但弹窗纹丝不动，常态匹配 ESC 按 10 次全无效。日志特征签名：`SetWindowPos 置顶失败: (5, 拒绝访问)` + `_terminate_process` 一片"终止进程异常"（OpenProcess 被拒）。**白荆/游戏内流程的任何驱动（CLI、测试、临时脚本）必须和 GUI 一样提权跑**；诊断时看到"点击日志正常但画面不变"先查权限再查模板。

## SQLite / GUI 卡死避坑 (重要)

- **WorkflowDB 必须用 WAL**：`storage/workflow_db.py` 构造时已统一执行 `PRAGMA journal_mode=WAL`。默认的 rollback journal 模式下写事务独占全库，多视图后台线程并发访问时读者全被阻塞 —— 实测 10 线程并发最慢单次 1.88s，开 WAL 后降到 0.55s。**不要**改回 DELETE 模式。
- **`_connect()` 是 `@contextmanager`，不是普通方法**：它 `yield` Connection 并在 `finally` 里 `close()`。调用点保持 `with self._connect() as conn:` 即可自动提交 + 关闭。**禁止**改成 `return conn` 的普通函数（历史 bug：只提交不 close，导致连接与文件句柄持续泄漏）。**禁止**写成 `conn = db._connect()` 裸调用。
- **GUI 卡死定位看 `logs/watchdog_main_thread.log`**：`services/hang_watchdog.py` 由主线程 `QTimer` 2s 心跳驱动，主线程冻结超 15s 时由独立 daemon 线程 `faulthandler.dump_traceback(all_threads=True)` 转储全线程栈。下次报"界面卡死"先读这个文件，能直接定位到代码行。
- **看门狗栈文件按故障类型分家**（2026-10-18）：两类硬故障原先混写 `logs/watchdog.log`，事后只能靠时间戳猜。现在 `settings/paths.py` 两个常量对应两个文件：
  - `WATCHDOG_MAIN_LOG_FILE` = `logs/watchdog_main_thread.log` ← `hang_watchdog`（主线程无心跳，`app/gui.py` 构造）
  - `WATCHDOG_TASK_LOG_FILE` = `logs/watchdog_task_heartbeat.log` ← `task_heartbeat_watchdog`（任务线程无心跳，`automation_controller` 构造）
  两个 dump 文件都受 `MAX_DUMP_BYTES`（5MB）保护，超了归档为带时间戳的副本（不删除历史）。**别再把新看门狗的 dump 指回单一 watchdog.log**，也别改文件名常量而不更新本条笔记。
- **多视图并发是常态**：`Main.qml` 每个 Loader 都是 `active: ... || xxEverLoaded`（切过即永不卸载），所有切过的视图后台线程仍在跑。新增视图的后台线程**必须**是 daemon 线程 + 网络请求带 timeout，否则会拖死主线程。
- **不要误判为网络问题**：IO 等待会释放 GIL，不会阻塞主线程。GUI 冻结的真正嫌疑是「后台线程长时间占 GIL」—— 通常是重活 import、CPU 密集计算、或 SQLite 写等待。

## 长等待必须可中断 (重要)

- **流程里的每一处长等待都必须接受停止回调**：`runtime/foreground_flow_runner.py` 的 `run_flow` 启动阶段有两处历史黑洞 —— `time.sleep(startup_wait_sec)` 与 `_wait_window_for_flow(pid, timeout=45)`。后者是纯 `time.sleep(0.6)` 轮询，**不看停止标志**，导致用户点「停止」后最长要等 45 秒才生效，表现为"按钮失灵 → 狂点 → 日志刷屏"（2026-09-11 实例：点了 27 次，40.3 秒后才关进程）。
- **新增等待一律传 `stop_check`**：`run_flow(..., stop_check=...)` / `_wait_window_for_flow(..., stop_check=...)` / `_wait_window_by_pid(..., stop_check=...)` 已支持；流程内用 `_sleep_interruptible(seconds, stop_check)` 与 `_stop_requested(stop_check)`，轮询粒度 0.25s。调用方传 `lambda: controller.stop_current_task`。
- **`stop_check=None` 必须保持旧行为**（向后兼容，勿改成必填）。中断时的收尾仍由既有 `close_on_exit` / `close_on_fail` 决定 —— 不要为"停止"另开一套关进程规则，否则会破坏 CJ 的"禁止自动关闭"。
- 仍未接线：`run_wool_background` / `_run_monthly_flow` 内部 `run_flow` 未传 `stop_check`（签名无 controller），其 45s 窗口等待仍不可中断。

### 失败诊断记录 (重要, 2026-10-18)

**为什么要做**：改动前"步骤没匹配上 / 任务失败"只落 `logs/auto_runner.log` 的 warning
自由文本 —— 没有截图、没法按流程/步骤聚合、跨天回看只能全文 grep 日志；
`workflow_runs` 表字段齐全却**零调用**（恒空），`daily_progress_errors` 只记 API 查询失败；
`artifacts/ocr_debug/` 恒空（唯一写它的入口属于已停用的 Flet 测试路径）。

- **一站式入口**：`runtime/flow_diagnostics.record_failure(logger, flow_key, step, error,
  hwnd=, game_key=, retry_count=, ocr_results=, ocr_sample=, save_image=, extra=)`
  → ① 抓失败当刻窗口截图 `artifacts/flow_failures/{时间}_{流程}_{步骤}.jpg`（宽 960 / JPEG q72，约 50KB）
  → ② 抓当帧 OCR 文本（`文字(0.90)@x,y` 形式，最多 15 条）
  → ③ 写 `workflow.sqlite3` 的 **`flow_failures`** 表（结构化：ts/date_key/flow_key/game_key/
  step/error/retry_count/hwnd/window_title/image_path/ocr_texts/extra_json）
  → ④ 按 mtime 清 14 天前截图（`FLOW_FAILURE_KEEP_DAYS`，同进程 10 分钟节流）
- **DB 侧**：`WorkflowDB.record_flow_failure / query_flow_failures / summarize_flow_failures /
  purge_old_flow_failures`。建表用 `CREATE TABLE IF NOT EXISTS`，老库首次打开自动迁移，无版本号。
- **埋点（新增失败务必接）**：`foreground_flow_runner.run_flow` 步骤重试耗尽（模板/OCR 未匹配）、
  `task_batch_runner._run_single_task` 状态为 failed、`baijing_daily._record_step_failure`
  （白荆俱乐部每日任务 / 每日外勤赠礼失败）。
- **三条铁律**：
  1. **永不抛异常** —— 调用点都在失败分支里，诊断自己炸了必须被吞掉（连 logger 抛异常也不能
     把已写库的 id 吞掉）；
  2. **不重复识别** —— 调用方已有当帧 OCR 结果时务必传 `ocr_results` 复用，一次失败重试动辄
     几秒，别为了记录再跑一遍 RapidOCR；
  3. **窗口不可用不退化全屏** —— 传了 hwnd 但 `GetWindowRect` 失败就直接不存图，否则会把用户
     桌面截图存进证据目录（与 `ocr_click_tester` 的全屏 OCR 防护同源教训）。
- **出报告**：`tools/flow_failure_report.py`（`--days 7 --top 20 --verbose --json --purge 30
  --flow BaiJing`）。按 (flow_key, step) 聚合次数 → "哪一步最常失败、最该优化"，
  附最近一次时间、是否有证据截图、样例错误、失败当帧屏幕文字与图片路径。
- **GUI/WebUI 入口**：`DailyView.qml` 底部「最近失败记录」卡片（bridge
  `getRecentFlowFailures(days) -> str` JSON，后台线程读库最多等 3s；截图缩略图点击
  `openFlowFailureImage` 开系统看图片、`openFlowFailureDir` 开证据目录）；WebUI
  `GET /api/daily/failures?days&limit&flow` + `GET /api/daily/failure-image/{name}`
  （`webui/static/app.js` 的 `loadFailures()`）。**`getRecentFlowFailures` 必须写
  `@Slot(int, result=str)`**，漏 `result` 时 QML 拿到 undefined 且不报错。
- **脚本级失败没有截图**：`task_batch_runner` 埋点拿不到 hwnd（进程已退出），只记文字；
  `capture_failure_image` 传了 hwnd 但 rect 取不到时**直接不存图**，绝不退化成全屏截图。
- 回归：`artifacts/test_flow_diagnostics_20261018.py`（落库字段/查询过滤/聚合/清理/报告渲染/
  不抛异常/埋点仍在/三个 bridge slot 的 MetaObject 返回类型，共 48 项断言）。

## 定时任务补跑 (2026-10-21)

- GUI 定时任务保存在 `app_config.json` 的 `scheduled_tasks`，`UiBridge._sched_timer`
  每 30 秒检查一次；GUI 必须常开才会触发。撞上正在跑的任务会进入
  `_pending_scheduled` 排队，到点不丢。
- 每个任务可带 `skip_if_done_today: true`（UI 勾“仅补跑今日未完成”）：到点后
  `_fire_scheduled_task` 传 `force_rerun=False`，`run_task_batch` 会按
  `_is_game_script_done_today` 逐游戏跳过已完成项；缺省/False 保持旧的强制重跑。
- **`_is_game_script_done_today` 脚本成功 ≠ 当日完成**（2026-10-24 修正）：
  白荆回廊把部分完成（1/15 任务、活跃度 5/100）也记 success（10-20 设计），
  曾导致手动运行被"今日脚本任务已全部成功"跳过。现在脚本行全成功之外还要求
  `completion_state ∈ {completed, unknown}`——关键指标未满（pending）或有关键
  失败一律视为未完成，允许手动/补跑重新执行。副作用：活跃度不满的游戏
  （如鸣潮 40/100）在当天第二次批次会重跑一次脚本（幂等，无害）。
  手动运行想无视跳过逻辑就勾 AutomationView 的"强制重跑"勾选框。
  回归 `artifacts/test_done_today_predicate_20261024.py`（12 项）。
- **qBittorrent 未配置不再报错**（2026-10-24）：`QBITTORRENT["path"]` 默认空，
  `Popen([""])` 曾在每批次抛 WinError 87；`ensure_qbittorrent_running` 现在
  空路径直接跳过（debug 级日志）。要用 qBittorrent 就在设置里配好路径。
- 2026-10-21 晚已删除 11:05 / 11:35 两条固定兜底，只保留 Qoder 10:01：
  固定时间兜底每天空跑，而各游戏更新日期/开服时间不同，靠时间猜不智能。
- 维护日改为**开批前预检**：`UiBridge._split_maintenance_targets` 在
  `run_task_batch` 前逐个 `get_maintenance_state`，维护中的游戏从本批剔除并按
  公告开服 +5min 排队补跑，其余游戏照常执行；全部维护中时传空列表（仍执行启动前签到）。
- 批次串行：`startAutomation` 在锁内同步置 `_is_running`，同一 30s tick 内
  定时任务/更新补跑不会并发启动；`email/shutdown` 作为批次参数传入，全部任务
  结束后统一发一封日报、只调度一次关机。
- 改 `ui/bridge.py` / QML 后必须重启 GUI 进程才生效（运行中的 GUI 不会热加载
  Python/QML）。

## 游戏更新日历与动态补跑 (2026-10-21)

- `services/game_update_calendar.py` 拉官方公告 `getAnnContent`：
  原神国服 `hk4e_cn`、崩铁 `sg-hkrpg-api.hoyoverse.com/common/hkrpg_global`、
  绝区零 `sg-announcement-static.hoyoverse.com/common/nap_global`，`lang=zh-cn`。
  公告时间为 UTC+8，解析“更新时间 / 更新开始时间 … 06:00 … 预计 N 个小时”，
  `开服 = 开始 + N 小时`。缓存 `storage/cache/game_update_calendar.json`（6 小时 TTL）。
- 2026-10-21 实测：原神 7.1 2026/09/23 06:00+5h、崩铁 4.6 2026/09/28 06:00+5h、
  绝区零 3.3 2026/10/21 06:00+5h（今天 11:00 开服）。
- `automation_controller._monitor_game` 给三款米游加 `progress_hint`：公告维护窗口内
  快速跳过（`GAME_UPDATING_SKIP`），不白等 launch 超时。
- `UiBridge` 动态补跑：维护跳过/失败 → `_handle_update_retry_result` 按公告开服 +5min
  写入 `storage/cache/update_retries.json`；`_check_update_retries` 到点用
  `force_rerun=False` 补跑（最多 3 次）；补跑成功/已无需补跑/达到上限后自动移除；
  `_unfired_scheduled_today` / 关机顺延包含补跑条目。
- 维护窗口边界用严格小于：11:00 开服、buffer 5 分钟 → 11:05 视为已开服，
  避免 11:05 的补跑又被维护检查跳过。
- 2026-10-21 扩展：鸣潮用官方 CMS
  `media-cdn-mingchao.kurogame.com/akiwebsite/website2.0/json/G152/zh/`
  （ArticleMenu.json + article/<id>.json，正文「更新维护时间：2026年9月30日04:00 ~ 11:00」）；
  异环用官网静态公告列表 `yh.wanmei.com/m/news/gamebroad/index.html`
  （des 含「10月15日06:00 - 11:00」）；无限暖暖用官方
  `https://infinitynikki.nuanpaper.com/api/news?offset=0&limit=30` 列表 +
  `/news/<id>` 详情页（服务端渲染），解析「2026年10月12日10:00-11:00（UTC+8）
  进行停服维护更新」；实测 2.10 的 10/12 更新为 10:00-11:00。只有接口失败时
  才退回 10:00-11:00 窗口兜底。日期来自公告本身，不要再用固定窗口猜日期。
- 维护检测有两层：`UiBridge._split_maintenance_targets` **开批前预检**（维护中的
  游戏不进入本批，直接排队，避免空跑/半开启动器）；`task_batch_runner._run_single_task`
  **启动前**再查一次（覆盖 GUI 未预检、WebUI/CLI 调用、批次中途进入维护的情况）。
  公告维护窗口内直接 skipped（不启动 launcher/脚本），桥接层按公告开服 +5min 排队；
  `automation_controller` 的 progress_hint 覆盖三款米游 + 鸣潮 + 无限暖暖，
  异环保留 ok-nte 更新日志检测并叠加公告维护状态。
- Qoder：`flow_configs.json` QODER 加 `not_before: "10:00"`；新增独立
  `QoderFlow`（`STANDALONE_TASK_APPS`）只跑 QODER，定时 10:01；QODER 的
  `close_process_on_finish=false`，**不许关 Qoder**；定时任务支持任务级
  `email_on_finish=false`，补跑任务不重复发邮件。
- 定时配置（2026-10-21）：Qoder 10:01（QoderFlow）；固定兜底 11:05 / 11:35 已删除，
  维护补跑完全由动态队列驱动（`storage/cache/update_retries.json`）。
- 改 bridge/QML 后必须重启 GUI 才生效。

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
## genshin.py 已验证事实 (sourced from site-packages/genshin/)

### 登录方式与 stoken 获取

| 登录方式 | genshin.py 方法 | 返回 stoken? | 备注 |
|---------|-----------------|-------------|------|
| passport-api 扫码 | `login_with_qrcode()` → `QRLoginResult` | **NO (model丢弃)** | QRLoginResult 模型只6字段，但 `client.cookie_manager.cookies` 含完整 Set-Cookie（含 `game_token`）→ 可转 stoken |
| Web 密码登录 | `login_with_password()` → `CNWebLoginResult` | **NO** | CN `loginByPassword` 接口 Set-Cookie 不含 login_ticket/game_token/stoken。JSON body 的 data 被 genshin.py 丢弃 |
| 短信验证码登录 | `login_with_mobile_number()` → `MobileLoginResult` | **NO** | 同上，Set-Cookie 不含可转换成 stoken 的字段 |
| App 密码登录 | `login_with_app_password()` | **YES** | **仅海外** (`@region_specific(OVERSEAS)`)，国服不可用 |
| hk4e-sdk 扫码 | 手写（本项目） | **NO** | 已删除 |

### game_token → stoken 转换

- API: `POST https://passport-api.mihoyo.com/account/ma-cn-session/app/getTokenByGameToken`
- genshin.py 实现: `fetch_stoken_with_game_token()` at `site-packages/genshin/client/manager/cookie.py:L222-L243`
- DS 签名: `generate_passport_ds(body)` with salt `"JwYDpKvLj6MrMqqYU6jTKF17KNO2PXoS"`
- 请求体: `{"account_id": int, "game_token": str}`
- headers: `x-rpc-device_id`, `x-rpc-device_fp`, `x-rpc-app_id: "bll8iq97cem8"`, `DS`
- 返回: `StokenResult(aid, mid, token)` — `token` 就是 stoken 字符串
- **source**: passport-api 扫码的 `queryQRLoginStatus` JSON body (`resp.data["data"]["game_token"]`) 中包含 `game_token`，**不在 Set-Cookie 里**。genshin.py 的 `_check_qrcode()` 丢弃了 body 中除 status 之外的所有字段，所以必须用 `_raw_request` 直接抓
- **已验证失败**: hk4e-sdk 的 game_token 调此接口返回 retcode -100 "登录状态失效"
- **本项目实现**: `complete_qrcode_login()` 中用 `_get_stoken_by_game_token_via_genshin()` 转换

### Geetest 行为验证

- genshin.py 对战迹/便笺 API 触发 `GeetestError` (retcode 10035/5003/10041/1034)
- genshin.py 内部尝试 `create_mmt()` → `solve_geetest()` → 用 `x-rpc-challenge/seccode/validate` 头重试
- **但 `verify_mmt()` 仅海外** (`@region_specific(OVERSEAS)`)，CN 没有 verify 端点
- **结果**: 国服即使走完滑块，重试仍然失败 (retcode=10041 或 5003)
- **已知死路**: 自编 `verifyVerification` URL 不存在 (retcode -1)
- **国服唯一解法**: cookie 中必须包含 stoken，直接跳过 Geetest
- **本项目策略**: `check_games_progress()` 中先 `enrich_cookie_for_record_api()`，无 stoken 则跳过便笺查询，不触发风控

### 战迹/便笺 API 端点 (国服)

| 游戏 | URL |
|------|-----|
| 原神 | `https://api-takumi-record.mihoyo.com/game_record/app/genshin/api/dailyNote?role_id={uid}&server={server}` |
| 星铁 | `https://api-takumi-record.mihoyo.com/game_record/app/hkrpg/api/note?role_id={uid}&server={server}` |
| 绝区零 | `https://api-takumi-record.mihoyo.com/event/game_record_zzz/api/zzz/note?role_id={uid}&server={server}` |

DS 签名: `generate_cn_dynamic_secret(body=None, query=params)` + salt `DS_SALT[CHINESE]`

### Cookie 使用

- `genshin.Client("stoken=xxx; stuid=123; cookie_token=yyy; ...")` **原生支持 cookie 字符串**
- `cookie_manager.cookies` 是 `dict[str, str]`，直接取 `cookies.get("stoken")`
- 国服便笺查询需要 cookie 中包含 stoken，否则必触发 Geetest

### 禁止做的事情

- **禁止编造 API 端点**: 所有端点必须能在 genshin.py 源码或官方文档中找到
- **禁止硬编码 DS salt**: 从 `genshin.constants.DS_SALT` 动态读取
- **禁止用 x-rpc-challenge/seccode/validate 头调战迹 API**: 该方式未在 genshin.py 中出现，是瞎编的
- **禁止假设 hk4e-sdk game_token 能换 stoken**: 已验证失败 (retcode -100)
## 每日进度「完成度」口径 (重要)

- **`progress_percent` 有两个完全不同的口径，禁止混用**：
  1. **脚本子任务比例**（`ok_ww_daily_parser` 等）：ok-ww 跑完 5 步就是 100%，
     与玩家真实游玩进度**无关**
  2. **真实游玩进度**：如鸣潮活跃度 `cur/total`（90/100 → 90%）
- `ui/bridge.py` 的 `UiBridge._real_completion_percent(game_key, tasks)` 是统一取值入口：
  按游戏找"关键进度"任务（鸣潮=今日活跃 / 原神=每日委托 / 绝区零=活跃度 / 暖暖=日活跃），
  用 `cur/total` 算百分比；`cur<=0` 视作未刷新返回 `None`；无关键任务返回 `None` 降级旧逻辑。
- 合并外部日志进度时**必须**用 `_real_completion_percent` 覆盖 `progress_percent`，
  并写 `progress_source`（真实任务名 或 `"script"`），供 UI 区分口径。

### 库街区（鸣潮）活跃度刷新限制

- **活跃度需进入游戏后才会刷新**，未进游戏时 API 返回 `cur=0`。
  直接把 0 写库并标 pending，UI 会显示"今日活跃 0/100"，实际玩家可能已 90/100。
- 写入 tracker 时必须判断 `cur<=0 且 total>0` → extra 加 `stale=True` + `stale_reason`，
  UI 显示"（未刷新）"而不是真实百分比（见 `DailyView.qml`）。
- 历史事故：2026-09-22~09-27 连续 6 天库里都是 0/100，用户投诉后才发现。

### 回答数值类问题必须实跑取证

- 涉及"完成度/进度/活跃度/体力"等数值结论，**先实跑 API 或查 SQLite**，
  禁止凭代码推断或沿用缓存数据下结论。用户在 2026-09-27 明确要求过"自动查一遍"。

### 发邮件前必须实时查进度（禁止发缓存数据）

- `ui/bridge.py::_send_daily_email_report` **不得**直接用 `tracker.load_today()` 发信。
  一条龙跑完往往已过几十分钟~几小时，库里可能还是跑之前的数据
  （2026-09-27 事故：库里活跃度 0/100，真实 100/100）。
- 必须走 `self._refresh_daily_snapshot_for_report(tracker_factory)`：
  子线程跑 `check_games_progress(tracker=tracker)` 实时查并写回库，
  `join(timeout=120)`；超时/异常降级到缓存但**绝不阻塞随后的关机流程**。
- 返回 `(snapshot, fresh)`，`fresh` 要传给 `build_daily_report(snapshot_fresh=...)`，
  邮件正文会据此标注"发信前实时查询"或"⚠ 缓存数据，可能不是最新"。
- 单测 `UiBridge` 纯逻辑方法时用 `UiBridge.__new__(UiBridge)` 绕过 QApplication 依赖。

### 每日进度「完成」判定口径（历史统计用）

- **禁止用 `is_all_success` 判定某游戏当天是否完成**：鸣潮/暖暖的任务列表混有
  资源与周本（结晶波片 132/240、千道门扉 0/6000），这些本来就不要求满，
  用"全部任务完成"会导致完成率永远为 0。
- 统一用 `GameDailyRecord.completion_state()`，返回四态：
  `completed` / `unknown`（关键计数未刷新）/ `failed` / `pending`。
  判定优先级：关键任务 `real_progress_percent()` → `is_all_success` → `has_failed`。
- **统计完成率时，分母必须排除 `unknown` 天**（官方 API 未刷新，并非真的没做）。
  鸣潮踩过：修正前 0%，修正后 87%（完成 7 / 有效 8，未刷新 22 天不计入）。
- `KEY_PROGRESS_TASKS` 的关键任务名**必须与实际数据源返回一致**，
  否则未刷新数据会被误判成"真未完成"。已验证：
  `wuwa=今日活跃`、`nikki=朝夕心愿`（whimbox 日志口径；Pearpal API 侧叫"每日心愿"
  满值 500，常年 0/500 不刷新——**Pearpal 落库已于 2026-10-24 停用**，5 项假进度
  任务（每日心愿/周常奖励/能量体力/派遣/登录天数）删除，名字仅为兼容历史记录）、
  `genshin=每日委托`、`zzz=活跃度`、`endfield=日活跃`、
  `nte=活跃度`（塔吉多 roleHome 的 dayvalue，2026-10-24 接入，上限 100）。
  脚本型游戏无计数，用 `KEY_STATUS_TASKS` 按状态判定：`nte=日常领取`、
  `nikki=朝夕心愿`（success=完成，failed=失败，优先级高于计数口径）。

### 每日进度 UI 呈现口径（2026-10-19 用户拍板，快赢档）

- **卡片结论（徽标/底色/汇总条）只认 `completion_state`**：活跃度满→"全部完成"
  绿底；未满→"未完成"米白底；关键指标失败→"有失败"红底；计数未刷新→"未刷新"。
- **禁止在 `pending` 分支回退 `is_all_success/has_failed`**：白荆活跃度 15/100 +
  俱乐部每日任务失败，旧回退逻辑把卡片染成红底"有失败"——细枝末节失败只留在
  任务明细，不拖卡片结论。绝区零"活跃度已满"+5 个边角失败同理：必须"全部完成"，
  汇总条计入完成而非失败（旧口径把这种计进"失败"不计"完成"）。
- `DailyView.qml` 主列表徽标/底色/汇总条（`buildStatusText`）与歷史回看段
  四态映射必须一致；`progress_source`（`ui/bridge.py::_attach_completion_state` 补）
  区分百分比口径：真实计数（活跃度）= 游玩进度直接显示，脚本子任务比例加
  "脚本"前缀——白荆活跃度 15/100 曾被误显示"脚本 15%"。
- 已完成卡片的头部右侧只显示完成度，不翻边角失败的旧账（"失败 N 项"仅出现在
  失败结论卡片；未完成卡片以"完成度 · 失败 N 项"后缀形式带出）。

### 事后日志复核口径 (重要)

- `task_batch_runner._verify_script_log_result` 只做 success→failed 改判，
  读取的是各脚本的**当日报告文件**，与森空岛/官方 API 数据无关。
- **终末地复核必须合并查询接口的日活跃**（`_load_api_key_progress_tasks`，
  用户拍板口径 2026-10-05）：API 日活跃 100/100 时即使 ⭐送礼 等边角任务失败
  也不改判；API 数据缺失时退回纯脚本口径（含"ok-ef 未正常结束"兜底）。
- **无限暖暖复核以朝夕心愿为关键判定**（`KEY_STATUS_TASKS["nikki"]`，用户拍板
  2026-10-06）：whimbox 解析器恒定输出 8 项任务，朝夕心愿 success 即整体完成、
  failed 才改判；美鸭梨挖掘/星海拾光等边角任务失败不触发整轮重试。
- **原神判定以 BetterGI 日志为准，双向覆盖**（`_apply_bettergi_verdict`，
  用户拍板 2026-10-24）：监控跟踪「委托奖励 + 探索派遣」两项标记
  （`bettergi_log_monitor._process_line`）。未确认全部完成 → 失败（只降不升）；
  **日志已确认全部完成 → 覆盖超时/退出异常一票否决**（超时只是收尾被杀），
  判成功且天然不触发统一重试。用户主动停止不覆盖；监控不可用保持 fail-closed。
  米游社便笺（每日委托计数）不再作为原神判定依据——cookie 过期时它整月无数据，
  曾把判定退化成"BetterGI 脚本行说了算"（原神 68% 完成率的根因）。
  回归 `artifacts/test_genshin_verdict_20261024.py`（14 项）。
- **2026-10-26 复核：原神失败 = BetterGI 自己跑坏 + 委托本来就没人做，两件事同时成立**
  - BetterGI 一条龙配置（`User/OneDragon/默认配置.json`）实际启用的 6 个任务是
    **领取邮件 / 每日地脉花 / 领取每日奖励 / 领取尘歌壶奖励 / 狗粮 / 至冬一条龙**，
    里面**没有"做每日委托"的任务**，只有"领取"。所以委托必须在那之前被别人做完，
    `▶ "领取『每日委托』奖励"` 才会出现；10-24 09:50、10-25 10:08 都成功领取，
    10-26 09:10 两次都是 `未完成或者已领取` → 当时确实没有可领的委托。
  - 同时今天 BetterGI 自己坏了：真实日志里 **43 条 `[ERR]`**，样本
    `assets/icon/add_button.jpg匹配异常`、`我辣么大一个地脉花哪去了？`、
    `地脉花处理失败 (重试3/3): 开启地脉花失败，已重试3次，终止处理`、
    `未能返回主界面`、`打开大地图失败，请检查按键绑定「打开地图」`、`传送失败`、
    `找不到出战角色编号块`；狗粮 09:11→09:21 十分钟就异常收尾（平时约 45 分钟），
    09:38 游戏退出。
  - **判定口径不变**（不放宽）：`委托奖励 + 探索派遣` 未被日志确认 → 原神判失败。
    但 `failure_message()` 现在会把两件事分开写清楚，并把 BetterGI 自身报错带进文案：
    `BetterGI 日志未确认今日委托奖励完成 —— 委托奖励：BetterGI 判定「未完成」并跳过领取
    （一条龙 6 个任务里只有「领取每日奖励」，没有做每日委托的任务）。本次 BetterGI
    自身报错 43 条（示例：…）`。监控新增 `error_count` / `error_samples`（最多 3 条，
    取 `[ERR]` 行的下一行正文；下一行还是日志头就不采），日报 `format_bettergi_daily_reward_summary`
    也会多一行报错。回归 `test_genshin_verdict_20261024.py` 用例 [6][7]（[7] 直接回放
    今天的真实日志，锁住"报错被抓到 + 委托没被误判成完成"）。
  - 排查入口：BetterGI 日志目录 `D:\Program Files\genshin ai\BetterGI\BetterGI\log\better-genshin-impact<日期>.log`，
    报错截图 `avatar_side_classify_error.png`、`User\OneDragon\*.json` 看一条龙任务清单。
- **崩铁（March7th）活跃度数值已接入判定**（用户拍板 2026-10-24）：
  解析器取日志 INFO 行「当前累计分数：X/500」（一天只涨不跌，取最后一次；
  无分数行但「每日实训已完成」→ 500/500 兜底）产出「活跃度」计数任务
  （extra current/total），`KEY_PROGRESS_TASKS['honkai_sr']` 早已注册，
  复核自动走真实进度口径：达标 → completed 不改判。超时侧由
  `_apply_starrail_score_verdict` 兜底（仅 script=March7th 启用）：超时但分数
  已达标 → 改判成功、不触发统一重试；未达标/无分数行 → 原判。
  注意既有行为：无计数时「每日实训」布尔任务名命中 KEY_PROGRESS_TASKS →
  completion_state=unknown → 复核从不改判（改判失败侧全靠超时兜底）。
  回归 `artifacts/test_starrail_activity_20261024.py`（22 项）。
- **绝区零（OneDragon）超时兜底 + KEY_STATUS 口径**（用户拍板 2026-10-24）：
  OneDragon 日志只有枚举状态（活跃度已满/未满/领取成功），**没有数值**，
  无法像崩铁那样算百分比。超时侧由 `_apply_zzz_activity_verdict` 兜底：
  超时 + 「活跃度已满」→ 改判成功不重试；仅认"已满"（"日常奖励领取成功"
  可能是中途领箱，不算）；未满/无数据 → 原判。`KEY_STATUS_TASKS['zzz']=
  ("活跃度奖励",)` 让复核不被体力刷本等边角失败拖垮（已满 → completed）。
  回归 `artifacts/test_zzz_activity_verdict_20261024.py`（14 项）。
- **异环（塔吉多）dayvalue 接入 + 社区任务/暖暖假进度删除**（用户拍板 2026-10-24）：
  塔吉多 H5 游戏名片接口 `GET /apihub/awapi/yh/roleHome?roleId=`（从
  yh-game-record 前端 bundle 挖掘并实测）返回 `dayvalue`（每日活跃度，上限 100）、
  体力、城市体力。`TajiduoClient.fetch_role_home()` 封装，`check_tajiduo_daily`
  产出「活跃度 X/100」+「体力/城市体力」（计量不参与完成判定）+ 游戏签到。
  复核：nte 分支合并 `_load_api_key_progress_tasks`，**dayvalue 满 → 直接
  return None**（ok-nte 边角失败不再改判烧重试）；不满/无数据 → 旧口径。
  同批删除：①社区任务（点赞/浏览/分享，恒 pending 无信息量）停止查询与落库，
  getUserTasks 仍供签到流程内部使用；②无限暖暖 Pearpal 5 项假进度停止查询
  与落库（check_nikki_daily/NIKKI_EXPECTED_TASKS/_record_nikki_to_tracker 整体删除），
  暖暖进度只来自 whimbox 日志。注意：`_load_api_key_progress_tasks` 只取
  source != "script" 的记录，塔吉多侧落库 source 必须 community。
  回归 `artifacts/test_nte_dayvalue_20261024.py`（25 项）。
- **ok-ef 一天可产出多份报告**（异常重跑、执行中快照），`find_today_ok_ef_report()`
  必须取时间戳最新一份；报告里空列表的占位符「无」不是任务名，解析时必须滤掉
  （2026-10-05 事故：读到 15:00 异常快照 +「无」被当任务名 → "无: 未知原因" 改判失败并烧掉一次重试）。

### 历史数据

- `daily_progress` 表自 2026-05-19 起有 132 天 / 2556 条记录。
- `clear_old_records(keep_days=7)` **当前无任何调用点**（所以历史才保留至今）。
  若将来接上自动清理，历史回看功能会只剩 7 天数据 —— 接入前务必确认。

### pyautogui fail-safe (重要)

- 本版本 pyautogui 的 fail-safe 触发条件是**鼠标位置精确等于 (0,0)**（`FAILSAFE_POINTS = [(0,0)]`），
  不是"角落 100px 区域"。鼠标顶到屏幕左上角 = 用户请求紧急停止，pyautogui 拒绝执行。
- **禁止裸调 `pyautogui.click`**：一律走 `runtime/foreground_flow_runner.py` 的
  `_pyautogui_click_guarded(x, y, clicks, interval, button)`。它把 FailSafeException
  转成带中文说明的 `FailSafeAborted`，由 task_batch_runner 收口成优雅失败
  （`should_stop_batch=True`，不重试不继续），而不是 traceback 崩溃。
  2026-09-29 CJ 事故：窗口贴屏幕左上角 + 用户鼠标随手放 (0,0) → 整个任务崩。
- **测试进程难以复现触发**：DPI 虚拟化会让 moveTo(0,0) 落到屏幕中心、
  SetCursorPos(0,0) 后 position() 返回 (1,1)。验证收口逻辑用 mock 抛 FailSafeAborted。
- click_sequence.py 自己管理 FAILSAFE（点击序列暂停期间禁用），不在本约定范围。

### 收尾动作必须 finally 兜底 (重要)

- `ui/bridge.py::_run_automation_backend` 的**邮件、自动关机、每日进度刷新、
  WebUI 状态复位、finished/running 事件全部放在 finally 里**，禁止移回 try 主体。
- 原因：任何任务内未捕获异常都会穿透 run_task_batch；写在 try 主体里的收尾
  会被整体跳过 —— 2026-09-29 中午事故：CJ 抛 FailSafeException，
  shutdown_on_finish=true 却没关机、邮件也没发，用户挂机跑完机器整夜没关。
- 异常路径会往报告追加 `❌ 任务异常中断（收尾兜底已执行）: <异常>`，
  邮件照发、关机照调，UI 弹"运行失败"提示。
- 验证方式：mock `task_batch_runner.run_task_batch` 抛异常，
  断言 `_schedule_shutdown` / `_send_daily_email_report` 各被调用一次、
  finished 事件发出、running 复位为 False。

### @Slot 必须声明 result 类型 (重要)

- **QML 会使用返回值的 bridge 方法，@Slot 必须写 `result=<类型>`**（如 `@Slot(result=str)`、
  `@Slot(str, result=bool)`）；纯动作（无返回值）才用 `@Slot()`。
- `@Slot()` 丢返回值后 Qt 按 void slot 处理，QML 拿到 `undefined`——不报错、不崩，
  只会静默变成空数据/恒 false，极难排查。
- 2026-09-29 事故：`getSetupItems` 缺 `result=str` → 路径配置向导 18 项全空（"已配置 0/0"）；
  同区域 `checkFileExists`/`saveSetupPaths` 同病 → 路径验证恒红叉、保存成功也不跳转。
- 排查手法：`UiBridge.staticMetaObject` 遍历 method，看 `typeName()` 是否为预期类型；
  或正则扫描「`@Slot(...)` 无 result 但函数带 `-> 类型` 注解」。
- 注意：**用 stub bridge 做的 QML 实例化测试测不出这类问题**（stub 自己写了正确 result），
  必须核对真实类的 MetaObject。

### 鸣潮启动链 (重要)

- `apps.WutheringWaves.prelaunch_flow = "WuwaLauncher"`：跑鸣潮任务时先执行
  `flow_configs.json` 的 `WuwaLauncher` 流程（启动官方启动器 launcher.exe →
  模板/OCR 点击「进入游戏」→ wait_process 等 `Wuthering Waves.exe` 出现），
  **完成后才启动 ok-ww**。机制由 `automation_controller._run_prelaunch_flow` 提供。
- 启动器进程：launcher.exe 启动后主进程是 `launcher_main.exe`（PID 不同），
  窗口跟随靠 `window_title_keywords`（['wuthering','waves','鸣潮','launcher']），
  不要只依赖启动 PID。
- launcher 最小化到托盘时 `ShowWindow(SW_RESTORE)` 需要**同完整性级别**（GUI 以管理员
  运行才能恢复管理员的启动器窗口）。非管理员进程恢复无效会表现为"模板一直匹配不到"。
- WuwaLauncher 开了 `enable_ocr_fallback=true`（OCR 找「进入游戏」兜底），
  模板分辨率与实际屏幕不符时靠它救。

### 无限暖暖进入主界面 (2026-10-22)

- `apps.InfinityNikki.prelaunch_flow = "NikkiLauncher"`：先跑前置流程再启动 whimbox。
  启动器是叠纸 `launcher.exe`（窗口标题「无限暖暖」、窗口进程 `xstarter.exe`，
  1280x720 实测 OCR「启动游戏」conf 0.80），不是 `run_one_dragon.bat`。
- 流程只有一步 `action=nikki_enter_game`（`runtime/nikki_launcher.py`）：
  启动器「启动游戏/更新/运行中」→ 等 `X6Game-Win64-Shipping.exe` 窗口 →
  登录页 OCR「点击进入游戏/确认/登录」→ 加载小月卡/道具弹窗 →
  左下角聊天气泡模板 `模板/暖暖-主界面.png` 连续 2 次命中。
- 模板来源：whimbox 2.5.6 `IconPageMainFeature` 二值化裁剪（GPL-3.0 本地自用）；
  36x30，左下角 (48,989)@1920x1080，合成背景匹配 0.99，阈值 0.8。
- `close_process_on_finish/fail=false`：游戏要留给 whimbox；whimbox 的
  `AllInOneTask` 检测到已在主界面会置 `is_already_in_game=True` 并跳过自己的
  启动/进入步骤（`StartGameTask` 返回「已成功进入游戏」）。
- 登录页防误点：OCR 同时出现「退出游戏/账号登出」时不点「确认」；加载弹窗
  点底部中央 (0.5,0.83)，早期再点顶部中央 (0.5,0.09)。
- 回归 `artifacts/test_nikki_launcher_20261022.py`（模板/启动器 OCR/已在主界面/
  启动链路/更新态/登录防误点/主界面连续命中/配置接线）。

### 崩铁进入主界面（复刻三月七方法, 2026-10-24, 默认不启用）

- `StarRailLauncher` 流程 + `action=starrail_enter_main`（`runtime/starrail_launcher.py`）
  复刻 March7th Assistant v2.2.0.3 的进入方法（其本地 logs/2026-10-19、10-23 实证）：
  **直接启动 StarRail.exe（不走官方启动器）→ 循环检测「点击进入」→ 点击 →
  等主界面特征连续 2 次命中**。模板分离度（三月七日志实测）：click_enter 真屏
  0.98+/加载界面 ≤0.31；main.png 主界面 0.96-1.00、其他界面 ≤0.55。
- **默认不接线**：`apps.HonkaiStarRail` 不配 `prelaunch_flow`，一条龙仍由 March7th
  自己启动游戏；要启用时在 app_config.json 加 `"prelaunch_flow": "StarRailLauncher"`。
  `close_process_on_finish/fail` 必须 false（游戏要留给 March7th 用）。
- 模板复制自 March7th `assets/images/screen`（本地自用）：崩铁-点击进入.png（101x27
  「点击进入」文字）/ 崩铁-主界面.png（32x29 特征图标）/ 开始游戏 / 月卡 / 欢迎 /
  消息通知（后四张为可选点击弹窗模板，匹配到才点）。
- OCR 兜底只认「点击进入」，**不能用"进入游戏"**——加载页问候语
  「156*****68，欢迎进入游戏」含该子串会误点问候语；OCR 见「前往启动器」字样
  快速失败（客户端未更新，等下去只有超时）。阈值 0.9（match_confidences=[0.92,0.9]）。
- 回归 `artifacts/test_starrail_launcher_20261024.py`（26 项：模板合成匹配/
  进程校验/全链路/已主界面零点击/弹窗/OCR 兜底+快速失败/退出路径/接线且未启用）。
  测试踩坑：`_run` helper 内部统一 patch `_click`，外部**再嵌套 patch 会内层覆盖外层**，
  状态机点击函数永不执行 → 循环死转（time.sleep 被 patch 成 no-op 后 600s 超时内纯热转）。
- **HSR_MONTHLY 也走这套进入方法（2026-10-25）**：月度流程第 1 步从「点一下点击进入」
  改成 `starrail_enter_main`（去月卡/欢迎弹窗 + 主界面连续 2 次命中），第 2 步是
  `monthly_exchange` 余烬兑换（见「商店兑换每月采购」一节）。

### 原神进入主界面（复刻 BetterGI 方法, 2026-10-24, 默认不启用）

- `GenshinLauncher` 流程 + `action=genshin_enter_main`（`runtime/genshin_launcher.py`）
  复刻 BetterGI 0.66 `GameTask/GameLoading` 的进入方法（其 Recognition.json 实证）：
  **直接启动 YuanShen.exe（不走米哈游启动器）→ 「点击进入/进入游戏」→ 空月祝福
  弹窗领取 → 等主界面特征连续 2 次命中**。
- ROI 沿用 BetterGI 的 rect(cw*a, ch*b, ...) 比例写法：「点击进入」在下半屏中 1/3、
  「进入游戏」在中央带 (685,595,549,64)@1920x1080 外扩、主界面（paimon_menu 派蒙
  图标 38x40，BetterGI 全任务共用的主界面判定）限定左上角 (0,0,0.3,0.3)。
- 模板复制自 BetterGI 安装目录（本地自用）：原神-点击进入 / 原神-进入游戏 /
  原神-主界面 / 原神-空月祝福。
- 空月祝福弹窗：welkin_moon_logo 标记命中后 OCR 点「领取」，无 OCR 命中按
  (0.5w, 0.8h) 比例兜底点击。OCR 入口文字只在下半屏中 2/3 区域内认——
  防止类似崩铁"欢迎进入游戏"问候语的区域外误点。
- **默认不接线**：`apps.GenshinImpact` 不配 `prelaunch_flow`，一条龙仍由 BetterGI
  自己启动游戏；要启用时加 `"prelaunch_flow": "GenshinLauncher"`。
  `close_process_on_finish/fail` 必须 false（游戏留给 BetterGI 用）。
- 回归 `artifacts/test_genshin_launcher_20261024.py`（36 项：四模板合成匹配/
  ROI 换算/进程校验/两条入口路径/已主界面零点击/空月祝福 OCR+比例兜底/
  OCR 区域防护/退出路径/接线且未启用）。
- **GENSHIN_MONTHLY 月度流程（2026-10-25）**：第 1 步复用 `genshin_enter_main`，
  第 2 步 `monthly_exchange` 星尘兑换（见「商店兑换每月采购」一节）。启动游戏用
  新字段 `exe_from_game: GenshinImpact` 取 `_discovery.game_exe_path` 的 YuanShen.exe；
  **不能用 `exe_from_app`**（apps.GenshinImpact.launcher 是 BetterGI）。
  独立任务 `GenshinMonthlyFlow` 已接入 catalog / STANDALONE_TASK_ORDER /
  `run_genshin_monthly_background` / task_batch_runner 分支。

### CredentialInput 双向同步 (重要)

- `components/CredentialInput.qml` 的 `text` 属性**必须保持双向同步写法**：
  `property string text: ""` + `onTextChanged` 写穿 `__input`，`__input.onTextChanged` 回写
  root.text（带 `!==` 守卫防循环）。**禁止**改回纯绑定 `property string text: __input.text`——
  纯绑定下外部赋值（onCredentialsChanged 回填）只改 property，内部输入框不更新，
  表现为"凭据存了但输入框是空的"。
- 塔吉多/鸣潮/终末地三个页面的所有凭据框（token/password/deviceid/手机号）都用此组件，
  页面内**不允许残留裸 HusInput 接收凭据**。
- 测试注意：HusInput 内部真实 TextInput 在 contentItem（Loader）里，不在 children 链，
  实例化测试直接对 HusInput 自身 `property("text")` 断言。

### 脚本日志解析结果必须落库 (重要)

- `services/whimbox_daily_parser` 等日志解析器产出任务时**必须带 `extra={current,total}`**：
  bridge 的注入合并与 `tracker.record_task` 都从 `extra` 取计数，缺 extra 会让
  「朝夕心愿 500/500」这类真实进度被当成 0（2026-10-07 暖暖"无有效数据"根因之一）。
- **内存消费 ≠ 数据落地**：`bridge._inject_external_log_progress` 只改当次页面显示，
  `task_batch_runner._recheck` 只用于失败复核——两者都不写 tracker。凡是"完成判定关键任务"
  （见 `KEY_STATUS_TASKS` / `KEY_PROGRESS_TASKS`）的解析结果，都必须有落库路径
  （如 `daily_checker._record_whimbox_script_tasks`），否则统计/历史/邮件永远拿不到。
- Pearpal notebook 的「每日心愿」常年返回 0/500（接口不刷新），
  暖暖真实进度**只存在于 whimbox 日志**，判定必须以 whimbox 侧为准。
- `KEY_PROGRESS_TASKS` 的任务名必须与解析器产出的**精确字符串一致**（大小写、全角）：
  zzz 实测是「活跃度奖励」不是「活跃度」，nikki 是「朝夕心愿」（whimbox 侧）。

### 关键判定任务分两类口径 (重要)

- **计数型** → `KEY_PROGRESS_TASKS`：API 返回 X/Y（wuwa 今日活跃、nikki 朝夕心愿、
  endfield 日活跃、genshin 每日委托）。拿不到计数（cur=0/缺 extra）→ `unknown`，
  统计时**排除出分母**（真的可能是官方未刷新）。
- **纯状态型** → `KEY_STATUS_TASKS`：日志/接口只给"已满/未满/成功/失败"、
  **天然没有 X/Y 计数**（zzz 活跃度奖励、nte 日常领取、nikki 朝夕心愿 success 分支）。
  按 status 判定，**绝不能走计数口径**——否则"活跃度已满"会被误判成"数据未刷新"
  （2026-10-08 实例）。
- `completion_state()` 里 KEY_STATUS_TASKS 优先级最高，其次 real_progress_percent()。
- **判定关键任务所在的脚本日志必须落库**（`daily_checker._record_*_script_tasks`）：
  只在 bridge 内存注入/失败复核里用完就丢的话，统计与历史回看永远拿不到。

### 白荆回廊日常流程改视觉版 (重要, 2026-10-18)

- 日常从「固定坐标序列回放」（`click_sequences/白荆回廊-日常.json`，100+ 步录于
  1.0.2.1082）重构为 `runtime/baijing_daily.py` 视觉版：主界面 → 点右侧「任务」按钮
  → 日常任务列表 → 一键/逐个「领取」→ 关闭列表回主界面。旧 JSON 保留作归档，
  `BAIJING_DAILY_SEQ` 常量仍在但不再默认回放。
- **「任务」按钮识别**：OCR 优先（RapidOCR 认成「一任务」，0.62 可用），模板
  `模板/白荆回廊-任务按钮.png` 兜底（1920 参考分辨率，红色角标已 inpaint 抹除，
  匹配时按窗口宽度换算缩放）。**不要先裁剪区域再 OCR**——实测裁剪会改变检测结果
  （右侧裁剪后「一任务」直接丢失），必须全窗识别后按 `TASK_BUTTON_REGION`（fx≥0.70）
  过滤；主界面中部「阶段任务154/200」也含「任务」，另有黑名单排除双保险。
- **「前往」类未完成任务暂不自动跳转**：会进战斗/抽卡/养成等子活动，需按活动类型
  分别实现，v1 只记录（`process_task_list` 的 `goto_seen`）。要补的话先截图各子活动页面。
- **日常任务静态目录**（2026-10-18 实测提取，列表每日相同）：`DAILY_TASK_CATALOG`
  15 项，理论总分 **140**（研发室紫角徽章无数字，分值 10 由用户确认）。
  **活跃度口径（用户 2026-10-18 确认）：里程碑 20/40/60/80/100 五档，满 100 即
  领完全部日常奖励**（叶脉联结页「日常任务累计活跃度达到100」任务可印证）；
  记录的「活跃度」任务 current 按 `DAILY_ACTIVITY_TARGET`(100) 封顶、total=100，
  原始实得（可超 100，用户实测 130）保留在 extra.earned。
  流程滚动列表逐屏 OCR（行聚类 ±40px + 目录关键词匹配），聚合出 15 个任务的
  (X/Y) 状态，核算活跃度记入「活跃度」任务（`KEY_PROGRESS_TASKS["baijing"]`，
  extra.current/total 口径，与 wuwa 一致）。
- **研发室「前往」任务已自动化**（`_handle_lab_harvest_task`，2026-10-18 用户截图实测）：
  点行内「前往」→「白荆穹顶」设施弹窗 →「一键**收取**」（注意是收取不是领取！）→
  点弹窗 X（关闭提示文字右侧 +25px）或空白处关闭 → 回日常列表，随后该任务的
  「领取奖励」由常规领取循环处理。其余「前往」类任务（战斗/漫巡/茶歇等）流程
  未实测，仍只记录不点击（`goto_seen`），要补按此模式先截图再实现。
- **记忆烙痕经验「前往」任务已自动化**（`_handle_memory_exp_task`，同日用户截图实测）：
  点行内「前往」→ 烙痕列表页（3列卡片网格）→ 点首个卡片（实测 (402,442)@1919x1079，
  → 窗口相对 (0.21,0.41)，列距≈0.15/行距≈0.38 作兜底候选）→ 详情页右下「强化**烙痕**」
  →「重置」→「+」一次 →「确认」→ 等级提升动画「点击屏幕继续」→ `_return_to_task_panel`
  逐层返回任务面板。新模板 `模板/白荆回廊-强化烙痕按钮.png`（匹配 conf=1.000；
  注意按钮是"烙痕"不是"熔垣"，低分辨率图样字易看错，用户 2026-10-18 指正）；
  「+」按钮位置由「重置」OCR 位置 + 0.1225 倍窗宽偏移推算，不写死坐标。
  `_return_to_task_panel` 返回顺序：已在面板→主界面重开「任务」→ 退出确认框点「确认」→
  页面左上角「返回」按钮（fx<0.2,fy<0.15 区域）→ ESC，最多 5 层。
- **场景退出登记表已扩到 7 个场景**（`SCENE_RULES`，2026-10-27 失败取证驱动补登记）：
  原有 过场遮罩/茶憩摘要/关注特写/休息室 之外，新增 **烙痕详情**（特质提升/烙痕等级；
  10-27 10:18 实锤 +1/确认都点中了、等级提升都出了，只因页面不在登记表里恢复认不出，
  连环拖死茶憩和叶脉联结）、**茶憩房间**（茶憩纪念函/飞镖/棒球 并集——10:22 当帧 OCR
  只认出房间陈设，纪念函没进识别结果；话题名"古典余温"每日轮换不能当特征）、
  **有猫零售**（有猫精选/精选礼包/有猫推荐；10-26 20:10 当帧流程漂进零售页认不出）。
  烙痕详情/有猫零售 = 点左上「返回」即走（共享 `_exit_via_top_left_back`）；
  茶憩房间 = 返回 → 确认框（与休息室同交互），退出后回休息室由下一轮恢复接手；
  烙痕详情返回后的列表页无独占特征，如实报 unknown 交通用兜底。
  照证据补登记的闭环继续有效：`未匹配任何已知场景` 的当帧文字就是候选特征来源。
  回归 `artifacts/test_baijing_scene_registry_20261026.py`（新增 [4.5] 节 9 项 + 顺序断言 7 条）。
- **午后茶憩「前往」任务已自动化**（`_handle_tea_break_task`，同日用户 7 张截图实测）：
  点行内「前往」（关键词「午后茶」，OCR 常把"歇"认成"憩"）→「是否前往茶憩?」确定
  (0.62,0.63) → 角色选择页（今日可邀份额）点首个角色 (0.41,0.42) → 茶憩场景喝茶 →
  等待 10s 后再来一杯（共 2 轮）→「茶憩结束」摘要随意点击 → 休息室「返回」→「确认」→
  主界面。喝茶/再来一杯是场景内动态交互（截图未捕捉按钮形态），OCR 轮询文字点击，
  轮询不到只记警告不盲点 3D 场景；话题菜单（选择话题）出现时点第一个话题兜底。
- **显影行动等四个关卡战斗「前往」任务已自动化**（`_handle_stage_battle_task`，
  同日用户截图实测 + 用户确认操作相同）：显影行动/自由贸易/定向物资保障/光刻协议
  共用 `STAGE_BATTLE_TASKS` 配置（task_key, 行关键词, 展示名）。流程：点行内「前往」
  → 关卡选择页（进入关卡/行动力特征）→ 点右下「快速战斗」(0.78,0.89) →「快速战斗设置」
  弹窗（指定次数默认 1）→ 再点弹窗内蓝色「快速战斗」(0.62,0.80，精确匹配防标题截胡）
  → 战斗结算页随意点击屏幕中心 → ESC 逐层返回。
- **`_find_row_button` 滚动重试**（重要，2026-10-20 修复）：扫描阶段会把列表滚到底部，
  而每个处理器返回面板时滚动位置不确定。找不到时用 `_scroll_row_list_to_top` 足量回顶
  （原实现只发 6 格，扫描累计下滚 12 格，导致第 3 行「研发室」永远不可见），再用
  `_scroll_list_with_verify` 逐屏校验下滚、覆盖到底，必要时回顶重扫一轮；
  **禁止**改回固定 3 次重试 + 不校验的 PostMessage 兜底。
- **2026-10-20 其他稳定性修复**：茶憩角色选择页等待 10s→45s（加载条 47%→99% 实测超 20s，
  加载中不得点返回/确认）；叶脉联结 tab OCR 失败时按 (0.10,0.55) 比例兜底，搜索区排除
  底部 VEINOS logo（历史误命中「叶脉联结计划」）；俱乐部卡片无「领取」按钮时记
  `no_claim→skipped`，只有点过「领取」仍未变化才记 failed；处理完重扫一次任务列表，
  `daily` 消息如实写“部分完成/未完成任何日常任务”（状态仍 success，避免整条重跑）。
- **友情勋章已自动化**（2026-10-20 用户截图实测）：任务行「前往」优先；若未进好友页，
  则关任务列表回主界面，点左上角功能图标行第 2 个（好友，比例 (0.138,0.047)，
  同行：设置 0.052 / 邮件 0.095 / 好友 0.138 / 俱乐部 0.183）→ 好友页
  OCR「一键领取/赠送」→ 弹窗点「确定/确认/知道了/关闭/点击屏幕继续」或 ESC →
  `_return_to_task_panel` 重开列表领奖励。弹窗按钮搜索区 (0.15,0.12,0.85,0.92)，
  失败字样只在中部 (0.20,0.18,0.80,0.82) 判断且只用“赠送失败/赠送次数/没有好友”等
  赠送独有词组，避免好友签名里的“失败/不足”误伤。
- **友情勋章 2026-10-20 21:35 实操验证**：主界面左上好友图标 (264,50)@1920x1080
  （比例 0.138,0.047）点中 → 好友页「一键领取/赠送」(1703,57) → 弹窗是
  「点击屏幕继续」模板（conf=0.991）→ 任务行变「赠送1次友情勋章 (1/1)」。
  证据：`artifacts/baijing_verify/manual_friend_popup_20261020.jpg` 与
  `manual_friend_after_20261020.jpg`。主界面入口单独验证也通过（点到好友页后返回）。
- **`_focus_game_window` 必须置顶兜底**（2026-10-20 实操）：游戏被 IDE 挡在后面时
  `SetForegroundWindow` 抛错 `(2, 'SetForegroundWindow', '系统找不到指定的文件。')`，
  原实现直接跳 except，后面的 SetWindowPos 置顶不执行，截图 OCR 读到的是遮挡窗口。
  现在失败时用 AttachThreadInput 兜底，并且无论前台是否成功都执行
  SetWindowPos TOPMOST→NOTOPMOST。别把置顶挪回 SetForegroundWindow 成功分支里。
- **`_ensure_daily_tab` 切页要重试**（2026-10-20 实操）：任务面板默认可能停在
  「叶脉联结」，点「日常」后切页有 1-2s 过渡；原实现固定等 1.2s 只查一次会误判
  “未切到日常页”。现在重试 3 次 ×1.2s，别改回单次检查。
- **中途领取**（`_claim_rewards_midway`，用户 2026-10-18 建议）：每个「前往」任务
  完成后立即点固定底栏的「一键领取」（底栏是固定件、任何滚动位置都可见，所以中途
  领取不需要滚动），并用模板 + 屏幕中心随意点双保险关奖励弹窗。避免已完成奖励
  堆积、把后面的任务行挤到可视区外。候选词含「全部领取」——在叶脉联结页也会正确
  命中该页的领取按钮。
- 用户通用叮嘱：**任务完成后若不知道页面到哪了，就 ESC 返回**（`_return_to_task_panel`
  的 ESC 兜底即为此设计，返回顺序：面板→主界面重开任务→退出确认点"确认"→左上"返回"→ESC）。
- **日常完毕后必须领叶脉联结**（`_claim_leaf_vein_rewards`，2026-10-18 用户叮嘱+截图实测）：
  日常 tab 处理完后切左侧「叶脉联结」tab（左列区域找，游戏内是"联结"不是"连结"）→
  「任务总览」→「全部领取」。该页有本日/本周/本期任务（含「日常任务累计活跃度
  达到100」300 分），**奖励与日常 tab 不互通**，漏领丢每日联结经验。
  入口在 `run_baijing_daily_vision_flow` 的 process_task_list 之后、close_task_list 之前。
- **`_ocr_best_match` 精确匹配优先**（重要，2026-10-18 踩坑）：子串匹配会被对话框正文
  截胡——「确认」按钮 (1196,684) 输给正文「将离开休息室，是否确认退出?」(952,479)，
  点到正文中间等于没点按钮。现在先精确匹配（OCR 文本==候选文字）再子串兜底，
  「一任务」这类子串场景不受影响。
- **面板落地页不一定在日常 tab**：`_ensure_daily_tab` 靠「累计活跃度」文字确认，
  不在则点左侧「日常」tab（tab 列区域限定 fx<0.28，避开任务名里的"日常"字样）。
- 任务列表打开后会存快照 `artifacts/baijing_verify/daily_list_<日期>.jpg`，核对导航是否正确。
- **有猫零售每日免费礼包**（`buy_daily_free_gift_pack`，2026-10-18 用户截图实测）：
  主界面底栏「有猫零售」→ 左侧「精选礼包」→ 顶部「有猫精选」→ 首张卡
  「每日外勤赠礼」点**卡底**「免费」→ 购买弹窗点「购买」→ 关奖励弹窗 → 返回主界面。
  挂在 `run_baijing_daily_vision_flow` 的 close_task_list 之后（必须先回主界面，
  底栏入口只在主界面），独立记一条 daily_progress（task_key=`daily_gift`）。
  实测坐标（1919x1077 窗口比例）：有猫零售 (0.712,0.935)｜返回 (0.102,0.049)｜
  精选礼包 (0.105,0.217)｜有猫精选 (0.670,0.203)｜卡底免费 (0.267,0.833)｜购买 (0.606,0.707)。
  两个 OCR 陷阱（都靠 `artifacts/test_baijing_shop_20261018.py` 回归）：
  ① 同页有两个「免费」——卡片顶部角标 (0.267,0.287，conf 仅 0.54) 与卡底价格按钮，
     搜索区域必须限定 fy 0.70~0.90，否则点角标等于点了别的地方；
  ② **购买弹窗特征不能用「购买」**——买完卡片按钮变「已购买」，子串匹配让弹窗判定
     永远为"仍在"，每次都判失败重试；改用弹窗独有的「内容:」「合计」+ 区域限定。
  零售页特征词也**不要用「客服」**：主界面左下角有「Q客服」会假命中。
- **俱乐部商店每周全量购买**（`runtime/baijing_shop.py::buy_club_store_all`，2026-10-18
  视觉版，替代旧固定坐标序列 `白荆回廊-每周商店购买.json`）：商店每周刷新，漏买等一周。
  入口仍是「有猫零售」→ 左侧功能列底部「俱乐部商店」(0.106,0.834，搜索区 fy 0.72~0.98
  以避开「家具兑换」) → **反复全屏扫描**价格按钮（`^[¥￥]?\d{1,6}$` 或 免费/兑换）
  → 逐个点开 → 弹窗**先点「最大」把购买数量拉满**（+ 号右侧蓝色双箭头，
  `SHOP_MAX_QTY_FALLBACK=(0.713,0.524)`@1919x1079，无文字只能按比例点；
  2026-10-20 用户反馈不点永远只买 1 件）→ 点「购买」→ 等弹窗关闭 → **重新扫描**
  （买过的卡片不再有价格按钮，扫不到新目标即收敛）。挂在 `baijing_flow._maybe_run_weekly`（周一次，meta 键不变），
  **视觉版失败时回退旧坐标序列**，别把每周购买能力直接删掉。每日免费礼包（`_buy_in_dialog`）
  无数量选择，**不要**套用最大点击。
  关键设计：
  - 不写死坐标，卡片位置随数量/改版变；商品名靠「按钮正上方同列最近非价格文本」猜，
    只用于日志与去重签名；
  - 卡片图里的装饰数字（10/40/1000）也会被当成价格按钮，点开无弹窗即跳过，
    不会误购也不会死循环（`tried` 集合去重）；判定 success/already 用
    **"有没有真的弹出过购买对话框"** 而不是"有没有扫到目标"，否则全买完后
    残留装饰数字会把结果误判成 failed；
  - 顶栏货币（60,880/64）与左侧功能列靠 `CLUB_GRID_REGION`(fx≥0.18, fy 0.18~0.95) 排除；
  - **只覆盖当前可见区域**，若商店要翻页/滚动购买，日志里的"首屏可见 N 个目标"
    与快照 `artifacts/baijing_verify/club_store_<日期>.jpg` 可判断，需要再补翻页逻辑。
  回归：`artifacts/test_baijing_club_20261018.py`（4 组：全买完/装饰数字跳过/已买完
  already/不在主界面 skipped + 周常钩子不回放序列）。
- **俱乐部每日任务奖励**（`claim_club_daily_reward`，2026-10-18 用户截图实测，每日刷新）：
  挂在 `run_baijing_daily_vision_flow` 的 close_task_list 之后、免费礼包之前
  （用户原话「活跃度100领奖后」——即叶脉联结领完之后；底栏/左上角入口都只在主界面）。
  流程：主界面左上**第 4 个图标** → 弹「新物品已存入S.E.E.D.仓库」点空白继续 →
  俱乐部页点「俱乐部每日任务」卡片 → 卡片变「今日任务已完成」→ 返回主界面。
  三个坑：
  ① **入口图标没文字**，纯比例点击容易点错邻居（旁边是邮件/好友/铃铛）→ 裁了模板
     `模板/白荆回廊-俱乐部入口.png`（红点已 inpaint），整图只有 1 个峰 conf=0.924，
     `_template_center` 命中优先、比例 (0.183,0.047) 兜底。
  ② **俱乐部页特征不能用底部「俱乐部成员/俱乐部商店/俱乐部支援」**——弹「新物品已存入
     仓库」提示时这三行照样能 OCR 到，会把弹窗状态误判成已进页面，后面点卡片被弹窗挡住。
     实测弹窗帧只有底部按钮行能读出来，左栏「俱乐部等级/首席/共响金」「俱乐部每日任务/
     活动」全被白色遮罩冲淡认不出 → 用后者当页面特征。
  ③ **成功凭据只能用卡片上的「今日任务已完成」**：页面顶栏那条「今日俱乐部奖励已领取」
     在进页面前就存在，拿它当判据会永远判定成功。
  领取动作按实测做成三段兜底：点卡片标题 → 若出现独立「领取」按钮则点掉 →
  再兜底点卡片中心 (0.573,0.381)。
  回归：`artifacts/test_baijing_club_daily_20261018.py`（5 组，含"弹窗帧不得命中页面
  特征"这条防回归断言）。
- **有猫兑换每月购买**（`runtime/baijing_cat_exchange.py`，2026-10-25 用户新增需求）：
  「有猫零售 → 有猫兑换」页每月刷新，可以每月买点小东西；**具体买什么当月还没定**，
  所以购买清单做成配置 `app_config.json → baijing.cat_exchange.items`，清单为空时
  每月任务只报「待定」、**不进商店、不买任何东西、也不写月度 meta**（清单补上后本月还能补跑）。
  挂在 `baijing_flow._maybe_run_monthly`（月一次，meta 键 `monthly_sequence:baijing_cat_exchange`），
  一条龙里排在周常之后、关游戏之前；结果落 daily_progress（task_key=`monthly`）。
  定位策略与俱乐部商店**相反**：那边「扫价格按钮全量买」，这边「按清单点名买」，
  主锚是**商品名**（名称 OCR 置信度下限 0.45），价格只做预算校验：
  - 几何（2026-10-25 用户截图 1919x1079 实跑 RapidOCR 定标，参考图
    `artifacts/ref_baijing_cat_exchange_20261025.png`）：左列「有猫兑换」中心 frac
    (0.105,0.489)（上邻「特别供应」0.423、下邻「特级商店」0.559 → 窄带 fy 0.44~0.53 隔离）；
    页签行 fy 0.200（小猫礼品/大猫兑换/橘猫尊享/活动拾遗/动态立绘，页面特征取后两个）；
    商品名行 fy 0.483/0.795，五列 fx 0.280/0.433/0.587/0.741/0.895；
    价格数字在名下方 0.035 窗高、数字中心比名中心右移 0.013 窗宽。
  - **价格数字 OCR 召回只有约 60%**（个位价 6/6/6/6 全部漏检）→ 价格读不到时按
    「名+0.013w / 名+0.035h」比例兜底点价格按钮，**不要**因为读不到价格就放弃；
    但**设了 max_price 又读不到「合计」金额时一律不买**（宁可漏买不可超支）。
  - 弹窗复用商店同款「内容/合计」特征（**不要**用「购买」当特征：买完变「已购买」，
    子串匹配会让弹窗判定永远为"仍在"，每次都判失败重试）。
  - 状态口径：点完没弹窗要**复扫该列售罄标记**，区分「已买完」与「没点到」；
    「未找到」（商品不在当前页签/需翻页）与「已买完」分开记，**只有全部已买完才报 already**，
    其余一律 failed（宁可报失败也不报假成功）。
  - **只覆盖当前可见区域**（商店右侧还有翻页箭头，翻页未实现）→ 找不到就报"未找到"，需要时再补。
  - 回归：`artifacts/test_baijing_cat_exchange_20261025.py`（真实截图 OCR 几何定标 +
    配置解析 + 预算闸门 + 月度门控 + 空清单不进商店）。

### 白荆回廊任务列表防护 + 零售/俱乐部自保 (重要, 2026-10-22)

**10-21 事故复盘**：日常只完成 3/15、活跃度 20/100，flow_failures 显示俱乐部连续
3 天失败，零售礼包被跳过且**无记录**（用户看不到漏买）。根因链：
① 研发室收取后任务列表面板被关回主界面，`_wait_task_list` 被主界面右下角 2 字
「任务」按钮文字**反向子串命中**（候选含"日常任务/任务列表"等长词时，
`_ocr_best_match` 规则"OCR 文本 ∈ 候选 且 ≥2 字符"让 2 字"任务"通关），
假阳性判"列表已打开" → 之后 5 个任务的找行全部在错误界面空转（每行 6 次滚动 ≈21s）；
② close_task_list 的 OCR「关闭」被新手引导气泡「点击按钮或空白区域关闭」(conf 0.89)
截胡，点它无效 → 判未回主界面 → 俱乐部+零售被 `back_ok` 门禁整体跳过。

**四项修复（全在 `runtime/baijing_daily.py`）**：
1. **`TASK_LIST_MARKERS` 只留强特征且不含「任务」二字**（累计活跃度/主线指引/
   一键领取/全部领取/领取奖励）。反向子串陷阱是主因，候选含"任务"即死。
2. **`_ensure_task_list_open`**：`process_task_list` 每个任务处理前（研发室/记忆/
   茶憩/友情/战斗循环/领取循环/重扫共 7 处）强特征快检列表在屏（`_wait_task_list`
   snapshot=False 不覆盖快照），丢了就 `open_daily_task_list` 重开，重开失败记
   warning 跳过该任务，不再空转。
3. **`close_task_list` 失败后重开列表再关**：`_ensure_main_ui` 3 轮没回主界面的
   中间态，重开任务列表（面板有可靠关闭按钮模板）再走 2 轮 ensure。
4. **`_enter_club_page` 入口重试**：原有两轮特征验证之外，补"重试点入口 +
   第三次特征确认"，不点了就算。

**零售/俱乐部自保（10-21 深夜先做的一批）**：
- 新增 `_ensure_main_ui`（文件头规划的步骤 1，之前一直没实现）：检测→消遮挡→
  点关闭（OCR `exclude=("点击按钮","空白区域")` 排引导气泡 + 模板兜底）→ESC→再检测，
  最多 MAX_CLOSE_ATTEMPTS 轮；`close_task_list` 是其薄包装（失败时加重开恢复）。
- `buy_daily_free_gift_pack` / `claim_club_daily_reward` 开头改为先 `_ensure_main_ui`
  自保，**移除主流程 `back_ok` 门禁**（close 失败不再连坐跳过），gift 循环内
  attempt>1 也 ensure 一次。
- **skipped 也落 daily_progress**（`daily_gift`/`club_daily`）：10-21 静默跳过
  无记录，用户看不到漏买；现在 skipped 带 detail 落库，邮件/卡片可见。

**测试**（7 个套件全绿）：
- `artifacts/test_baijing_task_list_guard_20261022.py`（21 项：四项修复逐一锁定，
  含"主界面 2 字任务按钮不得假阳性"防回归断言）
- `artifacts/test_baijing_ensure_main_20261022.py`（12 项：ensure 五场景/自保/门禁移除/skipped 落账）
- 既有 5 套件回归（daily_fixes/club_daily/club/shop/max_qty）ALL PASS。

**串行跑测试的坑（2026-10-22 实测）**：测试进程共享鼠标物理位置——mock 没覆盖
`bd.pyautogui` 时，`_ensure_main_ui`/`baijing_shop` 的裸 `pyautogui.press("escape")`
会真实执行，pyautogui 所有函数（**含 press 等键盘函数**）都带 fail-safe 检查，
鼠标恰在角落就 FailSafeException（单独跑碰巧过、串行跑炸）。**所有 mock 测试的
Env 都必须隔离 `mod.pyautogui`**（press 记录式），三个旧测试已补。

#### 2026-10-24 日常 1/15 事故：特征撞车 + 返回死循环

当天 12:12 一条龙只完成登录 1/15、活跃度 5/100，两处根因：

1. **`LAB_DIALOG_MARKERS` 里的「白荆穹顶」撞车**：任务行描述「在白荆穹顶的初级
   研发室…」和收取提示「资源已存入白荆穹顶仓库」都含这四个字，弹窗关掉后
   `_close_lab_dialog` 仍判"还开着"→ 补按 ESC 把任务列表一起关掉。现在特征只留
   「当前设施生产情况/一键收取」，并在关闭循环顶部先判结果列表（`TASK_LIST_CORE_MARKERS`）。
2. **`_return_to_task_panel` 对静态「确认」文字死循环**：记忆烙痕强化后页面上有个
   点不动的「确认」，原实现每轮都点同一坐标、从不 ESC，卡 2 分钟；后续任务全部
   "列表不在屏"跳过。现在同坐标连续命中判定为静态文字，改走 ESC；并补点
   「点击屏幕继续」过场遮罩。
3. **`_ensure_task_list_open` 重开失败时先 `_ensure_main_ui`**：原来只重试找
   「任务」按钮，界面卡在子页面时永远失败并连带跳过整轮。

回归：`artifacts/test_baijing_stuck_recovery_20261024.py`（5 组）+
既有 7 套件 ALL PASS。注意 `runtime/baijing_daily.py` 目前是**未跟踪文件**，
`git diff` 看不到改动，别误以为没改。

#### 2026-10-25 白荆一条龙心跳盲区（看门狗误杀，已修）

- **事故**：提权单跑白荆，冷启动等游戏本体 4 分钟 + 进入阶段耗满后，任务
  09:28:41 启动、09:38:41 被 task_heartbeat_watchdog 按"600 秒无心跳"强杀
  （整点 600s，一个 beat 都没有）。根因：`execute_task` 开任务即启动看门狗，
  但 `beat()` 只写在 `_monitor_game` 循环里——白荆走 `run_baijing_full_flow`，
  全程零心跳。10-24 GUI 跑 446s < 600s 纯属侥幸没炸；**任何白荆一条龙总时长
  超 10 分钟（冷启动基本必超）都会被腰斩**，哪怕提权跑。
- **修复**：`runtime/baijing_flow.py::_make_stop_check_with_heartbeat` 把
  `controller._heartbeat.beat()` 挂到 stop_check 每次轮询上（白荆流程所有长循环
  本就轮询 stop_check）。线程真卡死 → 停止轮询 → 心跳停 → 看门狗照常兜底，
  保护不失效。回归 `artifacts/test_baijing_heartbeat_20261025.py`（13 项：
  纯逻辑 7 + 全流程接线 3 + 停止传播 3）。修后实测 795s 全程无看门狗干扰。
- **遗留疑点（当天已修，2026-10-25 下午）**：末轮重扫 0/15 + 叶脉漏领的根因
  是 `_dismiss_overlay` 的「关闭」模板撞上**任务面板自己的右上角 X** (1847,159)：
  无弹窗时也稳定命中（conf 0.968），一点面板就没了。日志实锤链：09:58:20.939
  "列表已打开" → 09:58:21.466 dismiss 点 (1847,159) → 重扫 6 屏 0 行（快照里
  列表明明开着）；领取循环同样每轮"误关面板→重开"空转烧掉 12 轮。四项修复
  （全在 `runtime/baijing_daily.py`）：① `_in_host_panel_close_zone`（窗口相对
  fx≥0.88, fy≤0.25）命中即跳过不点，中央弹窗 X 不受影响；② `CLAIM_EXCLUDE`
  加「已领取」——那是行内状态文字不是按钮，曾 3+ 轮点了没反应；③ 重扫前奏
  改为先 dismiss 后 ensure（自愈顺序）；④ `scan_daily_task_states` 首屏 0 行
  且全窗 OCR 无列表强特征 → 立即止损 + `_record_step_failure` 落库，不再白滚
  5 屏。另：叶脉入口加 `_ensure_task_list_open` 自愈（此前默认面板还开着，
  在错误界面上找 tab 永远找不到）、失败补 `_record_step_failure` 埋点（此前
  只有 warning，flow_failures 查不到）。回归
  `artifacts/test_baijing_scan_churn_20261025.py`（22 项）+ 既有 9 套件全绿；
  `daily_fixes` 的 LeafHarness 需补 mock `_ensure_task_list_open`（不 mock 会
  在 NoSleepTime 冻结时间下把真实 `_wait_task_list` 跑成死循环，已补）。

#### 2026-10-25 17:49 / 19:06 提权实跑结果（最终 10/15，活跃度 85/100）

- **17:49 第一轮**（`artifacts/run_baijing_only_20261025.py` 经 UAC 启动）500s：
  日常 9/15、活跃度 70/100、叶脉联结=True、免费礼包=failed、俱乐部=skipped；
  没有 UIPI / 看门狗 / 游戏崩溃干扰。
- **19:06 第二轮** 391s：本地记录 10/15、85/100，免费礼包=**already**；
  但其中茶憩是**假成功**：本地记 1/1，游戏内实际仍 0/1（19:50 截图实锤）。
  真实当时应为 9/15、70/100，之后补完 5 次战斗才是 105。
- **免费礼包 failed 是误报**：同一天上午 09:59 已领过「每日外勤赠礼」，
  17:49 强制重跑时商品已从货架消失。修复：`buy_daily_free_gift_pack` 开头读
  今日 `daily_progress` + 新增 `task_meta` 日标记
  （`baijing_daily_gift:<date>`，一天只写一次、不会被后续失败覆盖），
  success/already/标记命中直接返回 already。回归
  `artifacts/test_baijing_gift_guard_20261025.py`（11 项）。**注意**：该守卫会
  读真实 DB，所有调用该 handler 的旧测试（shop/ensure_main）必须 mock
  `_baijing_task_status_today` / `_daily_gift_marked_done` / `_mark_daily_gift_done`。
- **茶憩假成功（2026-10-25 19:06，已修）**：`_handle_tea_break_task` 末尾
  `return _return_to_task_panel(...)`——只要能回到任务面板就 True，caller 随即
  `tea_state["done"]=True`；最终重扫的 `old.done or st.done` 也无法纠正。实际
  那轮既没看到茶憩场景/摘要，游戏内仍是 0/1。修复：① handler 必须有
  **茶憩场景或结束摘要**证据才 True，否则 `_record_step_failure` + False；
  ② `process_task_list` 不再乐观写 tea done，最终以任务列表重扫为准（重扫失败
  保守判未完成）；③ 点确定后有时**直接进休息室 3D 场景**（19:50 截图 OCR：
  午后茶憩/拜访他人/布置模式，左下茜茜/伊琅相思/卯绒绒），此时点左下第一个
  头像（约 (0.057,0.83)）再走场景/摘要，不再死等角色选择页标签。回归
  `artifacts/test_baijing_tea_false_positive_20261025.py`（13 项）+
  `test_baijing_tea_marker_20261025.py`（同调者位阶长词保留）。
  截图证据 `artifacts/flow_failures/20261025_195202_BaiJingDaily_午后茶憩.jpg`。
  **教训：任何 UI 流程的"成功"只能是任务行/接口的真实结果，不能是"回到面板了"
  这种中间状态。**
- **「完成5次战斗」目录修正 + 指定次数=2**（2026-10-25 用户弹窗截图实锤）：
  日常实际有 16 项，包含「完成3次战斗」(10) 和「完成5次战斗」(4/5, 20)；
  旧目录只有前者且误称「完成2次战斗」，所以本地活跃度一直少 20。已改为
  `battle_3` + `battle_5`，理论总分 160。四个关卡任务各 1 场只有 4 场，
  `process_task_list` 现在给**本批第一个实际执行**的关卡传 `battle_count=2`：
  「快速战斗设置」弹窗里点一次蓝色 `+`（`IMAGING_DIALOG_PLUS_BTN=(0.580,0.570)`，
  来自用户 1919x1079 截图，HSV 蓝钮中心实测一致），再点确认；其余三关仍 1 场。
  这样 2+1+1+1=5，完成 5 次战斗后本地/游戏内都是 105。回归
  `artifacts/test_baijing_battle_count_20261025.py`（15 项，含截图比例校验）。
  **注意**：同一天四个关卡任务已完成时本批不会进关卡，2 次逻辑只在
  新一天/关卡任务未完成时触发；不要用当天已完成的状态判断功能没生效。
- **剩余 5 项本来就没自动化**（只记录不跳转）：回廊漫巡、卓越道路、技能升级、
  击败60、休息室小游戏。当前自动化覆盖 16 项中的 11 项，满打 105/100；
  这 5 项要实现才拿得到更高分（回廊漫巡 20、击败60 10 等）。

#### 2026-10-26 休息室 / 关注特写：ESC 无效的场景态必须独立处理

- **事故链**（12:47-12:53 日志 + 两张 flow_failures 截图）：午后茶憩点「确定」后掉进
  **休息室 3D 场景** → 旧代码按 10-25 的经验点左下第一个角色头像 → 进入的其实是
  **「关注」特写镜头**（画面只剩莫红袖 + 右上「退出关注」）→ 茶憩场景/摘要永远等不到
  → `_return_to_task_panel` 连按 5 次 ESC（**这两个界面都不吃 ESC**）→ 任务列表反复
  "重开失败"→ 友情勋章/显影行动/领取阶段/叶脉联结/俱乐部/零售全部跳过 → 日常只剩
  **2/16、活跃度 20/100**（前一天 9/16、70/100）。
- **正确操作（用户 12:53 截图实锤）**：休息室茶憩入口是**右下「午后茶憩」立牌卡**
  （`REST_ROOM_TEA_ENTRY=(0.883,0.830)`），离开必须点**左上「返回」**
  （`REST_ROOM_BACK=(0.101,0.048)`）→ 确认框「确认」（OCR 优先，读不到按
  `REST_ROOM_LEAVE_CONFIRM=(0.623,0.634)` 比例兜底）。
- **场景退出登记表 `recover_scene()` + `SCENE_RULES`（同日重构，取代一次性 if/else）**：
  - 规则表顺序即优先级：`过场遮罩(点击屏幕继续) → 茶憩摘要(茶憩结束/点击退出) →
    关注特写(退出关注) → 休息室(拜访他人/布置模式)`，每条 = 特征文字 + 专属退出函数。
  - **每轮只截一次屏**：`_scene_results()` 取一次 `ocr_window_content`，所有判定用
    `_pick_result()` 在同一份结果上做（匹配规则与 `_ocr_best_match` 一致：精确优先、
    区域过滤、exclude 黑名单）。旧实现每个判定各自截屏，恢复一轮要跑 4-6 次 OCR。
  - 返回 `(状态, 是否做过动作)`：`panel` / `main` / `scene:<名称>` / `unknown`。
    `unknown` 与"命中场景但反复退不出去"都会调 `_record_unknown_scene()` 落
    flow_failures（截图 + 当帧前 12 条文字），**同一 context 600 秒只记一次**。
    以后遇到新页面 = 照证据加一行登记，不再靠猜。
  - 接入三处恢复入口：`_return_to_task_panel`（步骤 2.4）、`_ensure_main_ui`
    （消遮挡之前）、`_ensure_task_list_open`（重开失败之后）。**ESC/点关闭只作为
    "谁都没匹配上"时的兜底**。一次性函数 `_leave_rest_room` / `_recover_scene_state` /
    `_in_focus_mode` / `_exit_focus_mode` 已删除，别再往回加。
  - **规则：凡是 ESC 无效的游戏内场景，一律登记成一条场景规则，不要指望 ESC。**
  - 哨兵坑：节流用 `None` 判断"从未记录"，不能用 `_SCENE_LOGGED.get(key, 0.0)`——
    测试里 `time.time` 被换成从 0 起步的假时钟，`now - 0.0 < 600` 会把第一次记录也吞掉。
- **休息室特征只用「拜访他人 / 布置模式」**（`REST_ROOM_MARKERS`）——
  「午后茶憩」在日常任务行里也有这四个字，拿它判定会把任务列表误认成休息室。
- 回归：`artifacts/test_baijing_scene_registry_20261026.py`（10 组：四场景各自退出 +
  panel/main/blank 判定 / 一轮一次 OCR / 未知场景落证据 / 反复退不出去落证据且点击有界 /
  登记表顺序（面板特征优先，绝不在面板上点「返回」）/ 三个恢复入口在休息室残留时全程零 ESC /
  旧函数确已删除）；`test_baijing_tea_false_positive_20261025.py` 的休息室用例同步改成
  断言点立牌卡。桩数据要点：驱动 `recover_scene` 要 patch **`bd.ocr_window_content`**
  （登记表的数据缝），只 patch `_ocr_best_match` 喂不进去。
- **未实机验证**：本轮只跑了静态与状态机回归，真实休息室界面是否就是这两张截图
  的形态，要下次跑白荆才能确认（茶憩失败仍会诚实落 flow_failures + 截图，不会再拖整轮）。

#### 2026-10-27 活跃度记录改为读游戏面板（目录 90 vs 面板 105）

- 用户实锤：验证截图累计活跃度 **105**、五档奖励全部「已领取」，daily_progress 却记
  90/100。根因是活跃度用 `DAILY_TASK_CATALOG` 已完成任务分值相加；目录已和游戏版本
  脱节（当天游戏是「完成2次战斗」，目录写「完成3次战斗」），同样 10 个已完成任务
  目录算出 90、面板 105。10-25 也出现过同样的 +15（目录 70 / 面板 85）。
- 修复：`process_task_list` 结束优先 OCR 每日面板顶部大数字
  （`_read_activity_number`：全窗 OCR + `ACTIVITY_NUMBER_REGION` 区域过滤 + 3 次
  取非零最大，兼容 1/0/5 拆块），读不到才回退目录；`activity_source` /
  `activity_catalog` 落库留痕。`current` 仍按 100 封顶，`earned` 保留面板值
  （105），所以 UI 显示 100/100「已完成」而不是 90/100。
- 诊断：`scan_daily_task_states` 新增 `capture_prefix`，初始扫描/重扫每屏存
  `daily_scan_*_日期.jpg`，下次对不上时直接看每屏任务与徽标分值，别再只靠日志猜。
- **战斗项是每日变体（用户 2026-10-27 指出）**：游戏每天可能是「完成2/3/4次战斗」
  中的一种（不会同时显示），旧目录只认「完成3次战斗」→ 当天是「完成2次战斗」时
  匹配不到，靠 OCR 误读成 3/3。目录 `battle_3` 已改为匹配 2/3/4（同一个 10 分
  槽位，key 保留兼容历史数据），`battle_5` 仍是独立 20 分；4 次战斗的分值尚未
  实测、暂按 10，最终活跃度仍以面板值为准。
- 回归：`artifacts/test_baijing_activity_panel_20261027.py`（完整/拆分数字、区域外、
  只读到 0、动画取最大、超上限、真实 2026-10-27 截图 105 定位、战斗 2/3/4/5 变体、
  快照接线、源码接线）。
  2026-10-27 的 activity/daily 记录已补正为 `100/100（实得 105）`，原值备份在
  `storage/cache/daily_progress_baijing_20261027_backup.json`。

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
- **第二期**：崩铁 March7th `config.yaml`、绝区零 ZZZ OneDragon
  `config/01/one_dragon/charge_plan.yml` + `team.yml`。YAML 必须用 `ruamel.yaml`
  保留注释/顺序，**禁止用 PyYAML dump**（会重排 45KB 配置并丢注释）；依赖需同时
  更新 `requirements.txt` 和 `services/dependency_manager.py`。
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
  `ui/bridge._save_wool_flow_field`、`webui/api/wool.toggle`。
- **升级注意**：共享流程定义更新只进 example；已有本地 `flow_configs.json` 不会
  自动合并，需要手动把新流程/字段补进本地文件（或删本地让 example 重新复制，
  会丢个人开关）。这是拆分换来的代价，别再把个人开关提交回仓库。
- **回归**：`artifacts/test_flow_config_split_20261028.py`（缺失复制/已有不覆盖/
  加载器回退/GUI 只写本地/example 安全默认/接线）。
