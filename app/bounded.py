"""Proportional allocation with per-crew minimums and maximums (bounded split).

Location contracts may guarantee each crew a *minimum* fuel contribution and
cap it at a *maximum*. The allocator starts every crew at its minimum and
distributes the remaining cents only among crews with positive weight that
have not reached their cap.

The surplus is spread by iterative "water filling":

1. Among the still-open crews the exact proportional increment is
   ``remaining * weight / open_weight`` (compared with integer cross
   multiplication, so no precision is lost).
2. Every crew whose increment reaches or exceeds its remaining capacity is
   locked at its maximum; all such locks in one round use the same balance
   and weights, then the locked capacities are deducted together.
3. The weights are re-read from the survivors and the process repeats until a
   round produces no new lock.
4. The surviving crews then receive the floored proportional share, and the
   last integer cents are handed out by the same largest-remainder rule
   (UTF-8 byte order of unit_id breaking ties) used by ``/allocate``.

Zero-weight crews can never earn surplus: they receive exactly their minimum,
so their maximum does not contribute to the distributable upper bound.

Validation is staged and all-or-nothing: duplicate unit_ids reuse the
ordinary allocation error, then *all* inverted bounds (minimum > maximum)
are reported together in input order, and only afterwards is the invoice
total checked against the guaranteed floor total and the distributable cap.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .allocator import DuplicateUnitIdError

# Basis markers returned per crew.
BASIS_MINIMUM = "minimum"
BASIS_WEIGHTED = "weighted"
BASIS_MAXIMUM = "maximum"


class BoundsInvertedError(Exception):
    """A crew's minimum_cents is greater than its maximum_cents.

    ``inversions`` holds every offending crew as
    ``(input index, minimum_cents, maximum_cents)`` sorted by input index.
    """

    def __init__(self, inversions: list[tuple[int, int, int]]) -> None:
        self.inversions = inversions
        positions = ", ".join(f"units[{index}]" for index, _, _ in inversions)
        super().__init__(
            "minimum_cents must be <= maximum_cents for every crew; inverted bounds at "
            + positions
        )


class InfeasibleBoundsError(Exception):
    """The invoice total cannot be split under the given bounds.

    ``reason`` is ``"below_minimum"`` or ``"above_maximum"``. The maximum
    total only counts maximum_cents of positive-weight crews; zero-weight
    crews are stuck at their minimum.
    """

    def __init__(
        self,
        reason: str,
        total_cents: int,
        minimum_total: int,
        distributable_maximum: int,
    ) -> None:
        self.reason = reason
        self.total_cents = total_cents
        self.minimum_total = minimum_total
        self.distributable_maximum = distributable_maximum
        if reason == "below_minimum":
            message = (
                f"total_cents ({total_cents}) is below the sum of minimum_cents "
                f"({minimum_total}); every crew must receive at least its guaranteed minimum"
            )
        else:
            message = (
                f"total_cents ({total_cents}) exceeds the distributable cap "
                f"({distributable_maximum}); positive-weight crews are bounded by "
                "maximum_cents and zero-weight crews can only receive their minimum_cents"
            )
        super().__init__(message)


@dataclass(frozen=True)
class BoundedUnit:
    """One crew's usage together with its guaranteed floor and cap."""

    unit_id: str
    watts: int
    minutes: int
    minimum_cents: int
    maximum_cents: int


@dataclass(frozen=True)
class BoundedShare:
    """The bounded share of one crew, all amounts in integer cents.

    ``amount_basis`` explains the final amount:

    * ``minimum``  — the crew keeps its guaranteed minimum (also covers
      zero-weight crews, crews whose floored share adds nothing and crews
      with minimum == maximum);
    * ``maximum``  — the crew was locked at its cap while surplus was spread;
    * ``weighted`` — the crew received surplus through proportional weighting
      (floored share, possibly plus one largest-remainder cent).
    """

    unit_id: str
    weight: int
    minimum_cents: int
    maximum_cents: int
    final_cents: int
    amount_basis: str


@dataclass(frozen=True)
class BoundedAllocationReport:
    """The full bounded split of a bill across every crew."""

    total_cents: int
    total_weight: int
    remainder_cents_distributed: int
    shares: list[BoundedShare]

    @property
    def allocated_cents(self) -> int:
        return sum(share.final_cents for share in self.shares)


def allocate_bounded(
    total_cents: int, units: Sequence[BoundedUnit]
) -> BoundedAllocationReport:
    """Split ``total_cents`` under each crew's minimum/maximum constraints.

    The returned finals always satisfy ``minimum_cents <= final_cents <=
    maximum_cents`` and sum exactly to ``total_cents``. Any rule violation
    (duplicate unit_id, inverted bounds, or a total outside the feasible
    range) rejects the whole batch without producing partial results.
    """
    # Stage 1: duplicate unit_ids reuse the ordinary allocation error, so the
    # loc/message shape stays identical across endpoints.
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

    # Stage 2: report every inverted bound at once, in input position order.
    inversions = [
        (index, unit.minimum_cents, unit.maximum_cents)
        for index, unit in enumerate(units)
        if unit.minimum_cents > unit.maximum_cents
    ]
    if inversions:
        raise BoundsInvertedError(inversions)

    count = len(units)
    weights = [unit.watts * unit.minutes for unit in units]
    minimums = [unit.minimum_cents for unit in units]
    maximums = [unit.maximum_cents for unit in units]

    # Stage 3: feasibility of the invoice total.
    minimum_total = sum(minimums)
    # Zero-weight crews cannot earn surplus, so only the maximums of
    # positive-weight crews extend the distributable upper bound.
    distributable_maximum = sum(
        maximums[i] if weights[i] > 0 else minimums[i] for i in range(count)
    )
    if total_cents < minimum_total:
        raise InfeasibleBoundsError(
            "below_minimum", total_cents, minimum_total, distributable_maximum
        )
    if total_cents > distributable_maximum:
        raise InfeasibleBoundsError(
            "above_maximum", total_cents, minimum_total, distributable_maximum
        )

    amounts = list(minimums)
    bases = [BASIS_MINIMUM] * count
    remaining = total_cents - minimum_total
    # Open crews have positive weight and room above their minimum. Crews
    # outside this set (zero weight, or minimum == maximum) keep the floor.
    open_crews = {
        i for i in range(count) if weights[i] > 0 and minimums[i] < maximums[i]
    }

    # Water-filling rounds. An open crew's amount stays at its minimum until
    # it locks, so its remaining capacity is always maximum - minimum.
    while open_crews and remaining > 0:
        open_weight = sum(weights[i] for i in open_crews)
        # remaining * weight >= capacity * open_weight  <=>  exact increment
        # reaches or exceeds the crew's remaining capacity.
        newly_locked = [
            i
            for i in open_crews
            if remaining * weights[i]
            >= (maximums[i] - amounts[i]) * open_weight
        ]
        if not newly_locked:
            break
        for i in newly_locked:
            capacity = maximums[i] - amounts[i]
            amounts[i] = maximums[i]
            bases[i] = BASIS_MAXIMUM
            remaining -= capacity
            open_crews.discard(i)

    # Final integer stage: largest remainder among the surviving open crews.
    remainder_distributed = 0
    if open_crews and remaining > 0:
        open_list = sorted(open_crews)
        open_weight = sum(weights[i] for i in open_list)
        floors: dict[int, int] = {}
        fractional_remainders: dict[int, int] = {}
        for i in open_list:
            quotient, fractional = divmod(remaining * weights[i], open_weight)
            floors[i] = quotient
            fractional_remainders[i] = fractional
        leftover = remaining - sum(floors.values())
        order = sorted(
            open_list,
            key=lambda i: (
                -fractional_remainders[i],
                units[i].unit_id.encode("utf-8"),
            ),
        )
        awarded = set(order[:leftover])
        remainder_distributed = leftover
        for i in open_list:
            final = minimums[i] + floors[i] + (1 if i in awarded else 0)
            amounts[i] = final
            # A survivor that adds nothing sits exactly on its floor; the
            # amount is then governed by the guaranteed minimum.
            bases[i] = BASIS_MINIMUM if final == minimums[i] else BASIS_WEIGHTED

    shares = [
        BoundedShare(
            unit_id=units[i].unit_id,
            weight=weights[i],
            minimum_cents=minimums[i],
            maximum_cents=maximums[i],
            final_cents=amounts[i],
            amount_basis=bases[i],
        )
        for i in range(count)
    ]
    return BoundedAllocationReport(
        total_cents=total_cents,
        total_weight=sum(weights),
        remainder_cents_distributed=remainder_distributed,
        shares=shares,
    )
