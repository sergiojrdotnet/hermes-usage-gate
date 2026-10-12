"""Hermes Usage Gate plugin: hold back new board cards while a provider's plan usage is high."""
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from usage_gate.runtime import load_state, on_dispatch_tick, read_settings, refresh  # noqa: E402


def _setup(parser: Any) -> None:
    commands = parser.add_subparsers(dest="usage_gate_command")
    commands.add_parser("status", help="Last readings, decisions and the allowlist in force")
    commands.add_parser("check", help="Read every provider's usage now and apply the result")
    parser.set_defaults(func=_run)


def _run(args: Any) -> None:
    command = getattr(args, "usage_gate_command", None)
    if command == "check":
        result: Any = refresh(force=True)
    elif command == "status":
        settings = read_settings()
        result = {"settings": {"enabled": settings.enabled, "session_percent": settings.default.session_percent,
                               "week_percent": settings.default.week_percent,
                               "refresh_minutes": settings.refresh_minutes, "hold_minutes": settings.hold_minutes,
                               "per_provider": {k: vars(v) for k, v in settings.per_provider.items()}},
                  "state": load_state()}
    else:
        result = {"usage": "hermes usage-gate {status,check}"}
    print(json.dumps(result, indent=2, default=str))


def register(ctx: Any) -> None:
    ctx.register_hook("on_kanban_dispatch_tick", on_dispatch_tick)
    ctx.register_cli_command(name="usage-gate", help="Board start gate driven by plan usage", setup_fn=_setup,
                             handler_fn=_run)
