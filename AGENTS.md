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

## Mihoyo Cookie Notes

- browser_cookie3 access to Edge cookies may require administrator privileges on this Windows environment.
- Do not swallow RequiresAdminError and then report it as "cookie not found". Surface the real permission problem.
- Flet GUI should elevate before attempting Edge cookie extraction.

## UI Framework Notes

- 项目有两套 UI：Flet（`ui/flet_app.py`，当前默认入口）和 PySide6+QML（`ui/qml/`，基于 HuskarUI 控件库）。
- HuskarUI 来源：`https://github.com/mengps/HuskarUI`，QML 控件前缀 `Hus`（HusCard / HusButton / HusText / HusSelect / HusCheckBox / HusTag / HusSpin / HusEmpty / HusMenu / HusNotification / HusInput / HusIcon 等）。
- QML 视图文件在 `ui/qml/views/`，可复用组件在 `ui/qml/components/`，主窗口在 `ui/qml/Main.qml`。
- Python ↔ QML 桥接在 `ui/bridge.py`（UiBridge），主题令牌在 `ui/theme.py`。

### PyHuskarUI 兼容性避坑 (重要)

1. **PySide6 版本锁定**: PyHuskarUI 0.6.1 的 QML 文件内 `ParallelAnimation` 直接放在 `Rectangle` 的 `data` 子节点中，在 Qt 6.11 中会报错 `Cannot assign object of type "QQuickParallelAnimation" to list property "data"; expected "QObject"`。必须降级 PySide6: `pip install "PySide6>=6.8,<6.10"`，涉及 PyHuskarUI 库内 7 个文件（HusButton/HusBadge/HusButtonBlock/HusCheckBox/HusRadio/HusRadioBlock/HusSwitch）。

2. **不要同时启用 qrc 资源和文件系统 QML 路径**: PyHuskarUI 的 QML 同时存在于编译资源 (`qml_rc.py` 注册的 `qrc:`) 和文件系统，只能二选一。使用默认的编译资源即可，**禁止**调用 `engine.addImportPath(str(pyhuskarui_qml_dir))`。

3. **QML 初始化不要跨模块导入**: `gui.py → main_window.py → bridge` 的导入链在 `python -m` 模式下会导致 QML 加载失败。正确做法是将 QApplication + HusApp + Engine + Bridge 的初始化**内联**写在 `app/gui.py` 的 `main()` 中，不走 `from ..ui.main_window import run_huskar_ui`。

4. **QML 磁盘缓存**: Qt 的 QML 编译缓存按 organizationName/appName 分区，位于 `%LOCALAPPDATA%/GameAuto/`。切换 PySide6 版本后需清掉旧缓存（`*.qmlc`），否则可能加载过期字节码。

5. **禁止硬编码文件路径**: 所有数据路径必须通过 `settings/paths.py` 或 `app_config.json` 配置，**禁止**在代码中写死绝对路径（如 `D:\Program Files\...`）。抽卡数据必须写入 SQLite 数据库（`gacha_records` 表），从数据库读取，不能依赖本地文件系统中的固定文件。`ui/shared.py` 中的 `DEFAULT_ENDFIELD_PATH` 是反面教材，新代码不得效仿。

## Scope Notes

- Startup sign behavior should be shared through startup_sign_runner.py rather than duplicated separately in Flet and CLI flows.
- Keep project-visible notes like this file concise and focused on repeated engineering pitfalls.

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