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
