"""无限暖暖 whimbox 一条龙体力/幻境配置（JSON）。

配置文件：<whimbox_app>/configs/config.json
OneDragon 段的值都是 {"value": ..., "description": ...} 包装，只改 value，
description 原样保留。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .base import ConfigError, FieldSpec, JsonConfigAdapter, script_root


class WhimboxConfigAdapter(JsonConfigAdapter):
    game_key = "nikki"
    display_name = "无限暖暖 / whimbox"
    app_key = "InfinityNikki"
    primary_file = "config"
    extra_process_names = (
        "whimbox.exe",
        "whimbox_launcher.exe",
        "X6Game-Win64-Shipping.exe",
    )

    def _resolve_files(self) -> Dict[str, Path]:
        roots = self._candidate_roots()
        active = self._running_root(roots) or roots[0]
        ordered = [active] + [r for r in roots if r != active]
        for root in ordered:
            cfg = root / "configs" / "config.json"
            if cfg.is_file():
                return {"config": cfg}
        return {"config": active / "configs" / "config.json"}

    def _candidate_roots(self) -> List[Path]:
        try:
            app_root = script_root(self.app_config, self.app_key)
        except Exception:
            app_root = None
        roots: List[Path] = []
        if app_root:
            roots.append(app_root)
        if app_root:
            parent = app_root.parent
            for name in ("whimbox_app", "whimbox_launcher"):
                path = parent / name
                if path.is_dir() and path not in roots:
                    roots.append(path)
        if not roots:
            raise ConfigError("app_config.json 中找不到 whibox 安装目录")
        return roots

    def _running_root(self, roots: List[Path]) -> Optional[Path]:
        """优先找正在运行的一条龙/UI 所在副本，做到“跑哪份改哪份”。"""
        try:
            import psutil
        except Exception:
            return None
        found = []
        try:
            for proc in psutil.process_iter(["pid", "name", "exe", "cmdline", "cwd"]):
                try:
                    info = proc.info or {}
                except Exception:
                    continue
                name = str(info.get("name") or "").lower()
                exe = str(info.get("exe") or "")
                cwd = str(info.get("cwd") or "")
                cmd = " ".join(info.get("cmdline") or [])
                combined = (name + " " + exe + " " + cwd + " " + cmd).lower()
                if "whimbox" not in combined:
                    continue
                for idx, root in enumerate(roots):
                    needle = str(root).lower()
                    if needle in exe.lower() or needle in cwd.lower() or needle in cmd.lower():
                        # 真正的 startOneDragon 进程优先于只开着的 UI
                        priority = 0 if "startonedragon" in cmd.lower() else 1
                        found.append((priority, idx, root))
                        break
        except Exception:
            return None
        if not found:
            return None
        found.sort(key=lambda item: (item[0], item[1]))
        return found[0][2]

    def _setting_options(self) -> Dict[str, List[str]]:
        """从 whimbox 自带 setting_options.json 读取下拉选项；缺失时用当前已知值兜底。"""
        path = (
            self._files["config"].parent.parent
            / "python-embedded"
            / "Lib"
            / "site-packages"
            / "whimbox"
            / "assets"
            / "setting_options.json"
        )
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            return {k: [str(x) for x in v] for k, v in data.items() if isinstance(v, list)}
        except Exception:
            return {}

    def _fields(self, data: Dict[str, Any]) -> List[FieldSpec]:
        base = ("OneDragon",)
        opts = self._setting_options()

        def spec(
            key: str,
            label: str,
            kind: str = "str",
            help_text: str = "",
            option_key: str = "",
        ) -> FieldSpec:
            options: Tuple[str, ...] = ()
            if option_key:
                options = tuple(opts.get(option_key, ()))
            return FieldSpec(
                key, label, kind, "config", base + (key, "value"),
                help=help_text,
                allow_empty=False if kind == "str" else True,
                options=options,
            )

        return [
            spec("energy_cost", "消耗体力的幻境",
                 help_text="一条龙优先消耗体力的幻境",
                 option_key="energy_cost"),
            spec("jihua_target", "激化幻境兑换目标",
                 help_text="素材激化幻境里换什么",
                 option_key="jihua_target"),
            spec("jihua_cost", "激化幻境消耗材料"),
            spec("jihua_cost_2", "备选材料 1"),
            spec("jihua_cost_3", "备选材料 2"),
            spec("monster_target", "魔物试炼目标",
                 option_key="monster_target"),
            spec("bless_target", "祝福闪光目标",
                 option_key="bless_target"),
            spec("realm_target", "周本目标", kind="list",
                 help_text="逗号分隔，可多选",
                 option_key="realm_target"),
        ]
