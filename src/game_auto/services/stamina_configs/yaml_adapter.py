"""YAML 配置适配基类（ruamel.yaml round-trip，保留注释与键顺序）。

为什么必须用 ruamel：
- March7th 的 config.yaml 与 OneDragon 的 charge_plan.yml 里全是行内注释，
  PyYAML 的 dump 会丢掉注释并把整份文件重排（45KB 配置一次全变），
  出问题无法 diff、也无法回滚到"只改一行"的状态；
- ruamel 的 round-trip 模式保留注释、引号风格、键顺序，只改目标标量。

缩进：这两个脚本都用"顶层序列不缩进"（`plan_list:` 下的 `- ` 从第 0 列开始），
所以固定 mapping=2 / sequence=2 / offset=0；实测 charge_plan.yml 往返 0 行差异，
config.yaml 仅一条折叠 URL 被合并为单行（可接受）。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

from ruamel.yaml import YAML

from .base import ConfigError, JsonConfigAdapter


class YamlConfigAdapter(JsonConfigAdapter):
    """JSON 适配器的 YAML 版本：读取/写回都走 ruamel round-trip。"""

    def _yaml(self) -> YAML:
        yaml = YAML()
        yaml.preserve_quotes = True
        yaml.width = 4096  # 避免长行被折行，减少无关 diff
        yaml.indent(mapping=2, sequence=2, offset=0)
        return yaml

    def _read_files(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for key, path in self._files.items():
            if not path.is_file():
                raise ConfigError(f"配置文件不存在: {path}")
            try:
                data = self._yaml().load(path.read_text(encoding="utf-8"))
            except Exception as exc:
                raise ConfigError(f"读取 {path.name} 失败: {exc}")
            if data is None:
                raise ConfigError(f"{path.name} 是空文件")
            out[key] = data
        return out

    def _write_file(self, path: Path, data: Any) -> None:
        tmp = path.with_name(path.name + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
                self._yaml().dump(data, handle)
            os.replace(tmp, path)
        except Exception as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise ConfigError(f"写入 {path.name} 失败: {exc}")
