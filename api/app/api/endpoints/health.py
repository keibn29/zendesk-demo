"""Health endpoint."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["ops"])


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe. Intentionally does not touch upstream APIs."""
    return {"status": "ok"}