"""FastAPI application exposing the fuel-cost allocation service."""

from __future__ import annotations

from dataclasses import asdict

from fastapi import FastAPI, status

from .adjustments import build_adjustments
from .allocator import UnitUsage, allocate
from .errors import register_error_handlers
from .schemas import (
    AdjustmentsRequest,
    AdjustmentsResponse,
    AllocateRequest,
    AllocateResponse,
    UnitAdjustmentOut,
    UnitShareOut,
)

app = FastAPI(
    title="Film Crew Generator Fuel Cost Allocator",
    version="1.0.0",
    description=(
        "Splits a mobile generator's total fuel bill (integer cents) across "
        "film crews with the largest-remainder method, so the per-crew "
        "breakdown always sums back to the invoiced total."
    ),
)

register_error_handlers(app)


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    """Liveness probe used by docker-compose and the verify service."""
    return {"status": "ok"}


@app.post(
    "/allocate",
    response_model=AllocateResponse,
    status_code=status.HTTP_200_OK,
    tags=["allocation"],
    summary="Split a fuel bill across crews by watts*minutes weight",
)
def allocate_costs(payload: AllocateRequest) -> AllocateResponse:
    usages = [
        UnitUsage(unit_id=u.unit_id, watts=u.watts, minutes=u.minutes) for u in payload.units
    ]
    report = allocate(payload.total_cents, usages)
    return AllocateResponse(
        total_cents=report.total_cents,
        total_weight=report.total_weight,
        allocated_cents=report.allocated_cents,
        remainder_cents_distributed=report.remainder_cents_distributed,
        allocations=[UnitShareOut(**asdict(share)) for share in report.shares],
    )


@app.post(
    "/adjustments",
    response_model=AdjustmentsResponse,
    status_code=status.HTTP_200_OK,
    tags=["adjustment"],
    summary="Diff two allocation versions after meter readings are corrected",
)
def adjust_costs(payload: AdjustmentsRequest) -> AdjustmentsResponse:
    original = [
        UnitUsage(unit_id=u.unit_id, watts=u.watts, minutes=u.minutes)
        for u in payload.original_units
    ]
    corrected = [
        UnitUsage(unit_id=u.unit_id, watts=u.watts, minutes=u.minutes)
        for u in payload.corrected_units
    ]
    report = build_adjustments(payload.total_cents, original, corrected)
    return AdjustmentsResponse(
        total_cents=report.total_cents,
        original_total_weight=report.original_total_weight,
        corrected_total_weight=report.corrected_total_weight,
        total_adjustment_cents=report.total_adjustment_cents,
        adjustments=[UnitAdjustmentOut(**asdict(item)) for item in report.adjustments],
    )
