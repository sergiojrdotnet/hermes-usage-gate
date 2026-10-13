"""Usage Gate backend, mounted by Hermes at /api/plugins/hermes-usage-gate/ for the dashboard and Desktop.

Read-only: settings live in the default profile's config.yaml. Handlers are plain functions, so FastAPI runs
them in its thread pool: they read provider usage and call the hermes CLI, which can take a few seconds."""
import sys
from pathlib import Path
from typing import Any

from fastapi import APIRouter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from usage_gate.runtime import refresh, status_view  # noqa: E402

router = APIRouter()


@router.get("/status")
def status() -> dict[str, Any]:
    return status_view()


@router.post("/check")
def check() -> dict[str, Any]:
    refresh(force=True)
    return status_view()
