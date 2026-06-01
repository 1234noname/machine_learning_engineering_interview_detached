"""GET /images/{path} - private, signed image proxy.

Serves Fashion200k subset images from the configured StorageBackend behind
HMAC-signed, time-limited tokens - never a public, unauthenticated URL
(ADR 0007 non-redistribution constraint). The storage layer returns only a
route-agnostic SignedToken; this route is the layer that
owns the /images/ URL shape and maps backend outcomes onto HTTP status:

    200 image/*    valid token + object present (the stored bytes)
    400            token or expires query param missing (fail fast at boundary)
    403            token invalid / expired / tampered / signed for another path
    404 image/png  valid token but object absent (graceful placeholder)

The backend is read from request.app.state.storage (wired at lifespan in
main.py; injected directly by tests).
"""

from __future__ import annotations

from avsa_core.storage import NotFound, StorageBackend
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from avsa_api.static.placeholder import PLACEHOLDER_PNG

router = APIRouter()


@router.get("/images/{path:path}")
async def get_image(
    request: Request,
    path: str,
    token: str | None = None,
    expires: int | None = None,
) -> Response:
    """Serve a signed image, or a 404 PNG placeholder when the object is absent."""
    if token is None or expires is None:
        return JSONResponse(
            status_code=400,
            content={
                "code": "missing_signed_url_params",
                "message": "both 'token' and 'expires' query parameters are required",
            },
        )

    backend: StorageBackend = request.app.state.storage

    if not backend.verify_signed_url(path, token, expires):
        return JSONResponse(
            status_code=403,
            content={
                "code": "invalid_signed_url",
                "message": "signed URL is invalid, expired, or bound to a different path",
            },
        )

    try:
        data = backend.get_object(path)
    except NotFound:
        return Response(content=PLACEHOLDER_PNG, media_type="image/png", status_code=404)

    return Response(content=data, media_type="image/jpeg")
