"""Decide which bots the board may start, from each provider's plan usage."""
from __future__ import annotations

import re
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
    "resume_interrupted_chats": True,
    "resume_message": "The plan limit has reset; continue where you stopped.",
    "resume_within_hours": 12.0,
    "providers": {},
    "openrouter": {"enabled": True, "key_env": "OPENROUTER_MANAGEMENT_KEY", "workspace_id": "", "label": "OpenRouter",
                   "balance_warn_usd": 2.0, "balance_hold_usd": 2.0, "key_warn_percent": 80.0},
}

RESUME = "resume"
WAIT = "wait"
EXPIRED = "expired"
SKIPPED_USER = "skipped-user"
SKIPPED_RECOVERED = "skipped-recovered"
GAVE_UP = "gave-up"
MAX_RESUMES = 3


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
    per_provider: dict[str, dict] = field(default_factory=dict)
    comment_held_cards: bool = True
    labels: dict[str, str] = field(default_factory=dict)
    resume_interrupted_chats: bool = True
    resume_message: str = DEFAULTS["resume_message"]
    resume_within_hours: float = DEFAULTS["resume_within_hours"]
    openrouter: dict = field(default_factory=lambda: dict(DEFAULTS["openrouter"]))

    def thresholds(self, provider: str) -> Thresholds:
        """The provider's own values where set, the defaults for the rest."""
        override = self.per_provider.get(provider, {})
        return Thresholds(override.get("session_percent", self.default.session_percent),
                          override.get("week_percent", self.default.week_percent))

    def label(self, provider: str, detected: Optional[str] = None) -> str:
        """The plan the provider reports, else its configured label, else its id."""
        return detected or self.labels.get(provider, provider)


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
        if not isinstance(values, dict):
            continue
        override = {key: _number(values[key], getattr(default, key))
                    for key in ("session_percent", "week_percent") if values.get(key) is not None}
        if override:
            per_provider[str(name)] = override
        if values.get("label"):
            labels[str(name)] = str(values["label"])
    return Settings(enabled=raw.get("enabled", DEFAULTS["enabled"]) is not False,
                    refresh_minutes=max(1.0, _number(raw.get("refresh_minutes"), DEFAULTS["refresh_minutes"])),
                    hold_minutes=max(0.0, _number(raw.get("hold_minutes"), DEFAULTS["hold_minutes"])),
                    default=default, per_provider=per_provider,
                    comment_held_cards=raw.get("comment_held_cards", DEFAULTS["comment_held_cards"]) is not False,
                    labels=labels,
                    resume_interrupted_chats=raw.get("resume_interrupted_chats",
                                                     DEFAULTS["resume_interrupted_chats"]) is not False,
                    resume_message=str(raw.get("resume_message") or DEFAULTS["resume_message"]).strip(),
                    resume_within_hours=max(0.5, _number(raw.get("resume_within_hours"),
                                                         DEFAULTS["resume_within_hours"])),
                    openrouter=_openrouter_settings(raw.get("openrouter")))


def _openrouter_settings(raw: Any) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    base = DEFAULTS["openrouter"]
    return {"enabled": raw.get("enabled", base["enabled"]) is not False,
            "key_env": str(raw.get("key_env") or base["key_env"]),
            "workspace_id": str(raw.get("workspace_id") or ""),
            "label": str(raw.get("label") or base["label"]),
            "balance_warn_usd": max(0.0, _number(raw.get("balance_warn_usd"), base["balance_warn_usd"])),
            "balance_hold_usd": max(0.0, _number(raw.get("balance_hold_usd"), base["balance_hold_usd"])),
            "key_warn_percent": max(1.0, _number(raw.get("key_warn_percent"), base["key_warn_percent"]))}


MODEL_FAMILIES = ("fable", "mythos", "opus", "sonnet", "haiku", "astra", "sol", "luna", "terra")
_FAMILY = re.compile(r"\b(" + "|".join(MODEL_FAMILIES) + r")\b", re.IGNORECASE)


def model_family(text: Any) -> Optional[str]:
    """The model family a model id or a usage window's label names (``claude-fable-5-1`` and "Fable week" are
    both ``fable``), or None."""
    match = _FAMILY.search(str(text or ""))
    return match.group(1).lower() if match else None


PLAN_PRODUCTS = {"openai-codex": "ChatGPT"}


def product_plan(provider: str, plan: Any) -> Optional[str]:
    """The plan a usage reply names, prefixed with its product (``Prolite`` is ``ChatGPT Prolite``)."""
    plan = str(plan or "").strip()
    if not plan:
        return None
    product = PLAN_PRODUCTS.get(provider, provider)
    return plan if plan.lower().startswith(product.lower()) else f"{product} {plan}"


def claude_plan(profile: Any) -> Optional[str]:
    """A Claude account profile's plan and its usage tier: ``Claude Max 5x``, ``Claude Pro``."""
    if not isinstance(profile, dict):
        return None
    organization, account = profile.get("organization") or {}, profile.get("account") or {}
    kind = str(organization.get("organization_type") or "").lower().removeprefix("claude_")
    if not kind:
        kind = "max" if account.get("has_claude_max") else "pro" if account.get("has_claude_pro") else ""
    if not kind:
        return None
    tier = re.search(r"_(\d+x)$", str(organization.get("rate_limit_tier") or ""))
    return " ".join(["Claude", kind.replace("_", " ").title()] + ([tier.group(1)] if tier else []))


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
    plan: Optional[str] = None


def reading_from_snapshot(provider: str, snapshot: Any, now: datetime, models: Iterable[str] = ()) -> Reading:
    """A Hermes ``AccountUsageSnapshot`` as a Reading. A window that limits one model family (a weekly cap on
    Fable alone, say) carries that family in ``model`` and only counts when one of these bots runs it; whether the
    snapshot calls it account- or model-scoped, its label naming a family is what decides."""
    if snapshot is None:
        return Reading(provider, now, error="no usage returned")
    plan = product_plan(provider, getattr(snapshot, "plan", None))
    if getattr(snapshot, "unavailable_reason", None):
        return Reading(provider, now, error=str(snapshot.unavailable_reason), plan=plan)
    in_use = {model_family(m) for m in models if m} - {None}
    names = [m.lower() for m in models if m]
    windows = []
    for window in getattr(snapshot, "windows", ()) or ():
        used = getattr(window, "used_percent", None)
        label = getattr(window, "label", "")
        kind = window_kind(label)
        if used is None or kind is None:
            continue
        family = model_family(label)
        if family and family not in in_use:
            continue
        if not family and getattr(window, "scope", "account") == "model" and not any(n in label.lower() for n in names):
            continue
        reset = getattr(window, "reset_at", None)
        windows.append({"label": label, "kind": kind, "used_percent": float(used), "model": family,
                        "resets_at": reset.isoformat() if reset else None})
    if not windows:
        return Reading(provider, now, error="no session or weekly window in the usage reply", plan=plan)
    return Reading(provider, now, windows=tuple(windows), plan=plan)


def over_threshold(reading: Reading, thresholds: Thresholds) -> list[dict]:
    """The reading's windows at or above their threshold, each with the threshold it crossed."""
    over = []
    for window in reading.windows:
        limit = thresholds.session_percent if window["kind"] == "session" else thresholds.week_percent
        if window["used_percent"] >= limit:
            over.append({**window, "threshold": limit})
    return over


def _describe(over: list[dict]) -> str:
    return "; ".join(f"{w['label']} {w['used_percent']:.0f}% ≥ {w['threshold']:.0f}%" for w in over)


def decide(reading: Reading, thresholds: Thresholds, previous: Optional[dict], hold: timedelta) -> dict:
    """The provider's new state. A window over its threshold that limits the whole account pauses every bot on the
    plan (``decision`` PAUSED); one that limits a single model family only pauses that family (``paused_models``),
    and the plan stays open for the rest. Without a usable reading the last state holds for ``hold`` after the last
    good reading, then everything opens, so a broken usage API never stops board work for good. ``paused_since``
    and ``model_paused_since`` mark pause episodes: they stay the same for as long as the pause lasts."""
    now = reading.at.isoformat()
    previous = previous or {}
    was_paused = previous.get("decision") == PAUSED
    if reading.windows:
        over = over_threshold(reading, thresholds)
        account = [w for w in over if not w.get("model")]
        families = sorted({w["model"] for w in over if w.get("model")})
        state = {"decision": PAUSED if account else OPEN, "last_reading_at": now, "checked_at": now,
                 "windows": list(reading.windows), "over": over, "paused_models": families}
        plan = reading.plan or previous.get("plan")
        if plan:
            state["plan"] = plan
        if account:
            state["paused_since"] = previous.get("paused_since") if was_paused else now
            state["reason"] = _describe(account)
        elif families:
            state["reason"] = f"{_describe(over)} (only {', '.join(families)} paused)"
        else:
            state["reason"] = "below thresholds (" + ", ".join(
                f"{w['label']} {w['used_percent']:.0f}%" for w in reading.windows) + ")"
        before = previous.get("model_paused_since") or {}
        state["model_paused_since"] = {f: before.get(f, now) for f in families}
        return state
    reason = reading.error or "usage unavailable"
    last = previous.get("last_reading_at")
    if last and (was_paused or previous.get("paused_models")) and reading.at - datetime.fromisoformat(last) < hold:
        return {**previous, "checked_at": now, "reason": f"{previous.get('reason', '')} (kept: {reason})"}
    opened = {k: v for k, v in previous.items() if k not in ("over", "paused_since", "paused_models", "model_paused_since")}
    return {**opened, "decision": OPEN, "checked_at": now, "reason": f"open: {reason}"}


def holds(provider_state: Optional[dict], model: Any) -> bool:
    """Whether a bot on ``model`` is held back by its provider's state."""
    state = provider_state or {}
    return state.get("decision") == PAUSED or (model_family(model) or "") in (state.get("paused_models") or [])


def held_windows(provider_state: dict, model: Any) -> list[dict]:
    """The over-threshold windows that hold a bot on ``model``: the account's, plus its own family's."""
    family = model_family(model)
    return [w for w in provider_state.get("over") or [] if not w.get("model") or w.get("model") == family]


def held_episode(provider_state: dict, model: Any) -> str:
    """One id per pause episode as a bot on ``model`` lives it, so it is told once per episode."""
    if provider_state.get("decision") == PAUSED:
        return provider_state.get("paused_since") or ""
    family = model_family(model) or ""
    return f"{family}:{(provider_state.get('model_paused_since') or {}).get(family, '')}"


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


def allowlist(profiles: dict[str, tuple[str, str]], states: dict[str, dict], hold_all: bool = False) -> Optional[list[str]]:
    """Profiles the board may start, from ``{profile: (provider, model)}``, or None when nobody is held.
    ``hold_all`` holds every profile: an empty allowlist, which the board reads as nobody."""
    if hold_all:
        return []
    held = {name for name, (provider, model) in profiles.items() if holds(states.get(provider), model)}
    if not held:
        return None
    return sorted(name for name in profiles if name not in held)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def is_limit_error(status_code: Any, reason: Any, error: Any) -> bool:
    """Whether a failed model call hit the plan's usage limit (as opposed to any other failure)."""
    if status_code == 429:
        return True
    text = " ".join(str(part) for part in (reason or "", (error or {}).get("type", "") if isinstance(error, dict) else error,
                                           (error or {}).get("message", "") if isinstance(error, dict) else ""))
    return bool(re.search(r"\b429\b|rate[\s_-]?limit|usage[\s_-]?limit|quota", text, re.IGNORECASE))


def resume_action(entry: dict, provider_state: Optional[dict], now: datetime, within: timedelta,
                  user_after: bool, answered_after: bool) -> str:
    """What to do with one interrupted chat: resume it, keep waiting, or drop it (expired, given up after
    ``MAX_RESUMES`` resumes that hit the limit again, or you or the bot already moved on). Only resumed when its
    provider's latest decision is open."""
    first = datetime.fromisoformat(entry.get("first_interrupted_at") or entry["interrupted_at"])
    if now - first > within:
        return EXPIRED
    if user_after:
        return SKIPPED_USER
    if answered_after:
        return SKIPPED_RECOVERED
    if entry.get("resumes", 0) >= MAX_RESUMES:
        return GAVE_UP
    if (provider_state or {}).get("decision") != OPEN or holds(provider_state, entry.get("model")):
        return WAIT
    return RESUME


def openrouter_state(credits: Optional[dict], keys: Optional[list], settings: dict, now: datetime,
                     error: Optional[str] = None, previous: Optional[dict] = None,
                     hold: timedelta = timedelta(0)) -> dict:
    """OpenRouter's balance and each key's spend, with the warnings to show. No bot runs on OpenRouter, but the
    company's memory service does, so a balance under ``balance_hold_usd`` holds every bot's cards (``holding``;
    ``held_since`` marks the episode). When it can't be read, the last numbers stay with the error next to them,
    and a hold lasts ``hold`` after the last good reading, like a plan's pause."""
    at = now.isoformat()
    previous = previous or {}
    if error:
        state = {**previous, "checked_at": at, "error": error}
        last = previous.get("last_reading_at")
        if previous.get("holding") and not (last and now - datetime.fromisoformat(last) < hold):
            state = {k: v for k, v in state.items() if k != "held_since"} | {"holding": False}
        return state
    total, used = _number((credits or {}).get("total_credits"), 0.0), _number((credits or {}).get("total_usage"), 0.0)
    balance = total - used
    holding = settings["balance_hold_usd"] > 0 and balance < settings["balance_hold_usd"]
    warnings = []
    if balance < settings["balance_warn_usd"]:
        warnings.append(f"Balance ${balance:.2f} is under ${settings['balance_warn_usd']:.2f}")
    rows = []
    for key in keys or []:
        if not isinstance(key, dict) or key.get("disabled"):
            continue
        limit, remaining = key.get("limit"), key.get("limit_remaining")
        spent = _number(limit, 0.0) - _number(remaining, 0.0) if limit is not None and remaining is not None else None
        percent = 100.0 * spent / _number(limit, 0.0) if spent is not None and _number(limit, 0.0) > 0 else None
        name = str(key.get("name") or key.get("label") or "key")
        rows.append({"name": name, "limit": limit, "limit_reset": key.get("limit_reset"), "spent": spent,
                     "percent": percent, **{k: _number(key.get(k), 0.0)
                                            for k in ("usage_daily", "usage_weekly", "usage_monthly", "usage")}})
        if percent is not None and percent >= settings["key_warn_percent"]:
            period = f" {key['limit_reset']}" if key.get("limit_reset") else ""
            warnings.append(f"Key {name} at {percent:.0f}% of its ${_number(limit, 0.0):.2f}{period} limit")
    state = {"checked_at": at, "last_reading_at": at, "balance": balance, "total_credits": total, "total_usage": used,
             "balance_warn_usd": settings["balance_warn_usd"], "balance_hold_usd": settings["balance_hold_usd"],
             "holding": holding, "keys": rows, "warnings": warnings, "error": None}
    if holding:
        state["held_since"] = previous.get("held_since") if previous.get("holding") else at
    return state


def openrouter_held_comment(label: str, balance: float, hold_usd: float) -> str:
    """The comment left on a card held because the OpenRouter balance is low."""
    return (f"Held by the usage gate: the {label} balance is ${balance:.2f}, under ${hold_usd:.2f}, and the company's "
            "memory service runs on it, so every bot's cards wait. Adding credit is a founder decision. This card "
            "starts on its own once the balance is back over the limit.")
