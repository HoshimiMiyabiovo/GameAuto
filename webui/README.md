# GameAuto WebUI

浏览器访问 `http://127.0.0.1:60171`，与桌面 QML 共用同一 `AutomationController / WorkflowDB`。

## 启动

WebUI **只在已设置账号后启动**：打开 GUI →「WebUI 远程」→ 填写账号密码并保存，服务会立即在后台启动；之后随 GUI 自动启动。

```bat
:: 直接启动（未设置账号会拒绝运行）
python -m game_auto.webui.app
```

端口固定为 `127.0.0.1:60171`。**不要**改成 `0.0.0.0`，也不要把端口暴露到局域网。

## 鉴权

- 首次使用必须先在桌面 GUI 设置 WebUI 账号；未设置时 WebUI 不启动。
- 除 `/api/auth/*` 与 `/api/health`，所有 `/api/*` 以及 `/ws/logs` 都必须携带登录 token。
- 登录 60 秒内限 5 次；会话 12 小时过期；改密后旧会话立即失效。
- 密码使用 PBKDF2-HMAC-SHA256（20 万轮 + 随机盐），落盘前经 Windows DPAPI 加密。

## 接口

- `GET /api/apps` 列表 / `POST /api/apps/prefs` 保存勾选 / `POST /api/apps/run` 执行 / `POST /api/apps/stop`
- `GET /api/daily/progress` `POST /api/daily/check` `GET /api/daily/verify-image/{name}`（白荆验证图）
- `GET /api/wool/flows` `POST /api/wool/run`
- `GET /api/click-sequences` `POST /api/click-sequences/{name}/play` `DELETE`（受保护 403）
- `GET /api/logs` `WS /ws/logs`

敏感字段落盘 `dpapi:` 加密，`manager.load` 透明解密，Web 仅返回脱敏。
