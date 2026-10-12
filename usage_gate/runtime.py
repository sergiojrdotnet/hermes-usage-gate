"""Hermes side of the usage gate: read profiles, fetch usage, apply ``kanban.dispatch_profiles``."""
from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from usage_gate.gate import Reading, allowlist, decide, reading_from_snapshot, settings_from, utcnow

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


def _hermes(*args: str) -> subprocess.CompletedProcess:
    binary = os.environ.get("USAGE_GATE_HERMES_BIN") or str(Path.home() / ".local" / "bin" / "hermes")
    return subprocess.run([binary, "-p", "default", "config", *args], capture_output=True, text=True, timeout=60)


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


def refresh(force: bool = False) -> dict:
    """Read every provider in use, decide, and apply the allowlist. Safe to call from any thread."""
    with _lock:
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
        state = {"checked_at": now.isoformat(), "enabled": settings.enabled,
                 "thresholds": {"session_percent": settings.default.session_percent,
                                "week_percent": settings.default.week_percent,
                                "per_provider": {k: vars(v) for k, v in settings.per_provider.items()}},
                 "providers": providers, "applied": apply_allowlist(wanted, applied)}
        save_state(state)
        return state


def on_dispatch_tick(**_: Any) -> None:
    """Dispatcher hook: refresh in the background, so a slow usage API never delays a tick."""
    if _running.is_set():
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
