"""Pure unit tests for the adjustment orchestration (no HTTP involved)."""

from __future__ import annotations

import random

import pytest

from app.adjustments import (
    AdjustmentDuplicateUnitIdError,
    AdjustmentZeroTotalWeightError,
    UnitSetMismatchError,
    build_adjustments,
)
from app.allocator import UnitUsage


def _adjustment_map(report):
    return {item.unit_id: item for item in report.adjustments}


def test_deltas_sum_to_zero_randomized() -> None:
    rng = random.Random(20260914)
    for _ in range(300):
        count = rng.randint(1, 30)
        unit_ids = [f"u{i}" for i in range(count)]
        original = [
            UnitUsage(uid, rng.randint(0, 5000), rng.randint(0, 500)) for uid in unit_ids
        ]
        if all(u.watts * u.minutes == 0 for u in original):
            original[0] = UnitUsage(unit_ids[0], 1, 1)
        # Shuffled, independently re-rolled readings, same unit_id set.
        corrected = [
            UnitUsage(uid, rng.randint(0, 5000), rng.randint(0, 500)) for uid in unit_ids
        ]
        if all(u.watts * u.minutes == 0 for u in corrected):
            corrected[0] = UnitUsage(unit_ids[0], 1, 1)
        rng.shuffle(corrected)
        total = rng.randint(0, 10**9)

        report = build_adjustments(total, original, corrected)
        by_id = _adjustment_map(report)
        assert set(by_id) == set(unit_ids)
        assert report.total_adjustment_cents == 0
        assert sum(item.original_cents for item in report.adjustments) == total
        assert sum(item.corrected_cents for item in report.adjustments) == total
        for item in report.adjustments:
            assert item.adjustment_cents == item.corrected_cents - item.original_cents
        assert [item.unit_id for item in report.adjustments] == unit_ids


def test_positive_and_negative_deltas_sum_to_zero() -> None:
    # total 10: original 50/50 split, corrected 2:1 split (largest remainder
    # gives the leftover cent to the heavier unit).
    report = build_adjustments(
        10,
        [UnitUsage("a", 1, 1), UnitUsage("b", 1, 1)],
        [UnitUsage("a", 2, 1), UnitUsage("b", 1, 1)],
    )
    by_id = _adjustment_map(report)
    assert by_id["a"].original_cents == 5
    assert by_id["a"].corrected_cents == 7
    assert by_id["a"].adjustment_cents == 2
    assert by_id["b"].original_cents == 5
    assert by_id["b"].corrected_cents == 3
    assert by_id["b"].adjustment_cents == -2
    assert report.total_adjustment_cents == 0
    assert report.original_total_weight == 2
    assert report.corrected_total_weight == 3


def test_unchanged_readings_yield_all_zero_adjustments() -> None:
    units = [UnitUsage("a", 3, 2), UnitUsage("b", 1, 4), UnitUsage("c", 5, 1)]
    report = build_adjustments(12345, units, list(units))
    assert report.total_adjustment_cents == 0
    for item in report.adjustments:
        assert item.original_cents == item.corrected_cents
        assert item.adjustment_cents == 0


def test_adjustments_follow_original_order_and_ignore_corrected_order() -> None:
    original = [UnitUsage("zeta", 1, 1), UnitUsage("alpha", 2, 1), UnitUsage("mike", 3, 1)]
    corrected = [
        UnitUsage("mike", 3, 1),
        UnitUsage("zeta", 1, 1),
        UnitUsage("alpha", 2, 1),
    ]
    report = build_adjustments(100, original, corrected)
    assert [item.unit_id for item in report.adjustments] == ["zeta", "alpha", "mike"]
    assert report.total_adjustment_cents == 0


def test_missing_unit_id_is_located_in_original_array() -> None:
    with pytest.raises(UnitSetMismatchError) as excinfo:
        build_adjustments(
            10,
            [UnitUsage("a", 1, 1), UnitUsage("b", 1, 1)],
            [UnitUsage("a", 2, 2), UnitUsage("c", 1, 1)],
        )
    locs = {loc for loc, _ in excinfo.value.fields}
    assert ("body", "original_units", 1) in locs  # 'b' missing from corrected
    assert ("body", "corrected_units", 1) in locs  # 'c' has no original match


def test_extra_corrected_unit_is_located() -> None:
    with pytest.raises(UnitSetMismatchError) as excinfo:
        build_adjustments(
            10,
            [UnitUsage("a", 1, 1)],
            [UnitUsage("a", 1, 1), UnitUsage("b", 1, 1)],
        )
    locs, messages = zip(*excinfo.value.fields)
    assert ("body", "corrected_units", 1) in locs
    assert any("'b'" in message for message in messages)


@pytest.mark.parametrize(
    "array_name,units",
    [
        ("original_units", [UnitUsage("a", 1, 1), UnitUsage("a", 2, 2)]),
        ("corrected_units", [UnitUsage("x", 1, 1), UnitUsage("x", 2, 2)]),
    ],
)
def test_duplicate_within_either_array_is_located(array_name, units) -> None:
    other = [UnitUsage("a", 1, 1), UnitUsage("b", 1, 1)]
    kwargs = {
        "original_units": units if array_name == "original_units" else other,
        "corrected_units": units if array_name == "corrected_units" else other,
    }
    with pytest.raises(AdjustmentDuplicateUnitIdError) as excinfo:
        build_adjustments(10, **kwargs)
    array, index, unit_id, first = excinfo.value.occurrences[0]
    assert array == array_name
    assert index == 1
    assert unit_id in ("a", "x")
    assert first == 0


def test_duplicate_check_runs_before_set_mismatch_check() -> None:
    # Even though the sets also differ, the duplicate must surface first and
    # the batch must still be rejected wholesale.
    with pytest.raises(AdjustmentDuplicateUnitIdError):
        build_adjustments(
            10,
            [UnitUsage("a", 1, 1), UnitUsage("a", 1, 1)],
            [UnitUsage("a", 1, 1), UnitUsage("b", 1, 1)],
        )


def test_zero_weight_in_either_version_rejects_whole_batch() -> None:
    good = [UnitUsage("a", 1, 1), UnitUsage("b", 2, 2)]
    bad = [UnitUsage("a", 0, 5), UnitUsage("b", 3, 0)]
    with pytest.raises(AdjustmentZeroTotalWeightError) as excinfo:
        build_adjustments(10, bad, good)
    assert excinfo.value.array_name == "original_units"
    with pytest.raises(AdjustmentZeroTotalWeightError) as excinfo:
        build_adjustments(10, good, bad)
    assert excinfo.value.array_name == "corrected_units"


def test_set_mismatch_takes_precedence_over_zero_weight() -> None:
    with pytest.raises(UnitSetMismatchError):
        build_adjustments(
            10,
            [UnitUsage("a", 0, 0)],
            [UnitUsage("b", 1, 1)],
        )


def test_amounts_match_two_independent_allocations() -> None:
    from app.allocator import allocate

    original = [UnitUsage("a", 2, 3), UnitUsage("b", 1, 6), UnitUsage("c", 5, 5)]
    corrected = [UnitUsage("c", 4, 5), UnitUsage("a", 2, 4), UnitUsage("b", 1, 6)]
    total = 9999
    report = build_adjustments(total, original, corrected)
    first = allocate(total, original)
    second = allocate(total, corrected)
    first_by_id = {s.unit_id: s.final_cents for s in first.shares}
    second_by_id = {s.unit_id: s.final_cents for s in second.shares}
    for item in report.adjustments:
        assert item.original_cents == first_by_id[item.unit_id]
        assert item.corrected_cents == second_by_id[item.unit_id]
        assert item.adjustment_cents == second_by_id[item.unit_id] - first_by_id[item.unit_id]
    assert report.total_adjustment_cents == 0
