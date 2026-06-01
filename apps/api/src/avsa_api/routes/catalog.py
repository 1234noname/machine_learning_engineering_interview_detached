"""GET /catalog - paginated catalog browse surface

Returns a windowed, stably-ordered page of products from catalog.products
for the shopper landing's browse grid. The endpoint reuses the existing
read-time image-signing surface: the stored image_url is a
TOKENLESS /images/{key} proxy path, and this route
mints a short-lived HMAC token via the storage backend's signed_url so the
served URL is /images/{key}?token=…&expires=… - never the bare stored
value and never a public retailer URL (ADR 0007 non-redistribution).

Pagination contract:
    - limit over the configured maximum is CLAMPED to that maximum (a soft
      cap, not a client error): the browse grid must always render a page.
    - page < 1 / limit < 1 are malformed pagination → 422 (fail fast at
      the boundary, distinct from "page past the end" which is a valid 200).
    - a page beyond the data returns 200 with an empty items list.

State is read from request.app.state: the asyncpg pool (db_pool, wired
at lifespan in main.py), the storage backend,
and the raw parsed config (config_raw). The browse limit ceiling is config-driven
([catalog] browse_max_limit), never hardcoded.
"""

from __future__ import annotations

from typing import Any

from avsa_core.storage import StorageBackend
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

router = APIRouter()

_CURRENCY = "ZAR"

_IMAGE_PREFIX = "/images/"

_DEFAULT_BROWSE_MAX_LIMIT = 100

_PAGE_QUERY = (
    "SELECT id, title, category, price_cents, image_url "
    "FROM catalog.products ORDER BY id LIMIT $1 OFFSET $2"
)
_COUNT_QUERY = "SELECT count(*) FROM catalog.products"


class CatalogItem(BaseModel):
    """One product in the browse grid - exactly the browse-grid columns."""

    id: str = Field(description="Catalog product identifier (UUID).")
    title: str
    category: str
    price_cents: int = Field(description="Price in ZAR cents (integer).")
    currency: str = Field(description="ISO 4217 currency code; constant 'ZAR'.")
    image_url: str = Field(
        description="Read-time signed /images/{key}?token=…&expires=… proxy path."
    )


class CatalogPage(BaseModel):
    """A paginated page of catalog products plus pagination metadata."""

    items: list[CatalogItem]
    page: int = Field(description="1-based page number echoed from the request.")
    limit: int = Field(description="Effective page size after clamping to the configured max.")
    total: int = Field(description="Total number of products in the catalog.")


def _browse_max_limit(request: Request) -> int:
    """Read the config-driven browse-limit ceiling from [catalog]."""
    config_raw: dict[str, Any] = getattr(request.app.state, "config_raw", {})
    catalog_cfg = config_raw.get("catalog", {}) if isinstance(config_raw, dict) else {}
    return int(catalog_cfg.get("browse_max_limit", _DEFAULT_BROWSE_MAX_LIMIT))


def _sign_image_url(stored_url: str, backend: StorageBackend) -> str:
    """Mint a read-time signed proxy URL for a tokenless /images/{key} path.

    The stored value is /images/{key}; we extract {key}, sign it via the
    backend, and rebuild /images/{key}?token=…&expires=…. A stored value that is not a proxy path
    (defensive: a stray absolute URL) is returned unchanged rather than signed.
    """
    if not stored_url.startswith(_IMAGE_PREFIX):
        return stored_url
    key = stored_url[len(_IMAGE_PREFIX) :]
    signed = backend.signed_url(key)
    return f"{_IMAGE_PREFIX}{key}?token={signed['token']}&expires={signed['expires']}"


@router.get("/catalog", response_model=CatalogPage)
async def get_catalog(
    request: Request,
    page: int = Query(1, ge=1, description="1-based page number (>= 1)."),
    limit: int = Query(20, ge=1, description="Page size; clamped to the configured max."),
) -> CatalogPage:
    """Return a paginated, stably-ordered page of catalog products.

    page/limit below their minimum yield 422 (FastAPI ge=1); an
    over-max limit is clamped to the configured ceiling (soft cap). Each
    item's image_url is signed at read time.
    """
    effective_limit = min(limit, _browse_max_limit(request))
    offset = (page - 1) * effective_limit

    db_pool = getattr(request.app.state, "db_pool", None)
    if db_pool is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "catalog_unavailable",
                "message": "catalog database is not configured",
            },
        )

    backend: StorageBackend = request.app.state.storage

    async with db_pool.acquire() as conn:
        rows = await conn.fetch(_PAGE_QUERY, effective_limit, offset)
        total = await conn.fetchval(_COUNT_QUERY)

    items = [
        CatalogItem(
            id=str(row["id"]),
            title=row["title"],
            category=row["category"],
            price_cents=int(row["price_cents"]),
            currency=_CURRENCY,
            image_url=_sign_image_url(row["image_url"], backend),
        )
        for row in rows
    ]

    return CatalogPage(items=items, page=page, limit=effective_limit, total=int(total or 0))
