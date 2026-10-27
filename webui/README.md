# GameAuto WebUI

浏览器访问 `http://localhost:8000`，与桌面 QML 共用同一 `AutomationController / WorkflowDB`。

## 启动
```bat
pip install -r requirements.txt
python -m webui.app
:: 或
uvicorn webui.app:app --host 0.0.0.0 --port 8000
```
`WEBUI_PORT` 环境变量可改端口。

## 接口
- `GET /api/apps` 列表 / `POST /api/apps/prefs` 保存勾选 / `POST /api/apps/run` 执行 / `POST /api/apps/stop`
- `GET /api/daily/progress` `POST /api/daily/check` `GET /api/daily/verify-image/{name}`（白荆验证图）
- `GET /api/wool/flows` `POST /api/wool/run`
- `GET /api/click-sequences` `POST /api/click-sequences/{name}/play` `DELETE`（受保护 403）
- `GET /api/logs` `WS /ws/logs`

敏感字段落盘 `dpapi:` 加密，`manager.load` 透明解密，Web 仅返回脱敏。
