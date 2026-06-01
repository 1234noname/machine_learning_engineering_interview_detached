"""Embed router - POST /embed.

Wire format (must match VitClient in crates/batcher/src/vit_client.rs):
  Request:  {"images": [<base64-string>, ...]}
  Response: {
      "embeddings": [[f32; 768], ...],
      "attributes": [
          {"category": str, "colour": str,
           "category_confidence": f32, "colour_confidence": f32},
          ...
      ]
  }

attributes is parallel to embeddings (one entry per image). embeddings is UNCHANGED
(same field, dim 768, L2-normalised) so the response is an additive, backward-compatible
extension - the Rust batcher
(crates/batcher/src/vit_client.rs) keeps deserializing only embeddings; the
attributes are consumed downstream.
"""

from __future__ import annotations

import base64
import binascii
import logging
import time
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request
from prometheus_client import Histogram
from pydantic import BaseModel

if TYPE_CHECKING:
    from avsa_model.stub import StubEmbedder
    from avsa_model.vit import VitEmbedder

_log = logging.getLogger(__name__)

router = APIRouter()

# Per-stage histograms for the /embed handler (experiment A in
# docs/qps-local-optimisation.md). The aggregate handler time is captured by
# prometheus-fastapi-instrumentator's http_request_duration_seconds{handler=
# "/embed"}; these split the time inside the handler so we can target the
# dominant slice. Bucket layout covers 1 ms → 1 s in geometric steps, the
# range the stages were observed at on local MPS fp16.
_BUCKETS = (0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1.0)
_decode_hist = Histogram(
    "avsa_model_embed_decode_seconds",
    "Time spent base64-decoding the images list inside /embed.",
    buckets=_BUCKETS,
)
_embedder_hist = Histogram(
    "avsa_model_embed_embedder_seconds",
    "Time spent in embedder.embed_with_attributes() inside /embed.",
    buckets=_BUCKETS,
)
_response_build_hist = Histogram(
    "avsa_model_embed_response_build_seconds",
    "Time spent constructing the EmbedResponse object inside /embed"
    " (before FastAPI's outer JSON serialisation).",
    buckets=_BUCKETS,
)


class EmbedRequest(BaseModel):
    images: list[str]


class Attribute(BaseModel):
    """Per-image attribute prediction.

    category/colour are the argmax labels; the confidences are the
    softmax probability at the winning index, in [0, 1].
    """

    category: str
    colour: str
    category_confidence: float
    colour_confidence: float


class EmbedResponse(BaseModel):
    embeddings: list[list[float]]
    attributes: list[Attribute]


@router.post("/embed", response_model=EmbedResponse)
async def embed(body: EmbedRequest, request: Request) -> EmbedResponse:
    """Accept a batch of base64-encoded images; return embedding + attributes each.

    Returns:
        200 - embeddings of shape (N, 768) L2-normalised, plus attributes
              (one entry per image, parallel to embeddings).
        422 - empty images list (Pydantic validation or explicit check).
        400 - malformed base64 string.
    """
    if not body.images:
        raise HTTPException(status_code=422, detail="images list must not be empty")

    t_decode_start = time.perf_counter()
    raw_images: list[bytes] = []
    for b64 in body.images:
        try:
            raw_images.append(base64.b64decode(b64, validate=True))
        except (binascii.Error, ValueError) as exc:
            _log.debug("malformed base64 in request: %s", exc)
            raise HTTPException(status_code=400, detail="malformed base64 image data") from exc
    t_decode_end = time.perf_counter()
    _decode_hist.observe(t_decode_end - t_decode_start)

    embedder: StubEmbedder | VitEmbedder = request.app.state.embedder
    embeddings, attributes = embedder.embed_with_attributes(raw_images)
    t_embed_end = time.perf_counter()
    _embedder_hist.observe(t_embed_end - t_decode_end)

    resp = EmbedResponse(
        embeddings=embeddings,
        attributes=[
            Attribute(
                category=attr.category,
                colour=attr.colour,
                category_confidence=attr.category_confidence,
                colour_confidence=attr.colour_confidence,
            )
            for attr in attributes
        ],
    )
    _response_build_hist.observe(time.perf_counter() - t_embed_end)
    return resp
