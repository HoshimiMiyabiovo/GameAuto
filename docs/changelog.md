# Changelog

> 约定：每次提交在对应日期下**只追加一行**，写清"做了什么 / 为什么"。
> 实现细节、踩坑与口径放 `docs/conventions/`，这里不展开。
>
> 历史技术说明（原 `overview.md`）已归档到
> [notes/overview-2026-11.md](notes/overview-2026-11.md)。

## 2026-11-02

- 提交既有改动：绝区零恶名狩猎/自动战斗、崩铁自塑尘脂、月商店滚动查找、每日进度脚本失败态。
- 新增插件式 `games/` 游戏档案注册表，收敛 nte 等按游戏分支与三处重复更新窗口。
- `ui/bridge.py` 拆为 `ui/bridge_views/` 按视图 mixin（bridge 745 行，各文件均 <1000 行）。
- 文档：AGENTS.md 拆为 `docs/conventions/` 索引 + 8 篇；原 overview 技术说明归档，本文件改作 changelog。
- 修复拆分后 mixin 内相对导入层级（`.bridge_backends` → `..bridge_backends`），并清理 bridge 无用导入。
- 根目录整理：`模板/` → `assets/templates/`、`click_sequences/` → `assets/click_sequences/`、`start/`+`launchers/` → `scripts/`；模板路径与本地 flow/app 配置一次性迁移。
- 视图回收套件改为轮询等待释放（离屏首次编译 1-2s 时固定 0.75s 断言会误报）。
