"""Uniform error envelope: every error response pinpoints the offending field(s).

Error body shape:

    {
      "detail": {
        "code": "MACHINE_READABLE_CODE",
        "message": "human readable summary",
        "fields": [{"loc": ["body", "units", 0, "watts"], "message": "..."}, ...]
      }
    }

``loc`` follows the JSON path of the offending value (list indices are
integers), so a cost accountant or an API client can locate every problem.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .allocator import DuplicateUnitIdError, ZeroTotalWeightError
from .adjustments import (
    AdjustmentDuplicateUnitIdError,
    AdjustmentZeroTotalWeightError,
    UnitSetMismatchError,
)


def _envelope(code: str, message: str, fields: list[dict[str, Any]]) -> dict[str, Any]:
    return {"detail": {"code": code, "message": message, "fields": fields}}


def register_error_handlers(app: FastAPI) -> None:
    """Attach every exception handler to the given FastAPI app."""

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        fields = [
            {
                "loc": list(err.get("loc", ())),
                "message": err.get("msg", "invalid value"),
                "type": err.get("type", "value_error"),
            }
            for err in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content=_envelope(
                "VALIDATION_ERROR",
                "Request failed validation; each entry in detail.fields locates one problem.",
                fields,
            ),
        )

    @app.exception_handler(DuplicateUnitIdError)
    async def duplicate_unit_id_handler(
        request: Request, exc: DuplicateUnitIdError
    ) -> JSONResponse:
        fields = [
            {
                "loc": ["body", "units", index, "unit_id"],
                "message": (
                    f"duplicate unit_id {unit_id!r}; "
                    f"first occurrence is units[{first}].unit_id"
                ),
            }
            for index, unit_id, first in exc.duplicates
        ]
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content=_envelope("DUPLICATE_UNIT_ID", str(exc), fields),
        )

    @app.exception_handler(ZeroTotalWeightError)
    async def zero_total_weight_handler(
        request: Request, exc: ZeroTotalWeightError
    ) -> JSONResponse:
        fields = [
            {
                "loc": ["body", "units"],
                "message": (
                    "every unit has watts*minutes == 0; at least one unit "
                    "must have positive watts and minutes"
                ),
            }
        ]
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content=_envelope("ZERO_TOTAL_WEIGHT", str(exc), fields),
        )

    @app.exception_handler(AdjustmentDuplicateUnitIdError)
    async def adjustment_duplicate_unit_id_handler(
        request: Request, exc: AdjustmentDuplicateUnitIdError
    ) -> JSONResponse:
        fields = [
            {
                "loc": ["body", array_name, index, "unit_id"],
                "message": (
                    f"duplicate unit_id {unit_id!r}; "
                    f"first occurrence is {array_name}[{first}].unit_id"
                ),
            }
            for array_name, index, unit_id, first in exc.occurrences
        ]
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content=_envelope("DUPLICATE_UNIT_ID", str(exc), fields),
        )

    @app.exception_handler(UnitSetMismatchError)
    async def unit_set_mismatch_handler(
        request: Request, exc: UnitSetMismatchError
    ) -> JSONResponse:
        fields = [{"loc": list(loc), "message": message} for loc, message in exc.fields]
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content=_envelope("UNIT_SET_MISMATCH", str(exc), fields),
        )

    @app.exception_handler(AdjustmentZeroTotalWeightError)
    async def adjustment_zero_total_weight_handler(
        request: Request, exc: AdjustmentZeroTotalWeightError
    ) -> JSONResponse:
        fields = [
            {
                "loc": ["body", exc.array_name],
                "message": (
                    f"every unit in {exc.array_name} has watts*minutes == 0; "
                    "at least one unit must have positive watts and minutes"
                ),
            }
        ]
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content=_envelope("ZERO_TOTAL_WEIGHT", str(exc), fields),
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        code = {
            status.HTTP_404_NOT_FOUND: "NOT_FOUND",
            status.HTTP_405_METHOD_NOT_ALLOWED: "METHOD_NOT_ALLOWED",
        }.get(exc.status_code, "HTTP_ERROR")
        message = exc.detail if isinstance(exc.detail, str) else "HTTP error"
        return JSONResponse(
            status_code=exc.status_code,
            headers=getattr(exc, "headers", None),
            content=_envelope(code, message, []),
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_envelope("INTERNAL_ERROR", "Unexpected server error.", []),
        )
