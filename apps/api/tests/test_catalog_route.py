"""Route under test:
    GET /catalog?page=<int>&limit=<int>
        → 200 application/json:
              {"items": [{id, title, category, price_cents, currency, image_url}, ...],
               "page": <int>, "limit": <int>, "total": <int>}
        - limit over the configured max ([catalog] browse_max_limit) → CLAMPED
          to that max (soft cap), not 422 - a browse grid must always render.
        - page<1 / limit<1 → 422 (malformed pagination, fail fast at the boundary).
        - page past the end → 200 with an empty items list (not 404).
        - image_url is SIGNED at read time: the stored value is a tokenless
          /images/{key} proxy path, and the route mints a
          short-lived token via StorageBackend.signed_url, returning
          /images/{key}?token=…&expires=….

Test strategy (minimal mocks):
  - Data-dependent behaviour (pagination, stable ORDER BY id, projection, the
    read-time-signed image_url, the response model) is asserted against the
    REAL database. A fixture seeds a handful of rows with all-zero-prefixed
    UUIDs - which sort first, so they land on page 1 regardless of any other
    catalog rows - runs the live route over the app's real asyncpg pool, and
    deletes them on teardown. Postgres does the ORDER BY / LIMIT / OFFSET /
    count, so a drift in the route's SQL is actually caught (a mock that
    re-implemented the slicing would mask it). DB-gated: skipped when no
    DATABASE_URL / AVSA_DB_URL is set (the integration lane runs them).
  - Param validation (page<1 / limit<1 → 422) needs NO database and NO mock:
    FastAPI rejects at the query-param boundary, before the handler.
  - The OpenAPI contract (the /catalog operation shape) is a pure spec parse.
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

try:
    from avsa_core.storage.local import LocalStorageBackend

    _STORAGE_AVAILABLE = True
except ImportError:
    _STORAGE_AVAILABLE = False


def _require_storage() -> None:
    if not _STORAGE_AVAILABLE:
        pytest.fail(
            "avsa_core.storage.local.LocalStorageBackend not importable - the "
            "/catalog route signs image_url at read time via the storage "
            "backend's signed_url surface ( / handoff)."
        )


_DB_URL = os.environ.get("DATABASE_URL") or os.environ.get("AVSA_DB_URL")
_db_required = pytest.mark.skipif(
    not _DB_URL,
    reason="DATABASE_URL / AVSA_DB_URL not set - catalog DB tests run in the integration lane",
)

# ----------------------------------------------------------------------------
# Seed rows
# ----------------------------------------------------------------------------

_KEY_PREFIX = "/images/fashion200k/images/women"
_SEED_ROWS: list[dict[str, Any]] = [
    {
        "id": "00000000-0000-0000-0000-000000000001",
        "title": "black knit midi dress",
        "category": "dress",
        "price_cents": 129900,
        "image_url": f"{_KEY_PREFIX}/dresses/casual/100/100_1.jpeg.jpg",
    },
    {
        "id": "00000000-0000-0000-0000-000000000002",
        "title": "silk blouse",
        "category": "top",
        "price_cents": 84900,
        "image_url": f"{_KEY_PREFIX}/tops/blouses/200/200_2.jpeg.jpg",
    },
    {
        "id": "00000000-0000-0000-0000-000000000003",
        "title": "navy pleated mini skirt",
        "category": "skirt",
        "price_cents": 59900,
        "image_url": f"{_KEY_PREFIX}/skirts/mini/300/300_3.jpeg.jpg",
    },
    {
        "id": "00000000-0000-0000-0000-000000000004",
        "title": "red wool coat",
        "category": "coat",
        "price_cents": 219900,
        "image_url": f"{_KEY_PREFIX}/coats/winter/400/400_4.jpeg.jpg",
    },
    {
        "id": "00000000-0000-0000-0000-000000000005",
        "title": "white cotton tee",
        "category": "top",
        "price_cents": 24900,
        "image_url": f"{_KEY_PREFIX}/tops/tees/500/500_5.jpeg.jpg",
    },
]
_SEED_IDS = [r["id"] for r in _SEED_ROWS]
_SEED_BY_ID = sorted(_SEED_ROWS, key=lambda r: r["id"])
# pgvector accepts a "[f, f, ...]" string literal; the value is irrelevant to browse.
_ZERO_EMBEDDING = "[" + ",".join(["0.0"] * 768) + "]"


def _stored_key(image_url: str) -> str:
    """Strip the leading /images/ to get the storage key the route signs."""
    assert image_url.startswith("/images/"), f"test setup: {image_url!r} not a proxy path"
    return image_url[len("/images/") :]


# ----------------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------------


@pytest.fixture
async def app_client(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[AsyncClient, None]:
    """Real app + client, no database required.

    Used by the param-validation tests: FastAPI rejects page<1 / limit<1 at the
    query-param boundary before the handler touches the pool, so these need
    neither a DB nor a mock. A test HMAC secret lets the lifespan build its
    (real) storage backend cleanly.
    """
    monkeypatch.setenv("AVSA_STORAGE_HMAC_SECRET", "catalog-route-test-secret")
    from avsa_api.main import app

    async with (
        LifespanManager(app) as manager,
        AsyncClient(transport=ASGITransport(app=manager.app), base_url="http://test") as ac,
    ):
        yield ac


@pytest.fixture
async def seeded_catalog_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[AsyncClient, None]:
    """Real DB + real app pool + real signer; deterministic seeded rows.

    Seeds _SEED_ROWS into catalog.products (committed so the app's own pool
    connection sees them), runs the live route, and deletes them on teardown.
    No DB logic is mocked - Postgres executes the real ORDER BY / LIMIT /
    OFFSET / count. A real LocalStorageBackend (tmp dir + test secret) is
    injected so the read-time image-URL signing is exercised for real.
    """
    if not _DB_URL:
        pytest.skip("DATABASE_URL / AVSA_DB_URL not set")
    _require_storage()
    import asyncpg  # type: ignore[import-untyped]

    monkeypatch.setenv("AVSA_STORAGE_HMAC_SECRET", "catalog-route-test-secret")
    monkeypatch.setenv("AVSA_DB_URL", _DB_URL)

    conn = await asyncpg.connect(_DB_URL)
    try:
        await conn.execute("DELETE FROM catalog.products WHERE id = ANY($1::uuid[])", _SEED_IDS)
        for row in _SEED_ROWS:
            await conn.execute(
                """
                INSERT INTO catalog.products
                    (id, title, category, colour, formality, occasion,
                     price_cents, image_url, embedding)
                VALUES ($1::uuid, $2, $3, 'black', 'casual', 'everyday', $4, $5, $6)
                """,
                row["id"],
                row["title"],
                row["category"],
                row["price_cents"],
                row["image_url"],
                _ZERO_EMBEDDING,
            )

        backend = LocalStorageBackend(root=tmp_path)
        from avsa_api.main import app

        async with (
            LifespanManager(app) as manager,
            AsyncClient(transport=ASGITransport(app=manager.app), base_url="http://test") as ac,
        ):
            app.state.storage = backend
            yield ac
    finally:
        await conn.execute("DELETE FROM catalog.products WHERE id = ANY($1::uuid[])", _SEED_IDS)
        await conn.close()


# ----------------------------------------------------------------------------
# Param validation
# ----------------------------------------------------------------------------


async def test_get_catalog_invalid_params_rejected(app_client: AsyncClient) -> None:
    """page<1 and limit<1 are malformed pagination → 422 (fail fast at the boundary)."""
    r_page = await app_client.get("/catalog?page=0&limit=2")
    assert r_page.status_code == 422, (
        f"page=0 is below the minimum (page >= 1) and must return 422; got {r_page.status_code}"
    )

    r_limit = await app_client.get("/catalog?page=1&limit=0")
    assert r_limit.status_code == 422, (
        f"limit=0 is below the minimum (limit >= 1) and must return 422; got {r_limit.status_code}"
    )


# ----------------------------------------------------------------------------
# Data-dependent behaviour
# ----------------------------------------------------------------------------


@_db_required
async def test_get_catalog_returns_paginated_page(seeded_catalog_client: AsyncClient) -> None:
    """GET /catalog returns items + pagination metadata in the documented shape."""
    response = await seeded_catalog_client.get("/catalog?page=1&limit=2")

    assert response.status_code == 200, (
        f"GET /catalog must return 200; got {response.status_code} body={response.text[:200]!r}"
    )
    body = response.json()

    assert isinstance(body.get("items"), list), f"'items' must be a list; got {body!r}"
    assert len(body["items"]) == 2, (
        f"page=1&limit=2 must return exactly 2 items; got {len(body['items'])}"
    )
    assert body.get("page") == 1, f"'page' must echo 1; got {body.get('page')!r}"
    assert body.get("limit") == 2, f"'limit' must echo 2; got {body.get('limit')!r}"
    assert body.get("total") >= len(_SEED_ROWS), (
        f"'total' must count at least the {len(_SEED_ROWS)} seeded rows; got {body.get('total')!r}"
    )

    # The seeded all-zero-prefix ids sort first, so page 1 is exactly them.
    assert [it["id"] for it in body["items"]] == [_SEED_BY_ID[0]["id"], _SEED_BY_ID[1]["id"]], (
        "page 1 must be the lowest-id (seeded) rows in id order"
    )

    item = body["items"][0]
    for field in ("id", "title", "category", "price_cents", "currency", "image_url"):
        assert field in item, f"each item must include {field!r}; got keys {list(item)!r}"
    assert isinstance(item["price_cents"], int), (
        f"price_cents must be int; got {item['price_cents']!r}"
    )
    assert item["currency"] == "ZAR", f"currency must be ZAR; got {item['currency']!r}"
    assert item["title"] == _SEED_BY_ID[0]["title"], "projection must carry the real row's title"


@_db_required
async def test_get_catalog_limit_bound_enforced(seeded_catalog_client: AsyncClient) -> None:
    """An over-max limit is CLAMPED to [catalog] browse_max_limit (100), not 422."""
    response = await seeded_catalog_client.get("/catalog?page=1&limit=10000")

    assert response.status_code == 200, (
        f"an over-max limit must be clamped (200), not rejected; got {response.status_code}"
    )
    body = response.json()
    effective_limit = body["limit"]
    assert effective_limit < 10000, f"limit must be clamped below 10000; got {effective_limit!r}"
    assert effective_limit == 100, (
        f"limit must clamp to the configured browse_max_limit (100); got {effective_limit!r}"
    )
    assert len(body["items"]) <= effective_limit, (
        f"item count {len(body['items'])} must not exceed the clamped limit {effective_limit}"
    )


@_db_required
async def test_get_catalog_page_param(seeded_catalog_client: AsyncClient) -> None:
    """page=2 returns the next slice with stable ORDER BY id - no dupes, no gaps."""
    r1 = await seeded_catalog_client.get("/catalog?page=1&limit=2")
    r2 = await seeded_catalog_client.get("/catalog?page=2&limit=2")
    assert r1.status_code == 200 and r2.status_code == 200

    page1_ids = [it["id"] for it in r1.json()["items"]]
    page2_ids = [it["id"] for it in r2.json()["items"]]

    assert not (set(page1_ids) & set(page2_ids)), (
        f"page 2 must not repeat page 1 ids (stable order + correct OFFSET); "
        f"p1={page1_ids!r} p2={page2_ids!r}"
    )
    # The first four rows in id-order are exactly the four lowest seeded ids.
    assert page1_ids + page2_ids == [r["id"] for r in _SEED_BY_ID[:4]], (
        f"pages 1+2 must be the first four seeded ids in order; got {page1_ids + page2_ids!r}"
    )


@_db_required
async def test_get_catalog_empty_page_returns_200_empty(
    seeded_catalog_client: AsyncClient,
) -> None:
    """A page beyond the data returns 200 with an empty items list, not 404."""
    total = (await seeded_catalog_client.get("/catalog?page=1&limit=1")).json()["total"]
    # With limit=1, page (total + 5) addresses offset >= total → past the end.
    past_the_end = total + 5
    response = await seeded_catalog_client.get(f"/catalog?page={past_the_end}&limit=1")

    assert response.status_code == 200, (
        f"a page past the end must return 200 (empty is not an error); got {response.status_code}"
    )
    body = response.json()
    assert body["items"] == [], f"page past the end must yield empty items; got {body['items']!r}"
    assert body["total"] >= len(_SEED_ROWS), (
        f"'total' must still report the full count on an empty page; got {body['total']!r}"
    )


@_db_required
async def test_get_catalog_image_url_is_signed(seeded_catalog_client: AsyncClient) -> None:
    """Returned image_url is the /images/{key} proxy path SIGNED at read time.

    The stored value is tokenless; the route mints a short-lived token via
    the storage backend. The returned URL must keep the /images/{key} shape and
    carry token= and expires= - never the bare stored value or a retailer URL.
    """
    response = await seeded_catalog_client.get("/catalog?page=1&limit=5")
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) >= len(_SEED_ROWS), (
        "page 1 must contain the seeded rows for the signing check"
    )

    for item, stored in zip(items, _SEED_BY_ID, strict=False):
        url = item["image_url"]
        stored_path = stored["image_url"]  # tokenless /images/{key}
        key = _stored_key(stored_path)
        assert url.startswith(f"/images/{key}"), (
            f"image_url must be the /images/{{key}} proxy path; stored={stored_path!r} got={url!r}"
        )
        assert "token=" in url, f"image_url must be signed (token= present); got {url!r}"
        assert "expires=" in url, f"image_url must carry the signed expiry; got {url!r}"
        assert url != stored_path, f"image_url must NOT be the bare tokenless value; got {url!r}"
        assert not url.startswith("http"), f"image_url must be a relative proxy path; got {url!r}"


@_db_required
async def test_get_catalog_response_matches_pydantic_model(
    seeded_catalog_client: AsyncClient,
) -> None:
    """The response validates against the route's declared CatalogPage model."""
    try:
        from avsa_api.routes.catalog import CatalogPage  # type: ignore[import-not-found]
    except ImportError:
        pytest.fail("avsa_api.routes.catalog.CatalogPage must be importable (the response model)")

    response = await seeded_catalog_client.get("/catalog?page=1&limit=2")
    assert response.status_code == 200

    model = CatalogPage.model_validate(response.json())
    assert model.page == 1, f"validated model page must be 1; got {model.page!r}"
    assert model.limit == 2, f"validated model limit must be 2; got {model.limit!r}"
    assert len(model.items) == 2, f"validated model must carry 2 items; got {len(model.items)}"


# ----------------------------------------------------------------------------
# OpenAPI spec contract
# ----------------------------------------------------------------------------

_SPEC_PATH = Path(__file__).resolve().parents[3] / "specs" / "api" / "chat.openapi.yaml"


def test_openapi_spec_has_catalog_operation() -> None:
    """specs/api/chat.openapi.yaml must declare GET /catalog + its paginated response."""
    import yaml

    assert _SPEC_PATH.is_file(), f"OpenAPI spec not found at {_SPEC_PATH}"
    spec = yaml.safe_load(_SPEC_PATH.read_text(encoding="utf-8"))

    paths = spec.get("paths", {})
    assert "/catalog" in paths, f"spec must declare the /catalog path; got {sorted(paths)!r}"

    op = paths["/catalog"].get("get")
    assert op is not None, "/catalog must declare a GET operation"

    params = op.get("parameters", [])
    query_names = {p.get("name") for p in params if p.get("in") == "query"}
    assert "page" in query_names, f"GET /catalog must document a 'page' param; got {query_names!r}"
    assert "limit" in query_names, (
        f"GET /catalog must document a 'limit' param; got {query_names!r}"
    )

    responses = op.get("responses", {})
    ok = responses.get("200") or responses.get(200)
    assert ok is not None, "GET /catalog must document a 200 response"
    schema = ok.get("content", {}).get("application/json", {}).get("schema", {})
    assert schema, "GET /catalog 200 response must declare an application/json schema"

    resolved = schema
    ref = schema.get("$ref")
    if ref:
        assert ref.startswith("#/components/schemas/"), f"unexpected $ref form {ref!r}"
        comp_name = ref.split("/")[-1]
        resolved = spec.get("components", {}).get("schemas", {}).get(comp_name, {})
        assert resolved, f"$ref {ref!r} must resolve to a defined component schema"

    props = resolved.get("properties", {})
    for field in ("items", "page", "limit", "total"):
        assert field in props, (
            f"the /catalog 200 schema must declare {field!r}; got {sorted(props)!r}"
        )

    items_schema = props["items"]
    assert items_schema.get("type") == "array", f"'items' must be an array; got {items_schema!r}"
    item_schema = items_schema.get("items", {})
    item_ref = item_schema.get("$ref")
    if item_ref:
        comp_name = item_ref.split("/")[-1]
        item_schema = spec.get("components", {}).get("schemas", {}).get(comp_name, {})
    item_props = set(item_schema.get("properties", {}))
    for field in ("id", "title", "category", "image_url"):
        assert field in item_props, (
            f"each /catalog item must declare {field!r}; got {sorted(item_props)!r}"
        )
    assert "price_cents" in item_props or "price" in item_props, (
        f"the /catalog item schema must carry a price field; got {sorted(item_props)!r}"
    )
