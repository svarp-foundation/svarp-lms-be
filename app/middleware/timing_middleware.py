import time
import logging
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger("svarp-lms.api")

# Threshold in seconds — anything above this is flagged [SLOW]
SLOW_REQUEST_THRESHOLD = 1.0


class TimingMiddleware(BaseHTTPMiddleware):
    """
    Middleware that tracks every API request with:
    - HTTP method & path
    - Response status code
    - Response time in milliseconds
    - User-Agent header
    - Flags requests exceeding SLOW_REQUEST_THRESHOLD with [SLOW]
    
    Adds X-Process-Time header to every response for client-side debugging.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        start_time = time.perf_counter()

        response = await call_next(request)

        process_time_sec = time.perf_counter() - start_time
        process_time_ms = round(process_time_sec * 1000, 2)

        # Add timing header to response
        response.headers["X-Process-Time"] = f"{process_time_ms}ms"

        # Build log line
        method = request.method
        path = request.url.path
        status_code = response.status_code
        user_agent = request.headers.get("user-agent", "-")

        # Skip noisy static file / health check requests from detailed logging
        if path.startswith("/static") or path == "/":
            return response

        slow_flag = " [SLOW]" if process_time_sec > SLOW_REQUEST_THRESHOLD else ""

        logger.info(
            "%s %s → %d | %sms%s | UA: %.60s",
            method,
            path,
            status_code,
            process_time_ms,
            slow_flag,
            user_agent,
        )

        return response
