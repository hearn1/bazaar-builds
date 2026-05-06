"""Curator state and freeze handling for automated build evaluation."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Optional

SCHEMA_VERSION = 1
VALID_PHASES = frozenset({"implementation", "local_dry_run", "shadow_cron", "live_cron"})


class StateError(ValueError):
    """Raised when curator state cannot be parsed safely."""


@dataclass
class FreezeWindow:
    until: str
    reason: Optional[str] = None

    def active_at(self, now: datetime) -> bool:
        if not self.until:
            return False
        return _parse_time(self.until) > now

    def to_dict(self) -> dict[str, Any]:
        data = {"freeze_removals_until": self.until}
        if self.reason is not None:
            data["notes"] = self.reason
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FreezeWindow":
        until = _required_optional_str(data, "freeze_removals_until")
        if until is not None:
            _parse_time(until)
        return cls(until=until or "", reason=_optional_str(data, "notes"))

    @classmethod
    def from_optional_date(cls, value: Optional[str]) -> Optional["FreezeWindow"]:
        if value is None:
            return None
        _parse_time(value)
        return cls(until=value)


@dataclass
class FreezeStatus:
    active: bool = False
    scope: Optional[str] = None
    until: Optional[str] = None
    reason: Optional[str] = None


@dataclass
class CuratorState:
    schema_version: int = SCHEMA_VERSION
    phase: str = "implementation"
    dry_run: bool = True
    patch_label: Optional[str] = None
    expected_bazaardb_patch_label: Optional[str] = None
    global_freeze: Optional[FreezeWindow] = None
    hero_freezes: dict[str, FreezeWindow] = field(default_factory=dict)
    notes: Optional[str] = None

    def freeze_status(self, hero: str, now: Optional[datetime] = None) -> FreezeStatus:
        check_time = now or datetime.now(timezone.utc)
        if check_time.tzinfo is None:
            check_time = check_time.replace(tzinfo=timezone.utc)

        hero_key = _hero_key(hero)
        hero_freeze = next(
            (freeze for candidate, freeze in self.hero_freezes.items() if _hero_key(candidate) == hero_key),
            None,
        )
        if hero_freeze and hero_freeze.active_at(check_time):
            return FreezeStatus(True, "hero", hero_freeze.until, hero_freeze.reason)
        if self.global_freeze and self.global_freeze.active_at(check_time):
            return FreezeStatus(True, "global", self.global_freeze.until, self.global_freeze.reason)
        return FreezeStatus()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "phase": self.phase,
            "dry_run": self.dry_run,
            "patch_label": self.patch_label,
            "expected_bazaardb_patch_label": self.expected_bazaardb_patch_label,
            "freeze_removals_until": self.global_freeze.until if self.global_freeze else None,
            "hero_freezes": {
                hero: freeze.to_dict()
                for hero, freeze in sorted(self.hero_freezes.items())
            },
            **({"notes": self.notes} if self.notes is not None else {}),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CuratorState":
        version = _int_field(data, "schema_version")
        if version > SCHEMA_VERSION:
            raise StateError(
                f"Curator state schema_version {version} is newer than supported {SCHEMA_VERSION}"
            )
        if version < SCHEMA_VERSION:
            raise StateError(
                f"Curator state schema_version {version} is older than supported {SCHEMA_VERSION}"
            )

        phase = _str_field(data, "phase")
        if phase not in VALID_PHASES:
            raise StateError(f"phase must be one of {sorted(VALID_PHASES)}")
        dry_run = _bool_field(data, "dry_run")
        patch_label = _required_optional_str(data, "patch_label")
        expected_bazaardb_patch_label = _required_optional_str(data, "expected_bazaardb_patch_label")
        freeze_removals_until = _required_optional_str(data, "freeze_removals_until")
        global_freeze = None
        if freeze_removals_until is not None:
            _parse_time(freeze_removals_until)
            global_freeze = FreezeWindow(freeze_removals_until)

        raw_hero_freezes = _dict_field(data, "hero_freezes")
        hero_freezes: dict[str, FreezeWindow] = {}
        for hero, freeze in raw_hero_freezes.items():
            if not isinstance(hero, str) or not hero:
                raise StateError("hero_freezes keys must be non-empty strings")
            if not isinstance(freeze, dict):
                raise StateError(f"hero_freezes.{hero} must be an object")
            hero_freezes[hero] = FreezeWindow.from_dict(freeze)

        return cls(
            schema_version=version,
            phase=phase,
            dry_run=dry_run,
            patch_label=patch_label,
            expected_bazaardb_patch_label=expected_bazaardb_patch_label,
            global_freeze=global_freeze,
            hero_freezes=hero_freezes,
            notes=_optional_str(data, "notes"),
        )


def load_state(path: Path) -> CuratorState:
    if not path.exists():
        return CuratorState()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise StateError(f"Could not parse curator state {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise StateError(f"Curator state {path} must contain a JSON object")
    return CuratorState.from_dict(data)


def save_state(state: CuratorState, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state.to_dict(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    path.write_text(payload, encoding="utf-8")


def _parse_time(value: str) -> datetime:
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        try:
            parsed_date = date.fromisoformat(value)
        except ValueError as exc:
            raise StateError(f"freeze date {value!r} must be YYYY-MM-DD") from exc
        return datetime.combine(parsed_date, time.max, tzinfo=timezone.utc)
    raw = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise StateError(f"freeze timestamp {value!r} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise StateError(f"freeze timestamp {value!r} must include a timezone")
    return parsed


def _hero_key(hero: str) -> str:
    key = re.sub(r"[^a-z0-9]+", "_", hero.casefold()).strip("_")
    if not key:
        raise StateError("hero must produce a non-empty key")
    return key


def _str_field(data: dict[str, Any], field_name: str) -> str:
    value = data.get(field_name)
    if not isinstance(value, str) or not value:
        raise StateError(f"{field_name} must be a non-empty string")
    return value


def _optional_str(data: dict[str, Any], field_name: str) -> Optional[str]:
    value = data.get(field_name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise StateError(f"{field_name} must be a string or null")
    return value


def _required_optional_str(data: dict[str, Any], field_name: str) -> Optional[str]:
    if field_name not in data:
        raise StateError(f"{field_name} is required")
    return _optional_str(data, field_name)


def _int_field(data: dict[str, Any], field_name: str) -> int:
    value = data.get(field_name)
    if not isinstance(value, int):
        raise StateError(f"{field_name} must be an integer")
    return value


def _dict_field(data: dict[str, Any], field_name: str) -> dict[str, Any]:
    value = data.get(field_name)
    if not isinstance(value, dict):
        raise StateError(f"{field_name} must be an object")
    return value


def _bool_field(data: dict[str, Any], field_name: str) -> bool:
    value = data.get(field_name)
    if not isinstance(value, bool):
        raise StateError(f"{field_name} must be a boolean")
    return value
