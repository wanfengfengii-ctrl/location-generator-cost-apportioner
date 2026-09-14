"""Adjustment orchestration for corrected generator readings.

When a crew corrects a meter reading after the shoot, cost accounting needs the
*delta* between the original and the corrected split, not two reports to diff
by hand. This module runs the existing largest-remainder allocation twice —
once for the original readings and once for the corrected readings, always
against the same invoice total — and merges the two results by ``unit_id``.

Because both versions allocate the very same ``total_cents``, the per-unit
deltas always sum to exactly zero; the response exposes that sum as a
reconciliation check.

Both readings arrays must contain exactly the same set of unique ``unit_id``
values, and each version must have a positive total weight. Any violation
rejects the whole batch: validation is staged (duplicates, then set mismatch,
then zero weight) and no partial adjustment is ever produced.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .allocator import (
    AllocationError,
    UnitUsage,
    ZeroTotalWeightError,
    allocate,
)


class AdjustmentDuplicateUnitIdError(AllocationError):
    """A unit_id is repeated inside one of the two readings arrays."""

    def __init__(self, occurrences: list[tuple[str, int, str, int]]) -> None:
        # Each entry: (array name, repeated index, unit_id, first index).
        self.occurrences = occurrences
        repeated = sorted({unit_id for _, _, unit_id, _ in occurrences})
        super().__init__(
            "unit_id values must be unique within each readings array; repeated: "
            + ", ".join(repr(u) for u in repeated)
        )


class UnitSetMismatchError(AllocationError):
    """The two readings arrays do not contain exactly the same unit_id set."""

    def __init__(self, fields: list[tuple[tuple, str]]) -> None:
        # Each entry: (JSON-path loc of the offending array element, message).
        self.fields = fields
        super().__init__(
            "original_units and corrected_units must contain exactly the same "
            "unit_id values, each exactly once"
        )


class AdjustmentZeroTotalWeightError(AllocationError):
    """One readings version has an all-zero watts*minutes total weight."""

    def __init__(self, array_name: str) -> None:
        # Name of the offending request array: "original_units" / "corrected_units".
        self.array_name = array_name
        super().__init__(
            f"total weight of {array_name} (sum of watts*minutes) must be greater than zero"
        )


@dataclass(frozen=True)
class UnitAdjustment:
    """One crew's original amount, corrected amount and signed delta."""

    unit_id: str
    original_cents: int
    corrected_cents: int
    adjustment_cents: int


@dataclass(frozen=True)
class AdjustmentReport:
    """The full set of deltas; ``total_adjustment_cents`` is always exactly 0."""

    total_cents: int
    original_total_weight: int
    corrected_total_weight: int
    adjustments: list[UnitAdjustment]

    @property
    def total_adjustment_cents(self) -> int:
        return sum(item.adjustment_cents for item in self.adjustments)


def _find_duplicates(
    array_name: str, units: Sequence[UnitUsage]
) -> list[tuple[str, int, str, int]]:
    """Return (array, repeated index, unit_id, first index) for every repeat."""
    seen: dict[str, int] = {}
    occurrences: list[tuple[str, int, str, int]] = []
    for index, unit in enumerate(units):
        first = seen.get(unit.unit_id)
        if first is None:
            seen[unit.unit_id] = index
        else:
            occurrences.append((array_name, index, unit.unit_id, first))
    return occurrences


def build_adjustments(
    total_cents: int,
    original_units: Sequence[UnitUsage],
    corrected_units: Sequence[UnitUsage],
) -> AdjustmentReport:
    """Allocate ``total_cents`` under both readings and diff them per unit.

    Returns adjustments in ``original_units`` order. Raises an
    :class:`AllocationError` subclass (rejecting the whole batch) if either
    array has duplicate unit_ids, if the two unit_id sets differ, or if either
    version's total weight is zero.
    """
    # Stage 1: unit_id must be unique within each array.
    occurrences = _find_duplicates("original_units", original_units)
    occurrences += _find_duplicates("corrected_units", corrected_units)
    if occurrences:
        raise AdjustmentDuplicateUnitIdError(occurrences)

    # Stage 2: both arrays must contain exactly the same unit_id set.
    original_ids = [unit.unit_id for unit in original_units]
    original_id_set = set(original_ids)
    corrected_id_set = {unit.unit_id for unit in corrected_units}
    mismatches: list[tuple[tuple, str]] = []
    for index, unit_id in enumerate(original_ids):
        if unit_id not in corrected_id_set:
            mismatches.append(
                (
                    ("body", "original_units", index),
                    f"unit_id {unit_id!r} has no matching entry in corrected_units",
                )
            )
    for index, unit in enumerate(corrected_units):
        if unit.unit_id not in original_id_set:
            mismatches.append(
                (
                    ("body", "corrected_units", index),
                    f"unit_id {unit.unit_id!r} has no matching entry in original_units",
                )
            )
    if mismatches:
        raise UnitSetMismatchError(mismatches)

    # Stage 3: reuse the existing apportionment for each version. Neither
    # allocation can partially fail: duplicates were already rejected above,
    # so only the zero-weight rule remains, reported per version.
    try:
        original_report = allocate(total_cents, original_units)
    except ZeroTotalWeightError:
        raise AdjustmentZeroTotalWeightError("original_units") from None
    try:
        corrected_report = allocate(total_cents, corrected_units)
    except ZeroTotalWeightError:
        raise AdjustmentZeroTotalWeightError("corrected_units") from None

    corrected_by_id = {share.unit_id: share for share in corrected_report.shares}
    adjustments = [
        UnitAdjustment(
            unit_id=share.unit_id,
            original_cents=share.final_cents,
            corrected_cents=corrected_by_id[share.unit_id].final_cents,
            adjustment_cents=corrected_by_id[share.unit_id].final_cents - share.final_cents,
        )
        for share in original_report.shares
    ]
    return AdjustmentReport(
        total_cents=total_cents,
        original_total_weight=original_report.total_weight,
        corrected_total_weight=corrected_report.total_weight,
        adjustments=adjustments,
    )
