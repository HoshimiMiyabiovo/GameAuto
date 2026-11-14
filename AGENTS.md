# Game Auto Agent Notes

本文件是**索引**：具体约定已按主题拆到 `docs/conventions/`，改代码前先读对应分篇。

## 必读顺序

| 分篇 | 内容 | 何时必读 |
|---|---|---|
| [01-workflow.md](docs/conventions/01-workflow.md) | 执行工作流、依赖规则、日志规则、注释规范、工程化基线、数据目录 | 任何改动 |
| [02-credentials-security.md](docs/conventions/02-credentials-security.md) | DPAPI 凭据、米游社/库街区协议事实、WebUI 安全 | 碰凭据、签到、WebUI |
| [03-ui-qml.md](docs/conventions/03-ui-qml.md) | QML 视图回收、PyHuskarUI 避坑、@Slot 返回值、桥接 | 改 UI/QML/bridge |
| [04-game-flows.md](docs/conventions/04-game-flows.md) | 各游戏进入主界面、日常/月度流程、白荆/终末地踩坑 | 改流程或模板点击 |
| [05-daily-progress.md](docs/conventions/05-daily-progress.md) | 每日进度口径、API 回填、日志复核、签到双写 | 改进度/日报/统计 |
| [06-robustness.md](docs/conventions/06-robustness.md) | 长等待可中断、OCR 铁律、fail-safe、finally 收尾、失败诊断 | 改执行链路 |
| [07-operations.md](docs/conventions/07-operations.md) | 定时任务补跑、更新日历、薅羊毛、月度合成/周常 | 改调度与后台任务 |
| [08-game-config.md](docs/conventions/08-game-config.md) | 第三方脚本刷本配置、flow_configs 本地/示例拆分 | 改刷本配置 |

## 游戏档案（插件式）

包代码在 `src/game_auto/`（src 布局）。本目录各分篇里出现的包内路径
（`runtime/`、`ui/`、`services/`、`settings/` 等）都相对 `src/game_auto/`；
`assets/`、`data/`、`docs/`、`tests/`、`tools/`、`scripts/` 相对仓库根。

新增游戏只需在 `src/game_auto/games/` 下加一个模块：构造 `GameProfile` 并 `register()`。
身份、默认配置、日常日志解析/合并、月商店、更新窗口、专项视图 key 都由
[games/registry.py](file:///e:/GithubProject/game_auto/src/game_auto/games/registry.py) 自动发现，
settings / bridge / 批处理 / 更新日历无需再改映射表。

## Git 推送代理坑

- 全局 git 代理 `http.proxy=http://127.0.0.1:7897`（Clash 系）。代理客户端没开时
  push 报 `Failed to connect to github.com:443 over proxy 127.0.0.1`（端口无监听）。
- 绕开：`git -c http.proxy= -c https.proxy= push origin master`（2026-11-14 实测
  直连可用、推送成功）。用临时 `-c` 参数，**别动全局代理配置**——代理客户端在线
  时仍走它；空串 `-c http.proxy=` 只覆盖单次命令。

## 变更记录

每次改动在 [docs/changelog.md](docs/changelog.md) 追加一行（只写"做了什么/为什么"）。
