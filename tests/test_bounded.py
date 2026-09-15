"""Pure unit tests for the bounded (minimum/maximum) allocator."""

from __future__ import annotations

import random

import pytest

from app.allocator import DuplicateUnitIdError
from app.bounded import (
    BASIS_MAXIMUM,
    BASIS_MINIMUM,
    BASIS_WEIGHTED,
    BoundsInvertedError,
    BoundedUnit,
    InfeasibleBoundsError,
    allocate_bounded,
)


def _map(report):
    return {share.unit_id: share for share in report.shares}


def _oracle_locked(weights, minimums, maximums, total):
    """Independent event-based water-filling: return the set of capped crews.

    At each level the crew whose capacity/weight ratio is smallest hits its
    cap first; every crew tied at that ratio locks in the same round.
    """
    count = len(weights)
    eligible = {
        i
        for i in range(count)
        if weights[i] > 0 and minimums[i] < maximums[i]
    }
    locked: set[int] = set()
    remaining = total - sum(minimums)
    while eligible - locked and remaining > 0:
        pool = eligible - locked
        pool_weight = sum(weights[i] for i in pool)
        # Smallest exact increment needed to hit the cap.
        hitters = {
            i
            for i in pool
            if remaining * weights[i]
            >= (maximums[i] - minimums[i]) * pool_weight
        }
        if not hitters:
            break
        for i in hitters:
            remaining -= maximums[i] - minimums[i]
            locked.add(i)
    return locked


def test_multi_round_cap_relocking_reconciles() -> None:
    # Equal weights, caps 20 / 40 / 100 over 100 cents: round 1 locks "a"
    # (20), round 2 locks "b" (40), the 40 remainder goes wholly to "c".
    report = allocate_bounded(
        100,
        [
            BoundedUnit("a", 1, 1, 0, 20),
            BoundedUnit("b", 1, 1, 0, 40),
            BoundedUnit("c", 1, 1, 0, 100),
        ],
    )
    by_id = _map(report)
    assert by_id["a"].final_cents == 20
    assert by_id["a"].amount_basis == BASIS_MAXIMUM
    assert by_id["b"].final_cents == 40
    assert by_id["b"].amount_basis == BASIS_MAXIMUM
    assert by_id["c"].final_cents == 40
    assert by_id["c"].amount_basis == BASIS_WEIGHTED
    assert report.allocated_cents == 100
    assert report.remainder_cents_distributed == 0


def test_minimums_are_floors_before_weighting() -> None:
    # Floors 10 / 15 / 5 total 30; surplus 60 split by weight 1 vs 3 over
    # open crews "a"/"c" -> 15 / 45 on top of their floors, while zero-weight
    # "b" keeps exactly its minimum regardless of its large cap.
    report = allocate_bounded(
        90,
        [
            BoundedUnit("a", 1, 1, 10, 100),
            BoundedUnit("b", 0, 9, 15, 999),
            BoundedUnit("c", 3, 1, 5, 100),
        ],
    )
    by_id = _map(report)
    assert by_id["a"].final_cents == 25
    assert by_id["a"].amount_basis == BASIS_WEIGHTED
    assert by_id["b"].final_cents == 15
    assert by_id["b"].amount_basis == BASIS_MINIMUM
    assert by_id["b"].weight == 0
    assert by_id["c"].final_cents == 50
    assert by_id["c"].amount_basis == BASIS_WEIGHTED
    assert report.allocated_cents == 90


def test_tie_remainder_uses_utf8_byte_order() -> None:
    # 3 cents, equal weights, no caps binding: floors 1/1, one leftover cent.
    # "zebra" (0x7A...) precedes "équipe" (0xC3...) in UTF-8 byte order.
    report = allocate_bounded(
        3,
        [
            BoundedUnit("équipe", 2, 2, 0, 100),
            BoundedUnit("zebra", 2, 2, 0, 100),
        ],
    )
    by_id = _map(report)
    assert by_id["zebra"].final_cents == 2
    assert by_id["équipe"].final_cents == 1
    assert report.remainder_cents_distributed == 1


def test_total_equal_to_minimum_total_everyone_on_floor() -> None:
    report = allocate_bounded(
        25,
        [
            BoundedUnit("a", 2, 2, 10, 10),
            BoundedUnit("b", 3, 3, 5, 50),
            BoundedUnit("z", 0, 4, 10, 10),
        ],
    )
    assert report.allocated_cents == 25
    assert all(share.final_cents == share.minimum_cents for share in report.shares)
    assert all(share.amount_basis == BASIS_MINIMUM for share in report.shares)
    assert report.remainder_cents_distributed == 0


def test_total_equal_to_distributable_cap_locks_every_positive_weight_crew() -> None:
    report = allocate_bounded(
        70,
        [
            BoundedUnit("a", 1, 1, 0, 20),
            BoundedUnit("b", 1, 1, 10, 40),
            BoundedUnit("z", 0, 8, 10, 500),
        ],
    )
    by_id = _map(report)
    assert by_id["a"].final_cents == 20
    assert by_id["a"].amount_basis == BASIS_MAXIMUM
    assert by_id["b"].final_cents == 40
    assert by_id["b"].amount_basis == BASIS_MAXIMUM
    assert by_id["z"].final_cents == 10
    assert by_id["z"].amount_basis == BASIS_MINIMUM
    assert report.allocated_cents == 70


def test_zero_weight_crew_cap_does_not_extend_distributable_total() -> None:
    # Positive-weight crew can absorb at most 50; zero-weight "z" is stuck at
    # 0 even though its maximum_cents says 1000.
    with pytest.raises(InfeasibleBoundsError) as excinfo:
        allocate_bounded(
            100,
            [
                BoundedUnit("z", 0, 3, 0, 1000),
                BoundedUnit("a", 1, 1, 0, 50),
            ],
        )
    assert excinfo.value.reason == "above_maximum"
    assert excinfo.value.distributable_maximum == 50


def test_total_below_minimum_total_rejected() -> None:
    with pytest.raises(InfeasibleBoundsError) as excinfo:
        allocate_bounded(
            9,
            [BoundedUnit("a", 1, 1, 5, 50), BoundedUnit("b", 2, 2, 5, 50)],
        )
    assert excinfo.value.reason == "below_minimum"
    assert excinfo.value.minimum_total == 10


def test_inverted_bounds_all_reported_in_input_order() -> None:
    with pytest.raises(BoundsInvertedError) as excinfo:
        allocate_bounded(
            100,
            [
                BoundedUnit("ok", 1, 1, 0, 10),
                BoundedUnit("bad1", 1, 1, 8, 3),
                BoundedUnit("bad2", 1, 1, 9, 9),
                BoundedUnit("bad3", 1, 1, 4, 1),
            ],
        )
    # The equal case is not an inversion; both strict inversions are listed,
    # sorted by input position.
    assert excinfo.value.inversions == [(1, 8, 3), (3, 4, 1)]


def test_inverted_bounds_take_precedence_over_feasibility() -> None:
    # Both rules are violated; the inversion must surface first and no result
    # may be produced.
    with pytest.raises(BoundsInvertedError):
        allocate_bounded(0, [BoundedUnit("a", 1, 1, 10, 5)])


def test_duplicate_unit_id_reuses_allocation_error() -> None:
    with pytest.raises(DuplicateUnitIdError) as excinfo:
        allocate_bounded(
            20,
            [
                BoundedUnit("a", 1, 1, 0, 10),
                BoundedUnit("a", 2, 2, 0, 10),
            ],
        )
    assert excinfo.value.duplicates == [(1, "a", 0)]


def test_response_preserves_input_order() -> None:
    report = allocate_bounded(
        50,
        [
            BoundedUnit("zeta", 1, 1, 0, 100),
            BoundedUnit("alpha", 1, 1, 0, 100),
            BoundedUnit("mike", 0, 0, 4, 4),
        ],
    )
    assert [s.unit_id for s in report.shares] == ["zeta", "alpha", "mike"]


def test_minimum_equals_maximum_pins_positive_weight_crew() -> None:
    report = allocate_bounded(
        50,
        [
            BoundedUnit("a", 1, 1, 10, 10),
            BoundedUnit("b", 1, 1, 0, 100),
        ],
    )
    by_id = _map(report)
    assert by_id["a"].final_cents == 10
    assert by_id["a"].amount_basis == BASIS_MINIMUM
    assert by_id["b"].final_cents == 40
    assert by_id["b"].amount_basis == BASIS_WEIGHTED


def test_randomized_invariants_and_oracle() -> None:
    rng = random.Random(20260915)
    for _ in range(500):
        count = rng.randint(1, 12)
        units: list[BoundedUnit] = []
        for i in range(count):
            watts = rng.choice([0, 0, rng.randint(1, 20)])
            minutes = rng.randint(0, 20)
            minimum = rng.randint(0, 80)
            maximum = minimum + rng.randint(0, 120)
            units.append(BoundedUnit(f"u{i:03d}", watts, minutes, minimum, maximum))
        weights = [u.watts * u.minutes for u in units]
        minimums = [u.minimum_cents for u in units]
        maximums = [u.maximum_cents for u in units]
        min_total = sum(minimums)
        cap_total = sum(
            maximums[i] if weights[i] > 0 else minimums[i] for i in range(count)
        )
        total = rng.randint(min_total, cap_total)

        report = allocate_bounded(total, units)
        shares = report.shares
        assert len(shares) == count
        assert report.allocated_cents == total
        assert report.remainder_cents_distributed < count

        expected_locked = _oracle_locked(weights, minimums, maximums, total)
        survivor_amount = 0
        survivors: set[int] = set()
        for i, share in enumerate(shares):
            assert share.weight == weights[i]
            assert minimums[i] <= share.final_cents <= maximums[i]
            if i in expected_locked:
                assert share.final_cents == maximums[i]
                assert share.amount_basis == BASIS_MAXIMUM
            elif weights[i] == 0 or minimums[i] == maximums[i]:
                assert share.final_cents == minimums[i]
                assert share.amount_basis == BASIS_MINIMUM
            else:
                survivors.add(i)
                survivor_amount += share.final_cents - minimums[i]
                if share.final_cents == minimums[i]:
                    assert share.amount_basis == BASIS_MINIMUM
                else:
                    assert share.amount_basis == BASIS_WEIGHTED

        # Survivors split the cents left after locked capacities by Hamilton.
        locked_capacity = sum(maximums[i] - minimums[i] for i in expected_locked)
        survivor_remaining = total - min_total - locked_capacity
        assert survivor_amount == max(survivor_remaining, 0)
        if survivors and survivor_remaining > 0:
            survivor_weight = sum(weights[i] for i in survivors)
            floors = {
                i: (survivor_remaining * weights[i]) // survivor_weight
                for i in survivors
            }
            leftover = survivor_remaining - sum(floors.values())
            order = sorted(
                survivors,
                key=lambda i: (
                    -(survivor_remaining * weights[i] % survivor_weight),
                    units[i].unit_id.encode("utf-8"),
                ),
            )
            awarded = set(order[:leftover])
            for i in survivors:
                expected = minimums[i] + floors[i] + (1 if i in awarded else 0)
                assert shares[i].final_cents == expected
