"""Meoo（秒悟）每日登录赠送积分——薅羊毛集合里的 CLI 型子流程。

平台规则（用户口径 + meoo.com）：每天登录一次 Meoo（跑一次 `meoo whoami`
即可，无需浏览器）可领每日赠送积分（FREE 套餐上限 2,000/天）。

与窗口型羊毛任务的根本差异：没有窗口、没有进程、成败核验只能走 CLI 输出。
所以本模块的架构是「触发 + 证据门控」：
  1. 触发：`meoo whoami`（一次已鉴权 API 调用，按用户口径算当日登录）；
  2. 证据：`meoo account --json` 读 `data.points.totalGranted`（累计获得积分）
     —— 每日到账时该数会 +2,000，这是唯一可靠的到账信号
     （`daily_login.used` 是配额消耗口径、不代表是否领取，2026-11-18 实测）；
  3. 门控：workflow.sqlite3 task_meta 记录「最近一次见到的 totalGranted 及日期」，
     今日 totalGranted 超过记录值 → 到账成功；持平 → 未到账（fail-closed，
     薅羊毛批次重试时会再试，到账即翻绿）。

⚠️ 触发方式未实锤（2026-11-18 注册当天无法判别 whoami 是否算登录——注册即
首次登录，当日赠送可能已随注册发放）。首日/次日按证据门控的失败信息校准：
若连续多日「未到账」，说明触发动作不是 whoami，需改走 meoo.com 网页签到或
其他入口（看积分明细获取记录定位）。

账号标识脱敏：whoami 输出含用户名/ID，日志一律只写「登录态有效」，不落原文
（与 01-workflow 日志规则一致）。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from typing import Optional, Tuple

LOGGER = logging.getLogger("GameAutomation")

META_KEY = "meoo_login_bonus:last"   # task_meta 键，值 = JSON {date, total_granted}
SUBPROCESS_TIMEOUT = 60              # CLI 单次调用超时
BASELINE_NOTE = "首次运行：已记录基线，明日起开始判定每日到账"


class MeooCliError(RuntimeError):
    """CLI 调用失败（非零退出/超时/输出不可解析）。"""


def _build_env(cli_path: str) -> dict:
    """子进程 PATH 前置 CLI 所在目录：meoo.cmd 依赖同目录的 node.exe，
    而薅羊毛批次跑在 GUI 进程环境里，安装后的用户 PATH 未必已继承。"""
    env = dict(os.environ)
    node_dir = os.path.dirname(os.path.abspath(cli_path))
    env["PATH"] = node_dir + os.pathsep + env.get("PATH", "")
    return env


def _run_cli(cli_path: str, args: list, env: dict) -> str:
    """执行 meoo 子命令并返回 stdout；失败抛 MeooCliError。"""
    try:
        proc = subprocess.run(
            [cli_path, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=SUBPROCESS_TIMEOUT,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise MeooCliError(f"CLI 超时({SUBPROCESS_TIMEOUT}s): {args}") from exc
    except OSError as exc:
        raise MeooCliError(f"CLI 无法执行（路径/权限）: {exc}") from exc
    if proc.returncode != 0:
        raise MeooCliError(
            f"CLI 退出码 {proc.returncode}: {(proc.stderr or proc.stdout or '').strip()[:200]}"
        )
    return proc.stdout or ""


def parse_account(stdout: str) -> Tuple[int, int]:
    """解析 `meoo account --json` → (totalGranted, dailyLoginUsed)。

    daily_login.used 是配额消耗口径（不代表是否领取，实测 2026-11-18），
    返回它只为日志观察；到账判定只用 totalGranted。
    """
    try:
        data = json.loads(stdout)["data"]
        granted = int(data["points"]["totalGranted"])
    except (ValueError, KeyError, TypeError) as exc:
        raise MeooCliError(f"account 输出解析失败: {str(exc)[:120]}") from exc
    daily_used = 0
    for b in (data.get("plan") or {}).get("benefits") or []:
        if (b or {}).get("typeCode") == "daily_login":
            try:
                daily_used = int((b or {}).get("used") or 0)
            except (TypeError, ValueError):
                daily_used = 0
            break
    return granted, daily_used


def evaluate_daily_bonus(
    today_key: str,
    total_now: int,
    stored: Optional[dict],
) -> Tuple[bool, str, dict]:
    """每日到账判定（纯函数，回归锁这里）。

    stored = 上一次运行记录 {date, total_granted}（None=首次运行）。
    返回 (ok, msg, value_to_store)——value_to_store 一律写回
    max(total_now, stored_total)（到账金额只增不减，防止误存回落值）。

    逻辑：
      - 首次运行：记录基线，明日起判定（不算成功也不算失败，不算静默假成功
        ——msg 明说这是基线）；
      - stored 是今天：total_now > stored_total → 今日到账（+Δ）；持平 →
        未到账 fail-closed（批次重试会再试）；
      - stored 是往日：total_now > stored_total → 到账（可能今天早些时候已
        到过账，同样记成功）；持平 → 今日未到账 fail-closed。
    """
    new_value = {"date": today_key, "total_granted": total_now}
    if not stored:
        return True, BASELINE_NOTE, new_value
    stored_date = str(stored.get("date") or "")
    stored_total = int(stored.get("total_granted") or 0)
    if total_now > stored_total:
        delta = total_now - stored_total
        return True, f"积分到账 +{delta}（{stored_total} → {total_now}）", new_value
    # 持平或回落：未见到账。回落视为异常但同样 fail-closed（不静默放行）。
    if stored_date == today_key:
        return (False, f"今日已试运行，totalGranted 持平 {total_now}，未见到账"
                "（明日批次自动重试；连续多日持平请查 meoo.com 积分明细）", new_value)
    return (False, f"今日未见到账（基线 {stored_total}@{stored_date} → 现 {total_now}）",
            new_value)


def run_wool(logger: logging.Logger, cfg: Optional[dict] = None) -> Tuple[bool, str]:
    """薅羊毛集合的 MEOO 子流程入口：触发登录 + 证据门控判定每日赠送。"""
    cfg = cfg or {}
    cli_path = str(cfg.get("cli_path") or "meoo").strip()
    if not os.path.isfile(cli_path):
        # PATH 上找得到也行（用户装了完整 Node 后的场景）
        logger.debug("[MEOO] cli_path 不存在，退回 PATH 解析: %s", cli_path)
    env = _build_env(cli_path)

    # ① 触发：whoami（一次已鉴权调用，按用户口径计当日登录）
    who = _run_cli(cli_path, ["whoami"], env)
    if not who.strip():
        raise MeooCliError("whoami 无输出")
    logger.info("[MEOO] 登录态有效（whoami 正常）")

    # ② 证据：account --json
    granted, daily_used = parse_account(_run_cli(cli_path, ["account", "--json"], env))
    logger.info("[MEOO] totalGranted=%d dailyLogin.used=%d", granted, daily_used)

    # ③ 门控：workflow.sqlite3 task_meta 记录最近值并判定
    from ..storage.workflow_db import WorkflowDB
    from ..settings.paths import WORKFLOW_DB_PATH

    today_key = time.strftime("%Y-%m-%d")
    stored = None
    try:
        db = WorkflowDB(str(WORKFLOW_DB_PATH))
        raw = db.get_task_meta(META_KEY)
        if raw:
            stored = json.loads(raw)
    except Exception as exc:  # noqa: BLE001 - 读不到基线按首次运行处理
        logger.warning("[MEOO] 读取基线失败（按首次运行处理）: %s", exc)

    ok, msg, new_value = evaluate_daily_bonus(today_key, granted, stored)
    try:
        db = WorkflowDB(str(WORKFLOW_DB_PATH))
        db.set_task_meta(META_KEY, json.dumps(new_value, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001 - 写不进基线下次只能重新引导
        logger.warning("[MEOO] 写入基线失败: %s", exc)
    (logger.info if ok else logger.warning)("[MEOO] %s", msg)
    return ok, msg
