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

from usage_gate.gate import (RESUME, WAIT, Reading, allowlist, decide, held_comment, held_episode, held_windows,
                             holds, is_limit_error, openrouter_held_comment, openrouter_state, reading_from_snapshot,
                             resume_action, settings_from, utcnow)

log = logging.getLogger("hermes_usage_gate")
CHAT_PLATFORMS = ("desktop", "tui", "cli")
RESUME_CHAIN = timedelta(hours=1)
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


def _openrouter_get(path: str, key: str) -> Any:
    import urllib.request
    request = urllib.request.Request("https://openrouter.ai/api/v1" + path, headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response).get("data")


def refresh_openrouter(settings: Any, previous: Optional[dict]) -> Optional[dict]:
    """OpenRouter's state, or None when it isn't configured (no management key in the environment)."""
    config = settings.openrouter
    key = os.environ.get(config["key_env"], "").strip()
    if not config["enabled"] or not key:
        return None
    workspace = config["workspace_id"]
    hold = timedelta(minutes=settings.hold_minutes)
    try:
        credits = _openrouter_get("/credits", key)
        keys = _openrouter_get("/keys" + (f"?workspace_id={workspace}" if workspace else ""), key)
    except Exception as exc:
        return openrouter_state(None, None, config, utcnow(), error=f"could not read OpenRouter: {exc}",
                                previous=previous, hold=hold)
    return openrouter_state(credits, keys, config, utcnow(), previous=previous, hold=hold)


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
                       notes: dict[str, str], openrouter: Optional[dict] = None) -> dict[str, str]:
    """Leave one comment per held card per pause, so the board shows why it isn't starting.
    Returns ``{card id: pause it was told about}`` for the cards still held."""
    holding_all = bool((openrouter or {}).get("holding"))
    held = {name: (provider, model) for name, (provider, model) in profiles.items()
            if holding_all or holds(providers.get(provider), model)}
    if not held:
        return {}
    tz = _timezone()
    kept = {}
    for card in held_cards(set(held)):
        card_id = str(card.get("id"))
        provider, model = held[card["assignee"]]
        if holding_all:
            episode = f"openrouter:{openrouter.get('held_since', '')}"
            text = openrouter_held_comment(settings.openrouter["label"], openrouter.get("balance", 0.0),
                                           openrouter.get("balance_hold_usd", settings.openrouter["balance_hold_usd"]))
        else:
            episode = held_episode(providers[provider], model)
            text = held_comment(settings.label(provider), held_windows(providers[provider], model), tz)
        if notes.get(card_id) != episode:
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
def _file_lock(name: str):
    path = hermes_root() / "usage-gate" / name
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(path, "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


@contextlib.contextmanager
def _exclusive():
    """One refresh at a time across threads, processes and every profile's copy of this plugin."""
    with _lock, _file_lock("refresh.lock"):
        yield


def interruptions_path() -> Path:
    return hermes_root() / "usage-gate" / "interrupted.json"


def load_interruptions() -> dict:
    try:
        return json.loads(interruptions_path().read_text())
    except (OSError, ValueError):
        return {}


def _save_interruptions(entries: dict) -> None:
    path = interruptions_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(entries, indent=2) + "\n")
    os.replace(tmp, path)


def _current_profile() -> str:
    try:
        from hermes_constants import get_hermes_home
        home = Path(get_hermes_home())
    except Exception:
        home = Path(os.environ.get("HERMES_HOME") or hermes_root())
    return home.name if home.parent.name == "profiles" else "default"


def record_interruption(profile: str, session_id: str, provider: str, platform: str, error: str,
                        at: Optional[datetime] = None, model: str = "") -> None:
    """Note a chat whose model call failed on its plan's limit, so it is resumed once the limit is back. A failure
    in a chat still waiting, or shortly after the gate resumed it, continues the same interruption: its expiry
    still counts from the first failure and its resumes keep adding up."""
    at = at or utcnow()
    with _file_lock("interrupted.lock"):
        entries = load_interruptions()
        key = f"{profile}:{session_id}"
        previous = entries.get(key) or {}
        resolved = previous.get("resolved_at")
        chained = previous.get("status") == "waiting" or (
            previous.get("status") == "resumed" and resolved and at - datetime.fromisoformat(resolved) < RESUME_CHAIN)
        entries[key] = {"profile": profile, "session_id": session_id, "provider": provider, "model": model,
                        "platform": previous.get("platform", platform) if chained else platform,
                        "first_interrupted_at": previous.get("first_interrupted_at", at.isoformat()) if chained
                        else at.isoformat(),
                        "interrupted_at": at.isoformat(), "resumes": previous.get("resumes", 0) if chained else 0,
                        "error": error[:300], "status": "waiting"}
        _save_interruptions(entries)


def on_api_request_error(session_id: str = "", platform: str = "", provider: str = "", model: str = "",
                         status_code: Any = None, reason: Any = None, error: Any = None, **_: Any) -> None:
    """Hook for every failed model call: keep the chats that stopped on a plan limit. Board cards, schedules,
    webhooks and email runs are left out; the dispatcher and their schedules already take care of them."""
    try:
        if not session_id or platform not in CHAT_PLATFORMS or os.environ.get("HERMES_KANBAN_TASK"):
            return
        if not is_limit_error(status_code, reason, error):
            return
        profile = _profile_of(session_id)
        if is_room_session(profile, session_id):
            return
        message = error.get("message", "") if isinstance(error, dict) else str(error or "")
        record_interruption(profile, session_id, provider, platform, message, model=str(model or ""))
    except Exception:
        log.exception("usage gate: could not record an interrupted chat")


def _state_db(profile: str) -> Path:
    root = hermes_root()
    return (root if profile == "default" else root / "profiles" / profile) / "state.db"


@contextlib.contextmanager
def _session_store(profile: str):
    import sqlite3
    connection = sqlite3.connect(f"file:{_state_db(profile)}?mode=ro", uri=True, timeout=10)
    try:
        yield connection
    finally:
        connection.close()


def _has_session(profile: str, session_id: str) -> bool:
    if not _state_db(profile).exists():
        return False
    with _session_store(profile) as store:
        return store.execute("select 1 from sessions where id=?", (session_id,)).fetchone() is not None


def _profile_of(session_id: str) -> str:
    """The profile whose session store holds the chat. The Desktop's backend serves every profile from one
    process, so the process's own Hermes home is only the first guess."""
    guess = _current_profile()
    for profile in [guess, *(p for p in profile_models() if p != guess)]:
        try:
            if _has_session(profile, session_id):
                return profile
        except Exception:
            continue
    return guess


def is_room_session(profile: str, session_id: str) -> bool:
    """A bot's turn in a Desktop group chat runs in a hidden session titled "Group: …". The room engine owns those
    turns, so a resume from here would answer where nobody reads it."""
    return session_title(profile, session_id).startswith("Group:")


def session_title(profile: str, session_id: str) -> str:
    if not _state_db(profile).exists():
        return ""
    with _session_store(profile) as store:
        row = store.execute("select title from sessions where id=?", (session_id,)).fetchone()
    return row[0] if row and row[0] else ""


def session_since(profile: str, session_id: str, since: datetime) -> tuple[bool, bool]:
    """(you wrote after ``since``, the bot answered after ``since``) from the profile's session store. The note
    Hermes leaves for a failed turn is not an answer."""
    if not _state_db(profile).exists():
        return False, False
    epoch = since.timestamp()
    with _session_store(profile) as store:
        user = store.execute("select 1 from messages where session_id=? and role='user' and timestamp>? limit 1",
                             (session_id, epoch)).fetchone()
        answer = store.execute(
            "select 1 from messages where session_id=? and role='assistant' and timestamp>? and coalesce(content,'')!=''"
            " and coalesce(tool_calls,'') in ('', '[]', 'null') and coalesce(display_kind,'')!='failed_turn' limit 1",
            (session_id, epoch)).fetchone()
    return bool(user), bool(answer)


def start_resume(profile: str, session_id: str, message: str) -> None:
    """Send ``message`` into the chat as a one-shot turn, in the background; its output goes to a log file."""
    logs = hermes_root() / "usage-gate" / "resume-logs"
    logs.mkdir(mode=0o700, parents=True, exist_ok=True)
    binary = os.environ.get("USAGE_GATE_HERMES_BIN") or str(Path.home() / ".local" / "bin" / "hermes")
    with open(logs / f"{profile}-{session_id}.log", "a") as out:
        subprocess.Popen([binary, "-p", profile, "chat", "--resume", session_id, "-q", message, "--oneshot"],
                         stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)


def resume_interrupted_chats(settings: Any, providers: dict[str, dict]) -> None:
    """Resume each waiting chat whose plan is open again, once; drop the expired ones and the ones you or the
    bot already continued."""
    now = utcnow()
    within = timedelta(hours=settings.resume_within_hours)
    with _file_lock("interrupted.lock"):
        entries = load_interruptions()
        for key, entry in entries.items():
            if entry.get("status") != "waiting":
                continue
            try:
                user_after, answered_after = session_since(entry["profile"], entry["session_id"],
                                                           datetime.fromisoformat(entry["interrupted_at"]))
            except Exception as exc:
                log.warning("usage gate: could not read %s: %s", key, exc)
                continue
            action = resume_action(entry, providers.get(entry.get("provider", "")), now, within, user_after,
                                   answered_after)
            if action == WAIT:
                continue
            if action == RESUME:
                try:
                    start_resume(entry["profile"], entry["session_id"], settings.resume_message)
                except OSError as exc:
                    log.warning("usage gate: could not resume %s: %s", key, exc)
                    continue
                entry["status"] = "resumed"
                entry["resumes"] = entry.get("resumes", 0) + 1
                log.warning("usage gate: resumed %s's chat %s", entry["profile"], entry["session_id"])
            else:
                entry["status"] = action
            entry["resolved_at"] = now.isoformat()
        cutoff = now - timedelta(days=7)
        entries = {k: e for k, e in entries.items()
                   if e.get("status") == "waiting" or datetime.fromisoformat(e.get("resolved_at", e["interrupted_at"])) > cutoff}
        _save_interruptions(entries)


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
            openrouter = refresh_openrouter(settings, state.get("openrouter"))
            holding = bool((openrouter or {}).get("holding"))
            if holding != bool((state.get("openrouter") or {}).get("holding")):
                log.warning("usage gate: OpenRouter balance %s every bot's cards",
                            "is low, holding" if holding else "is back, releasing")
            wanted = allowlist(profiles, providers, hold_all=holding)
        else:
            openrouter, wanted = None, None
        applied = apply_allowlist(wanted, applied)
        notes = state.get("held_notes") or {}
        if settings.enabled and settings.comment_held_cards:
            notes = comment_held_cards(settings, profiles, providers, notes, openrouter)
        if settings.enabled and settings.resume_interrupted_chats:
            resume_interrupted_chats(settings, providers)
        state = {"checked_at": now.isoformat(), "enabled": settings.enabled, "settings": settings_view(settings),
                 "providers": providers, "applied": applied, "held_notes": notes, "openrouter": openrouter}
        save_state(state)
        return state


def settings_view(settings: Any) -> dict:
    return {"enabled": settings.enabled, "session_percent": settings.default.session_percent,
            "week_percent": settings.default.week_percent, "refresh_minutes": settings.refresh_minutes,
            "hold_minutes": settings.hold_minutes, "comment_held_cards": settings.comment_held_cards,
            "resume_interrupted_chats": settings.resume_interrupted_chats, "resume_message": settings.resume_message,
            "resume_within_hours": settings.resume_within_hours,
            "openrouter": {k: v for k, v in settings.openrouter.items() if k != "key_env"},
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
                      "held_bots": sorted(n for n, (p, m) in profiles.items()
                                          if p == provider and n != "default" and holds(current, m)),
                      **current})
    holding_all = bool((state.get("openrouter") or {}).get("holding"))
    paused = {n for n, (p, m) in profiles.items() if holding_all or holds(providers.get(p), m)}
    held = [{k: c.get(k) for k in ("id", "title", "assignee", "status")} for c in held_cards(paused)] if paused else []
    tz = _timezone()
    return {"settings": settings_view(settings), "checked_at": state.get("checked_at"), "plans": plans,
            "allowlist": state.get("applied"), "held": held, "interrupted": interrupted_view(settings),
            "openrouter": state.get("openrouter"),
            "timezone": getattr(tz, "key", None)}


def interrupted_view(settings: Any) -> list[dict]:
    """The chats stopped by a plan limit in the last week, newest first, each with its title and plan label."""
    rows = []
    for entry in load_interruptions().values():
        try:
            title = session_title(entry["profile"], entry["session_id"])
        except Exception:
            title = ""
        rows.append({**entry, "title": title, "label": settings.label(entry.get("provider", ""))})
    return sorted(rows, key=lambda e: e.get("interrupted_at", ""), reverse=True)


TOKEN_COLUMNS = ("calls", "input", "output", "cache_read", "cache_write", "reasoning")


def _profile_homes(root: Optional[Path] = None) -> dict[str, Path]:
    root = root or hermes_root()
    homes = {"default": root}
    profiles = root / "profiles"
    if profiles.is_dir():
        homes.update({p.name: p for p in sorted(profiles.iterdir()) if p.is_dir() and not p.name.startswith(".")})
    return homes


def token_usage(days: Optional[float] = None, now: Optional[datetime] = None) -> dict:
    """Tokens per bot, side task and model, from every profile's session store. A session counts when it started
    in the last ``days`` (all of them when None), the way ``hermes insights`` counts it. ``main`` is the bot's
    own turns; the other tasks are side tasks (title generation, compression, vision, the background review…)."""
    import sqlite3
    cutoff = (now or utcnow()).timestamp() - days * 86400 if days else 0
    settings = read_settings()
    rows = []
    for profile, home in _profile_homes().items():
        if not (home / "state.db").exists():
            continue
        try:
            with _session_store(profile) as store:
                found = store.execute(
                    "select coalesce(nullif(u.task, ''), 'main'), u.model, u.billing_provider, sum(u.api_call_count),"
                    " sum(u.input_tokens), sum(u.output_tokens), sum(u.cache_read_tokens), sum(u.cache_write_tokens),"
                    " sum(u.reasoning_tokens) from session_model_usage u join sessions s on s.id = u.session_id"
                    " where s.started_at >= ? group by 1, 2, 3", (cutoff,)).fetchall()
        except sqlite3.Error as exc:
            log.warning("usage gate: could not read %s's token usage: %s", profile, exc)
            continue
        for task, model, provider, *counts in found:
            rows.append({"bot": profile, "task": task, "model": model, "provider": provider,
                         "label": settings.label(provider),
                         **{k: int(v or 0) for k, v in zip(TOKEN_COLUMNS, counts)}})
    rows.sort(key=lambda r: (r["bot"], r["task"] != "main", r["task"], r["model"]))
    return {"days": days, "rows": rows}


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
