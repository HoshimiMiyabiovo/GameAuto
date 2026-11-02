# 凭据加密、平台协议与 WebUI 安全

> 由 AGENTS.md 拆分；改动相关代码前先读本篇。

## 凭据 DPAPI 加密（2026-10-25 起含 phone）

- `settings/manager.py` 的 `_SENSITIVE_SUBSTRINGS` 决定哪些键落盘自动 DPAPI 加密（token / cookie / password / secret / api_key / **phone**）。登录手机号是 PII（塔吉多、库街区都靠它自动重登），曾以明文落盘，2026-10-25 已纳入清单并自动迁移为密文。
- `load()` 内置自愈迁移：检出盘上明文敏感值 → `save()` 重写为密文，读取方无需兼容分支（`secure_store.resolve` 对无前缀明文原样返回）。新增敏感字段直接让键名命中清单即可，**不要自建加密逻辑**，也不要把新键加进 `_SENSITIVE_EXCLUDE`。
- 注意：运行中的旧 GUI 进程保存配置会把新纳入清单的字段写回明文——重启 GUI 后首次 load 自动再加密，属自愈场景，但改完清单应重启 GUI。
## Mihoyo Cookie Notes

- browser_cookie3 access to Edge cookies may require administrator privileges on this Windows environment.
- Do not swallow RequiresAdminError and then report it as "cookie not found". Surface the real permission problem.
- QML GUI (Flet 已暂停维护) should elevate before attempting Edge cookie extraction.
## WebUI 安全约定 (2026-10-28)

- **未设置账号不启动**：`webui_auth` 没有账号时 `app/gui.py` 不启动 WebUI；在 GUI 的 WebUI 设置页保存账号后立即后台启动（`webui.app.start_webui_background`）。
- **全接口鉴权**：除 `/api/auth/*` 与 `/api/health`，所有 `/api/*` 以及 `/ws/logs` 都必须校验 token；禁止恢复 `has_account()` 时绕过鉴权的写法。
- **点击序列名**：统一走 `runtime/click_sequence.sequence_path()`（`Path(name).name == name`），禁止再用 `目录 / f"{name}.json"` 直接拼接。
- **配置备份**：`settings/manager.save()` 的 `.bak` 必须写加密后的 persisted；禁止 `shutil.copy2` 旧文件，否则新增敏感字段迁移会把明文留在备份里。
- **前端**：`webui/static/app.js` 所有 innerHTML 动态值必须经 `esc()`，URL 经 `safeUrl()`；禁止用名字拼 inline onclick。
- **CORS**：只允许 `http://localhost:60171` / `http://127.0.0.1:60171`，不要再放开任意 localhost 端口。
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
