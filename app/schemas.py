"""Pydantic request/response schemas for the allocation API.

All numeric inputs are validated as *strict* non-negative integers: JSON
strings, booleans and floats are rejected instead of being silently coerced,
so a malformed batch is always refused as a whole.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

# A JSON integer >= 0. strict=True rejects "100", 10.0, 10.5 and true.
NonNegInt = Annotated[int, Field(strict=True, ge=0)]


class UnitIn(BaseModel):
    """One crew's usage record inside an allocation batch."""

    model_config = ConfigDict(extra="forbid")

    unit_id: Annotated[str, Field(min_length=1, max_length=128)]
    watts: NonNegInt
    minutes: NonNegInt


class AllocateRequest(BaseModel):
    """Body of POST /allocate: the invoice total plus every crew's usage."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "total_cents": 10000,
                    "units": [
                        {"unit_id": "lighting", "watts": 2000, "minutes": 180},
                        {"unit_id": "camera", "watts": 800, "minutes": 150},
                        {"unit_id": "vfx", "watts": 500, "minutes": 96},
                    ],
                }
            ]
        },
    )

    total_cents: NonNegInt
    units: Annotated[list[UnitIn], Field(min_length=1)]


class UnitShareOut(BaseModel):
    """Per-crew result: weight, floored share, remainder flag, final amount."""

    unit_id: str
    watts: int
    minutes: int
    weight: int
    floor_cents: int
    remainder_awarded: bool
    final_cents: int


class AllocateResponse(BaseModel):
    """Full split; ``allocated_cents`` always equals ``total_cents``."""

    total_cents: int
    total_weight: int
    allocated_cents: int
    remainder_cents_distributed: int
    allocations: list[UnitShareOut]


class BoundedUnitIn(BaseModel):
    """One crew's usage with its guaranteed minimum and capped maximum."""

    model_config = ConfigDict(extra="forbid")

    unit_id: Annotated[str, Field(min_length=1, max_length=128)]
    watts: NonNegInt
    minutes: NonNegInt
    minimum_cents: NonNegInt
    maximum_cents: NonNegInt


class AllocateBoundedRequest(BaseModel):
    """Body of POST /allocate-bounded: invoice total, usage, floors and caps."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "total_cents": 10000,
                    "units": [
                        {
                            "unit_id": "lighting",
                            "watts": 2000,
                            "minutes": 180,
                            "minimum_cents": 3000,
                            "maximum_cents": 8000,
                        },
                        {
                            "unit_id": "camera",
                            "watts": 800,
                            "minutes": 150,
                            "minimum_cents": 1000,
                            "maximum_cents": 4000,
                        },
                        {
                            "unit_id": "vfx",
                            "watts": 500,
                            "minutes": 96,
                            "minimum_cents": 500,
                            "maximum_cents": 2000,
                        },
                    ],
                }
            ]
        },
    )

    total_cents: NonNegInt
    units: Annotated[list[BoundedUnitIn], Field(min_length=1)]


class BoundedUnitShareOut(BaseModel):
    """Per-crew bounded result: weight, bounds, final amount, and its basis."""

    unit_id: str
    weight: int
    minimum_cents: int
    maximum_cents: int
    final_cents: int
    amount_basis: str


class AllocateBoundedResponse(BaseModel):
    """Full bounded split; ``allocated_cents`` always equals ``total_cents``."""

    total_cents: int
    total_weight: int
    allocated_cents: int
    remainder_cents_distributed: int
    allocations: list[BoundedUnitShareOut]


class AdjustmentsRequest(BaseModel):
    """Body of POST /adjustments: one invoice total, two readings versions.

    ``original_units`` and ``corrected_units`` must each hold at least one
    entry; their unit_id sets must match exactly (cross-checked by the
    adjustment service, which can locate individual array elements).
    """

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "total_cents": 10000,
                    "original_units": [
                        {"unit_id": "lighting", "watts": 2000, "minutes": 180},
                        {"unit_id": "camera", "watts": 800, "minutes": 150},
                    ],
                    "corrected_units": [
                        {"unit_id": "lighting", "watts": 2000, "minutes": 200},
                        {"unit_id": "camera", "watts": 800, "minutes": 150},
                    ],
                }
            ]
        },
    )

    total_cents: NonNegInt
    original_units: Annotated[list[UnitIn], Field(min_length=1)]
    corrected_units: Annotated[list[UnitIn], Field(min_length=1)]


class UnitAdjustmentOut(BaseModel):
    """Per-crew delta; ``adjustment_cents`` may be positive, zero or negative."""

    unit_id: str
    original_cents: int
    corrected_cents: int
    adjustment_cents: int


class AdjustmentsResponse(BaseModel):
    """Result of diffing both allocations.

    ``total_adjustment_cents`` is the reconciliation check: both versions
    split the same invoice total, so the signed deltas always sum to zero.
    """

    total_cents: int
    original_total_weight: int
    corrected_total_weight: int
    total_adjustment_cents: int
    adjustments: list[UnitAdjustmentOut]
