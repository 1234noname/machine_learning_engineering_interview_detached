"""AVSA model service - application entry point. Mounts health, embed and embed_text routers."""

import logging
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from prometheus_fastapi_instrumentator import Instrumentator

from avsa_model.embed import router as embed_router
from avsa_model.embed_text import router as embed_text_router
from avsa_model.health import router as health_router

_log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Load the embedders at startup; store them in app.state for handlers to use."""
    stub_mode = os.environ.get("AVSA_MODEL_STUB", "0") == "1"

    if stub_mode:
        _log.info("AVSA_MODEL_STUB=1 - using stub embedders (no model weights downloaded)")
        from avsa_model.stub import StubEmbedder
        from avsa_model.text_stub import StubTextEmbedder

        app.state.embedder = StubEmbedder()
        app.state.text_embedder = StubTextEmbedder()
    else:
        _log.info("AVSA_MODEL_STUB=0 - loading google/vit-base-patch16-224 (may take a moment)")
        from avsa_model.vit import VitEmbedder

        app.state.embedder = VitEmbedder()

        _log.info("AVSA_MODEL_STUB=0 - loading CLIP text encoder (may take a moment)")
        from avsa_model.text_encoder import TextEncoder

        app.state.text_embedder = TextEncoder()

    yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(health_router)
app.include_router(embed_router)
app.include_router(embed_text_router)

Instrumentator().instrument(app).expose(app)
