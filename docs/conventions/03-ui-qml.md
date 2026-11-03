# QML/PyHuskarUI 约定与桥接避坑

> 由 AGENTS.md 拆分；改动相关代码前先读本篇。

## UI Framework Notes

- 项目有两套 UI：**PySide6+QML + HuskarUI**（`ui/qml/`，**当前默认入口**，`app/gui.py` → `_launch_qml_ui`）和 **Flet**（`ui/flet_app.py`，暂停维护，不再被 `start_gui_admin.bat` 调用，run_flet_gui 无任何 caller，保留仅作回退方案）。
- HuskarUI 来源：`https://github.com/mengps/HuskarUI`，QML 控件前缀 `Hus`（HusCard / HusButton / HusText / HusSelect / HusCheckBox / HusTag / HusSpin / HusEmpty / HusMenu / HusNotification / HusInput / HusIcon 等）。
- QML 视图文件在 `ui/qml/views/`，可复用组件在 `ui/qml/components/`，主窗口在 `ui/qml/Main.qml`。
- Python ↔ QML 桥接在 `ui/bridge.py`（UiBridge），主题令牌在 `ui/theme.py`。
- **凭据输入必须用 `components/CredentialInput.qml`**（默认 `echoMode: TextInput.Password`，自带"显示/隐藏"按钮）。**禁止**直接用 `HusInput` 接收 token / cookie / api_key，会明码显示。
### QML 视图加载 / 回收（2026-11-01）

- `Main.qml` 的所有视图用 `Loader` + `*EverLoaded` 门控：首屏只加载当前视图，其余首次切到才加载
  （`asynchronous: true`）。**不要**改成全部 `active: true`，首屏会白吃上百 MB。
- **重页面会延迟释放**：距离最后一次切页超过 `viewReleaseDelayMs`（默认 180000 = 3 分钟）后，
  `releaseInactiveHeavyViews()` 把**非当前页**的 `dailyEverLoaded / gachaEverLoaded / nikkiEverLoaded /
  wuwaEverLoaded / staminaConfigEverLoaded` 置 false → Loader 销毁 item，切回时重建。
  每个重页面 Loader 的 `onVisibleChanged` 调 `touchViewActivity()` 重置计时。
- 释放的三道闸：`viewReleaseDelayMs > 0`（设 0 = 关闭回收）、`!bridge.isRunning`
  （任务在跑时 DailyView 有轮询、GachaView 可能在导入，销毁会中断）、且 `currentView !== 该页`。
- **实测口径（离屏 2026-11-01，别重复踩）**：逐页加载 +10~29MB/页，10 页切一遍 +142MB 不回落；
  但 **Windows 堆不会把释放的内存立刻还给系统**，所以"切一圈停住"时任务管理器看不出下降；
  频繁切同一页反而更费（反复重建 10 轮 +24MB）。收益在长期不用的页面和后续分配复用，
  默认只挑 5 个重页面、延迟 3 分钟，就是为了不让正常切页被 1~2 秒的重建拖慢。
- 回归：`tests/suite_view_release_20261101.py`（自己设 `QT_QPA_PLATFORM=offscreen`，不弹窗；
  验证延迟窗口、超时释放、切回重建、isRunning 保护、delay=0 关闭）。
### PyHuskarUI 兼容性避坑 (重要)

1. **PySide6 版本锁定**: PyHuskarUI 0.6.1 的 QML 文件内 `ParallelAnimation` 直接放在 `Rectangle` 的 `data` 子节点中，在 Qt 6.11 中会报错 `Cannot assign object of type "QQuickParallelAnimation" to list property "data"; expected "QObject"`。必须降级 PySide6: `pip install "PySide6>=6.8,<6.10"`，涉及 PyHuskarUI 库内 7 个文件（HusButton/HusBadge/HusButtonBlock/HusCheckBox/HusRadio/HusRadioBlock/HusSwitch）。

2. **不要同时启用 qrc 资源和文件系统 QML 路径**: PyHuskarUI 的 QML 同时存在于编译资源 (`qml_rc.py` 注册的 `qrc:`) 和文件系统，只能二选一。使用默认的编译资源即可，**禁止**调用 `engine.addImportPath(str(pyhuskarui_qml_dir))`。

3. **QML 初始化不要跨模块导入**: `gui.py → main_window.py → bridge` 的导入链在 `python -m` 模式下会导致 QML 加载失败。正确做法是将 QApplication + HusApp + Engine + Bridge 的初始化**内联**写在 `app/gui.py` 的 `main()` 中，不走 `from ..ui.main_window import run_huskar_ui`。

4. **QML 磁盘缓存**: Qt 的 QML 编译缓存按 organizationName/appName 分区，位于 `%LOCALAPPDATA%/GameAuto/`。切换 PySide6 版本后需清掉旧缓存（`*.qmlc`），否则可能加载过期字节码。

5. **禁止硬编码文件路径**: 所有数据路径必须通过 `settings/paths.py` 或 `app_config.json` 配置，**禁止**在代码中写死绝对路径（如 `D:\Program Files\...`）。抽卡数据必须写入 SQLite 数据库（`gacha_records` 表），从数据库读取，不能依赖本地文件系统中的固定文件。`ui/shared.py` 中的 `DEFAULT_ENDFIELD_PATH` 是反面教材，新代码不得效仿。

6. **HusTag 枚举只有 5 个值**: `State_Default / State_Success / State_Processing / State_Error / State_Warning`（见 pyhuskarui 的 HusTag.qml）。**没有 State_Info**，写了不会编译报错，但赋给 `tagState`（int）时是 undefined，控制台报 `Unable to assign [undefined] to int` 且 Tag 落回默认样式。蓝色"信息感"用 `State_Processing`（2026-11-03 已修正 ClickSequenceView 的误用；测试套件离屏跑 QML 时这类错会暴露）。
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
