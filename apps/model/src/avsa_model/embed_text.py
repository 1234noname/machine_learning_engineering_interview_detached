"""Embed-text router - POST /embed_text.

Wire format (mirrors /embed for images):
  Request:  {"texts": ["string", ...]}
  Response: {"embeddings": [[f32; 512], ...]}
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

if TYPE_CHECKING:
    from avsa_model.text_encoder import TextEncoder
    from avsa_model.text_stub import StubTextEmbedder

_log = logging.getLogger(__name__)

router = APIRouter()


class EmbedTextRequest(BaseModel):
    texts: list[str]


class EmbedTextResponse(BaseModel):
    embeddings: list[list[float]]


@router.post("/embed_text", response_model=EmbedTextResponse)
async def embed_text(body: EmbedTextRequest, request: Request) -> EmbedTextResponse:
    """Accept a batch of text strings; return one 512-dim embedding each.

    Returns:
        200 - embeddings of shape (N, 512), L2-normalised.
        422 - empty texts list (Pydantic validation or explicit check).
        400 - malformed payload (e.g. texts field is not a list of strings).
    """
    if not body.texts:
        raise HTTPException(status_code=422, detail="texts list must not be empty")

    embedder: StubTextEmbedder | TextEncoder = request.app.state.text_embedder
    embeddings = embedder.embed(body.texts)
    return EmbedTextResponse(embeddings=embeddings)
