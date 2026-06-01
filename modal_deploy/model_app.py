"""Modal deployment — AVSA model service (GPU ViT inference).

Architecture matches thestacks/apps/vision/modal_app.py:

  1. AvsaModel   — GPU class (A10G) running google/vit-base-patch16-224 + CLIP
                   text encoder. Exposes:
                   - ``embed_batch`` / ``embed_text_batch`` — Modal SDK methods
                     (used by bench_in_memory and the legacy shim below).
                   - ``embed_http`` / ``embed_text_http`` / ``healthz_http`` —
                     direct @modal.fastapi_endpoint HTTP methods that call _do_embed
                     / _do_embed_text in-process (no cross-container RPC hop).
                   Weights are baked into the image at build time so cold starts
                   only pay weight-loading cost (~10s), not the download
                   (~600 MB for ViT + CLIP).

  2. model_api   — CPU ASGI function hosting a FastAPI shim. HTTP handler calls
                   AvsaModel methods via Modal SDK. Kept for backward
                   compatibility; the direct endpoints above eliminate the ~1.4 s
                   cross-container RPC overhead measured in §1c of
                   docs/qps-optimisation.md.

Deploy:  modal deploy modal_deploy/model_app.py
Serve:   modal serve modal_deploy/model_app.py   (hot-reload dev mode)

Direct GPU-class endpoints (shim collapsed, §18g of docs/qps-optimisation.md):
  embed:   https://erinversfeldcodes--avsa-model-avsamodel-embed-http.modal.run
  text:    https://erinversfeldcodes--avsa-model-avsamodel-embed-text-http.modal.run
  healthz: https://erinversfeldcodes--avsa-model-avsamodel-healthz-http.modal.run

Set AVSA_MODEL_URL to the embed URL above for the batcher deployment.
The legacy shim (model_api) remains deployed for backward compatibility:
  https://erinversfeldcodes--avsa-model-model-api.modal.run
"""

import os
from typing import Any

import modal

MODAL_APP_NAME = os.environ.get("MODAL_APP_NAME", "avsa-model")
_ViT_MODEL = "google/vit-base-patch16-224"
_CLIP_MODEL = "sentence-transformers/clip-ViT-B-32"

# ---------------------------------------------------------------------------
# Image — GPU class
# ---------------------------------------------------------------------------


def _download_models() -> None:
    """Pre-download ViT + CLIP weights into the container image at build time.

    Runs once during ``modal deploy`` (or when the image layer is invalidated).
    Subsequent cold starts load from local HuggingFace cache on disk, not from
    the network.
    """
    from sentence_transformers import SentenceTransformer
    from transformers import AutoFeatureExtractor, AutoModel

    AutoFeatureExtractor.from_pretrained(_ViT_MODEL)
    AutoModel.from_pretrained(_ViT_MODEL)
    SentenceTransformer(_CLIP_MODEL)


_model_image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install(
        # Required by @modal.fastapi_endpoint on the GPU class.
        "fastapi[standard]>=0.120,<0.130",
        "torch>=2.3.0",
        "transformers>=4.40,<4.47",
        "sentence-transformers>=2.7,<4.0",
        "Pillow>=10.0,<12.0",
        "numpy>=1.26,<3",
        "onnxruntime-gpu>=1.18,<2.0",
        "onnx>=1.16,<2.0",
        # TRT §3m: TensorRT + CUDA Python bindings for TRT 10 API (execute_async_v3).
        # tensorrt bundles the TRT runtime; cuda-python provides the stream handle type.
        "tensorrt>=10.0,<11.0",
        "cuda-python>=12.0,<13.0",
        # torch_tensorrt: torch.compile backend that routes compilation through TRT.
        # Version tracks PyTorch major.minor (torch 2.9 → torch_tensorrt 2.9.x).
        "torch_tensorrt>=2.0,<3.0",
    )
    .env({"PYTHONPATH": "/app/src", "AVSA_CUDA_THREAD_PINNED": "1"})
    .run_function(_download_models)
    .add_local_dir("apps/model/src", remote_path="/app/src")
    .add_local_file("config/avsa.toml", remote_path="/app/config/avsa.toml")
    .add_local_dir("data/attribute_heads", remote_path="/app/data/attribute_heads")
)

# ---------------------------------------------------------------------------
# Image — ASGI (CPU, lightweight; calls AvsaModel via SDK)
# ---------------------------------------------------------------------------

_api_image = modal.Image.debian_slim(python_version="3.12").pip_install(
    "fastapi>=0.120,<0.130",
    "uvicorn[standard]>=0.27,<0.35",
    "pydantic>=2.6,<3.0",
    # modal SDK is installed in the ASGI container so it can call the GPU class.
    "modal>=1.0,<2.0",
)

app = modal.App(MODAL_APP_NAME)


# ---------------------------------------------------------------------------
# GPU class
# ---------------------------------------------------------------------------


@app.cls(
    gpu="A10G",
    image=_model_image,
    max_containers=6,
    # Keep 5 containers always warm — guarantees the first 5 drain tasks never
    # cold-start. Task 6 may trigger scale-up on first use; min=5 keeps the
    # gap between warm pool and N small to minimise cold-start latency spikes.
    min_containers=5,
    # 10-minute timeout: covers torch.compile warmup (~30s) + inference.
    timeout=600,
    scaledown_window=1200,
    # Route inference to the allocated A10G. The config default is "cpu"
    # (CI-safe); this secret env override is required to actually use the GPU.
    secrets=[
        modal.Secret.from_dict(
            {"AVSA_MODEL_DEVICE": "cuda", "AVSA_CUDA_THREAD_PINNED": "1"}
        )
    ],
)
class AvsaModel:
    @modal.enter()
    def load(self) -> None:
        """Load ViT + attribute heads + CLIP text encoder onto GPU.

        When use_trt=True (§3m), VitEmbedder.__init__ calls _build_trt_engine(),
        which exports to ONNX and runs the TRT builder (~60s on first cold start;
        subsequent starts deserialize the cached engine from /tmp). The warmup
        loop pre-heats the TRT execution context for all batch sizes 1-24.
        When use_compile=True (baseline), torch.compile compiles lazily (~25s)
        on the first forward pass — the warmup loop prevents that from stalling
        real requests.

        Thread-pinning: torch.compile's reduce-overhead backend records CUDA graphs
        in the thread that first runs a forward pass. Those graphs can only be
        replayed from the same thread. Modal's @fastapi_endpoint routes sync handlers
        through anyio's thread pool (a different thread each call). To avoid the
        "already recording to mempool_id" error, ALL forward passes — warmup and
        serving — are dispatched through self._cuda_ex, a single-worker
        ThreadPoolExecutor. That guarantees one thread owns all CUDA graph state.
        """
        import concurrent.futures
        import os
        import sys

        sys.path.insert(0, "/app/src")
        # avsa.toml uses relative paths (e.g. ./data/attribute_heads/...).
        # The Modal container CWD is /root, not /app — change here so all
        # relative config paths resolve correctly.
        os.chdir("/app")

        # Single-threaded executor: ALL CUDA activity (model loading, compilation,
        # warmup, and every inference call) runs in T1 — the one worker thread.
        # This prevents torch.compile's CUDA graphs from being recorded in the
        # main thread and replayed in an anyio worker thread, which causes
        # "beginAllocateToPool: already recording to mempool_id".
        self._cuda_ex = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="cuda-worker"
        )

        # Load models AND run warmup entirely inside T1.
        # Timeout matches @app.cls(timeout=600); subtract headroom for model load + CUDA graph warmup.
        self._cuda_ex.submit(self._load_and_warmup).result(timeout=580)

    # ------------------------------------------------------------------
    # Warmup (must run in _cuda_ex thread)
    # ------------------------------------------------------------------

    def _load_and_warmup(self) -> None:
        """Load models and warm CUDA graphs — runs entirely inside _cuda_ex (T1).

        Keeping model instantiation, torch.compile compilation, CUDA graph
        recording, and all inference in the same thread avoids the
        "beginAllocateToPool: already recording to mempool_id" error that occurs
        when graphs are captured in one thread and replayed in another.
        """
        import io
        import tomllib

        from avsa_model.text_encoder import TextEncoder
        from avsa_model.vit import VitEmbedder
        from PIL import Image as PILImage

        # Model loading (moves weights to CUDA) happens in T1.
        self.embedder = VitEmbedder()
        self.text_embedder = TextEncoder()

        _buf = io.BytesIO()
        PILImage.new("RGB", (224, 224), color=(128, 128, 128)).save(_buf, format="JPEG")
        _img = _buf.getvalue()

        try:
            with open("/app/config/avsa.toml", "rb") as _f:
                _cfg = tomllib.load(_f)
                _compile_mode = _cfg.get("model", {}).get(
                    "compile_mode", "reduce-overhead"
                )
        except Exception:
            _compile_mode = "reduce-overhead"

        # reduce-overhead (CUDA graphs): every size 1-24 needs its own graph.
        # torch_tensorrt: cover only 3 bench-critical shapes (see load() docstring).
        _warm_shapes = [1, 8, 24] if _compile_mode == "torch_tensorrt" else range(1, 25)
        for _n in _warm_shapes:
            self.embedder.embed_with_attributes([_img] * _n)

    # ------------------------------------------------------------------
    # Shared implementation helpers — always dispatched through _cuda_ex
    # so CUDA graph replay happens in the correct thread.
    # ------------------------------------------------------------------

    def _do_embed(self, images_b64: list[str]) -> dict[str, Any]:
        import base64

        raw = [base64.b64decode(b) for b in images_b64]
        embeddings, attributes = self.embedder.embed_with_attributes(raw)
        return {
            "embeddings": embeddings,
            "attributes": [
                {
                    "category": a.category,
                    "colour": a.colour,
                    "category_confidence": a.category_confidence,
                    "colour_confidence": a.colour_confidence,
                }
                for a in attributes
            ],
        }

    def _do_embed_text(self, texts: list[str]) -> dict[str, Any]:
        embeddings = self.text_embedder.encode(texts)
        return {"embeddings": embeddings}

    # ------------------------------------------------------------------
    # Modal SDK methods — used by bench_in_memory and the legacy shim
    # ------------------------------------------------------------------

    @modal.method()
    def embed_batch(self, images_b64: list[str]) -> dict[str, Any]:
        """Run ViT + attribute heads on a batch of base64-encoded images."""
        return self._cuda_ex.submit(lambda: self._do_embed(images_b64)).result()

    @modal.method()
    def embed_text_batch(self, texts: list[str]) -> dict[str, Any]:
        """Run CLIP text encoder on a list of strings."""
        return self._cuda_ex.submit(lambda: self._do_embed_text(texts)).result()

    # ------------------------------------------------------------------
    # Direct HTTP endpoints — no CPU shim, no cross-container RPC.
    # Batcher AVSA_MODEL_URL (helm/values.yaml) points here (§18g).
    # ------------------------------------------------------------------

    @modal.fastapi_endpoint(method="GET")
    def healthz_http(self) -> dict[str, str]:
        return {"status": "ok"}

    @modal.fastapi_endpoint(method="POST")
    def embed_http(self, item: dict) -> dict[str, Any]:
        images = item.get("images") or []
        if not images:
            from fastapi import HTTPException

            raise HTTPException(status_code=422, detail="images list must not be empty")
        # Dispatch to the dedicated CUDA thread so CUDA graphs are replayed from
        # the same thread that captured them during warmup (see load() docstring).
        return self._cuda_ex.submit(lambda: self._do_embed(images)).result()

    @modal.fastapi_endpoint(method="POST")
    def embed_text_http(self, item: dict) -> dict[str, Any]:
        texts = item.get("texts") or []
        if not texts:
            from fastapi import HTTPException

            raise HTTPException(status_code=422, detail="texts list must not be empty")
        return self._cuda_ex.submit(lambda: self._do_embed_text(texts)).result()

    @modal.method()
    def bench_in_memory(
        self, batch_size: int = 1, n_passes: int = 50
    ) -> dict[str, Any]:
        """Time VitEmbedder.embed() in-process; return QPS + latency.

        No HTTP round-trip — measures the GPU compute ceiling directly.
        Called by scripts/bench-qps.py via the Modal SDK when --target=model
        points at a Modal endpoint, bypassing the FastAPI shim and network.

        NOTE: calls embedder.embed(), not embed_with_attributes(). The
        production path (embed_batch) runs embed_with_attributes(), which adds
        the category and colour attribute-head forward passes on top of the ViT
        trunk. The GPU ceiling reported here therefore slightly overstates the
        real production ceiling; the gap is small (attribute heads are cheap
        relative to ViT) but the number is not an exact production proxy.
        """
        return self._cuda_ex.submit(
            lambda: self._bench_impl(batch_size, n_passes)
        ).result()

    def _bench_impl(self, batch_size: int, n_passes: int) -> dict[str, Any]:
        """Inner bench loop — runs inside _cuda_ex (the dedicated CUDA thread)."""
        import io
        import time

        from PIL import Image as PILImage  # type: ignore[import-not-found]

        images: list[bytes] = []
        for i in range(batch_size):
            buf = io.BytesIO()
            PILImage.new("RGB", (224, 224), color=(i * 10 % 256, 40, 80)).save(
                buf, format="JPEG"
            )
            images.append(buf.getvalue())

        # Extra warmup to settle CUDA graph state (load() already warmed bs=1
        # and bs=24; caller may pass a different batch_size).
        for _ in range(3):
            self.embedder.embed(images)

        latencies: list[float] = []
        for _ in range(n_passes):
            t0 = time.perf_counter()
            self.embedder.embed(images)
            latencies.append((time.perf_counter() - t0) * 1000.0)

        latencies.sort()
        total_s = sum(latencies) / 1000.0
        qps = (n_passes * batch_size) / total_s if total_s > 0 else 0.0
        return {
            "qps": qps,
            "p50_ms": latencies[n_passes // 2],
            "p95_ms": latencies[min(n_passes - 1, int(n_passes * 0.95))],
            "batch_size": batch_size,
            "n_passes": n_passes,
            "device": self.embedder.device,
        }


# ---------------------------------------------------------------------------
# ASGI shim (CPU) — thin FastAPI that proxies to AvsaModel via Modal SDK
# ---------------------------------------------------------------------------


@app.function(
    image=_api_image,
    secrets=[modal.Secret.from_dict({"MODAL_APP_NAME": MODAL_APP_NAME})],
    scaledown_window=1200,
)
@modal.asgi_app()
def model_api() -> Any:
    import os

    import modal as _modal
    from fastapi import FastAPI, HTTPException, Request

    _app_name = os.environ.get("MODAL_APP_NAME", MODAL_APP_NAME)
    _model_cls = _modal.Cls.from_name(_app_name, "AvsaModel")

    fastapi_app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @fastapi_app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @fastapi_app.post("/embed")
    async def embed(request: Request) -> dict[str, Any]:
        # Parse manually: Pydantic models defined inside a function don't have
        # module-level scope, so FastAPI's get_type_hints can't resolve them.
        data = await request.json()
        images = data.get("images", [])
        if not images:
            raise HTTPException(status_code=422, detail="images list must not be empty")
        model = _model_cls()
        return await model.embed_batch.remote.aio(images)  # type: ignore[no-any-return]

    @fastapi_app.post("/embed-text")
    async def embed_text(request: Request) -> dict[str, Any]:
        data = await request.json()
        texts = data.get("texts", [])
        if not texts:
            raise HTTPException(status_code=422, detail="texts list must not be empty")
        model = _model_cls()
        return await model.embed_text_batch.remote.aio(texts)  # type: ignore[no-any-return]

    return fastapi_app
