"""FastAPI application exposing the fuel-cost allocation service."""

from __future__ import annotations

from dataclasses import asdict

from fastapi import FastAPI, status

from .adjustments import build_adjustments
from .allocator import UnitUsage, allocate
from .bounded import BoundedUnit, allocate_bounded
from .errors import register_error_handlers
from .schemas import (
    AdjustmentsRequest,
    AdjustmentsResponse,
    AllocateBoundedRequest,
    AllocateBoundedResponse,
    AllocateRequest,
    AllocateResponse,
    BoundedUnitShareOut,
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
    "/allocate-bounded",
    response_model=AllocateBoundedResponse,
    status_code=status.HTTP_200_OK,
    tags=["allocation"],
    summary="Split a fuel bill with per-crew minimum floors and maximum caps",
)
def allocate_bounded_costs(payload: AllocateBoundedRequest) -> AllocateBoundedResponse:
    usages = [
        BoundedUnit(
            unit_id=u.unit_id,
            watts=u.watts,
            minutes=u.minutes,
            minimum_cents=u.minimum_cents,
            maximum_cents=u.maximum_cents,
        )
        for u in payload.units
    ]
    report = allocate_bounded(payload.total_cents, usages)
    return AllocateBoundedResponse(
        total_cents=report.total_cents,
        total_weight=report.total_weight,
        allocated_cents=report.allocated_cents,
        remainder_cents_distributed=report.remainder_cents_distributed,
        allocations=[
            BoundedUnitShareOut(
                unit_id=share.unit_id,
                weight=share.weight,
                minimum_cents=share.minimum_cents,
                maximum_cents=share.maximum_cents,
                final_cents=share.final_cents,
                amount_basis=share.amount_basis,
            )
            for share in report.shares
        ],
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
