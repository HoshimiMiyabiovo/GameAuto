"""本地敏感配置的 DPAPI 加密存储（仅 Windows）。

email.password 已在 email_reporter.py 中实现 DPAPI，本模块抽取为通用工具，
供所有 token / api_key / device_id 等敏感字段复用。

约定：
- 密文以 "dpapi:<base64>" 前缀存储到 app_config.json 的对应字段，避免与旧明文歧义
- 同时兼容旧明文（无前缀）与 email 旧式 "base64 无前缀 + password_protect=dpapi" 的回读
- 非 Windows / 无 pywin32 时，is_available()=False，调用方应回退明文并 toast 提示
"""

from __future__ import annotations

import base64
import logging
from typing import Optional

try:
    import win32crypt  # type: ignore
    _HAS_DPAPI = True
except ImportError:
    _HAS_DPAPI = False

LOGGER = logging.getLogger("GameAutomation.SecureStore")

# 统一前缀，任何受保护字段加密后均以此开头
_DPAPI_PREFIX = "dpapi:"
# 兼容 email 旧式标记（无前缀但靠 password_protect 字段识别）
_LEGACY_PROTECT_VALUE = "dpapi"


def is_available() -> bool:
    return _HAS_DPAPI


def is_encrypted(value: str) -> bool:
    return isinstance(value, str) and value.startswith(_DPAPI_PREFIX)


def protect(plain: str) -> str:
    """明文 -> 密文（带前缀）。空串原样返回。"""
    if not plain:
        return ""
    if not _HAS_DPAPI:
        raise RuntimeError("当前环境无 win32crypt，无法使用 DPAPI 加密")
    blob = win32crypt.CryptProtectData(plain.encode("utf-8"), _LEGACY_PROTECT_VALUE, None, None, None, 0)
    return _DPAPI_PREFIX + base64.b64encode(blob).decode("ascii")


def unprotect(cipher: str) -> str:
    """密文（带前缀）-> 明文。非加密值会抛异常，由 resolve 处理。"""
    if not cipher:
        return ""
    if not cipher.startswith(_DPAPI_PREFIX):
        raise ValueError("不是 DPAPI 密文")
    if not _HAS_DPAPI:
        raise RuntimeError("当前环境无 win32crypt，无法解密 DPAPI 密文")
    b64 = cipher[len(_DPAPI_PREFIX):]
    blob = base64.b64decode(b64.encode("ascii"))
    return win32crypt.CryptUnprotectData(blob)[1].decode("utf-8")


def resolve(value: str, legacy_protect_flag: str = "") -> str:
    """统一解密入口（供读取侧使用）：

    1) 带 dpapi: 前缀 -> DPAPI 解密，失败则回退原值并打 warning
    2) 不带前缀但 legacy_protect_flag == "dpapi"（兼容 email 旧式）-> 按旧 email 方式解密
    3) 其他 -> 视为明文直接返回（向后兼容）
    """
    if not value:
        return ""
    # 新式：前缀
    if is_encrypted(value):
        try:
            return unprotect(value)
        except Exception as exc:
            LOGGER.warning("[SecureStore] DPAPI 解密失败，回退明文: %s", exc)
            return value
    # 兼容 email 旧式：base64 无前缀 + password_protect 标记
    if legacy_protect_flag == _LEGACY_PROTECT_VALUE:
        # 尝试按 email 旧式解密（纯 base64）
        if _HAS_DPAPI:
            try:
                blob = base64.b64decode(value.encode("ascii"))
                return win32crypt.CryptUnprotectData(blob)[1].decode("utf-8")
            except Exception:
                # 不是合法 base64/解密失败，视为明文
                return value
        return value
    # 明文
    return value


def try_protect(plain: str) -> tuple[str, bool]:
    """尝试加密，返回 (密文或原值, 是否已加密)。无 DPAPI 时返回 (原值, False)。"""
    if not plain:
        return "", False
    if is_encrypted(plain):
        return plain, True
    if not _HAS_DPAPI:
        return plain, False
    try:
        return protect(plain), True
    except Exception as exc:
        LOGGER.warning("[SecureStore] 加密失败，回退明文: %s", exc)
        return plain, False


# 便捷别名，供旧 email 代码复用
def protect_password(plain: str) -> str:
    return protect(plain)

def unprotect_password(cipher: str) -> str:
    return unprotect(cipher)
