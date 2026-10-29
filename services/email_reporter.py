"""SMTP 邮件发送 + 每日进度报告格式化。

配置位于 app_config.json 的 `email` 段，示例：

    "email": {
        "enabled": true,
        "smtp_host": "smtp.qq.com",
        "smtp_port": 465,
        "use_ssl": true,
        "sender": "xxx@qq.com",
        "password": "授权码",
        "recipients": ["user@example.com"],
        "subject_prefix": "[GameAuto]"
    }

未配置 / 字段缺失时，`is_email_configured()` 返回 False，发送会给出明确错误。

**敏感字段（password）** 通过 Windows DPAPI（`win32crypt.CryptProtectData`）加密后以 base64
形式存到 `app_config.json` 的 `email.password`，并打上 `password_protect: "dpapi"` 标记。
DPAPI 加密的字节只能被本机同用户解密，跨用户/跨机器不可读，避免明文落盘。
"""

from __future__ import annotations

import base64
import json
import logging
import smtplib
import ssl
import time
from dataclasses import dataclass
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Iterable, List, Optional

try:
    import win32crypt  # type: ignore
    _HAS_DPAPI = True
except ImportError:
    _HAS_DPAPI = False

LOGGER = logging.getLogger("GameAutomation.EmailReport")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_APP_CONFIG_PATH = _PROJECT_ROOT / "app_config.json"

_STATUS_LABEL = {
    "success": "✅ 成功",
    "already": "✅ 已完成",
    "pending": "⏳ 待执行",
    "skipped": "⏭ 跳过",
    "failed": "❌ 失败",
}


@dataclass
class EmailConfig:
    smtp_host: str
    smtp_port: int
    use_ssl: bool
    sender: str
    password: str
    recipients: List[str]
    subject_prefix: str = "[GameAuto]"


def _load_raw_config() -> dict:
    try:
        return json.loads(_APP_CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as exc:
        LOGGER.warning("[email] 读取 app_config.json 失败: %s", exc)
        return {}


def load_email_config() -> Optional[EmailConfig]:
    """从 app_config.json 读取 email 段；任何必填字段缺失时返回 None。"""
    cfg = _load_raw_config().get("email") or {}
    if not isinstance(cfg, dict) or not cfg.get("enabled"):
        return None

    smtp_host = str(cfg.get("smtp_host") or "").strip()
    sender = str(cfg.get("sender") or "").strip()
    raw_password = str(cfg.get("password") or "")
    password = _resolve_password(raw_password, str(cfg.get("password_protect") or ""))
    recipients_raw = cfg.get("recipients") or []
    if isinstance(recipients_raw, str):
        recipients = [r.strip() for r in recipients_raw.split(",") if r.strip()]
    else:
        recipients = [str(r).strip() for r in recipients_raw if str(r).strip()]

    if not smtp_host or not sender or not password or not recipients:
        LOGGER.warning("[email] 配置不完整：smtp_host/sender/password/recipients 缺一不可")
        return None

    try:
        smtp_port = int(cfg.get("smtp_port") or (465 if cfg.get("use_ssl", True) else 25))
    except (TypeError, ValueError):
        smtp_port = 465

    return EmailConfig(
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        use_ssl=bool(cfg.get("use_ssl", True)),
        sender=sender,
        password=password,
        recipients=recipients,
        subject_prefix=str(cfg.get("subject_prefix") or "[GameAuto]").strip() or "[GameAuto]",
    )


# ── Windows DPAPI 加密（统一走 services.secure_store） ───────────

_PASSWORD_PROTECT = "dpapi"

# 兼容旧接口：email 模块对外仍暴露 is_dpapi_available / protect_password 等，
# 内部转调 secure_store，保证新式 dpapi: 前缀与旧式 base64+flag 均可解密
try:
    from .secure_store import (  # type: ignore
        is_available as _sec_available,
        protect as _sec_protect,
        unprotect as _sec_unprotect,
        is_encrypted as _sec_is_encrypted,
    )
    _HAS_SECURE = True
except Exception:
    _sec_available = lambda: False  # type: ignore
    _sec_protect = None  # type: ignore
    _sec_unprotect = None  # type: ignore
    _sec_is_encrypted = lambda v: False  # type: ignore
    _HAS_SECURE = False


def is_dpapi_available() -> bool:
    # 统一走 secure_store
    if _HAS_SECURE:
        try:
            return bool(_sec_available())
        except Exception:
            return _HAS_DPAPI
    return _HAS_DPAPI


def protect_password(plain: str) -> str:
    """明文密码 → 加密串（新式 dpapi: 前缀）。"""
    if _HAS_SECURE and _sec_protect is not None:
        return _sec_protect(plain)
    # 回退旧实现
    if not _HAS_DPAPI:
        raise RuntimeError("当前环境没有 win32crypt，无法使用 DPAPI 加密")
    if not plain:
        return ""
    blob = win32crypt.CryptProtectData(plain.encode("utf-8"), _PASSWORD_PROTECT, None, None, None, 0)
    return base64.b64encode(blob).decode("ascii")


def unprotect_password(protected: str) -> str:
    """加密串 → 明文（兼容新式前缀与旧式 base64）。"""
    if _HAS_SECURE and _sec_is_encrypted(protected):
        return _sec_unprotect(protected)  # type: ignore
    if _HAS_SECURE and _sec_unprotect is not None:
        # 尝试新式解密，失败则回退旧式
        try:
            if protected.startswith("dpapi:"):
                return _sec_unprotect(protected)  # type: ignore
        except Exception as exc:
            LOGGER.debug("[email] 新式 DPAPI 解密失败，回退旧式: %s", exc)
    if not _HAS_DPAPI:
        raise RuntimeError("当前环境没有 win32crypt，无法解密 DPAPI 密码")
    if not protected:
        return ""
    # 兼容旧式无前缀 base64
    try:
        blob = base64.b64decode(protected.encode("ascii"))
        return win32crypt.CryptUnprotectData(blob)[1].decode("utf-8")
    except Exception:
        # 若本身是新式前缀，尝试 secure_store 解密
        if _HAS_SECURE and _sec_unprotect is not None:
            return _sec_unprotect(protected)  # type: ignore
        raise


def _resolve_password(raw_password: str, protect_flag: str) -> str:
    """根据 protect_flag 解密；未标记时视为明文（向后兼容）。"""
    if not raw_password:
        return ""
    # 优先走统一 secure_store（支持新式 dpapi: 前缀与旧式 base64）
    if _HAS_SECURE:
        try:
            from .secure_store import resolve as _resolve  # type: ignore

            return _resolve(raw_password, protect_flag)
        except Exception as exc:
            LOGGER.debug("[email] secure_store 解密失败，回退旧逻辑: %s", exc)
    if protect_flag == _PASSWORD_PROTECT:
        try:
            return unprotect_password(raw_password)
        except Exception as exc:
            LOGGER.warning("[email] DPAPI 解密失败，尝试作为明文处理: %s", exc)
            return raw_password
    # 新式前缀即使无 flag 也尝试解密
    if raw_password.startswith("dpapi:"):
        try:
            return unprotect_password(raw_password)
        except Exception as exc:
            LOGGER.warning("[email] DPAPI 解密失败，回退明文: %s", exc)
            return raw_password
    return raw_password


def mask_email_config(cfg: Optional[EmailConfig]) -> dict:
    """返回给 UI 的脱敏配置：password 永远为空，加 has_password 标志。"""
    if cfg is None:
        return {
            "smtp_host": "",
            "smtp_port": 465,
            "use_ssl": True,
            "sender": "",
            "password": "",
            "has_password": False,
            "recipients": [],
            "subject_prefix": "[GameAuto]",
            "password_protect": "",
        }
    return {
        "smtp_host": cfg.smtp_host,
        "smtp_port": cfg.smtp_port,
        "use_ssl": cfg.use_ssl,
        "sender": cfg.sender,
        "password": "",  # 永远不要把解密后的密码发到前端
        "has_password": bool(cfg.password),
        "recipients": list(cfg.recipients),
        "subject_prefix": cfg.subject_prefix,
        "password_protect": _PASSWORD_PROTECT if cfg.password else "",
    }


def load_raw_email_section() -> dict:
    """返回 app_config.json 中 email 段的原始 dict（不抛异常）。"""
    return _load_raw_config().get("email") or {}


def save_email_section(section: dict) -> tuple[bool, str]:
    """覆盖写入 email 段。section 是 dict，会替换原 email 段但保留其他顶层字段。

    返回 (成功, 错误信息)。
    """
    try:
        root_cfg = _load_raw_config()
        root_cfg["email"] = section
        from ..settings.manager import atomic_write_json
        atomic_write_json(_APP_CONFIG_PATH, root_cfg)
        return True, ""
    except Exception as exc:
        LOGGER.exception("[email] 写回 app_config.json 失败")
        return False, str(exc)


def is_email_configured() -> bool:
    return load_email_config() is not None


def _format_task_line(task) -> str:
    status = _STATUS_LABEL.get(task.status, task.status)
    line = f"  - {task.task_name or task.task_key}: {status}"
    if task.message:
        line += f"  ({task.message})"
    return line


def build_daily_report(
    automation_report: Iterable[str],
    automation_warnings: Iterable[str],
    automation_total: int,
    automation_success: int,
    automation_failed: int,
    daily_snapshot,  # DailyProgressSnapshot | None
    snapshot_fresh: bool = False,  # True=发信前刚实时查过；False=用的是数据库缓存
) -> tuple[str, str]:
    """构造 (subject, body_text)。body_text 是纯文本，兼容所有邮件客户端。"""
    date_key = time.strftime("%Y-%m-%d")
    automation_report = list(automation_report)
    automation_warnings = list(automation_warnings)
    subject = f"[GameAuto] 一条龙报告 {date_key}"

    lines: List[str] = []
    lines.append(f"GameAuto 一条龙报告 - {date_key}")
    lines.append("=" * 60)
    lines.append("")

    lines.append("[本轮任务执行结果]")
    if not automation_report:
        lines.append("  (无报告)")
    else:
        for line in automation_report:
            lines.append(f"  {line}")
    lines.append("")
    lines.append(
        f"汇总: 共 {automation_total} 项，成功 {automation_success}，失败 {automation_failed}，警告 {len(automation_warnings)}"
    )
    if automation_warnings:
        lines.append("")
        lines.append("[警告]")
        for w in automation_warnings:
            lines.append(f"  - {w}")
    lines.append("")
    lines.append("-" * 60)
    lines.append("")

    if daily_snapshot is None or not daily_snapshot.games:
        lines.append("[每日任务进度]")
        lines.append("  (今日无 tracker 记录，请先在『每日进度』页点击刷新)")
    else:
        # 明确标注数据新鲜度，避免收件人把缓存数据当成刚查的结果
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        if snapshot_fresh:
            lines.append(f"[每日任务进度] {daily_snapshot.date_key}  （发信前实时查询，{stamp}）")
        else:
            lines.append(
                f"[每日任务进度] {daily_snapshot.date_key}  "
                f"⚠ 实时查询未成功，以下为数据库缓存数据（生成于 {stamp}），可能不是最新"
            )
        for game in daily_snapshot.games:
            if game.is_all_success:
                head = "✅ 全部完成"
            elif game.has_failed:
                head = "❌ 有失败"
            else:
                head = "⏳ 进行中"
            lines.append(f"- {game.game_name} ({game.game_key}) {head}  完成度 {game.progress_percent}%")
            if game.errors:
                for e in game.errors:
                    lines.append(f"  ! 错误: {e}")
            for task in game.tasks:
                lines.append(_format_task_line(task))
        lines.append("")
        completed = len(daily_snapshot.completed_games)
        failed = len(daily_snapshot.failed_games)
        lines.append(
            f"今日进度汇总: 共 {len(daily_snapshot.games)} 款，完成 {completed}，失败 {failed}"
        )

    lines.append("")
    lines.append("=" * 60)
    lines.append("本邮件由 GameAuto 自动发送")

    return subject, "\n".join(lines)


def send_email(
    config: EmailConfig,
    subject: str,
    body_text: str,
    timeout_sec: int = 20,
) -> tuple[bool, str]:
    """发送纯文本邮件。返回 (成功, 错误信息)。"""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = config.sender
    msg["To"] = ", ".join(config.recipients)
    msg.attach(MIMEText(body_text, "plain", "utf-8"))

    # 邮件里只显示收件人地址，不打印密码 / 授权码到日志
    masked_sender = _mask_email(config.sender)
    LOGGER.info(
        "[email] 准备发送: subject=%r from=%s to=%s use_ssl=%s port=%s",
        subject, masked_sender, config.recipients, config.use_ssl, config.smtp_port,
    )

    try:
        if config.use_ssl:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(config.smtp_host, config.smtp_port, timeout=timeout_sec, context=context) as smtp:
                smtp.login(config.sender, config.password)
                smtp.sendmail(config.sender, config.recipients, msg.as_string())
        else:
            with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=timeout_sec) as smtp:
                smtp.starttls(context=ssl.create_default_context())
                smtp.login(config.sender, config.password)
                smtp.sendmail(config.sender, config.recipients, msg.as_string())
    except (smtplib.SMTPAuthenticationError, smtplib.SMTPException, OSError) as exc:
        LOGGER.warning("[email] 发送失败: %s", exc)
        return False, str(exc)
    except Exception as exc:
        LOGGER.exception("[email] 发送异常")
        return False, str(exc)

    LOGGER.info("[email] 发送成功: subject=%r", subject)
    return True, ""


def _mask_email(addr: str) -> str:
    if "@" not in addr:
        return "***"
    name, domain = addr.split("@", 1)
    if len(name) <= 2:
        masked_name = "*" * len(name)
    else:
        masked_name = name[0] + "*" * (len(name) - 2) + name[-1]
    return f"{masked_name}@{domain}"
