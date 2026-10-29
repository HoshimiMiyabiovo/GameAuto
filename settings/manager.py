"""JSON 配置读写与默认值合并。"""

import copy
import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

LOGGER = logging.getLogger("GameAutomation")

from .defaults import (
    DEFAULT_APPS,
    DEFAULT_APP_ORDER,
    DEFAULT_BAIJING_CAT_EXCHANGE,
    DEFAULT_QBITTORRENT,
)
from .paths import APP_CONFIG_PATH

try:
    from ..services.secure_store import is_encrypted, resolve as _sec_resolve, try_protect
    _HAS_SECURE = True
except Exception:
    _HAS_SECURE = False
    is_encrypted = lambda v: False  # type: ignore
    def _sec_resolve(v, flag=""):  # type: ignore
        return v
    def try_protect(v):  # type: ignore
        return v, False


_CONFIG_FILE = APP_CONFIG_PATH
_BACKUP_SUFFIX = ".bak"
_MISSING = object()

_cache: Optional[Dict] = None

# ── 敏感字段加解密（DPAPI） ──────────────────────────────────────────

# 任何键名含以下子串即视为敏感（大小写不敏感），值将被 DPAPI 加密。
# phone 也算敏感：登录手机号是 PII（塔吉多/库街区都用它自动重登），
# 一旦加入本清单，load() 的明文检测会自动把盘上旧明文重写为密文。
_SENSITIVE_SUBSTRINGS = ("token", "cookie", "password", "secret", "api_key", "apikey", "phone")
# 精确排除：这些键即使含敏感子串也不加密（标记位/非密文）
_SENSITIVE_EXCLUDE = {"password_protect", "subject_prefix"}

def _is_sensitive_key(key: str) -> bool:
    lk = str(key).lower()
    if lk in _SENSITIVE_EXCLUDE:
        return False
    return any(sub in lk for sub in _SENSITIVE_SUBSTRINGS)

def _walk_decrypt(obj: Any, parent: Optional[Dict] = None):
    """递归解密敏感字段（原地修改）。parent 用于取 password_protect 兼容旧 email。"""
    if isinstance(obj, dict):
        for k, v in list(obj.items()):
            if isinstance(v, dict):
                _walk_decrypt(v, v)
            elif isinstance(v, list):
                for item in v:
                    if isinstance(item, dict):
                        _walk_decrypt(item, item)
                    elif isinstance(item, list):
                        _walk_decrypt(item, None)
            elif isinstance(v, str) and v and _is_sensitive_key(k):
                # email 旧式：靠同级 password_protect 标记
                legacy_flag = ""
                if k == "password" and isinstance(obj, dict):
                    legacy_flag = str(obj.get("password_protect") or "")
                # 已是新式前缀则走 _sec_resolve，否则也经它兼容旧式
                try:
                    dec = _sec_resolve(v, legacy_flag)
                    # 仅当解密结果与原值不同或已确认是密文时才覆盖，避免误改
                    if dec != v or is_encrypted(v) or legacy_flag == "dpapi":
                        obj[k] = dec
                except Exception as exc:
                    LOGGER.warning("[Config] 敏感字段解密失败，保留原值 key=%s: %s", k, exc)
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, dict):
                _walk_decrypt(item, item)
            elif isinstance(item, list):
                _walk_decrypt(item, None)

def _walk_encrypt(obj: Any):
    """递归加密敏感字段（原地修改，供落盘前调用）。已加密值跳过。"""
    if isinstance(obj, dict):
        for k, v in list(obj.items()):
            if isinstance(v, dict):
                _walk_encrypt(v)
            elif isinstance(v, list):
                for item in v:
                    if isinstance(item, (dict, list)):
                        _walk_encrypt(item)
            elif isinstance(v, str) and v and _is_sensitive_key(k):
                if is_encrypted(v):
                    continue
                # 空/已加密跳过
                try:
                    enc, ok = try_protect(v)
                    if ok:
                        obj[k] = enc
                except Exception as exc:
                    LOGGER.warning("[Config] 敏感字段加密失败，将按明文保留 key=%s: %s", k, exc)
    elif isinstance(obj, list):
        for item in obj:
            _walk_encrypt(item)

def _decrypt_config(cfg: Dict) -> Dict:
    """对整份合并后配置做原地解密，返回同一对象。"""
    if not _HAS_SECURE:
        return cfg
    try:
        _walk_decrypt(cfg, None)
    except Exception as exc:
        LOGGER.warning("[Config] 配置解密失败，返回原配置: %s", exc)
    return cfg

def _encrypt_for_persist(cfg: Dict) -> Dict:
    """为落盘准备的深拷贝加密副本（不改内存缓存）。"""
    if not _HAS_SECURE:
        return cfg
    clone = copy.deepcopy(cfg)
    try:
        _walk_encrypt(clone)
        # email 段额外打标记，保持与旧式读取兼容（新式前缀已足够，但保留标记便于识别）
        if isinstance(clone.get("email"), dict) and clone["email"].get("password"):
            # 若 password 已加密，确保 password_protect 标记为 dpapi
            pwd = str(clone["email"].get("password") or "")
            if is_encrypted(pwd):
                clone["email"]["password_protect"] = "dpapi"
    except Exception as exc:
        LOGGER.warning("[Config] 配置加密失败，准备按明文落盘: %s", exc)
    return clone


def decrypt_config(raw: Dict) -> Dict:
    """对外暴露：对任意已加载的原始 config（含密文）做解密，深拷贝不改原对象。"""
    if not isinstance(raw, dict):
        return raw
    clone = copy.deepcopy(raw)
    return _decrypt_config(clone)


def encrypt_config_for_persist(raw: Dict) -> Dict:
    """对外暴露：为直接写盘的 raw config 准备加密副本。"""
    if not isinstance(raw, dict):
        return raw
    return _encrypt_for_persist(raw)


def _default_config() -> Dict:
    return {
        "_version": "1.0",
        "_comment": "用户配置覆盖文件；默认 apps 定义已内置在代码中，这里只保存差异项。",
        "setup_completed": False,
        "app_order": list(DEFAULT_APP_ORDER),
        "qbittorrent": copy.deepcopy(DEFAULT_QBITTORRENT),
        "log": {"dir": "data/logs", "file": "auto_runner.log", "backup_count": 0},
        "global": {"min_run_time": 300, "check_interval": 10, "debug_mode": False},
        "baijing": {"cat_exchange": copy.deepcopy(DEFAULT_BAIJING_CAT_EXCHANGE)},
        "apps": copy.deepcopy(DEFAULT_APPS),
    }


def _merge_defaults(defaults: Any, overrides: Any) -> Any:
    if isinstance(defaults, dict) and isinstance(overrides, dict):
        merged = copy.deepcopy(defaults)
        for key, value in overrides.items():
            if key in merged:
                merged[key] = _merge_defaults(merged[key], value)
            else:
                merged[key] = copy.deepcopy(value)
        return merged
    return copy.deepcopy(overrides)


def _extract_overrides(current: Any, defaults: Any) -> Any:
    if isinstance(current, dict) and isinstance(defaults, dict):
        overrides: Dict[str, Any] = {}
        for key, value in current.items():
            if key in defaults:
                child = _extract_overrides(value, defaults[key])
                if child is not _MISSING:
                    overrides[key] = child
            else:
                overrides[key] = copy.deepcopy(value)
        return overrides if overrides else _MISSING

    if current == defaults:
        return _MISSING
    return copy.deepcopy(current)


def _normalize_for_save(config: Dict) -> Dict:
    defaults = _default_config()
    overrides = _extract_overrides(config, defaults)
    persisted = {
        "_version": str(config.get("_version", defaults["_version"])),
        "_comment": defaults["_comment"],
    }
    if isinstance(overrides, dict):
        for key, value in overrides.items():
            if key in {"_version", "_comment"}:
                continue
            persisted[key] = value
    return persisted


def _raw_has_plaintext_sensitive(raw: Any) -> bool:
    """检测原始落盘配置中是否存在未加密的敏感明文（用于触发迁移）。"""
    if not _HAS_SECURE:
        return False
    try:
        from ..services.secure_store import is_available as _avail
        if not _avail():
            return False
    except Exception:
        return False
    found = False

    def _walk(o: Any, parent: Any = None):
        nonlocal found
        if found:
            return
        if isinstance(o, dict):
            for k, v in o.items():
                if isinstance(v, dict):
                    _walk(v, v)
                elif isinstance(v, list):
                    for it in v:
                        if isinstance(it, (dict, list)):
                            _walk(it, it if isinstance(it, dict) else None)
                elif isinstance(v, str) and v and _is_sensitive_key(k):
                    # 已是新式前缀则视为已加密
                    if is_encrypted(v):
                        continue
                    # 兼容 email 旧式：password + password_protect=dpapi 视为已加密
                    if k == "password" and isinstance(o, dict) and str(o.get("password_protect") or "") == "dpapi":
                        # 尝试 base64 解码校验，若能解则视为已加密，失败则算明文
                        try:
                            import base64
                            base64.b64decode(v.encode("ascii"))
                            continue
                        except Exception as exc:
                            LOGGER.debug("[Config] password 旧式 base64 校验失败，按明文处理: %s", exc)
                    found = True
                    return
        elif isinstance(o, list):
            for it in o:
                _walk(it, None)

    _walk(raw, None)
    return found


def load(force: bool = False) -> Dict:
    global _cache
    if _cache is None or force:
        defaults = _default_config()
        if _CONFIG_FILE.exists():
            try:
                with open(_CONFIG_FILE, encoding="utf-8") as f:
                    raw = json.load(f)
                merged = _merge_defaults(defaults, raw)
                _cache = _decrypt_config(merged)
                # 若原始文件含明文敏感字段且 DPAPI 可用，自动迁移为密文（后台重写一次）
                if _raw_has_plaintext_sensitive(raw):
                    try:
                        # 用解密后的缓存重写一次，落盘时会自动加密
                        save(_cache)
                    except Exception as exc:
                        LOGGER.warning("[Config] 明文敏感字段自动迁移失败: %s", exc)
            except (json.JSONDecodeError, OSError):
                _cache = defaults
        else:
            _cache = defaults
    return _cache


def atomic_write_json(path: Path, data: Dict) -> None:
    """原子写入 JSON 文件：写临时文件 → os.replace → 短重试。

    供所有需要直接写 app_config.json 的代码点统一调用，
    避免 `open(path, 'w')` 被另一个进程瞬时锁住抛 PermissionError。

    若写入的是 app_config.json，会在落盘前自动对敏感字段做 DPAPI 加密
    （内存中的传入 dict 不会被修改）。
    """
    path = Path(path)
    # 落盘前自动加密 app_config.json 的敏感字段
    write_data = data
    try:
        if path.resolve() == _CONFIG_FILE.resolve():
            write_data = _encrypt_for_persist(data)
    except Exception:
        write_data = data
    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=".tmp_",
        suffix=path.suffix or ".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(write_data, f, ensure_ascii=False, indent=2)
        last_exc: Optional[Exception] = None
        for attempt in range(3):
            try:
                os.replace(tmp_path, str(path))
                last_exc = None
                break
            except PermissionError as exc:
                last_exc = exc
                time.sleep(0.1 * (attempt + 1))
        if last_exc is not None:
            raise last_exc
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError as exc:
            LOGGER.debug("[Config] 清理临时文件失败: %s", exc)
        raise


def save(config: Dict) -> None:
    global _cache
    persisted = _normalize_for_save(config)

    # 备份“当前将要落盘”的加密副本。不能直接复制旧文件：新增敏感字段后的
    # 首次迁移会把迁移前的明文原样留在 .bak 里。
    if _CONFIG_FILE.exists():
        try:
            atomic_write_json(
                Path(str(_CONFIG_FILE) + _BACKUP_SUFFIX),
                _encrypt_for_persist(persisted),
            )
        except OSError as exc:
            LOGGER.warning("[Config] 配置备份写入失败: %s", exc)

    atomic_write_json(_CONFIG_FILE, persisted)
    _cache = _merge_defaults(_default_config(), persisted)


def get(key: str, default: Any = None) -> Any:
    return load().get(key, default)


def get_app_order() -> List[str]:
    return load().get("app_order", [])


def get_apps() -> Dict:
    return load().get("apps", {})


def get_app(app_key: str) -> Optional[Dict]:
    return get_apps().get(app_key)


def get_qbittorrent() -> Dict:
    return load().get("qbittorrent", copy.deepcopy(DEFAULT_QBITTORRENT))


def get_log_config() -> Dict:
    cfg = load().get("log", {})
    return {
        "dir": cfg.get("dir", "data/logs"),
        "file": cfg.get("file", "auto_runner.log"),
        "backup_count": cfg.get("backup_count", 0),
    }


def get_global() -> Dict:
    return load().get("global", {"min_run_time": 300, "check_interval": 10, "debug_mode": False})

def update_app_order(order: List[str]) -> None:
    cfg = load()
    cfg["app_order"] = order
    save(cfg)


def upsert_app(app_key: str, app_data: Dict) -> None:
    cfg = load()
    cfg.setdefault("apps", {})[app_key] = app_data
    save(cfg)


def remove_app(app_key: str) -> None:
    cfg = load()
    cfg.get("apps", {}).pop(app_key, None)
    if app_key in cfg.get("app_order", []):
        cfg["app_order"].remove(app_key)
    save(cfg)


def update_qbittorrent(data: Dict) -> None:
    cfg = load()
    cfg["qbittorrent"] = data
    save(cfg)


def update_log_config(data: Dict) -> None:
    cfg = load()
    cfg["log"] = data
    save(cfg)


def update_global(data: Dict) -> None:
    cfg = load()
    cfg["global"] = data
    save(cfg)


def is_setup_completed() -> bool:
    return bool(load().get("setup_completed", False))


def mark_setup_completed() -> None:
    cfg = load()
    cfg["setup_completed"] = True
    save(cfg)
