"""AVSA API - application entry point. Mounts routers; no inline logic."""

import logging
import os
import time as _time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from avsa_core.config import load_config, load_config_raw
from avsa_core.storage import _build_backend
from fastapi import FastAPI
from prometheus_fastapi_instrumentator import Instrumentator

from avsa_api.clients.orchestrator import OrchestratorClient
from avsa_api.middleware.rate_limit import SlidingWindowLimiter
from avsa_api.routes.catalog import router as catalog_router
from avsa_api.routes.chat import router as chat_router
from avsa_api.routes.health import router as health_router
from avsa_api.routes.images import router as images_router

_log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Initialise shared app state before serving; clean up after shutdown."""
    if os.environ.get("AVSA_ORCHESTRATOR_STUB") == "1":
        _log.warning(
            "AVSA_ORCHESTRATOR_STUB=1 - stub orchestrator active; must not be set in production"
        )

    config = load_config()
    app.state.config = config
    # Raw config mapping for routes that read nested tables not projected onto
    # APIConfig. Kept in app.state so the limit ceiling stays config-driven.
    app.state.config_raw = load_config_raw()
    app.state.limiter = SlidingWindowLimiter(config.rate_limit_rpm)
    app.state.orchestrator = OrchestratorClient()

    # Storage backend for the signed image proxy. Built from the raw [storage] config table;
    # local filesystem is the only backend.
    app.state.storage = _build_backend(load_config_raw())

    # Initialise the asyncpg connection pool used by the catalog routes.
    # Falls back gracefully when no DB URL is configured (unit test / stub mode)
    # — catalog routes handle a None pool by returning an empty/unavailable result.
    db_pool = None
    db_url = os.environ.get("AVSA_DB_URL", "") or config.db_url
    if db_url:
        try:
            import asyncpg  # type: ignore[import-untyped]

            db_pool = await asyncpg.create_pool(db_url)
            _log.info("asyncpg pool created for catalog DB reads")
        except Exception:
            _log.warning(
                "Failed to create asyncpg pool - catalog DB reads will be unavailable",
                exc_info=True,
            )
    else:
        _log.warning("AVSA_DB_URL not set - catalog DB reads will be unavailable")
    app.state.db_pool = db_pool

    yield

    if db_pool is not None:
        await db_pool.close()
        _log.info("asyncpg pool closed")


class _ServerTimingMiddleware:
    """Inject Server-Timing header with API latency before response headers are sent.

    Uses raw ASGI so the SSE body is never buffered — BaseHTTPMiddleware would
    wrap the body iterator, delaying chunks to the browser.
    """

    def __init__(self, app: Any) -> None:
        self._app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        t0 = _time.perf_counter()
        headers_sent = False

        async def _send(message: dict[str, Any]) -> None:
            nonlocal headers_sent
            if message["type"] == "http.response.start" and not headers_sent:
                headers_sent = True
                elapsed_ms = (_time.perf_counter() - t0) * 1000.0
                headers = list(message.get("headers", []))
                headers.append((b"server-timing", f"api;dur={elapsed_ms:.1f}".encode()))
                message = {**message, "headers": headers}
            await send(message)

        await self._app(scope, receive, _send)


_dev = os.getenv("AVSA_ENV", "development") != "production"
app = FastAPI(
    lifespan=lifespan,
    docs_url="/docs" if _dev else None,
    redoc_url=None,
    openapi_url="/openapi.json" if _dev else None,
)


app.add_middleware(_ServerTimingMiddleware)

app.include_router(health_router)
app.include_router(chat_router)
app.include_router(images_router)
app.include_router(catalog_router)

Instrumentator().instrument(app).expose(app)
