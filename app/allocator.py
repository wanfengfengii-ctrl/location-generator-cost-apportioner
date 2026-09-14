"""Largest-remainder (Hamilton) apportionment of an integer-cent fuel bill.

Each crew contributes an integer weight of ``watts * minutes``. Every crew's
exact share ``total_cents * weight / total_weight`` is floored, and the
remaining cents are handed out one by one to the crews with the largest
fractional remainder. Ties are broken by the UTF-8 byte order of ``unit_id``
(ascending), which makes the split fully deterministic.

This module is deliberately free of web-framework imports so the domain logic
can be unit-tested and reused on its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


class AllocationError(Exception):
    """Base class for domain errors that reject a whole allocation batch."""


class DuplicateUnitIdError(AllocationError):
    """Raised when the same unit_id appears more than once in a batch."""

    def __init__(self, duplicates: list[tuple[int, str, int]]) -> None:
        # Each entry: (index of the repeated occurrence, unit_id, index of first occurrence).
        self.duplicates = duplicates
        repeated = sorted({unit_id for _, unit_id, _ in duplicates})
        super().__init__(
            "unit_id values must be unique; repeated: " + ", ".join(repr(u) for u in repeated)
        )


class ZeroTotalWeightError(AllocationError):
    """Raised when every unit has a zero watts*minutes weight."""

    def __init__(self) -> None:
        super().__init__("total weight (sum of watts*minutes) must be greater than zero")


@dataclass(frozen=True)
class UnitUsage:
    """One crew's metered usage of the generator."""

    unit_id: str
    watts: int
    minutes: int


@dataclass(frozen=True)
class UnitShare:
    """The computed share of one crew, all amounts in integer cents."""

    unit_id: str
    watts: int
    minutes: int
    weight: int
    floor_cents: int
    remainder_awarded: bool
    final_cents: int


@dataclass(frozen=True)
class AllocationReport:
    """The full split of a bill across every crew."""

    total_cents: int
    total_weight: int
    remainder_cents_distributed: int
    shares: list[UnitShare]

    @property
    def allocated_cents(self) -> int:
        return sum(share.final_cents for share in self.shares)


def allocate(total_cents: int, units: Sequence[UnitUsage]) -> AllocationReport:
    """Split ``total_cents`` across ``units`` proportionally to watts*minutes.

    The sum of the returned ``final_cents`` always equals ``total_cents``
    exactly. The whole batch is rejected (an AllocationError is raised) if any
    unit_id is duplicated or if every weight is zero.
    """
    seen: dict[str, int] = {}
    duplicates: list[tuple[int, str, int]] = []
    for index, unit in enumerate(units):
        first = seen.get(unit.unit_id)
        if first is None:
            seen[unit.unit_id] = index
        else:
            duplicates.append((index, unit.unit_id, first))
    if duplicates:
        raise DuplicateUnitIdError(duplicates)

    weights = [unit.watts * unit.minutes for unit in units]
    total_weight = sum(weights)
    if total_weight <= 0:
        raise ZeroTotalWeightError()

    floors: list[int] = []
    remainders: list[int] = []
    for weight in weights:
        # divmod on non-negative integers yields the floor and the exact
        # fractional remainder scaled by total_weight (0 <= r < total_weight).
        quotient, remainder = divmod(total_cents * weight, total_weight)
        floors.append(quotient)
        remainders.append(remainder)

    leftover = total_cents - sum(floors)
    # leftover == sum(remainders) / total_weight, and every remainder is a
    # fraction < 1 of total_weight, so 0 <= leftover < len(units) and only
    # units with a non-zero remainder can ever receive an extra cent.
    by_largest_remainder = sorted(
        range(len(units)),
        key=lambda i: (-remainders[i], units[i].unit_id.encode("utf-8")),
    )
    awarded = [False] * len(units)
    for i in by_largest_remainder[:leftover]:
        awarded[i] = True

    shares = [
        UnitShare(
            unit_id=unit.unit_id,
            watts=unit.watts,
            minutes=unit.minutes,
            weight=weight,
            floor_cents=floor,
            remainder_awarded=has_bonus,
            final_cents=floor + (1 if has_bonus else 0),
        )
        for unit, weight, floor, has_bonus in zip(units, weights, floors, awarded)
    ]
    return AllocationReport(
        total_cents=total_cents,
        total_weight=total_weight,
        remainder_cents_distributed=leftover,
        shares=shares,
    )
