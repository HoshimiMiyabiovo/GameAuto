# AI 对话史工具链（对话总库 / RAG / 模型画像）

> 对话归档的来源、串链顺序与口径铁律。抓取器/串链的踩坑细节见 changelog
> 对应日期（2026-11-17~18 密集接入期），本篇只收**长期有效的口径与结构**。

## 数据流（全链）

```
各来源本地存储
  → tools/<src>_capture/<src>_fetch.py     抓取 → add() 同构 JSON
      data/<src>_capture/export/<src>.json + raw/ 原始数据（均 gitignore）
  → F:/data/_ai_organize_tools/build.py    全部 do_* 聚合 → SESSIONS
      产物写 OUTDIR（默认 F:\data\AI对话汇总；python 写 F: 被拦时用
      AI_ORGANIZE_OUTDIR 暂存到工作区再 bash 拷回——见 01 分篇 F 盘节）
  → 串链（build.py main 内置）：recover scan → length scan → 报告 → inject
      （recover/length 经 AI_ORGANIZE_SOURCE 读**本轮暂存** data.json，
        不传就读 F: 上一轮旧库——模型恢复永远落后一轮，2026-11-18 实锤）
  → 拷回 F:（AI对话全览.html / data.json / model_length_report.html）
  → tools/ai_history_rag.py build          chunks.jsonl + index.bin（BM25 倒排）
```

GUI 开关：**「每日进度」页底部「AI 对话史」卡片**（`services/ai_history_refresh.py`
编排上述全链，单飞闸 + 双锁；不做定时自动化——2026-11-18 用户拍板）。
链内本地抓取器 = 妙手 / Kilo / TeleAgent / 码道 / DSH 五个（2026-11-20 补齐
后三个——此前只串了妙手+Kilo，三源导出一直靠手动跑、数据停在 11-18，用户发现后修）；
这些抓取器失败只降级（沿用上次聚合），不阻断 build/拷回/RAG。Grok（CDP 连
浏览器）与 Trae（官方分享页）有外部条件，仍手动跑。

## 配置（环境变量，2026-11-18 knob 化；全部可选，缺省=本机现值）

| 变量 | 含义 | 消费方 |
|---|---|---|
| `AI_ORGANIZE_OUTDIR` | 汇总目录（data.json / AI对话全览.html / 报告；默认 `F:\data\AI对话汇总`） | build.py OUTDIR、rag SOURCE_JSON、inject DEFAULT_HTML、refresh OUT_DIR_F |
| `AI_ORGANIZE_SOURCE` | 本轮暂存的 data.json（串链内部用，避免读上一轮旧库） | recover/length 的 SOURCE_JSON |
| `AI_ORGANIZE_TOOLS` | build.py 工具链目录（默认 `F:\data\_ai_organize_tools`） | build TEMPLATE、inject DEFAULT_TEMPLATE、refresh BUILD_PY |
| `AI_REPO_ROOT` | game_auto 仓库根（默认 `E:\GithubProject\game_auto`） | build.py 内 ai_history_* 工具与各抓取聚合 JSON 路径 |

换盘/换机只改环境变量不动代码。同目录此前在 build/rag/inject/refresh 四处
独立硬编码（2026-11-18 审计收敛）。各来源的**应用本体路径**（zcode/catpaw/
kilo/teleagent/codearts 等）以 `~` 相对或客观安装位置为准，属机器事实不算
可配置项；build.py 里残留的 `C:\Users\admin` 用户名硬编码已改 `expanduser`。

## 来源清单（20 个 do_*，9 个本地抓取器）

| 来源 | do_* | 正文位置 | 抓取器 |
|---|---|---|---|
| ZCode（本工具） | do_zcode | `~/.zcode/cli/db/db.sqlite`（session/message/part） | —（直读） |
| WorkBuddy | do_workbuddy | `~/.workbuddy/projects/**/*.jsonl` + workbuddy.db 标题 | — |
| Qoder | do_qoder | `~/.qoder/projects/**/*.jsonl` + IDE main.sqlite | — |
| OpenCode | do_opencode | `~/.local/share/opencode/opencode.db`（v2 三表） | — |
| MiniMax Code | do_minimax | `~/.minimax/v2/sessions/**/messages.jsonl` | — |
| Hermes Agent | do_hermes | Hermes state.db | — |
| GitHub Copilot | do_copilot | VSCode workspaceStorage chatSessions（增量回放） | — |
| Claude Code / VS Code | do_claude / do_claude_vscode | `~/.claude/projects/**/*.jsonl` | — |
| Cursor | do_cursor | state.vscdb cursorDiskKV（bubbles） | — |
| Codex | do_codex | `.codex/sessions/**/rollout-*.jsonl` | — |
| DeepSeek | do_deepseek | `F:/data/deepseek-all-chats/markdown/*.md` | — |
| Grok | do_grok | CDP 连浏览器抓 `grok.json` | tools/grok_capture |
| Trae | do_trae | 官方分享页公开接口 | tools/trae_capture |
| 妙手 CatPaw | do_catpaw | `%APPDATA%/catpaw-moon/catpaw-memory-*.db` | tools/catpaw_capture |
| Kilo Desktop | do_kilo | 内核 HTTP API（Basic kilo:KILO_SERVER_PASSWORD） | tools/kilo_capture |
| TeleAgent | do_teleagent | `~/.local/share/TeleAgent/users/*/teleagent.db` | tools/teleagent_capture |
| 码道 CodeArts | do_codearts | `~/.codeartsdoer/codearts-data/opencode.db` | tools/codearts_capture |
| DSH NEXT | do_dsh | `~/.dsh/sessions/**/session.v4.jsonl.zstd`（逐行 zstd 帧拼接） | tools/dsh_capture |
| Copilot 网页 | （并入 DeepSeek 同目录） | markdown 导出 | — |
| QClaw / cc-switch | NOTES 声明无正文/仅元数据 | — | — |

## 口径铁律

1. **add() 角色与截断**：`user/ai` 截 14000、`think/output` 20 万（中段省略）、
   `tool` 600。**主统计口径 = ai 正文 + think**；output/tool/user 是参考列
   （tool 是摘要，算进去会钉死中位数）。
2. **工具返回走 output 角色（①口径，2026-11-18 全源对齐）**：工具调用留 🔧
   摘要（600），返回全文进 output（20 万帽）。**Copilot 例外**：存档只有
   invocationMessage 展示消息、无返回字段。
3. **模型名**：有逐条/会话级 modelID 字段的来源按**原值**记录（不臆测映射，
   2026-11-17 用户纠偏；码道的 `deepseek-v4.1-flash-1017` 是真实 ID 同此口径）；
   客户端内部代号/无字段来源才进 `MODEL_ALIASES` 或 `TRAE_FIXED_MODEL`
   （用户拍板表，ai_history_model_recover.py；TeleAgent 的 `chat-flash` 属内部
   路由名，2026-11-19 拍板 = deepseek-v4.1-flash-1017，原值留 raw 可追溯）。
   会话 note 的模型标注两种格式都认：「模型：xxx」（Hermes/WorkBuddy/妙手）与
   「· 模型 xxx」无冒号（Kilo/TeleAgent/码道 fetch）。**Qoder 1.1.86-hotfix.5.2 起
   逐条 `message.model` 只写选择器档位 `auto`**（≤1.1.79 写真实内部代号）：
   恢复器遇 auto 用同文件当时生效的 `runtime-config.model` 解析（user 行
   `businessModelKey`、IDE 库 `chat_sessions.model` 同值可交叉验证），这是同文件
   真实字段、不算臆测映射；无 runtime-config 时 auto 原样保留（2026-11-20）。
4. **正文本地不存在的来源不上库**：码道桌面端 chatSessions jsonl 曾被误判
   「无正文可接」——真相源是内核数据目录（AgentKernel=opencode 变体，
   `~/.codeartsdoer/codearts-data/opencode.db`）。**找对话正文先找应用的
   内核/运行时数据目录，UI 存档不是真相源**（Kilo/TeleAgent 同形态）。
5. **新抓取器必带**：临时目录钉 `data/cache`（TEMP 坏境回退 cwd，见 01 分篇）、
   数据根 `data/<src>_capture/`（gitignore）、raw/ 留原始数据、export/ 聚合
   按 id 合并去重、独立套件（临时 DB 复现 schema 打桩，不碰真实数据）、
   ci_suites 登记、**模型恢复器**（`recover_<src>` + `RECOVERERS` 登记 +
   套件桩测；有真实字段按铁律 3 原值记。漏接恢复器=最终页模型列空，
   2026-11-17/11-19 两次复发后立此规）。

## 已知环境坑（详见 01 分篇）

- python 写 F: 被拦（非管理员全拒；管理员可直写）→ 暂存 + 拷回两步。
- Git Bash `TEMP=/tmp` 不存在 → tempfile 回退 cwd → 临时目录一律 `dir=` 钉
  `data/cache`；sqlite 排序临时文件同坑 → `PRAGMA temp_store = MEMORY`。
- Kilo 内核重启轮换密码/端口 → 管理员重跑 `tools/kilo_capture/kilo_env_dump.py`。
- 提权应用（码道/TeleAgent）进程 cmdline/env 普通权限读不到 → 提权探针
  （UAC 一次），读完删探针脚本。
- DSH 会话文件是「每行 JSON 单独压一个 zstd 帧、多帧顺序拼接」，单帧 API 只解出
  第一行（2026-11-20 实锤）→ 用 `backports.zstd.open(path,"rb")` 跨帧流式读；
  Python 3.13 无 stdlib `compression.zstd`（3.14 才内置），依赖走 requirements.txt
  的 `backports.zstd` + dependency_manager 的 import 名例外。
