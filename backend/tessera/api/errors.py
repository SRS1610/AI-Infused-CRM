"""Map kernel exceptions to JSON responses (never leak internals for unexpected errors)."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError

from tessera.exceptions import TesseraError, UserError
from tessera.logging import get_logger
from tessera.runtime import current_request_id

log = get_logger(__name__)


def _body(error: str, message: str, status: int, **extra: Any) -> JSONResponse:
    payload: dict[str, Any] = {"error": error, "message": message, "request_id": current_request_id()}
    payload.update({k: v for k, v in extra.items() if v is not None})
    return JSONResponse(status_code=status, content=payload)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(UserError)
    async def _user_error(request: Request, exc: UserError) -> JSONResponse:
        return _body(type(exc).__name__, exc.message, exc.http_status, **exc.context)

    @app.exception_handler(TesseraError)
    async def _kernel_error(request: Request, exc: TesseraError) -> JSONResponse:
        log.error("kernel error", error=type(exc).__name__, message=exc.message)
        return _body("ServerError", "Internal error", 500)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        return _body("RequestValidationError", "Invalid request", 422, details=exc.errors())

    @app.exception_handler(OperationalError)
    async def _db_down(request: Request, exc: OperationalError) -> JSONResponse:
        log.error("database unavailable", error=str(exc)[:300])
        return _body("ServiceUnavailable", "Database unavailable", 503)

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error")
        return _body("ServerError", "Internal error", 500)
