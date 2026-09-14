"""HTTP acceptance tests for the fuel-cost allocation API.

When API_BASE_URL is set (the ``verify`` docker-compose service sets it to
http://api:8000) the tests exercise the live HTTP service; otherwise they run
in-process against the ASGI app via FastAPI's TestClient. Both modes speak
httpx, so the assertions are identical.
"""

from __future__ import annotations

import os
import time

import httpx
import pytest

API_BASE_URL = os.environ.get("API_BASE_URL", "").rstrip("/")


def _wait_until_healthy(client: httpx.Client, attempts: int = 60) -> None:
    for _ in range(attempts):
        try:
            if client.get("/health").status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(1)
    raise RuntimeError(f"API at {API_BASE_URL!r} did not become healthy in time")


@pytest.fixture(scope="session")
def client() -> httpx.Client:
    if API_BASE_URL:
        with httpx.Client(base_url=API_BASE_URL, timeout=10.0) as http_client:
            _wait_until_healthy(http_client)
            yield http_client
    else:
        from fastapi.testclient import TestClient

        from app.main import app

        with TestClient(app) as test_client:
            yield test_client


def _allocate(client: httpx.Client, payload: dict) -> httpx.Response:
    return client.post("/allocate", json=payload)


def _adjust(client: httpx.Client, payload: dict) -> httpx.Response:
    return client.post("/adjustments", json=payload)


def _by_unit_id(body: dict) -> dict:
    return {item["unit_id"]: item for item in body["allocations"]}


class TestHealth:
    def test_health(self, client: httpx.Client) -> None:
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


class TestHappyPath:
    def test_readme_example(self, client: httpx.Client) -> None:
        resp = _allocate(
            client,
            {
                "total_cents": 10000,
                "units": [
                    {"unit_id": "lighting", "watts": 2000, "minutes": 180},
                    {"unit_id": "camera", "watts": 800, "minutes": 150},
                    {"unit_id": "vfx", "watts": 500, "minutes": 96},
                ],
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_cents"] == 10000
        assert body["total_weight"] == 528000
        assert body["allocated_cents"] == 10000
        assert body["remainder_cents_distributed"] == 1
        by_id = _by_unit_id(body)
        # exact shares: 6818.18.. / 2272.72.. / 909.09.. -> camera has the
        # largest fractional remainder and receives the one leftover cent.
        assert by_id["lighting"] == {
            "unit_id": "lighting",
            "watts": 2000,
            "minutes": 180,
            "weight": 360000,
            "floor_cents": 6818,
            "remainder_awarded": False,
            "final_cents": 6818,
        }
        assert by_id["camera"] == {
            "unit_id": "camera",
            "watts": 800,
            "minutes": 150,
            "weight": 120000,
            "floor_cents": 2272,
            "remainder_awarded": True,
            "final_cents": 2273,
        }
        assert by_id["vfx"] == {
            "unit_id": "vfx",
            "watts": 500,
            "minutes": 96,
            "weight": 48000,
            "floor_cents": 909,
            "remainder_awarded": False,
            "final_cents": 909,
        }

    def test_exact_split_without_remainder(self, client: httpx.Client) -> None:
        resp = _allocate(
            client,
            {
                "total_cents": 900,
                "units": [
                    {"unit_id": "alpha", "watts": 2, "minutes": 30},
                    {"unit_id": "bravo", "watts": 1, "minutes": 30},
                ],
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        by_id = _by_unit_id(body)
        assert by_id["alpha"]["weight"] == 60
        assert by_id["alpha"]["floor_cents"] == 600
        assert by_id["alpha"]["remainder_awarded"] is False
        assert by_id["alpha"]["final_cents"] == 600
        assert by_id["bravo"]["final_cents"] == 300
        assert body["remainder_cents_distributed"] == 0
        assert body["allocated_cents"] == 900

    def test_largest_remainder_wins_the_cent(self, client: httpx.Client) -> None:
        # weights 1,2,4 over 100 cents -> floors 14,28,57 with fractional
        # remainders 2/7, 4/7, 1/7: only "b" earns the leftover cent.
        resp = _allocate(
            client,
            {
                "total_cents": 100,
                "units": [
                    {"unit_id": "a", "watts": 1, "minutes": 1},
                    {"unit_id": "b", "watts": 2, "minutes": 1},
                    {"unit_id": "c", "watts": 4, "minutes": 1},
                ],
            },
        )
        assert resp.status_code == 200
        by_id = _by_unit_id(resp.json())
        assert (by_id["a"]["floor_cents"], by_id["a"]["remainder_awarded"], by_id["a"]["final_cents"]) == (14, False, 14)
        assert (by_id["b"]["floor_cents"], by_id["b"]["remainder_awarded"], by_id["b"]["final_cents"]) == (28, True, 29)
        assert (by_id["c"]["floor_cents"], by_id["c"]["remainder_awarded"], by_id["c"]["final_cents"]) == (57, False, 57)

    def test_tie_breaks_by_unit_id_utf8_ascending(self, client: httpx.Client) -> None:
        resp = _allocate(
            client,
            {
                "total_cents": 1,
                "units": [
                    {"unit_id": "zed", "watts": 5, "minutes": 2},
                    {"unit_id": "alpha", "watts": 5, "minutes": 2},
                ],
            },
        )
        assert resp.status_code == 200
        by_id = _by_unit_id(resp.json())
        assert by_id["alpha"]["final_cents"] == 1
        assert by_id["zed"]["final_cents"] == 0

    def test_tie_break_uses_utf8_bytes_for_non_ascii(self, client: httpx.Client) -> None:
        # UTF-8 bytes of "zebra" (0x7A...) sort before "équipe" (0xC3...).
        resp = _allocate(
            client,
            {
                "total_cents": 1,
                "units": [
                    {"unit_id": "équipe", "watts": 7, "minutes": 7},
                    {"unit_id": "zebra", "watts": 7, "minutes": 7},
                ],
            },
        )
        assert resp.status_code == 200
        by_id = _by_unit_id(resp.json())
        assert by_id["zebra"]["final_cents"] == 1
        assert by_id["équipe"]["final_cents"] == 0

    def test_zero_total_cents(self, client: httpx.Client) -> None:
        resp = _allocate(
            client,
            {"total_cents": 0, "units": [{"unit_id": "a", "watts": 3, "minutes": 5}]},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["allocated_cents"] == 0
        assert body["remainder_cents_distributed"] == 0
        only = body["allocations"][0]
        assert only["floor_cents"] == 0
        assert only["remainder_awarded"] is False
        assert only["final_cents"] == 0

    def test_zero_weight_unit_gets_nothing(self, client: httpx.Client) -> None:
        resp = _allocate(
            client,
            {
                "total_cents": 61,
                "units": [
                    {"unit_id": "idle", "watts": 0, "minutes": 90},
                    {"unit_id": "busy", "watts": 2, "minutes": 3},
                ],
            },
        )
        assert resp.status_code == 200
        by_id = _by_unit_id(resp.json())
        assert by_id["idle"]["weight"] == 0
        assert by_id["idle"]["final_cents"] == 0
        assert by_id["idle"]["remainder_awarded"] is False
        assert by_id["busy"]["final_cents"] == 61

    def test_response_preserves_input_order(self, client: httpx.Client) -> None:
        resp = _allocate(
            client,
            {
                "total_cents": 10,
                "units": [
                    {"unit_id": "zeta", "watts": 1, "minutes": 1},
                    {"unit_id": "alpha", "watts": 1, "minutes": 1},
                    {"unit_id": "mike", "watts": 1, "minutes": 1},
                ],
            },
        )
        assert resp.status_code == 200
        assert [a["unit_id"] for a in resp.json()["allocations"]] == ["zeta", "alpha", "mike"]

    def test_allocation_is_deterministic(self, client: httpx.Client) -> None:
        payload = {
            "total_cents": 123456789,
            "units": [
                {"unit_id": f"crew-{i}", "watts": (i * 37) % 500, "minutes": (i * 91) % 240 + 1}
                for i in range(50)
            ],
        }
        first = _allocate(client, payload)
        second = _allocate(client, payload)
        assert first.status_code == 200
        assert first.json() == second.json()

    @pytest.mark.parametrize(
        "total_cents,units",
        [
            (100, [{"unit_id": u, "watts": 1, "minutes": 1} for u in ("a", "b", "c")]),
            (7, [{"unit_id": "c", "watts": 3, "minutes": 1},
                 {"unit_id": "a", "watts": 0, "minutes": 5},
                 {"unit_id": "b", "watts": 3, "minutes": 1}]),
            (999_999_999_999, [{"unit_id": "x", "watts": 7, "minutes": 1},
                               {"unit_id": "y", "watts": 13, "minutes": 1},
                               {"unit_id": "z", "watts": 999983, "minutes": 1}]),
            (12345, [{"unit_id": f"u{i:03d}", "watts": 1, "minutes": 1} for i in range(500)]),
        ],
        ids=["equal-thirds", "zero-weight-mixed", "huge-amounts", "many-units"],
    )
    def test_sum_invariant_always_holds(self, client: httpx.Client, total_cents, units) -> None:
        resp = _allocate(client, {"total_cents": total_cents, "units": units})
        assert resp.status_code == 200
        body = resp.json()
        allocs = body["allocations"]
        assert len(allocs) == len(units)
        # The accounting invariant: integer cents that always sum to the bill.
        assert all(isinstance(a["final_cents"], int) for a in allocs)
        assert sum(a["final_cents"] for a in allocs) == total_cents
        assert body["allocated_cents"] == total_cents
        # Cross-check every row against independently recomputed expectations.
        weights = [u["watts"] * u["minutes"] for u in units]
        total_weight = sum(weights)
        expected_floors = [(total_cents * w) // total_weight for w in weights]
        awarded = 0
        for alloc, floor in zip(allocs, expected_floors):
            assert alloc["weight"] == alloc["watts"] * alloc["minutes"]
            assert alloc["floor_cents"] == floor
            assert alloc["final_cents"] == floor + (1 if alloc["remainder_awarded"] else 0)
            awarded += 1 if alloc["remainder_awarded"] else 0
        assert awarded == body["remainder_cents_distributed"]
        assert awarded == total_cents - sum(expected_floors)


class TestValidationErrors:
    @pytest.mark.parametrize("bad_total", [-1, 10.5, 10.0, "100", True, None, [1], {"x": 1}])
    def test_invalid_total_cents_rejected(self, client: httpx.Client, bad_total) -> None:
        resp = _allocate(
            client, {"total_cents": bad_total, "units": [{"unit_id": "a", "watts": 1, "minutes": 1}]}
        )
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert detail["code"] == "VALIDATION_ERROR"
        assert any(f["loc"][:2] == ["body", "total_cents"] for f in detail["fields"])

    @pytest.mark.parametrize(
        "field,value",
        [("watts", -1), ("minutes", -5), ("watts", 1.5), ("minutes", "60"), ("watts", True)],
    )
    def test_invalid_unit_numbers_rejected(self, client: httpx.Client, field, value) -> None:
        unit = {"unit_id": "a", "watts": 1, "minutes": 1, field: value}
        resp = _allocate(client, {"total_cents": 10, "units": [unit]})
        assert resp.status_code == 422
        fields = resp.json()["detail"]["fields"]
        assert any(f["loc"] == ["body", "units", 0, field] for f in fields)

    def test_missing_unit_field_located(self, client: httpx.Client) -> None:
        resp = _allocate(client, {"total_cents": 10, "units": [{"watts": 1, "minutes": 1}]})
        assert resp.status_code == 422
        fields = resp.json()["detail"]["fields"]
        assert any(f["loc"] == ["body", "units", 0, "unit_id"] for f in fields)

    def test_empty_unit_id_rejected(self, client: httpx.Client) -> None:
        resp = _allocate(
            client, {"total_cents": 10, "units": [{"unit_id": "", "watts": 1, "minutes": 1}]}
        )
        assert resp.status_code == 422
        fields = resp.json()["detail"]["fields"]
        assert any(f["loc"] == ["body", "units", 0, "unit_id"] for f in fields)

    def test_non_string_unit_id_rejected(self, client: httpx.Client) -> None:
        resp = _allocate(
            client, {"total_cents": 10, "units": [{"unit_id": 123, "watts": 1, "minutes": 1}]}
        )
        assert resp.status_code == 422
        fields = resp.json()["detail"]["fields"]
        assert any(f["loc"] == ["body", "units", 0, "unit_id"] for f in fields)

    def test_empty_units_rejected(self, client: httpx.Client) -> None:
        resp = _allocate(client, {"total_cents": 10, "units": []})
        assert resp.status_code == 422
        fields = resp.json()["detail"]["fields"]
        assert any(f["loc"] == ["body", "units"] for f in fields)

    def test_missing_units_rejected(self, client: httpx.Client) -> None:
        resp = _allocate(client, {"total_cents": 10})
        assert resp.status_code == 422
        fields = resp.json()["detail"]["fields"]
        assert any(f["loc"] == ["body", "units"] for f in fields)

    def test_extra_field_rejected_and_located(self, client: httpx.Client) -> None:
        resp = _allocate(
            client,
            {"total_cents": 10, "units": [{"unit_id": "a", "watts": 1, "minutes": 1, "foo": 1}]},
        )
        assert resp.status_code == 422
        fields = resp.json()["detail"]["fields"]
        assert any(f["loc"] == ["body", "units", 0, "foo"] for f in fields)

    def test_malformed_json_located(self, client: httpx.Client) -> None:
        resp = client.post(
            "/allocate", content=b"{not json", headers={"Content-Type": "application/json"}
        )
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert detail["code"] == "VALIDATION_ERROR"
        assert any(f["loc"][0] == "body" for f in detail["fields"])

    def test_error_envelope_shape(self, client: httpx.Client) -> None:
        resp = _allocate(client, {"total_cents": -3, "units": []})
        assert resp.status_code == 422
        assert resp.headers["content-type"].startswith("application/json")
        detail = resp.json()["detail"]
        assert set(detail) >= {"code", "message", "fields"}
        assert isinstance(detail["fields"], list) and detail["fields"]
        for field in detail["fields"]:
            assert "loc" in field and "message" in field


class TestBusinessRules:
    def test_duplicate_unit_id_rejected_and_located(self, client: httpx.Client) -> None:
        resp = _allocate(
            client,
            {
                "total_cents": 10,
                "units": [
                    {"unit_id": "a", "watts": 1, "minutes": 1},
                    {"unit_id": "b", "watts": 1, "minutes": 1},
                    {"unit_id": "a", "watts": 2, "minutes": 2},
                ],
            },
        )
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert detail["code"] == "DUPLICATE_UNIT_ID"
        locs = [f["loc"] for f in detail["fields"]]
        assert ["body", "units", 2, "unit_id"] in locs

    def test_every_duplicate_occurrence_reported(self, client: httpx.Client) -> None:
        resp = _allocate(
            client,
            {
                "total_cents": 10,
                "units": [
                    {"unit_id": "a", "watts": 1, "minutes": 1},
                    {"unit_id": "a", "watts": 1, "minutes": 1},
                    {"unit_id": "b", "watts": 1, "minutes": 1},
                    {"unit_id": "b", "watts": 1, "minutes": 1},
                ],
            },
        )
        assert resp.status_code == 400
        locs = [f["loc"] for f in resp.json()["detail"]["fields"]]
        assert ["body", "units", 1, "unit_id"] in locs
        assert ["body", "units", 3, "unit_id"] in locs

    def test_all_zero_weights_rejected(self, client: httpx.Client) -> None:
        resp = _allocate(
            client,
            {
                "total_cents": 10,
                "units": [
                    {"unit_id": "a", "watts": 0, "minutes": 0},
                    {"unit_id": "b", "watts": 0, "minutes": 5},
                ],
            },
        )
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert detail["code"] == "ZERO_TOTAL_WEIGHT"
        assert any(f["loc"] == ["body", "units"] for f in detail["fields"])

    def test_single_zero_weight_unit_rejected(self, client: httpx.Client) -> None:
        resp = _allocate(
            client, {"total_cents": 10, "units": [{"unit_id": "a", "watts": 0, "minutes": 0}]}
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["code"] == "ZERO_TOTAL_WEIGHT"


class TestRoutingErrors:
    def test_unknown_route(self, client: httpx.Client) -> None:
        resp = client.get("/does-not-exist")
        assert resp.status_code == 404
        assert resp.json()["detail"]["code"] == "NOT_FOUND"

    def test_wrong_method(self, client: httpx.Client) -> None:
        resp = client.get("/allocate")
        assert resp.status_code == 405
        assert resp.json()["detail"]["code"] == "METHOD_NOT_ALLOWED"


class TestAdjustments:
    def _payload(self, original, corrected, total_cents=10):
        return {
            "total_cents": total_cents,
            "original_units": [
                {"unit_id": uid, "watts": w, "minutes": m} for uid, w, m in original
            ],
            "corrected_units": [
                {"unit_id": uid, "watts": w, "minutes": m} for uid, w, m in corrected
            ],
        }

    def test_corrected_readings_produce_offsetting_deltas(self, client: httpx.Client) -> None:
        # total 10: 50/50 -> 2:1 split (the heavier crew also wins Hamilton's
        # leftover cent): +2 for "a", -2 for "b", net zero.
        resp = _adjust(
            client,
            self._payload(
                [("a", 1, 1), ("b", 1, 1)],
                [("a", 2, 1), ("b", 1, 1)],
            ),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_cents"] == 10
        assert body["original_total_weight"] == 2
        assert body["corrected_total_weight"] == 3
        by_id = {item["unit_id"]: item for item in body["adjustments"]}
        assert by_id["a"] == {
            "unit_id": "a",
            "original_cents": 5,
            "corrected_cents": 7,
            "adjustment_cents": 2,
        }
        assert by_id["b"] == {
            "unit_id": "b",
            "original_cents": 5,
            "corrected_cents": 3,
            "adjustment_cents": -2,
        }
        assert body["total_adjustment_cents"] == 0
        assert sum(item["adjustment_cents"] for item in body["adjustments"]) == 0

    def test_unchanged_readings_yield_zero_adjustments(self, client: httpx.Client) -> None:
        readings = [("lighting", 2000, 180), ("camera", 800, 150), ("vfx", 500, 96)]
        resp = _adjust(client, self._payload(readings, readings, total_cents=10000))
        assert resp.status_code == 200
        body = resp.json()
        assert body["original_total_weight"] == body["corrected_total_weight"] == 528000
        assert body["total_adjustment_cents"] == 0
        assert len(body["adjustments"]) == 3
        assert all(item["adjustment_cents"] == 0 for item in body["adjustments"])
        assert all(
            item["original_cents"] == item["corrected_cents"] for item in body["adjustments"]
        )

    def test_result_matches_two_allocate_calls(self, client: httpx.Client) -> None:
        payload = self._payload(
            [("a", 2, 3), ("b", 1, 6), ("c", 5, 5)],
            [("a", 2, 4), ("b", 1, 6), ("c", 4, 5)],
            total_cents=9999,
        )
        resp = _adjust(client, payload)
        assert resp.status_code == 200
        by_id = {item["unit_id"]: item for item in resp.json()["adjustments"]}
        for array_name in ("original_units", "corrected_units"):
            allocate_resp = _allocate(
                client,
                {
                    "total_cents": payload["total_cents"],
                    "units": payload[array_name],
                },
            )
            assert allocate_resp.status_code == 200
            for allocation in allocate_resp.json()["allocations"]:
                item = by_id[allocation["unit_id"]]
                key = "original_cents" if array_name == "original_units" else "corrected_cents"
                assert item[key] == allocation["final_cents"]
        assert resp.json()["total_adjustment_cents"] == 0

    def test_response_order_follows_original_units(self, client: httpx.Client) -> None:
        resp = _adjust(
            client,
            self._payload(
                [("zeta", 1, 1), ("alpha", 2, 1), ("mike", 3, 1)],
                [("mike", 3, 1), ("zeta", 1, 1), ("alpha", 2, 1)],
                total_cents=100,
            ),
        )
        assert resp.status_code == 200
        assert [item["unit_id"] for item in resp.json()["adjustments"]] == [
            "zeta",
            "alpha",
            "mike",
        ]

    def test_missing_unit_id_locates_both_array_elements(self, client: httpx.Client) -> None:
        # 'b' exists only in original_units, 'c' only in corrected_units.
        resp = _adjust(
            client,
            self._payload(
                [("a", 1, 1), ("b", 1, 1)],
                [("a", 2, 2), ("c", 1, 1)],
            ),
        )
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert detail["code"] == "UNIT_SET_MISMATCH"
        locs = [f["loc"] for f in detail["fields"]]
        assert ["body", "original_units", 1] in locs
        assert ["body", "corrected_units", 1] in locs

    def test_extra_unit_id_is_located_in_corrected_array(self, client: httpx.Client) -> None:
        resp = _adjust(
            client,
            self._payload(
                [("a", 1, 1)],
                [("a", 1, 1), ("b", 1, 1)],
            ),
        )
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert detail["code"] == "UNIT_SET_MISMATCH"
        locs = [f["loc"] for f in detail["fields"]]
        assert ["body", "corrected_units", 1] in locs
        assert all(loc[1] != "original_units" for loc in locs)

    def test_duplicate_unit_id_in_either_array_is_located(self, client: httpx.Client) -> None:
        resp = _adjust(
            client,
            {
                "total_cents": 10,
                "original_units": [
                    {"unit_id": "a", "watts": 1, "minutes": 1},
                    {"unit_id": "b", "watts": 1, "minutes": 1},
                ],
                "corrected_units": [
                    {"unit_id": "a", "watts": 1, "minutes": 1},
                    {"unit_id": "a", "watts": 2, "minutes": 2},
                ],
            },
        )
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert detail["code"] == "DUPLICATE_UNIT_ID"
        assert ["body", "corrected_units", 1, "unit_id"] in [
            f["loc"] for f in detail["fields"]
        ]

    def test_zero_weight_in_either_version_rejected(self, client: httpx.Client) -> None:
        base = {
            "total_cents": 10,
            "original_units": [
                {"unit_id": "a", "watts": 1, "minutes": 1},
                {"unit_id": "b", "watts": 2, "minutes": 2},
            ],
            "corrected_units": [
                {"unit_id": "a", "watts": 0, "minutes": 5},
                {"unit_id": "b", "watts": 3, "minutes": 0},
            ],
        }
        resp = _adjust(client, base)
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert detail["code"] == "ZERO_TOTAL_WEIGHT"
        assert any(
            f["loc"] == ["body", "corrected_units"] for f in detail["fields"]
        )

    @pytest.mark.parametrize("bad_total", [-1, 10.5, "100", True, None])
    def test_strict_integer_total_rejected(self, client: httpx.Client, bad_total) -> None:
        payload = self._payload([("a", 1, 1)], [("a", 2, 2)])
        payload["total_cents"] = bad_total
        resp = _adjust(client, payload)
        assert resp.status_code == 422
        fields = resp.json()["detail"]["fields"]
        assert any(f["loc"][:2] == ["body", "total_cents"] for f in fields)

    @pytest.mark.parametrize(
        "array_name,field,value",
        [
            ("original_units", "watts", -1),
            ("corrected_units", "minutes", 1.5),
            ("original_units", "watts", "5"),
            ("corrected_units", "minutes", True),
        ],
    )
    def test_strict_integer_readings_rejected(
        self, client: httpx.Client, array_name, field, value
    ) -> None:
        payload = self._payload([("a", 1, 1)], [("a", 2, 2)])
        payload[array_name][0][field] = value
        resp = _adjust(client, payload)
        assert resp.status_code == 422
        fields = resp.json()["detail"]["fields"]
        assert any(f["loc"] == ["body", array_name, 0, field] for f in fields)

    def test_empty_readings_array_rejected(self, client: httpx.Client) -> None:
        resp = _adjust(
            client,
            {
                "total_cents": 10,
                "original_units": [],
                "corrected_units": [{"unit_id": "a", "watts": 1, "minutes": 1}],
            },
        )
        assert resp.status_code == 422
        fields = resp.json()["detail"]["fields"]
        assert any(f["loc"] == ["body", "original_units"] for f in fields)

    def test_missing_readings_array_rejected(self, client: httpx.Client) -> None:
        resp = _adjust(
            client,
            {
                "total_cents": 10,
                "original_units": [{"unit_id": "a", "watts": 1, "minutes": 1}],
            },
        )
        assert resp.status_code == 422
        fields = resp.json()["detail"]["fields"]
        assert any(f["loc"] == ["body", "corrected_units"] for f in fields)

    def test_extra_field_rejected_and_located(self, client: httpx.Client) -> None:
        payload = self._payload([("a", 1, 1)], [("a", 2, 2)])
        payload["original_units"][0]["unexpected"] = 9
        resp = _adjust(client, payload)
        assert resp.status_code == 422
        fields = resp.json()["detail"]["fields"]
        assert any(
            f["loc"] == ["body", "original_units", 0, "unexpected"] for f in fields
        )

    def test_malformed_json_located(self, client: httpx.Client) -> None:
        resp = client.post(
            "/adjustments",
            content=b"{not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert detail["code"] == "VALIDATION_ERROR"
        assert any(f["loc"][0] == "body" for f in detail["fields"])

    def test_get_on_adjustments_not_allowed(self, client: httpx.Client) -> None:
        resp = client.get("/adjustments")
        assert resp.status_code == 405
        assert resp.json()["detail"]["code"] == "METHOD_NOT_ALLOWED"
