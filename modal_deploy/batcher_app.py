"""Modal deployment — AVSA batcher (Python asyncio, calls AvsaModel via Modal SDK).

Replicates the wire contract of the Rust batcher (crates/batcher):

  POST /embed
    Request:  {"image_bytes": "<base64-encoded image>"}
    Response: {"embedding": [f32 x 768], "attributes": {category, colour,
               category_confidence, colour_confidence}}

Batching: up to [batcher].max_batch_size requests are accumulated, or flushed
after [batcher].max_wait_ms — both read from config/avsa.toml.

The upstream GPU class is looked up via Modal SDK:
  modal.Cls.from_name(AVSA_MODEL_APP_NAME, "AvsaModel")
AVSA_MODEL_APP_NAME defaults to "avsa-model" and can be overridden in the
"avsa-batcher" secret for preview deployments.

Deploy:  modal deploy modal_deploy/batcher_app.py
Serve:   modal serve modal_deploy/batcher_app.py

Set AVSA_PROD_BATCHER_URL to this app's generated HTTPS URL before running
bench-prod.sh.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import tomllib
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import modal
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Image
# ---------------------------------------------------------------------------
batcher_image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install(
        "fastapi>=0.120,<0.130",
        "uvicorn[standard]>=0.27,<0.35",
        "pydantic>=2.6,<3.0",
        # modal SDK so the batcher can call AvsaModel.embed_batch.remote.aio()
        "modal>=1.0,<2.0",
    )
    .add_local_file("config/avsa.toml", remote_path="/app/config/avsa.toml")
)

MODAL_APP_NAME = os.environ.get("MODAL_APP_NAME", "avsa-batcher")

app = modal.App(MODAL_APP_NAME, image=batcher_image)

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _load_batcher_config() -> dict[str, Any]:
    for parent in [Path(__file__).resolve(), *Path(__file__).resolve().parents]:
        candidate = parent / "config" / "avsa.toml"
        if candidate.exists():
            with open(candidate, "rb") as f:
                return tomllib.load(f).get("batcher", {})
    return {}


# ---------------------------------------------------------------------------
# Wire types
# ---------------------------------------------------------------------------


class EmbedRequest(BaseModel):
    image_bytes: str  # base64-encoded raw image bytes


class Attributes(BaseModel):
    category: str
    colour: str
    category_confidence: float
    colour_confidence: float


class EmbedResponse(BaseModel):
    embedding: list[float]
    attributes: Attributes


# ---------------------------------------------------------------------------
# Asyncio batch queue
# ---------------------------------------------------------------------------


@dataclass
class _PendingRequest:
    image_bytes: bytes
    future: asyncio.Future[dict[str, Any]] = field(  # type: ignore[assignment]
        default_factory=lambda: asyncio.Future()  # type: ignore[return-value]
    )


class BatchQueue:
    """Accumulates single-image embed requests; flushes batches to AvsaModel."""

    def __init__(
        self,
        model_app_name: str,
        max_batch_size: int,
        max_wait_ms: int,
    ) -> None:
        self._model_app_name = model_app_name
        self._max_batch_size = max_batch_size
        self._max_wait_ms = max_wait_ms
        self._queue: asyncio.Queue[_PendingRequest] = asyncio.Queue()
        # Lazily resolved: needs to run inside an event loop where modal is configured.
        self._model_cls: Any = None

    def _get_model(self) -> Any:
        if self._model_cls is None:
            self._model_cls = modal.Cls.from_name(self._model_app_name, "AvsaModel")
        return self._model_cls()

    async def enqueue(self, image_bytes: bytes) -> dict[str, Any]:
        loop = asyncio.get_event_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        req = _PendingRequest(image_bytes=image_bytes, future=future)
        await self._queue.put(req)
        return await future

    async def run_drain_task(self) -> None:
        """Long-lived background task; drains the queue in batches."""
        while True:
            batch: list[_PendingRequest] = []
            first = await self._queue.get()
            batch.append(first)

            deadline = asyncio.get_event_loop().time() + self._max_wait_ms / 1000.0
            while len(batch) < self._max_batch_size:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    break
                try:
                    item = await asyncio.wait_for(self._queue.get(), timeout=remaining)
                    batch.append(item)
                except TimeoutError:
                    break

            await self._flush(batch)

    async def _flush(self, batch: list[_PendingRequest]) -> None:
        images_b64 = [base64.b64encode(r.image_bytes).decode() for r in batch]
        try:
            model = self._get_model()
            data: dict[str, Any] = await model.embed_batch.remote.aio(images_b64)
        except Exception as exc:
            for r in batch:
                if not r.future.done():
                    r.future.set_exception(exc)
            return

        embeddings: list[list[float]] = data.get("embeddings", [])
        attributes: list[dict[str, Any]] = data.get("attributes", [])
        for i, r in enumerate(batch):
            if r.future.done():
                continue
            if i < len(embeddings) and i < len(attributes):
                r.future.set_result(
                    {"embedding": embeddings[i], "attributes": attributes[i]}
                )
            else:
                r.future.set_exception(
                    ValueError("model response shorter than request batch")
                )


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(fastapi_app: FastAPI) -> AsyncGenerator[None, None]:
    cfg = _load_batcher_config()
    model_app_name = os.environ.get("AVSA_MODEL_APP_NAME", "avsa-model")
    max_batch_size: int = cfg.get("max_batch_size", 8)
    max_wait_ms: int = cfg.get("max_wait_ms", 50)

    queue = BatchQueue(
        model_app_name=model_app_name,
        max_batch_size=max_batch_size,
        max_wait_ms=max_wait_ms,
    )
    drain_task = asyncio.create_task(queue.run_drain_task())
    fastapi_app.state.queue = queue
    _log.info(
        "batcher ready model_app=%s max_batch_size=%d max_wait_ms=%d",
        model_app_name,
        max_batch_size,
        max_wait_ms,
    )
    yield
    drain_task.cancel()


_fastapi_app = FastAPI(
    lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
)


@_fastapi_app.get("/healthz")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@_fastapi_app.post("/embed", response_model=EmbedResponse)
async def embed(body: EmbedRequest, request: Request) -> EmbedResponse:
    try:
        raw = base64.b64decode(body.image_bytes, validate=True)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="invalid base64") from exc

    queue: BatchQueue = request.app.state.queue
    try:
        result = await queue.enqueue(raw)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"upstream error: {exc}") from exc

    attrs = result["attributes"]
    return EmbedResponse(
        embedding=result["embedding"],
        attributes=Attributes(
            category=attrs["category"],
            colour=attrs["colour"],
            category_confidence=attrs["category_confidence"],
            colour_confidence=attrs["colour_confidence"],
        ),
    )


# ---------------------------------------------------------------------------
# Modal entry point
# ---------------------------------------------------------------------------


@app.function(
    secrets=[modal.Secret.from_name("avsa-batcher")],
    scaledown_window=1200,
)
@modal.asgi_app()
def batcher_asgi() -> FastAPI:
    return _fastapi_app
