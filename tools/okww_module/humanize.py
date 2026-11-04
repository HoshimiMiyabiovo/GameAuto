"""Human-like combat input throttling for ok-ww.

This module is loaded *inside* the ok-ww runtime process by
``tools/okww_module/runner.py``.  It monkey-patches two ok-ww classes:

* ``BaseChar.perform`` marks the duration of a character action as "combat".
* ``BaseWWTask.click`` rate-limits centre-screen left clicks while that mark is
  active.

Only attack-fill clicks are throttled.  Skill keys, character switches and all
UI clicks (clicks with explicit coordinates or a feature name) keep their
original timing, so the decision points of a custom team axis stay unchanged.

The configuration is intentionally simple and can be overridden per team::

    {
      "enabled": true,
      "target_cps": 5.0,
      "jitter": 0.25,
      "micro_pause_chance": 0.03,
      "micro_pause_sec": [0.08, 0.18],
      "key_jitter_ms": 0,
      "teams": {
        "Aemeath__Chisa__Denia": {"target_cps": 5.5}
      }
    }

``key_jitter_ms`` is off by default.  A non-zero value adds a small random
delay before resonance/liberation/echo key sends; keep it at 0 unless the axis
has been re-validated.
"""

from __future__ import annotations

import functools
import json
import logging
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

logger = logging.getLogger("game_auto.okww.humanize")
_ACTIVE_SETTINGS: Optional["HumanizeSettings"] = None

DEFAULT_CONFIG: Dict[str, Any] = {
    "enabled": True,
    "target_cps": 5.0,
    "jitter": 0.25,
    "micro_pause_chance": 0.03,
    "micro_pause_sec": [0.08, 0.18],
    "key_jitter_ms": 0.0,
    "teams": {},
}

# A click gap below this is almost certainly a bug in the configuration; it
# also protects the game from accidental 20 CPS input if someone sets
# target_cps too high.
MIN_INTERVAL_SEC = 0.08
MAX_TARGET_CPS = 12.0


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


@dataclass(frozen=True)
class HumanizeSettings:
    enabled: bool = True
    target_cps: float = 5.0
    jitter: float = 0.25
    micro_pause_chance: float = 0.03
    micro_pause_min: float = 0.08
    micro_pause_max: float = 0.18
    key_jitter_ms: float = 0.0
    teams: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    @property
    def base_interval(self) -> float:
        cps = _clamp(float(self.target_cps), 0.5, MAX_TARGET_CPS)
        return max(MIN_INTERVAL_SEC, 1.0 / cps)

    def for_team(self, team_key: Optional[str]) -> "HumanizeSettings":
        if not team_key:
            return self
        override = self.teams.get(team_key)
        if override is None:
            # Windows paths are case-insensitive; team manifests are often
            # lower-cased on disk while class names are CamelCase.
            lower = team_key.lower()
            for key, value in self.teams.items():
                if key.lower() == lower:
                    override = value
                    break
        if not isinstance(override, dict) or not override:
            return self

        data: Dict[str, Any] = {
            "enabled": self.enabled,
            "target_cps": self.target_cps,
            "jitter": self.jitter,
            "micro_pause_chance": self.micro_pause_chance,
            "micro_pause_min": self.micro_pause_min,
            "micro_pause_max": self.micro_pause_max,
            "key_jitter_ms": self.key_jitter_ms,
            "teams": self.teams,
        }
        data.update(override)
        return settings_from_dict(data, fallback=self)


def _pair(value: Any, default: Tuple[float, float]) -> Tuple[float, float]:
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        try:
            low = float(value[0])
            high = float(value[1])
            if high < low:
                low, high = high, low
            return max(0.0, low), max(0.0, high)
        except (TypeError, ValueError):
            pass
    return default


def settings_from_dict(data: Optional[Dict[str, Any]], *, fallback: Optional[HumanizeSettings] = None) -> HumanizeSettings:
    base = fallback or HumanizeSettings()
    data = data if isinstance(data, dict) else {}
    micro = _pair(
        data.get("micro_pause_sec", data.get("micro_pause_range")),
        (base.micro_pause_min, base.micro_pause_max),
    )
    return HumanizeSettings(
        enabled=bool(data.get("enabled", base.enabled)),
        target_cps=_clamp(float(data.get("target_cps", base.target_cps)), 0.5, MAX_TARGET_CPS),
        jitter=_clamp(float(data.get("jitter", base.jitter)), 0.0, 0.9),
        micro_pause_chance=_clamp(
            float(data.get("micro_pause_chance", base.micro_pause_chance)), 0.0, 1.0
        ),
        micro_pause_min=micro[0],
        micro_pause_max=micro[1],
        key_jitter_ms=max(0.0, float(data.get("key_jitter_ms", base.key_jitter_ms))),
        teams=data.get("teams", base.teams) if isinstance(data.get("teams", base.teams), dict) else base.teams,
    )


def load_settings(path: Optional[str]) -> HumanizeSettings:
    if not path:
        return settings_from_dict(DEFAULT_CONFIG)
    config_path = Path(path)
    if not config_path.is_file():
        logger.info("humanize config not found, using defaults: %s", config_path)
        return settings_from_dict(DEFAULT_CONFIG)
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - a bad config must not break automation
        logger.warning("humanize config read failed (%s), using defaults: %s", config_path, exc)
        return settings_from_dict(DEFAULT_CONFIG)
    settings = settings_from_dict(raw)
    logger.info(
        "humanize config loaded: enabled=%s target_cps=%.2f jitter=%.2f teams=%d",
        settings.enabled,
        settings.target_cps,
        settings.jitter,
        len(settings.teams),
    )
    return settings


class CombatThrottle:
    """Per-task click budget with jitter and occasional micro-pauses."""

    def __init__(self, settings: HumanizeSettings, *, seed: Optional[int] = None):
        self.base_settings = settings
        self.team_settings = settings
        self._rng = random.Random(seed)
        self._next_allowed = 0.0
        self._team_key: Optional[Tuple[str, ...]] = None
        self.clicks = 0
        self.waited_sec = 0.0
        self.micro_pauses = 0

    def _resolve_team(self, task) -> HumanizeSettings:
        chars = getattr(task, "chars", None)
        if not chars:
            return self.team_settings
        names = tuple(sorted(char.__class__.__name__ for char in chars if char is not None))
        if not names:
            return self.team_settings
        if names != self._team_key:
            self._team_key = names
            self.team_settings = self.base_settings.for_team("__".join(names))
            logger.info(
                "humanize team=%s target_cps=%.2f",
                "__".join(names),
                self.team_settings.target_cps,
            )
        return self.team_settings

    def should_throttle(
        self,
        task,
        *,
        x: int,
        y: int,
        name: Optional[str],
        interval: float,
        move: bool,
        key: str,
    ) -> bool:
        settings = self._resolve_team(task)
        if not settings.enabled:
            return False
        if getattr(task, "_gameauto_combat_depth", 0) <= 0:
            return False
        if key != "left" or name or move:
            return False
        # Explicit coordinates are UI/navigation clicks, not attack fill.
        if x != -1 or y != -1:
            return False
        gap = self._next_gap(settings)
        # The caller already asked for a slower interval; leave the existing
        # ok-ww ``check_interval`` logic alone.
        if interval > 0 and interval >= gap:
            return False
        return True

    def wait_for_turn(self, task) -> None:
        settings = self._resolve_team(task)
        gap = self._next_gap(settings)
        now = time.monotonic()
        wait = self._next_allowed - now
        if wait > 0:
            time.sleep(wait)
            self.waited_sec += wait
            now = time.monotonic()

        if settings.micro_pause_chance > 0 and self._rng.random() < settings.micro_pause_chance:
            pause = self._rng.uniform(settings.micro_pause_min, settings.micro_pause_max)
            if pause > 0:
                time.sleep(pause)
                self.waited_sec += pause
                self.micro_pauses += 1
                now = time.monotonic()

        self._next_allowed = now + gap
        self.clicks += 1

    def _next_gap(self, settings: HumanizeSettings) -> float:
        gap = settings.base_interval * (1.0 + self._rng.uniform(-settings.jitter, settings.jitter))
        return max(MIN_INTERVAL_SEC, gap)

    def key_jitter(self, task) -> None:
        settings = self._resolve_team(task)
        if settings.key_jitter_ms <= 0:
            return
        delay = self._rng.uniform(0.0, settings.key_jitter_ms) / 1000.0
        if delay > 0:
            time.sleep(delay)

    def snapshot(self) -> Dict[str, Any]:
        return {
            "clicks": self.clicks,
            "waited_sec": round(self.waited_sec, 3),
            "micro_pauses": self.micro_pauses,
            "target_cps": round(self.team_settings.target_cps, 3),
            "team": "__".join(self._team_key) if self._team_key else "",
        }


def get_throttle(task) -> CombatThrottle:
    throttle = getattr(task, "_gameauto_humanize", None)
    if throttle is None:
        settings = getattr(task, "_gameauto_humanize_settings", None)
        if settings is None:
            settings = _ACTIVE_SETTINGS or settings_from_dict(DEFAULT_CONFIG)
        throttle = CombatThrottle(settings)
        task._gameauto_humanize = throttle
    return throttle


def install(config_path: Optional[str], *, logger_override=None) -> HumanizeSettings:
    """Patch ok-ww input paths.  Safe to call more than once."""
    global logger, _ACTIVE_SETTINGS
    if logger_override is not None:
        logger = logger_override

    settings = load_settings(config_path)
    _ACTIVE_SETTINGS = settings

    from src.char import BaseChar  # type: ignore[import-not-found]
    from src.task import BaseWWTask  # type: ignore[import-not-found]

    if getattr(BaseWWTask.BaseWWTask, "_gameauto_humanize_installed", False):
        return settings

    original_click = BaseWWTask.BaseWWTask.click
    original_perform = BaseChar.BaseChar.perform

    @functools.wraps(original_click)
    def humanized_click(
        self,
        x=-1,
        y=-1,
        move_back=False,
        name=None,
        interval=-1,
        move=False,
        down_time=0.01,
        after_sleep=0,
        key="left",
    ):
        throttle = get_throttle(self)
        if throttle.should_throttle(
            self, x=x, y=y, name=name, interval=interval, move=move, key=key
        ):
            throttle.wait_for_turn(self)
        return original_click(
            self,
            x,
            y,
            move_back,
            name,
            interval,
            move,
            down_time,
            after_sleep,
            key,
        )

    @functools.wraps(original_perform)
    def marked_perform(self):
        task = getattr(self, "task", None)
        if task is None:
            return original_perform(self)
        depth = getattr(task, "_gameauto_combat_depth", 0)
        task._gameauto_combat_depth = depth + 1
        try:
            return original_perform(self)
        finally:
            task._gameauto_combat_depth = max(0, getattr(task, "_gameauto_combat_depth", 1) - 1)

    def _jitter_key(original):
        @functools.wraps(original)
        def wrapper(self, *args, **kwargs):
            task = getattr(self, "task", None)
            if task is not None:
                get_throttle(task).key_jitter(task)
            return original(self, *args, **kwargs)

        return wrapper

    BaseWWTask.BaseWWTask.click = humanized_click
    BaseChar.BaseChar.perform = marked_perform
    BaseWWTask.BaseWWTask._gameauto_humanize_installed = True

    if settings.key_jitter_ms > 0:
        BaseChar.BaseChar.send_resonance_key = _jitter_key(BaseChar.BaseChar.send_resonance_key)
        BaseChar.BaseChar.send_liberation_key = _jitter_key(BaseChar.BaseChar.send_liberation_key)
        BaseChar.BaseChar.send_echo_key = _jitter_key(BaseChar.BaseChar.send_echo_key)

    logger.info(
        "humanize installed: enabled=%s target_cps=%.2f jitter=%.2f key_jitter_ms=%.1f",
        settings.enabled,
        settings.target_cps,
        settings.jitter,
        settings.key_jitter_ms,
    )
    return settings


def task_metrics(task) -> Dict[str, Any]:
    throttle = getattr(task, "_gameauto_humanize", None)
    return throttle.snapshot() if throttle is not None else {}
