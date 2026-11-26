"""AI 对话史全链刷新编排（GUI「AI 对话史」开关手动触发，2026-11-18）。

用户拍板：不做定时自动化，只在 game_auto 界面放一个开关手动触发。本模块把
此前散落的手动步骤串成一条可从 GUI 后台线程调用的链：

  ① catpaw_fetch   妙手 SQLite → catpaw.json（tools/catpaw_capture）
  ② kilo_fetch     Kilo 内核 API → kilo.json（tools/kilo_capture；内核没开/
                    密码过期时干净失败 → 跳过并沿用上一次聚合，不阻塞整链）
  ②b teleagent_fetch / codearts_fetch / dsh_fetch / raccoon_fetch / qwenwork_fetch
                    TeleAgent / 码道 / DSH NEXT / 商汤小浣熊 →
                    teleagent.json / codearts.json / dsh.json / raccoon.json
                    （失败同 Kilo 降级，不阻断总库与 RAG 重建）
  ③ build.py       重导总库（F:/data/_ai_organize_tools，AI_ORGANIZE_OUTDIR
                    指到 data/cache/ai_history_stage 暂存；串链内含 recover
                    scan → length scan → 模型报告 → inject）
  ④ 拷回 F:        AI对话全览.html / data.json / model_length_report.html
                    → F:/data/AI对话汇总/（**需管理员权限**——本机安全
                    软件按进程拦 python 对 F: 的写入，bash/cmd 不拦，11-17
                    Trae 抓取实锤；GUI 是 python 进程所以不能 shutil）
  ⑤ RAG 重建       tools/ai_history_rag.py build（chunks + index.bin）

线程模型：GUI 槽起 daemon 线程调 run_refresh()；本模块维护单飞锁
（refresh 中再次触发直接拒绝）与最近一次结果状态（bridge getAiHistoryStatus
读取）。每步 subprocess 隔离（超时/编码统一处理），进度经回调逐行外抛。
失败语义：①②③④⑤ 任一核心步骤失败 → 整链报失败（F: 旧页面/旧索引保留，
不半新半旧）；②/②b 三个外部抓取器特殊，失败只降级为警告（沿用上次聚合）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Callable, Dict, Optional

LOGGER = logging.getLogger("GameAutomation")

# 本文件在 src/game_auto/services/ 下：services → game_auto → src → 仓库根，
# 上 4 级才是仓库根（2026-11-18 端到端验证实锤：少算一级把 tools 路径拼成
# src/tools/...，首跑即 退出码 2）
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
STAGE_DIR = os.path.join(PROJECT_ROOT, "data", "cache", "ai_history_stage")
# 汇总目录/工具链目录与 build.py 同一口径（AI_ORGANIZE_OUTDIR / AI_ORGANIZE_TOOLS
# 可覆盖，缺省=本机现值）——此前三处独立硬编码会在换目录时静默断链（2026-11-18 审计）
OUT_DIR_F = os.environ.get("AI_ORGANIZE_OUTDIR") or r"F:\data\AI对话汇总"
BUILD_PY = os.path.join(os.environ.get("AI_ORGANIZE_TOOLS") or r"F:/data/_ai_organize_tools",
                        "build.py")

# 拷回 F: 的产物（build.py 串链三件套）
COPY_FILES = ("AI对话全览.html", "data.json", "model_length_report.html")

_FETCH_TIMEOUT = 600       # 抓取器（妙手/Kilo，各 <1 分钟）
_BUILD_TIMEOUT = 1200      # build.py 全链（本机实测 ~3 分钟）
_RAG_TIMEOUT = 1800        # RAG 切块+索引（157MB 数据源实测 ~4 分钟）

# 双锁：_FLIGHT 是单飞闸（acquire 后占整链时长，防重复触发）；_STATE_LOCK 只护
# _STATE 的毫秒级读写。不能合一——run_refresh 持闸期间每步都 _set/_snapshot，
# 同一把不可重入的 Lock 会第一个 emit 就自死锁（2026-11-18 套件栈实锤）；
# status_json 由 GUI 主线程调用，也绝不能被整链闸挡住（会冻 5-8 分钟界面）。
_FLIGHT = threading.Lock()
_STATE_LOCK = threading.RLock()
_STATE: Dict = {
    "running": False,
    "last_ok": None,       # None=从没跑过
    "last_ts": 0,
    "last_msg": "",
    "step": "",            # 当前步骤名（running 时给 UI 显示）
}


def _snapshot() -> Dict:
    with _STATE_LOCK:
        return dict(_STATE)


def _set(update: Dict) -> None:
    with _STATE_LOCK:
        _STATE.update(update)


def status_json() -> str:
    """bridge getAiHistoryStatus 的同步读（只读缓存态，无 IO）。"""
    return json.dumps(_snapshot(), ensure_ascii=False)


def _py(args: list, timeout: int, logger: logging.Logger, label: str,
        emit: Callable[[str], None], env_extra: Optional[dict] = None) -> str:
    """跑一个 python 子进程步骤（venv 解释器、repo 根 cwd、UTF-8）。

    返回 stdout（失败抛 RuntimeError 带 stderr 尾巴）。逐行打日志让 UI
    日志面板能看到链路进展。
    """
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    emit(f"[{label}] 运行 {os.path.basename(args[1]) if len(args) > 1 else label} ...")
    proc = subprocess.run(
        args, cwd=PROJECT_ROOT, env=env, capture_output=True,
        encoding="utf-8", errors="replace", timeout=timeout,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-8:]
        raise RuntimeError(f"{label} 退出码 {proc.returncode}: " + " | ".join(tail)[-600:])
    for line in (proc.stdout or "").splitlines()[-3:]:
        if line.strip():
            logger.info("[AI对话史][%s] %s", label, line.strip()[:200])
    return proc.stdout or ""


def _copy_to_f(src: str, dst_dir: str) -> None:
    """拷回 F: 汇总目录。

    拦截规则实锤（2026-11-18 提权探针，data/cache/aihist_admin_probe.json）：
    **非管理员**进程一律禁写 F 盘——python 直写、cmd /c copy、PowerShell
    Copy-Item 全部拒绝访问，robocopy 挂死，msys cp.exe 直接崩；**管理员**
    python 直写可用。GUI 本来就提权跑（start_gui_admin.bat 自提权），所以
    这里直接 shutil，不走任何子进程歪招。若 GUI 以普通权限跑，本步会
    PermissionError 收口成链失败（状态信息已写明原因）。
    """
    shutil.copyfile(src, os.path.join(dst_dir, os.path.basename(src)))


def run_refresh(logger: logging.Logger, emit: Callable[[str], None]) -> Dict:
    """全链刷新入口（阻塞版，调用方放 daemon 线程）。返回最终状态快照。

    emit(status_json) 在关键节点回调（bridge 转发 QML）；并发触发被单飞锁拒绝
    （返回 running 态快照，msg 带提示）。
    """
    if not _FLIGHT.acquire(blocking=False):
        # 拒绝路径直读 _STATE（标量读原子级，UI 提示够用），绝不抢刷新线程
        # 手里的 _FLIGHT/_STATE_LOCK。
        snap = dict(_STATE)
        snap["msg"] = "已有刷新在跑，忽略本次触发"
        return snap
    started = time.time()
    try:
        _set({"running": True, "step": "准备", "last_msg": ""})
        emit(json.dumps(_snapshot(), ensure_ascii=False))
        logger.info("[AI对话史] 全链刷新开始")
        os.makedirs(STAGE_DIR, exist_ok=True)
        for stale in os.listdir(STAGE_DIR):
            try: os.remove(os.path.join(STAGE_DIR, stale))
            except OSError: pass

        # ① 妙手
        _set({"step": "妙手抓取"})
        emit(json.dumps(_snapshot(), ensure_ascii=False))
        _py([sys.executable, "-X", "utf8",
             os.path.join(PROJECT_ROOT, "tools", "catpaw_capture", "catpaw_fetch.py")],
            _FETCH_TIMEOUT, logger, "妙手", emit)

        # ② Kilo（可降级）
        degraded: list = []
        try:
            _set({"step": "Kilo 抓取"})
            emit(json.dumps(_snapshot(), ensure_ascii=False))
            _py([sys.executable, "-X", "utf8",
                 os.path.join(PROJECT_ROOT, "tools", "kilo_capture", "kilo_fetch.py")],
                _FETCH_TIMEOUT, logger, "Kilo", emit)
        except (RuntimeError, subprocess.TimeoutExpired, OSError) as exc:
            degraded.append("Kilo")
            logger.warning("[AI对话史] Kilo 抓取失败（沿用上次聚合继续）: %s", str(exc)[:300])
            emit(f"[Kilo] 抓取失败，沿用上次聚合：{str(exc)[:120]}")

        # ②b 本地抓取器：TeleAgent / 码道 / DSH / 小浣熊。单个桌面源不可用
        #    时沿用上次聚合，避免一个应用没开就阻断总库与 RAG 的其余更新。
        for _label, _sub in (
            ("TeleAgent", ("teleagent_capture", "teleagent_fetch.py")),
            ("码道", ("codearts_capture", "codearts_fetch.py")),
            ("DSH", ("dsh_capture", "dsh_fetch.py")),
            ("小浣熊", ("raccoon_capture", "raccoon_fetch.py")),
            ("千问办公", ("qwenwork_capture", "qwenwork_fetch.py")),
        ):
            try:
                _set({"step": f"{_label} 抓取"})
                emit(json.dumps(_snapshot(), ensure_ascii=False))
                _py([sys.executable, "-X", "utf8",
                     os.path.join(PROJECT_ROOT, "tools", *_sub)],
                    _FETCH_TIMEOUT, logger, _label, emit)
            except (RuntimeError, subprocess.TimeoutExpired, OSError) as exc:
                degraded.append(_label)
                logger.warning("[AI对话史] %s 抓取失败（沿用上次聚合继续）: %s",
                               _label, str(exc)[:300])
                emit(f"[{_label}] 抓取失败，沿用上次聚合：{str(exc)[:120]}")

        # ③ build.py 重导（串链内含 recover/length/report/inject）
        _set({"step": "重导总库"})
        emit(json.dumps(_snapshot(), ensure_ascii=False))
        build_out = _py(
            [sys.executable, "-X", "utf8", BUILD_PY],
            _BUILD_TIMEOUT, logger, "重导", emit,
            env_extra={"AI_ORGANIZE_OUTDIR": STAGE_DIR, "PYTHONIOENCODING": "utf-8"},
        )
        m = re.search(r'"sessions":\s*(\d+)', build_out)
        n_sessions = int(m.group(1)) if m else 0

        # ④ 拷回 F:（管理员 python 直写；普通权限 GUI 会 PermissionError 收口）
        _set({"step": "拷回 F: 汇总目录"})
        emit(json.dumps(_snapshot(), ensure_ascii=False))
        for name in COPY_FILES:
            src = os.path.join(STAGE_DIR, name)
            if os.path.isfile(src):
                _copy_to_f(src, OUT_DIR_F)

        # ⑤ RAG 重建
        _set({"step": "RAG 重建"})
        emit(json.dumps(_snapshot(), ensure_ascii=False))
        rag_out = _py([sys.executable, "-X", "utf8",
                       os.path.join(PROJECT_ROOT, "tools", "ai_history_rag.py"), "build"],
                      _RAG_TIMEOUT, logger, "RAG", emit,
                      env_extra={"PYTHONIOENCODING": "utf-8"})
        m_blk = re.search(r"切块：\s*([\d,]+)", rag_out)
        m_idx = re.search(r"index\.bin\s*(\d+)MB", rag_out)
        n_blocks = int(m_blk.group(1).replace(",", "")) if m_blk else 0
        idx_mb = int(m_idx.group(1)) if m_idx else 0

        msg = (f"{n_sessions} 会话 · RAG {n_blocks} 块 / 索引 {idx_mb}MB · "
               f"耗时 {int(time.time() - started)}s"
               + "".join(f" · {name} 沿用旧数据" for name in degraded))
        _set({"running": False, "step": "", "last_ok": True,
              "last_ts": time.time(), "last_msg": msg})
        logger.info("[AI对话史] 全链刷新完成: %s", msg)
    except subprocess.TimeoutExpired as exc:
        msg = f"步骤[{_snapshot().get('step')}] 超时（{_snapshot().get('step')}上限见 ai_history_refresh 模块）"
        _set({"running": False, "step": "", "last_ok": False,
              "last_ts": time.time(), "last_msg": msg})
        logger.error("[AI对话史] %s: %s", msg, exc)
    except Exception as exc:  # noqa: BLE001 - 链路任一步失败都收口成状态，不崩 GUI 线程
        msg = f"步骤[{_snapshot().get('step')}] 失败: {str(exc)[:300]}"
        _set({"running": False, "step": "", "last_ok": False,
              "last_ts": time.time(), "last_msg": msg})
        logger.error("[AI对话史] %s", msg)
    finally:
        try: shutil.rmtree(STAGE_DIR, ignore_errors=True)
        except OSError: pass
        emit(json.dumps(_snapshot(), ensure_ascii=False))
        _FLIGHT.release()
    return _snapshot()
