"""Usage Gate backend, mounted by Hermes at /api/plugins/hermes-usage-gate/ for the dashboard and Desktop.

Handlers are plain functions, so FastAPI runs them in its thread pool: they read provider usage and call the
hermes CLI, which can take a few seconds."""
import sys
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from usage_gate.runtime import refresh, save_settings, status_view  # noqa: E402

router = APIRouter()


@router.get("/status")
def status() -> dict[str, Any]:
    return status_view()


@router.post("/check")
def check() -> dict[str, Any]:
    refresh(force=True)
    return status_view()


@router.post("/settings")
def settings(body: dict[str, Any]) -> dict[str, Any]:
    try:
        save_settings(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    refresh(force=True)
    return status_view()
