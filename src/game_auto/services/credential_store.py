"""凭据存储服务：敏感 token / did 存 SQLite（DPAPI 加密），不落 app_config.json。

背景：库街区 token 曾存在 app_config.json 的 `wuwa_kujiequ.profile` 里（值本身
由 settings.manager 做 DPAPI 加密，但明文键仍散在配置文件、且会随凭据推送回灌到
QML 输入框）。本模块把"密钥"类字段统一挪进 `workflow_db` 的 `credentials` 表：

- 写入：`set_secret()` → WorkflowDB.set_credential() → DPAPI 密文入库存 `dpapi:` 前缀
- 读取：`get_secret()` → WorkflowDB.get_credential() → 内部解密后返回明文
- 迁移：`migrate_wuwa_secrets_from_config()` 首次读取时把 config 里的旧值搬进库并把
  配置中该字段删掉（不覆盖库中已有新值），保证老用户的 token 不丢

对外只暴露"合成后的 profile"，调用方（bridge / startup_sign_runner / daily_checker）
无需关心凭据到底存在哪。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

LOGGER = logging.getLogger("GameAutomation.CredentialStore")

WUWA_CATEGORY = "wuwa_kujiequ"
# wuwa 分类下属于密钥的字段：不再写 config，改存 DB 加密
WUWA_SECRET_KEYS = ("token", "did")

_db = None


def _get_db():
    """懒加载 WorkflowDB（全进程复用同一连接配置）。"""
    global _db
    if _db is None:
        from ..storage.workflow_db import WorkflowDB
        from ..settings.paths import WORKFLOW_DB_PATH
        _db = WorkflowDB(str(WORKFLOW_DB_PATH))
    return _db


# ── 通用读写 ────────────────────────────────────────────────────────


def set_secret(category: str, key: str, value: str) -> None:
    """写入密钥（加密落库）。空值会被清成空串存储，不会误留旧值。"""
    _get_db().set_credential(category, key, value)


def get_secret(category: str, key: str) -> str:
    """读取密钥明文；不存在返回空串。"""
    return _get_db().get_credential(category, key)


def has_secret(category: str, key: str) -> bool:
    return _get_db().has_credential(category, key)


def get_category(category: str) -> Dict[str, str]:
    """读取某分类全部密钥（key → 明文）。"""
    return _get_db().get_credential_section(category)


# ── 库街区（wuwa_kujiequ）──────────────────────────────────────────


def migrate_wuwa_secrets_from_config(
    cfg: Optional[Dict[str, Any]] = None,
    persist: bool = True,
) -> bool:
    """把 config 里 `wuwa_kujiequ.profile.token/did` 迁移进 DB 并从配置删除。

    - 库中已有值时不覆盖（以库为准，避免旧配置回滚掉新抓的 token）
    - 迁移后配置里只保留 role_id / user_id / role_name 等非密钥字段
    - 全程不打印 token 明文，只记迁移了哪些键

    Returns:
        是否发生了迁移（有键被搬走）。
    """
    from ..settings import manager as cfg_manager

    if cfg is None:
        cfg = cfg_manager.load(force=True)

    section = cfg.get(WUWA_CATEGORY)
    if not isinstance(section, dict):
        return False
    profile = section.get("profile")
    if not isinstance(profile, dict):
        return False

    moved = []
    for key in WUWA_SECRET_KEYS:
        value = str(profile.get(key) or "").strip()
        if not value:
            continue
        if not has_secret(WUWA_CATEGORY, key):
            set_secret(WUWA_CATEGORY, key, value)
        profile.pop(key, None)
        moved.append(key)

    if moved:
        section["profile"] = profile
        cfg[WUWA_CATEGORY] = section
        if persist:
            cfg_manager.save(cfg)
        LOGGER.info("[CredentialStore] 库街区密钥已迁移入库: %s", ", ".join(moved))
    return bool(moved)


def load_wuwa_kujiequ_profile(cfg: Optional[Dict[str, Any]] = None):
    """合成库街区 profile：DB 里的 token/did + config 的角色信息。

    首次调用会自动迁移 config 里的旧 token。任一必需字段缺失返回 None，
    与 `clients.wuwa_kujiequ.profile_from_dict` 行为一致。

    注意：返回值含明文 token，只应在调用方内存中使用，不要写日志。
    """
    from ..settings import manager as cfg_manager
    from ..clients.wuwa_kujiequ import profile_from_dict

    if cfg is None:
        cfg = cfg_manager.load(force=True)

    # 首次读取时把旧配置里的 token/did 搬进库（幂等）
    migrate_wuwa_secrets_from_config(cfg)

    section = cfg.get(WUWA_CATEGORY) or {}
    if not isinstance(section, dict):
        section = {}

    profile_data: Dict[str, Any] = dict(section.get("profile") or {})
    # 密钥优先用 DB（加密存储），config 里若还残留旧值也不采用
    for key in WUWA_SECRET_KEYS:
        profile_data[key] = get_secret(WUWA_CATEGORY, key)

    profile = profile_from_dict(profile_data)
    if profile is None:
        return None

    # 缺 role_id 时用 roles[0] 兜底（与原 _run_wuwa_kujiequ_sign_backend 行为一致）
    roles = section.get("roles") or []
    if not profile.role_id and roles:
        profile = profile.with_role(roles[0])
    return profile


def has_wuwa_credentials() -> bool:
    """库街区签到所需的 token + did 是否都已配置（不解密）。"""
    return has_secret(WUWA_CATEGORY, "token") and has_secret(WUWA_CATEGORY, "did")
