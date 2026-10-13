"""Decide which bots the board may start, from each provider's plan usage."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

OPEN = "open"
PAUSED = "paused"

DEFAULTS = {
    "enabled": True,
    "session_percent": 70.0,
    "week_percent": 80.0,
    "refresh_minutes": 5.0,
    "hold_minutes": 30.0,
    "comment_held_cards": True,
    "providers": {},
}


@dataclass(frozen=True)
class Thresholds:
    session_percent: float
    week_percent: float


@dataclass(frozen=True)
class Settings:
    enabled: bool
    refresh_minutes: float
    hold_minutes: float
    default: Thresholds
    per_provider: dict[str, Thresholds] = field(default_factory=dict)
    comment_held_cards: bool = True
    labels: dict[str, str] = field(default_factory=dict)

    def thresholds(self, provider: str) -> Thresholds:
        return self.per_provider.get(provider, self.default)

    def label(self, provider: str) -> str:
        return self.labels.get(provider, provider)


def _number(value: Any, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def settings_from(raw: Optional[dict]) -> Settings:
    """``usage_gate`` from config.yaml, with every missing or invalid value at its default."""
    raw = raw if isinstance(raw, dict) else {}
    default = Thresholds(_number(raw.get("session_percent"), DEFAULTS["session_percent"]),
                         _number(raw.get("week_percent"), DEFAULTS["week_percent"]))
    per_provider, labels = {}, {}
    for name, values in (raw.get("providers") or {}).items():
        if isinstance(values, dict):
            per_provider[str(name)] = Thresholds(_number(values.get("session_percent"), default.session_percent),
                                                 _number(values.get("week_percent"), default.week_percent))
            if values.get("label"):
                labels[str(name)] = str(values["label"])
    return Settings(enabled=raw.get("enabled", DEFAULTS["enabled"]) is not False,
                    refresh_minutes=max(1.0, _number(raw.get("refresh_minutes"), DEFAULTS["refresh_minutes"])),
                    hold_minutes=max(0.0, _number(raw.get("hold_minutes"), DEFAULTS["hold_minutes"])),
                    default=default, per_provider=per_provider,
                    comment_held_cards=raw.get("comment_held_cards", DEFAULTS["comment_held_cards"]) is not False,
                    labels=labels)


def window_kind(label: str) -> Optional[str]:
    text = (label or "").lower()
    if "week" in text:
        return "week"
    if "session" in text or "5h" in text or "hour" in text:
        return "session"
    return None


@dataclass(frozen=True)
class Reading:
    """One provider's usage at one moment; ``windows`` is empty when it could not be read."""
    provider: str
    at: datetime
    windows: tuple[dict, ...] = ()
    error: Optional[str] = None


def reading_from_snapshot(provider: str, snapshot: Any, now: datetime, models: Iterable[str] = ()) -> Reading:
    """A Hermes ``AccountUsageSnapshot`` as a Reading. A model-scoped window (e.g. a weekly cap on one model
    family) only counts when its label names a model these bots run."""
    if snapshot is None:
        return Reading(provider, now, error="no usage returned")
    if getattr(snapshot, "unavailable_reason", None):
        return Reading(provider, now, error=str(snapshot.unavailable_reason))
    families = [m.lower() for m in models if m]
    windows = []
    for window in getattr(snapshot, "windows", ()) or ():
        used = getattr(window, "used_percent", None)
        kind = window_kind(getattr(window, "label", ""))
        if used is None or kind is None:
            continue
        label = getattr(window, "label", "")
        if getattr(window, "scope", "account") == "model" and not any(f in label.lower() for f in families):
            continue
        reset = getattr(window, "reset_at", None)
        windows.append({"label": label, "kind": kind, "used_percent": float(used),
                        "resets_at": reset.isoformat() if reset else None})
    if not windows:
        return Reading(provider, now, error="no session or weekly window in the usage reply")
    return Reading(provider, now, windows=tuple(windows))


def over_threshold(reading: Reading, thresholds: Thresholds) -> list[dict]:
    """The reading's windows at or above their threshold, each with the threshold it crossed."""
    over = []
    for window in reading.windows:
        limit = thresholds.session_percent if window["kind"] == "session" else thresholds.week_percent
        if window["used_percent"] >= limit:
            over.append({**window, "threshold": limit})
    return over


def judge(reading: Reading, thresholds: Thresholds) -> tuple[Optional[str], str]:
    """(OPEN or PAUSED, reason) from a reading; (None, reason) when the reading carries no usage."""
    if not reading.windows:
        return None, reading.error or "usage unavailable"
    over = over_threshold(reading, thresholds)
    if over:
        return PAUSED, "; ".join(f"{w['label']} {w['used_percent']:.0f}% ≥ {w['threshold']:.0f}%" for w in over)
    summary = ", ".join(f"{w['label']} {w['used_percent']:.0f}%" for w in reading.windows)
    return OPEN, f"below thresholds ({summary})"


def decide(reading: Reading, thresholds: Thresholds, previous: Optional[dict], hold: timedelta) -> dict:
    """The provider's new state. Without a usable reading the last decision holds for ``hold`` after the
    last good reading, then the provider opens, so a broken usage API never stops board work for good.
    ``paused_since`` marks one pause episode: it stays the same for as long as the provider remains paused."""
    verdict, reason = judge(reading, thresholds)
    now = reading.at.isoformat()
    previous = previous or {}
    was_paused = previous.get("decision") == PAUSED
    if verdict is not None:
        state = {"decision": verdict, "reason": reason, "last_reading_at": now, "checked_at": now,
                 "windows": list(reading.windows)}
        if verdict == PAUSED:
            state["over"] = over_threshold(reading, thresholds)
            state["paused_since"] = previous.get("paused_since") if was_paused else now
        return state
    last = previous.get("last_reading_at")
    if last and was_paused and reading.at - datetime.fromisoformat(last) < hold:
        return {**previous, "checked_at": now, "reason": f"{previous.get('reason', '')} (kept: {reason})"}
    opened = {k: v for k, v in previous.items() if k not in ("over", "paused_since")}
    return {**opened, "decision": OPEN, "checked_at": now, "reason": f"open: {reason}"}


def held_comment(label: str, over: list[dict], tz: Optional[Any] = None) -> str:
    """The comment left on a card the gate is holding back."""
    parts, resets = [], []
    for window in over:
        parts.append(f"{label} {window['label'].lower()} at {window['used_percent']:.0f}% "
                     f"(threshold {window['threshold']:.0f}%)")
        if window.get("resets_at"):
            when = datetime.fromisoformat(window["resets_at"])
            if tz is not None:
                when = when.astimezone(tz)
            resets.append(when.strftime("%Y-%m-%d %H:%M") + (f" {tz.key}" if getattr(tz, "key", None) else " UTC"))
    text = "Held by the usage gate: " + "; ".join(parts or [f"{label} usage is above its threshold"]) + "."
    if resets:
        text += f" The window resets at {max(resets)}."
    return text + " This card starts on its own once usage is back under the threshold; nothing to do."


def allowlist(profile_providers: dict[str, str], states: dict[str, dict]) -> Optional[list[str]]:
    """Profiles the board may start, or None when no provider is paused (no allowlist at all)."""
    paused = {provider for provider, state in states.items() if state.get("decision") == PAUSED}
    if not paused:
        return None
    return sorted(name for name, provider in profile_providers.items() if provider not in paused)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
