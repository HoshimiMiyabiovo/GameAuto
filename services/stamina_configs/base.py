"""第三方脚本「体力刷本」配置适配层。

为什么单独一层：
- 各脚本的配置格式/路径都不同，但 GameAuto 只想暴露统一的字段（刷什么、次数、
  队伍、树脂策略），QML 不应该知道某个脚本的 JSON 路径；
- 第三方配置里有大量 GameAuto 不关心的键、注释和顺序，必须只改白名单字段，
  写入前备份、原子替换，脚本/游戏运行中拒绝写入，避免写坏配置。

数据流：
    app_config.json（脚本安装路径） → adapter._resolve_files()
    → read() 返回 {game_key, fields, values, files, running}
    → QML 编辑 → write(values) → 备份 → 临时文件 → os.replace

格式：第一期只支持 JSON；YAML（March7th/ZZZ）需要 ruamel.yaml 保留注释，放到第二期。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ...settings.paths import CONFIG_BACKUP_DIR

LOGGER = logging.getLogger("GameAutomation.StaminaConfigs")


class ConfigError(Exception):
    """配置不存在、格式错误、校验失败或运行中拒绝写入。"""


def app_section(app_config: Dict[str, Any], app_key: str) -> Dict[str, Any]:
    apps = app_config.get("apps") if isinstance(app_config, dict) else {}
    app = (apps or {}).get(app_key)
    return app if isinstance(app, dict) else {}


def script_root(app_config: Dict[str, Any], app_key: str) -> Path:
    """从 app_config 的脚本/启动器路径推导脚本根目录，不写死盘符。"""
    app = app_section(app_config, app_key)
    disc = app.get("_discovery") if isinstance(app.get("_discovery"), dict) else {}
    for value in (
        disc.get("script_exe_path"),
        app.get("script_exe_path"),
        disc.get("launcher"),
        app.get("launcher"),
    ):
        if value:
            path = Path(str(value))
            return path.parent if path.suffix else path
    raise ConfigError(f"app_config.json 中 apps.{app_key} 没有可用的脚本路径")


@dataclass(frozen=True)
class FieldSpec:
    """一个可编辑字段。path 是相对某个 JSON 文件的嵌套键路径。"""

    key: str
    label: str
    kind: str  # bool / int / str / list
    file: str
    path: Tuple[str, ...]
    help: str = ""
    min_value: Optional[int] = None
    max_value: Optional[int] = None
    allow_empty: bool = True
    options: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "kind": self.kind,
            "file": self.file,
            "help": self.help,
            "min": self.min_value,
            "max": self.max_value,
            "allow_empty": self.allow_empty,
            "options": list(self.options),
        }


def _get_path(data: Any, path: Tuple[str, ...]) -> Any:
    cur = data
    for key in path:
        if isinstance(cur, dict):
            cur = cur[key]
        elif isinstance(cur, list):
            cur = cur[int(key)]
        else:
            raise KeyError(key)
    return cur


def _set_path(data: Any, path: Tuple[str, ...], value: Any) -> None:
    cur = data
    for key in path[:-1]:
        nxt = cur[int(key)] if isinstance(cur, list) else cur[key]
        cur = nxt
    last = path[-1]
    if isinstance(cur, list):
        cur[int(last)] = value
    elif isinstance(cur, dict):
        cur[last] = value
    else:
        raise ConfigError("配置路径不是对象/数组，无法写入")


def _coerce(spec: FieldSpec, raw: Any) -> Any:
    if spec.kind == "bool":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, float)) and raw in (0, 1):
            return bool(raw)
        text = str(raw).strip().lower()
        if text in ("true", "1", "yes", "on", "是"):
            return True
        if text in ("false", "0", "no", "off", "否"):
            return False
        raise ConfigError(f"{spec.label} 需要是/否")
    if spec.kind == "int":
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise ConfigError(f"{spec.label} 需要整数")
        if spec.min_value is not None and value < spec.min_value:
            raise ConfigError(f"{spec.label} 不能小于 {spec.min_value}")
        if spec.max_value is not None and value > spec.max_value:
            raise ConfigError(f"{spec.label} 不能大于 {spec.max_value}")
        return value
    if spec.kind == "list":
        if isinstance(raw, (list, tuple)):
            items = [str(x).strip() for x in raw]
        else:
            text = str(raw or "")
            for sep in ("，", ";", "；", "\n", "|"):
                text = text.replace(sep, ",")
            items = [x.strip() for x in text.split(",")]
        items = [x for x in items if x]
        if not items and not spec.allow_empty:
            raise ConfigError(f"{spec.label} 不能为空")
        return items
    value = str(raw or "").strip()
    if not value and not spec.allow_empty:
        raise ConfigError(f"{spec.label} 不能为空")
    return value


class JsonConfigAdapter:
    """JSON 配置适配器基类。子类负责路径、字段和特殊写入。"""

    game_key = ""
    display_name = ""
    app_key = ""
    primary_file = ""
    extra_process_names: Tuple[str, ...] = ()

    def __init__(
        self,
        app_config: Optional[Dict[str, Any]] = None,
        files: Optional[Dict[str, Path]] = None,
    ):
        self.app_config = app_config or {}
        self._files = {k: Path(v) for k, v in (files or self._resolve_files()).items()}

    # ── 子类实现 ────────────────────────────────────────────────

    def _resolve_files(self) -> Dict[str, Path]:
        raise NotImplementedError

    def _fields(self, data: Dict[str, Any]) -> List[FieldSpec]:
        raise NotImplementedError

    # ── 公共能力 ────────────────────────────────────────────────

    def _app(self) -> Dict[str, Any]:
        apps = self.app_config.get("apps") if isinstance(self.app_config, dict) else {}
        app = (apps or {}).get(self.app_key)
        return app if isinstance(app, dict) else {}

    def _process_names(self) -> Tuple[str, ...]:
        app = self._app()
        disc = app.get("_discovery") if isinstance(app.get("_discovery"), dict) else {}
        names: set[str] = set(self.extra_process_names)
        for container in (app, disc):
            for key in ("script_process_name", "game_process_name"):
                value = container.get(key)
                if value:
                    names.add(Path(str(value)).name)
            for key in ("script_exe_path", "game_exe_path", "launcher"):
                value = container.get(key)
                if value and str(value).lower().endswith(".exe"):
                    names.add(Path(str(value)).name)
        for opt in app.get("launcher_options") or []:
            if not isinstance(opt, dict):
                continue
            for key in ("script_process_name", "launcher"):
                value = opt.get(key)
                if value and str(value).lower().endswith(".exe"):
                    names.add(Path(str(value)).name)
        return tuple(sorted({n.lower() for n in names if n}))

    def running_reason(self) -> str:
        if not self._process_names():
            return ""
        try:
            import psutil

            wanted = set(self._process_names())
            for proc in psutil.process_iter(["name"]):
                name = str((proc.info or {}).get("name") or "").lower()
                if name and name in wanted:
                    return f"{name} 正在运行，请先关闭脚本/游戏再改配置"
        except Exception as exc:
            LOGGER.debug("[刷本配置] 检查进程失败（按未运行处理）: %s", exc)
        return ""

    def _read_files(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for key, path in self._files.items():
            if not path.is_file():
                raise ConfigError(f"配置文件不存在: {path}")
            try:
                out[key] = json.loads(path.read_text(encoding="utf-8-sig"))
            except Exception as exc:
                raise ConfigError(f"读取 {path.name} 失败: {exc}")
        return out

    def _backup(self, path: Path) -> str:
        try:
            dest_dir = CONFIG_BACKUP_DIR / self.game_key
            dest_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            dest = dest_dir / f"{path.name}.{stamp}.bak"
            shutil.copy2(path, dest)
            LOGGER.info("[刷本配置] 已备份 %s → %s", path, dest)
            return str(dest)
        except Exception as exc:
            raise ConfigError(f"备份 {path.name} 失败，已中止写入: {exc}")

    def _write_json(self, path: Path, data: Any) -> None:
        try:
            text = json.dumps(data, ensure_ascii=False, indent=2)
            if path.read_text(encoding="utf-8-sig").endswith("\n"):
                text += "\n"
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, path)
        except Exception as exc:
            raise ConfigError(f"写入 {path.name} 失败: {exc}")

    def _apply(self, data: Dict[str, Any], spec: FieldSpec, value: Any) -> None:
        """默认按 path 写入；特殊字段（如列表成员开关）由子类覆盖。"""
        _set_path(data[spec.file], spec.path, value)

    def _read_value(self, data: Dict[str, Any], spec: FieldSpec) -> Any:
        """默认按 path 读取；特殊字段由子类覆盖。"""
        return _get_path(data[spec.file], spec.path)

    def _notes(self, data: Dict[str, Any]) -> List[str]:
        """给 UI 的提示（例如检测到另一份脚本配置副本）。"""
        return []

    def read(self) -> Dict[str, Any]:
        data = self._read_files()
        specs = self._fields(data)
        values: Dict[str, Any] = {}
        for spec in specs:
            try:
                values[spec.key] = self._read_value(data, spec)
            except Exception:
                values[spec.key] = None
        return {
            "game_key": self.game_key,
            "display_name": self.display_name,
            "app_key": self.app_key,
            "files": {k: str(v) for k, v in self._files.items()},
            "primary_file": str(self._files.get(self.primary_file, "")),
            "fields": [spec.to_dict() for spec in specs],
            "values": values,
            "notes": self._notes(data),
            "running": self.running_reason(),
        }

    def write(self, values: Dict[str, Any]) -> Dict[str, Any]:
        reason = self.running_reason()
        if reason:
            raise ConfigError(reason)
        data = self._read_files()
        specs = {spec.key: spec for spec in self._fields(data)}
        unknown = [key for key in values if key not in specs]
        if unknown:
            raise ConfigError("不支持的字段: " + ", ".join(unknown))
        changed: Dict[str, int] = {}
        for key, raw in values.items():
            spec = specs[key]
            value = _coerce(spec, raw)
            try:
                self._apply(data, spec, value)
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                raise ConfigError(f"{spec.label} 写入失败: {exc}")
            changed[spec.file] = changed.get(spec.file, 0) + 1
        backups = []
        for file_key in changed:
            path = self._files[file_key]
            backups.append(self._backup(path))
            self._write_json(path, data[file_key])
        message = f"已保存 {sum(changed.values())} 项"
        LOGGER.info("[刷本配置] %s %s，备份: %s", self.display_name, message, backups)
        return {"ok": True, "message": message, "backups": backups}

    def primary_path(self) -> Optional[Path]:
        path = self._files.get(self.primary_file)
        return path if path else next(iter(self._files.values()), None)
