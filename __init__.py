"""Hermes Usage Gate plugin: hold back new board cards while a provider's plan usage is high, and resume the
chats a plan limit stopped once it is back."""
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from usage_gate.runtime import on_api_request_error, on_dispatch_tick, refresh, status_view, token_usage  # noqa: E402


def _setup(parser: Any) -> None:
    commands = parser.add_subparsers(dest="usage_gate_command")
    commands.add_parser("status", help="Settings, each plan's last reading and decision, held cards and "
                                   "interrupted chats")
    commands.add_parser("check", help="Read every provider's usage now and apply the result")
    tokens = commands.add_parser("tokens", help="Tokens per bot, side task and model")
    tokens.add_argument("--days", type=float, default=None, help="Only sessions started in the last N days")
    parser.set_defaults(func=_run)


def _run(args: Any) -> None:
    command = getattr(args, "usage_gate_command", None)
    if command == "check":
        refresh(force=True)
        result: Any = status_view()
    elif command == "status":
        result = status_view()
    elif command == "tokens":
        result = token_usage(getattr(args, "days", None))
    else:
        result = {"usage": "hermes usage-gate {status,check,tokens}"}
    print(json.dumps(result, indent=2, default=str))


def register(ctx: Any) -> None:
    ctx.register_hook("on_kanban_dispatch_tick", on_dispatch_tick)
    ctx.register_hook("api_request_error", on_api_request_error)
    ctx.register_cli_command(name="usage-gate", help="Board start gate driven by plan usage", setup_fn=_setup,
                             handler_fn=_run)
