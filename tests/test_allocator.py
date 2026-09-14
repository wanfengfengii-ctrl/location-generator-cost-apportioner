"""Pure unit tests for the largest-remainder allocator (no HTTP involved)."""

from __future__ import annotations

import random

import pytest

from app.allocator import (
    DuplicateUnitIdError,
    UnitUsage,
    ZeroTotalWeightError,
    allocate,
)


def test_sum_invariant_randomized() -> None:
    rng = random.Random(20260914)
    for _ in range(300):
        count = rng.randint(1, 30)
        units = [
            UnitUsage(f"u{i}", rng.randint(0, 5000), rng.randint(0, 500)) for i in range(count)
        ]
        if all(u.watts * u.minutes == 0 for u in units):
            units[0] = UnitUsage("u0", 1, 1)
        total = rng.randint(0, 10**9)
        report = allocate(total, units)

        assert report.allocated_cents == total
        assert sum(s.remainder_awarded for s in report.shares) == report.remainder_cents_distributed
        assert report.remainder_cents_distributed < len(units)
        for share in report.shares:
            assert share.weight == share.watts * share.minutes
            assert share.final_cents == share.floor_cents + (1 if share.remainder_awarded else 0)


def test_remainder_goes_to_largest_fraction() -> None:
    report = allocate(
        100,
        [UnitUsage("a", 1, 1), UnitUsage("b", 2, 1), UnitUsage("c", 4, 1)],
    )
    finals = {s.unit_id: s.final_cents for s in report.shares}
    assert finals == {"a": 14, "b": 29, "c": 57}


def test_tie_break_prefers_utf8_byte_order() -> None:
    # Equal weights -> equal remainders; "zebra" (0x7A...) < "équipe" (0xC3...)
    # in UTF-8 byte order, so "zebra" wins the single cent.
    report = allocate(1, [UnitUsage("équipe", 7, 7), UnitUsage("zebra", 7, 7)])
    finals = {s.unit_id: s.final_cents for s in report.shares}
    assert finals == {"zebra": 1, "équipe": 0}


def test_zero_total_cents_gives_zero_to_everyone() -> None:
    report = allocate(0, [UnitUsage("a", 5, 5), UnitUsage("b", 1, 1)])
    assert all(s.final_cents == 0 and not s.remainder_awarded for s in report.shares)
    assert report.remainder_cents_distributed == 0


def test_duplicate_unit_id_raises_with_all_occurrences() -> None:
    with pytest.raises(DuplicateUnitIdError) as excinfo:
        allocate(
            10,
            [
                UnitUsage("a", 1, 1),
                UnitUsage("a", 1, 1),
                UnitUsage("b", 1, 1),
                UnitUsage("b", 1, 1),
            ],
        )
    assert excinfo.value.duplicates == [(1, "a", 0), (3, "b", 2)]


def test_zero_total_weight_raises() -> None:
    with pytest.raises(ZeroTotalWeightError):
        allocate(10, [UnitUsage("a", 0, 5), UnitUsage("b", 3, 0)])


def test_empty_batch_raises() -> None:
    with pytest.raises(ZeroTotalWeightError):
        allocate(10, [])
