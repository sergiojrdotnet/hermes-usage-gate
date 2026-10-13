"""Hermes side of the usage gate: read profiles, fetch usage, apply ``kanban.dispatch_profiles``."""
from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import subprocess
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from usage_gate.gate import PAUSED, Reading, allowlist, decide, held_comment, reading_from_snapshot, settings_from, utcnow

log = logging.getLogger("hermes_usage_gate")
_lock = threading.Lock()
_running = threading.Event()


def hermes_root() -> Path:
    return Path(os.environ.get("USAGE_GATE_HERMES_ROOT") or Path.home() / ".hermes")


def state_path() -> Path:
    return hermes_root() / "usage-gate" / "state.json"


def _load_yaml(path: Path) -> dict:
    try:
        import hermes_yaml as yaml
    except ImportError:
        import yaml
    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def profile_models(root: Optional[Path] = None) -> dict[str, tuple[str, str]]:
    """``{profile: (provider, model)}`` for the default profile and every named profile."""
    root = root or hermes_root()
    homes = {"default": root}
    profiles = root / "profiles"
    if profiles.is_dir():
        homes.update({p.name: p for p in sorted(profiles.iterdir())
                      if p.is_dir() and not p.name.startswith(".") and (p / "config.yaml").is_file()})
    result = {}
    for name, home in homes.items():
        model = _load_yaml(home / "config.yaml").get("model") or {}
        provider = str(model.get("provider") or "").strip()
        if provider:
            result[name] = (provider, str(model.get("default") or ""))
    return result


def read_settings() -> Any:
    return settings_from(_load_yaml(hermes_root() / "config.yaml").get("usage_gate"))


def load_state() -> dict:
    try:
        return json.loads(state_path().read_text())
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    path = state_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n")
    os.replace(tmp, path)


def fetch(provider: str, models: list[str]) -> Reading:
    now = utcnow()
    try:
        from agent.account_usage import fetch_account_usage
        snapshot = fetch_account_usage(provider, read_only=True)
    except Exception as exc:
        return Reading(provider, now, error=f"fetch failed: {exc}")
    return reading_from_snapshot(provider, snapshot, now, models)


def _run_hermes(*args: str) -> subprocess.CompletedProcess:
    binary = os.environ.get("USAGE_GATE_HERMES_BIN") or str(Path.home() / ".local" / "bin" / "hermes")
    return subprocess.run([binary, "-p", "default", *args], capture_output=True, text=True, timeout=60)


def _hermes(*args: str) -> subprocess.CompletedProcess:
    return _run_hermes("config", *args)


def _kanban(*args: str) -> subprocess.CompletedProcess:
    return _run_hermes("kanban", *args)


def held_cards(paused_profiles: set[str]) -> list[dict]:
    """Ready or review cards assigned to a paused bot: the cards the gate is holding back."""
    cards = []
    for status in ("ready", "review"):
        result = _kanban("list", "--status", status, "--json")
        if result.returncode != 0:
            log.warning("usage gate: could not list %s cards: %s", status, result.stderr.strip()[-200:])
            continue
        try:
            listing = json.loads(result.stdout or "[]")
        except ValueError:
            continue
        listing = listing.get("tasks", []) if isinstance(listing, dict) else listing
        cards.extend(c for c in listing if isinstance(c, dict) and c.get("assignee") in paused_profiles)
    return cards


def _timezone() -> Any:
    name = _load_yaml(hermes_root() / "config.yaml").get("timezone")
    if not name:
        return None
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(str(name))
    except Exception:
        return None


def comment_held_cards(settings: Any, profiles: dict[str, tuple[str, str]], providers: dict[str, dict],
                       notes: dict[str, str]) -> dict[str, str]:
    """Leave one comment per held card per pause, so the board shows why it isn't starting.
    Returns ``{card id: pause it was told about}`` for the cards still held."""
    paused = {p: s for p, s in providers.items() if s.get("decision") == PAUSED}
    if not paused:
        return {}
    by_profile = {name: provider for name, (provider, _) in profiles.items() if provider in paused}
    tz = _timezone()
    kept = {}
    for card in held_cards(set(by_profile)):
        card_id = str(card.get("id"))
        provider = by_profile[card["assignee"]]
        episode = paused[provider].get("paused_since") or ""
        if notes.get(card_id) != episode:
            text = held_comment(settings.label(provider), paused[provider].get("over") or [], tz)
            result = _kanban("comment", card_id, "--author", "usage-gate", text)
            if result.returncode != 0:
                log.warning("usage gate: could not comment on %s: %s", card_id, result.stderr.strip()[-200:])
                continue
        kept[card_id] = episode
    return kept


def apply_allowlist(allowed: Optional[list[str]], applied: Optional[list[str]]) -> Optional[list[str]]:
    """Write the board's claim allowlist when it changed; returns what is in force afterwards."""
    if allowed == applied:
        return applied
    if allowed is None:
        result = _hermes("unset", "kanban.dispatch_profiles")
    else:
        result = _hermes("set", "kanban.dispatch_profiles", json.dumps(allowed))
    if result.returncode != 0:
        log.warning("usage gate: could not update kanban.dispatch_profiles: %s", result.stderr.strip()[-300:])
        return applied
    log.warning("usage gate: board may now start %s", "every bot" if allowed is None else ", ".join(allowed) or "nobody")
    return allowed


@contextlib.contextmanager
def _exclusive():
    """One refresh at a time across threads, processes and every profile's copy of this plugin."""
    path = hermes_root() / "usage-gate" / "refresh.lock"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with _lock, open(path, "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def refresh(force: bool = False) -> dict:
    """Read every provider in use, decide, and apply the allowlist. Safe to call from any thread."""
    with _exclusive():
        settings = read_settings()
        state = load_state()
        now = utcnow()
        last = state.get("checked_at")
        if not force and last and now - datetime.fromisoformat(last) < timedelta(minutes=settings.refresh_minutes):
            return state
        profiles = profile_models()
        applied = state.get("applied")
        providers = dict(state.get("providers") or {})
        if settings.enabled:
            for provider in sorted({p for p, _ in profiles.values()}):
                models = sorted({m for p, m in profiles.values() if p == provider})
                previous = providers.get(provider)
                providers[provider] = decide(fetch(provider, models), settings.thresholds(provider), previous,
                                             timedelta(minutes=settings.hold_minutes))
                if (previous or {}).get("decision") != providers[provider]["decision"]:
                    log.warning("usage gate: %s is now %s (%s)", provider, providers[provider]["decision"],
                                providers[provider]["reason"])
            wanted = allowlist({name: p for name, (p, _) in profiles.items()}, providers)
        else:
            wanted = None
        applied = apply_allowlist(wanted, applied)
        notes = state.get("held_notes") or {}
        if settings.enabled and settings.comment_held_cards:
            notes = comment_held_cards(settings, profiles, providers, notes)
        state = {"checked_at": now.isoformat(), "enabled": settings.enabled, "settings": settings_view(settings),
                 "providers": providers, "applied": applied, "held_notes": notes}
        save_state(state)
        return state


def settings_view(settings: Any) -> dict:
    return {"enabled": settings.enabled, "session_percent": settings.default.session_percent,
            "week_percent": settings.default.week_percent, "refresh_minutes": settings.refresh_minutes,
            "hold_minutes": settings.hold_minutes, "comment_held_cards": settings.comment_held_cards,
            "providers": {p: {**settings.per_provider.get(p, {}),
                              **({"label": settings.labels[p]} if p in settings.labels else {})}
                          for p in sorted(set(settings.per_provider) | set(settings.labels))}}


def status_view() -> dict:
    """Everything the Usage Gate page shows: settings, each plan's last reading and decision, the bots on each
    plan, the allowlist in force and the cards being held."""
    settings = read_settings()
    state = load_state()
    profiles = profile_models()
    providers = state.get("providers") or {}
    plans = []
    for provider in sorted({p for p, _ in profiles.values()} | set(providers)):
        current = providers.get(provider) or {}
        plans.append({"provider": provider, "label": settings.label(provider),
                      "thresholds": vars(settings.thresholds(provider)),
                      "bots": sorted(n for n, (p, _) in profiles.items() if p == provider and n != "default"),
                      **current})
    paused = {n for n, (p, _) in profiles.items() if (providers.get(p) or {}).get("decision") == PAUSED}
    held = [{k: c.get(k) for k in ("id", "title", "assignee", "status")} for c in held_cards(paused)] if paused else []
    tz = _timezone()
    return {"settings": settings_view(settings), "checked_at": state.get("checked_at"), "plans": plans,
            "allowlist": state.get("applied"), "held": held, "timezone": getattr(tz, "key", None)}


def on_dispatch_tick(profile_name: Optional[str] = None, **_: Any) -> None:
    """Dispatcher hook: refresh in the background, so a slow usage API never delays a tick. Only the default
    profile's dispatcher drives the gate; the plugin is installed in other profiles for its page and API."""
    if profile_name not in (None, "", "default") or _running.is_set():
        return
    _running.set()

    def _work() -> None:
        try:
            refresh()
        except Exception:
            log.exception("usage gate refresh failed")
        finally:
            _running.clear()

    threading.Thread(target=_work, name="usage-gate", daemon=True).start()
