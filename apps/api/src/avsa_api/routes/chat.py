"""POST /chat - multipart image upload with SSE streaming response."""

import json
import uuid
from collections.abc import AsyncGenerator

from avsa_core.storage import StorageBackend
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from starlette.responses import StreamingResponse

from avsa_api.clients.orchestrator import OrchestratorClient, get_orchestrator
from avsa_api.middleware.rate_limit import SlidingWindowLimiter, _client_ip, get_limiter
from avsa_api.routes.catalog import _sign_image_url

router = APIRouter()

ALLOWED_MIME: frozenset[str] = frozenset({"image/jpeg", "image/png", "image/webp", "image/heic"})

MAX_IMAGES: int = 8


def _sign_card_image_url(event: dict[str, object], backend: StorageBackend) -> dict[str, object]:
    """Sign the image_url inside a product_card event when present.

    The orchestrator includes image_url in ProductResultEvent
    metadata_json (grpc_server.ex build_product_event), so this signs
    it at read time when it is a tokenless /images/{key} proxy path; for an
    absolute / CDN URL _sign_image_url returns the value unchanged.
    """
    if event.get("type") != "product_card":
        return event
    card = event.get("card")
    if not isinstance(card, dict):
        return event
    raw_url = card.get("image_url", "")
    if not isinstance(raw_url, str) or not raw_url:
        return event
    signed = _sign_image_url(raw_url, backend)
    if signed == raw_url:
        # _sign_image_url returns the value unchanged for non-/images/ URLs
        return event
    return {**event, "card": {**card, "image_url": signed}}


async def _sse_stream(
    gen: AsyncGenerator[dict[str, object], None],
    *,
    storage_backend: StorageBackend | None = None,
) -> AsyncGenerator[str, None]:
    """Yield SSE-formatted strings from gen (the orchestrator chat stream)."""
    async for event in gen:
        if storage_backend is not None:
            event = _sign_card_image_url(event, storage_backend)
        yield f"data: {json.dumps(event)}\n\n"


async def _read_limited(upload: UploadFile, max_bytes: int) -> bytes:
    """Read *upload* in 64 KiB chunks, raising 413 if total exceeds *max_bytes*.

    Checking Content-Length is a cheap fast-reject but does not cover chunked
    Transfer-Encoding. This streaming counter is the authoritative size gate.
    """
    chunks: list[bytes] = []
    total = 0
    while chunk := await upload.read(65536):
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(status_code=413, detail="Payload exceeds size limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _valid_magic(data: bytes, mime: str) -> bool:
    """Return True when data's leading bytes match the expected magic for mime."""
    if mime == "image/jpeg":
        return len(data) >= 3 and data[:3] == b"\xff\xd8\xff"
    if mime == "image/png":
        return len(data) >= 8 and data[:8] == b"\x89PNG\r\n\x1a\n"
    if mime == "image/webp":
        return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    if mime == "image/heic":
        # HEIC/ISOBMFF: ftyp box begins at byte offset 4
        return len(data) >= 12 and data[4:8] == b"ftyp"
    return False


@router.post("/chat")
async def chat(
    request: Request,
    image: list[UploadFile] = File(default=[]),
    text: str = Form(default="", max_length=2000),
) -> StreamingResponse:
    """Accept one or more multipart images + optional text (or text only)."""
    limiter: SlidingWindowLimiter = get_limiter(request)
    limiter.check(_client_ip(request))

    # Starlette yields one empty UploadFile when the field is absent; drop those
    # so a text-only turn presents as zero images.
    images: list[UploadFile] = [f for f in image if f is not None and f.filename]

    if not images and not text:
        raise HTTPException(status_code=422, detail="At least one of image or text is required")

    if len(images) > MAX_IMAGES:
        raise HTTPException(
            status_code=422, detail=f"At most {MAX_IMAGES} images may be combined in one query"
        )

    max_bytes: int = request.app.state.config.max_upload_bytes

    if images:
        raw_cl = request.headers.get("content-length")
        try:
            declared_size = int(raw_cl) if raw_cl is not None else 0
        except ValueError:
            declared_size = 0
        if declared_size > max_bytes * len(images):
            raise HTTPException(status_code=413, detail="Payload exceeds size limit")

    image_bytes_list: list[bytes] = []
    for upload in images:
        if upload.content_type not in ALLOWED_MIME:
            raise HTTPException(status_code=415, detail="Unsupported media type")
        data = await _read_limited(upload, max_bytes)
        if not _valid_magic(data, upload.content_type or ""):
            raise HTTPException(status_code=415, detail="Unsupported media type")
        image_bytes_list.append(data)

    resume_header = request.headers.get("X-Resume-Conversation-Id", "")
    conversation_id: str
    try:
        conversation_id = str(uuid.UUID(resume_header)) if resume_header else str(uuid.uuid4())
    except ValueError:
        conversation_id = str(uuid.uuid4())

    orchestrator: OrchestratorClient = get_orchestrator(request)
    storage_backend: StorageBackend | None = getattr(request.app.state, "storage", None)

    return StreamingResponse(
        _sse_stream(
            orchestrator.stream_chat(image_bytes_list, text, conversation_id=conversation_id),
            storage_backend=storage_backend,
        ),
        media_type="text/event-stream",
        headers={"X-Conversation-Id": conversation_id},
    )
