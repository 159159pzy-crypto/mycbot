"""Request correlation ID propagation for logs and responses."""

import re
from contextvars import ContextVar
from uuid import uuid4

import structlog
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

CORRELATION_HEADER = "X-Correlation-ID"
CORRELATION_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
correlation_id_var: ContextVar[str | None] = ContextVar("correlation_id", default=None)


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        supplied = request.headers.get(CORRELATION_HEADER, "")
        correlation_id = supplied if CORRELATION_PATTERN.fullmatch(supplied) else str(uuid4())
        token = correlation_id_var.set(correlation_id)
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(correlation_id=correlation_id)
        try:
            try:
                response = await call_next(request)
            except Exception:
                structlog.get_logger("mybot.http").exception("unhandled_request_error")
                response = JSONResponse(
                    status_code=500,
                    content={"detail": "Internal Server Error"},
                )
            response.headers[CORRELATION_HEADER] = correlation_id
            return response
        finally:
            structlog.contextvars.clear_contextvars()
            correlation_id_var.reset(token)
