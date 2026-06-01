"""AVSA locust load-test task sets.

BatcherUser -- QPS benchmark; targets the Rust batcher ``/embed``
                endpoint with a realistic >=224px Fashion200k JPEG drawn from
                the held-out test corpus.  Batcher singular contract:
                ``{"image_bytes": <b64>}``.

ModelUser -- model-direct QPS benchmark; bypasses the batcher
                and POSTs the model's plural contract ``{"images": [<b64>]}``
                directly to the ViT model service (default :8090).  This
                measures **raw model QPS** — the number the original brief asks
                for.  Use alongside BatcherUser to derive the batching delta
                (system_qps / raw_model_qps).

ChatUser -- realistic mixed-workload for ``/chat`` (end-to-
                end, multipart/form-data).  Uses real test-split Fashion200k
                images (>=224x224 px).  Mix weights are config-driven
                (``[workload.chat_embed]`` in ``config/avsa.toml``).

EmbedUser -- companion embed task set; targets ``/embed``
                with real test-split images at full saturation (model QPS
                measurement, equivalent to BatcherUser but with realistic
                payloads).

Opt-in
------
Both ChatUser and EmbedUser are only activated when the ``LOCUST_TASKS`` environment
variable is set to ``chat_embed`` (or any comma-separated list that includes
``chat_embed``).  When ``LOCUST_TASKS`` is unset or set to another value the
BatcherUser/ModelUser behaviour is unchanged.

    LOCUST_TASKS=chat_embed locust -f locustfile.py ...

Environment variables
---------------------
AVSA_BATCHER_URL  -- host for BatcherUser / EmbedUser (default
                     http://localhost:8081; matches [api.batcher_url] in
                     config/avsa.toml).
AVSA_MODEL_URL    -- host for ModelUser (default
                     http://localhost:8090).  Override to target a different
                     model service port.
AVSA_API_URL      -- host for ChatUser (default http://localhost:8080).
AVSA_DATA_ROOT    -- override the local image data root (default: ``data/``
                     relative to the repo root; used by BatcherUser/ModelUser
                     and ChatUser/EmbedUser to locate the held-out test-split
                     JPEGs).

Fetch handling
--------------
AVSA sends image *bytes* directly — the model service does NOT fetch a URL.
URL-based fetch was deliberately removed from the model hot path so that the
I/O overhead of HTTP fetching does not inflate model latency measurements.
The batcher receives pre-decoded bytes from the Python API; it base64-encodes
them before calling the model.  This design means the /embed QPS numbers
reflect pure inference throughput, not network I/O.  A fetch-inclusive
variant (where the model fetches a URL) is a non-production comparison point
and is not implemented here.
"""

import base64
import os
import random
import tomllib
from pathlib import Path
from typing import Any

from locust import HttpUser, between, constant, task

# ---------------------------------------------------------------------------
# Host defaults — override via environment variables.
# ---------------------------------------------------------------------------

# Batcher host — override via AVSA_BATCHER_URL to match your local setup.
# Default matches [api.batcher_url] in config/avsa.toml.
_BATCHER_HOST: str = os.getenv("AVSA_BATCHER_URL", "http://localhost:8081")

# Model service host — override via AVSA_MODEL_URL.
# Default matches [batcher.vit_service_url] minus the /embed path.
_MODEL_HOST: str = os.getenv("AVSA_MODEL_URL", "http://localhost:8090")


# ---------------------------------------------------------------------------
# Config helpers — read config/avsa.toml once at module import.
# ---------------------------------------------------------------------------


def _find_repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "config" / "avsa.toml").exists():
            return parent
    raise FileNotFoundError("config/avsa.toml not found in any parent directory")


def _load_avsa_toml() -> dict[str, Any]:
    root = _find_repo_root()
    with (root / "config" / "avsa.toml").open("rb") as fh:
        return tomllib.load(fh)


def _load_chat_embed_weights(
    config: dict[str, Any],
) -> tuple[float, float, float, float]:
    """Read chat_embed task weights from config; validate they sum to 1.0."""
    section: dict[str, Any] = config.get("workload", {}).get("chat_embed", {})
    w_img = float(section.get("image_only_weight", 0.35))
    w_txt = float(section.get("text_only_weight", 0.25))
    w_both = float(section.get("image_text_weight", 0.25))
    w_multi = float(section.get("multi_turn_weight", 0.15))
    total = w_img + w_txt + w_both + w_multi
    if abs(total - 1.0) > 1e-9:
        raise ValueError(
            f"[workload.chat_embed] weights must sum to 1.0; got {total:.6f} "
            f"(image_only={w_img}, text_only={w_txt}, image_text={w_both}, "
            f"multi_turn={w_multi})"
        )
    return w_img, w_txt, w_both, w_multi


# Load at module import so errors surface immediately when the file is imported.
_AVSA_CONFIG: dict[str, Any] = _load_avsa_toml()
_W_IMAGE_ONLY, _W_TEXT_ONLY, _W_IMAGE_TEXT, _W_MULTI_TURN = _load_chat_embed_weights(
    _AVSA_CONFIG
)

# ---------------------------------------------------------------------------
# Corpus loader — lazy, populated on first access so the module imports
# cleanly even on machines without the image corpus (CI).
# ---------------------------------------------------------------------------

_REPO_ROOT: Path = _find_repo_root()

# Override the data root for testing via AVSA_DATA_ROOT.
_DATA_ROOT: Path = Path(os.getenv("AVSA_DATA_ROOT", str(_REPO_ROOT / "data")))

# Corpus is loaded lazily (populated by _get_corpus() on first use).
_CORPUS: list[Any] | None = None


def _get_corpus() -> list[Any]:
    """Return the held-out test-split corpus items, loaded once."""
    global _CORPUS
    if _CORPUS is None:
        from evals.workload.chat_embed.corpus import load_test_corpus

        _CORPUS = load_test_corpus(
            data_root=_DATA_ROOT / "fashion200k" / "images",
        )
    return _CORPUS


def _read_image_bytes(item: Any) -> bytes:
    """Read JPEG bytes from the local corpus item's path."""
    return Path(item.local_path).read_bytes()


# ---------------------------------------------------------------------------
# BatcherUser — QPS benchmark task set (batcher-fronted, system QPS)
# ---------------------------------------------------------------------------


class BatcherUser(HttpUser):
    """Locust task class for the  /embed system-QPS benchmark.

    Sends a realistic >=224px Fashion200k test-split JPEG (base64-encoded)
    to the Rust batcher ``/embed`` endpoint.  Batch size and concurrency are
    controlled externally via the locust ``-u`` / ``-r`` CLI flags (driven by
    ``just bench-qps`` from values in ``config/avsa.toml [bench]``).

    This measures **system QPS** — requests through the batcher, which
    aggregates concurrent single-image calls into the plural model batch.
    Compare with ModelUser (model-direct, :8090) for the raw model QPS
    and the batching delta.

    No ``wait_time`` gap between requests -- we saturate the batcher to
    measure raw throughput. Each task result is a single sample in the
    QPS / latency distribution that the results writer aggregates.

    Payload
    -------
    Real Fashion200k test-split JPEG (>=224px) from the held-out corpus.
    The ViT resizes all inputs to 224x224 so inference cost is identical
    regardless of input dimensions, but using realistic images ensures
    decode/transfer cost is representative — important on fast GPUs where
    those costs are non-negligible.

    Batcher contract (singular)
    ---------------------------
    ``POST /embed`` with ``{"image_bytes": <b64>}``
    -> ``{"embedding": [...], "attributes": {...}}``

    This must match ``crates/batcher/src/routes/embed.rs`` + ``AVSA.EmbedStep``.
    """

    # No think-time: benchmark saturates the endpoint to measure peak throughput.
    wait_time = constant(0)

    # Host set to the batcher URL via the locust --host CLI flag (or AVSA_BATCHER_URL).
    host = _BATCHER_HOST

    @task
    def embed_single_image(self) -> None:
        """POST a realistic >=224px JPEG (base64-encoded) to the batcher /embed.

        Draws one image at random from the held-out Fashion200k test corpus.
        Using realistic images (vs a synthetic 3x3) ensures the payload cost
        (JSON encode/decode, HTTP transfer) is representative of production
        traffic — critical for GPU benchmarks where decode/transfer is not
        negligible.
        """
        corpus = _get_corpus()
        item = random.choice(corpus)
        img_bytes = _read_image_bytes(item)
        b64 = base64.b64encode(img_bytes).decode()
        self.client.post(
            "/embed",
            json={"image_bytes": b64},
            name="/embed",
        )


# ---------------------------------------------------------------------------
# ModelUser — model-direct QPS benchmark (raw model QPS)
# ---------------------------------------------------------------------------


class ModelUser(HttpUser):
    """Locust task class for the  raw model QPS benchmark.

    Bypasses the Rust batcher and POSTs the model's plural ``/embed``
    contract **directly to the ViT model service** (:8090 by default).
    This measures **raw model QPS** — the number the original Solenya brief
    requests: "maximise model QPS".

    Compare with BatcherUser (batcher-fronted, :8081) to derive the batching
    delta::

        batching_delta = system_qps / raw_model_qps

    On CPU/MPS the delta is ~1.0 (inference is sequential, batching adds no
    parallelism gain); on GPU a delta > 1 reveals the batching layer's payoff.

    Model contract (plural)
    -----------------------
    ``POST /embed`` with ``{"images": [<b64>, ...]}``
    -> ``{"embeddings": [[f32; 768], ...], "attributes": [...]}``

    This matches ``apps/model/src/avsa_model/embed.py`` EmbedRequest.

    Payload
    -------
    A single real Fashion200k test-split JPEG (>=224px).  One image per
    request mirrors the batcher's natural call pattern (it assembles a batch
    of such images before forwarding).  Using a single image per request
    produces the most comparable latency/QPS metric against BatcherUser.

    Fetch handling
    --------------
    AVSA sends image *bytes* — the model does NOT fetch a URL.  The fetch
    was deliberately removed from the model hot path so that network I/O does
    not inflate model latency measurements.  See module docstring for details.

    Activation
    ----------
    This class is always active (like BatcherUser).  Select it explicitly::

        locust -f locustfile.py -u 8 -r 4 -t 30s \\
            --host http://localhost:8090 \\
            ModelUser
    """

    # No think-time: saturate the model to measure peak raw throughput.
    wait_time = constant(0)

    # Model service host — override via AVSA_MODEL_URL.
    host = _MODEL_HOST

    @task
    def embed_single_image_direct(self) -> None:
        """POST a single realistic >=224px JPEG directly to the model /embed.

        Uses the model's plural contract: ``{"images": [<b64>]}``.
        A single-element list produces one embedding per request — matching
        the per-request granularity of BatcherUser for a fair QPS comparison.
        """
        corpus = _get_corpus()
        item = random.choice(corpus)
        img_bytes = _read_image_bytes(item)
        b64 = base64.b64encode(img_bytes).decode()
        self.client.post(
            "/embed",
            json={"images": [b64]},
            name="/embed [model_direct]",
        )


# ---------------------------------------------------------------------------
# ChatUser — realistic /chat mixed-workload task set
# ---------------------------------------------------------------------------

# API host for ChatUser.
_API_HOST: str = os.getenv("AVSA_API_URL", "http://localhost:8080")

# Weights as integers for locust's @task(weight) decorator.
# Locust requires integer weights; scale to the nearest integer from the
# float weights loaded from config.  The relative proportions are preserved.
_WEIGHT_SCALE: int = 100
_TW_IMAGE_ONLY: int = round(_W_IMAGE_ONLY * _WEIGHT_SCALE)
_TW_TEXT_ONLY: int = round(_W_TEXT_ONLY * _WEIGHT_SCALE)
_TW_IMAGE_TEXT: int = round(_W_IMAGE_TEXT * _WEIGHT_SCALE)
_TW_MULTI_TURN: int = round(_W_MULTI_TURN * _WEIGHT_SCALE)


class ChatUser(HttpUser):
    """Realistic end-to-end /chat workload.

    Drives the FastAPI ``/chat`` endpoint (multipart/form-data, per
    ``apps/api/src/avsa_api/routes/chat.py``) with real Fashion200k
    test-split images (>=224x224 px).  The task mix is config-driven via
    ``[workload.chat_embed]`` in ``config/avsa.toml``.

    Task mix
    --------
    image_only  — POST /chat with a real image, no text (image-only query).
    text_only   — POST /chat with a text phrase (real product title), no image.
    image_text  — POST /chat with a real image AND a refinement text prompt.
    multi_turn  — Two-turn session: image-only first POST /chat, then a second
                  POST /chat reusing the conversation_id returned in the first
                  response (simulates a real discovery session).

    Activation
    ----------
    Opt-in only.  Run with::

        LOCUST_TASKS=chat_embed locust -f locustfile.py \\
            --class-picker                              \\
            -u 20 -r 4 -t 60s                          \\
            --host http://localhost:8080

    or select both ChatUser and EmbedUser at once using ``--class-picker`` /
    ``-L ChatUser,EmbedUser``.
    """

    host = _API_HOST
    wait_time = between(0.5, 1.5)

    def on_start(self) -> None:
        """Assign a unique virtual-IP header to avoid rate-limit bucket sharing."""
        idx = id(self) % 65536
        self._forwarded_for: str = f"10.{(idx >> 8) & 0xFF}.{idx & 0xFF}.2"

    @task(_TW_IMAGE_ONLY)
    def image_only(self) -> None:
        """POST /chat with a real test-split image and no text prompt."""
        corpus = _get_corpus()
        item = random.choice(corpus)
        img_bytes = _read_image_bytes(item)
        self.client.post(
            "/chat",
            files={
                "image": (f"{item.item_id.rsplit('/', 1)[-1]}", img_bytes, "image/jpeg")
            },
            headers={"X-Forwarded-For": self._forwarded_for},
            name="/chat [image_only]",
        )

    @task(_TW_TEXT_ONLY)
    def text_only(self) -> None:
        """POST /chat with a text-only query (real product title phrase), no image."""
        corpus = _get_corpus()
        item = random.choice(corpus)
        self.client.post(
            "/chat",
            data={"text": item.title},
            headers={"X-Forwarded-For": self._forwarded_for},
            name="/chat [text_only]",
        )

    @task(_TW_IMAGE_TEXT)
    def image_text(self) -> None:
        """POST /chat with a real image AND a text refinement prompt."""
        corpus = _get_corpus()
        item = random.choice(corpus)
        img_bytes = _read_image_bytes(item)
        self.client.post(
            "/chat",
            files={
                "image": (f"{item.item_id.rsplit('/', 1)[-1]}", img_bytes, "image/jpeg")
            },
            data={"text": f"show me {item.category}s like this"},
            headers={"X-Forwarded-For": self._forwarded_for},
            name="/chat [image_text]",
        )

    @task(_TW_MULTI_TURN)
    def multi_turn(self) -> None:
        """Two-turn conversation: image-only first, then text refinement.

        Turn 1: POST /chat with a real image → capture the conversation id from
                the ``X-Conversation-Id`` **response header** (chat.py:201).
                There is no ``conversation_id`` SSE event — the server never
                emits one.
        Turn 2: POST /chat with just a text refinement, sending the captured id
                back in the ``X-Resume-Conversation-Id`` **request header**
                (chat.py:166-169).  Placing the id in a ``conversation_id``
                form field is silently ignored by the server (session-fixation
                guard, chat.py:165).

        If the header is absent or empty on the turn-1 response (e.g. the
        server returned an error), the second turn is skipped — the task still
        counts as a completed multi-turn attempt so the benchmark does not
        stall.
        """
        corpus = _get_corpus()
        item = random.choice(corpus)
        img_bytes = _read_image_bytes(item)

        # Turn 1 — image-only to open the conversation.
        resp1 = self.client.post(
            "/chat",
            files={
                "image": (f"{item.item_id.rsplit('/', 1)[-1]}", img_bytes, "image/jpeg")
            },
            headers={"X-Forwarded-For": self._forwarded_for},
            name="/chat [multi_turn/1]",
        )

        # Capture the server-issued conversation id from the response header.
        # The server returns it ONLY here; there is no SSE conversation_id event.
        conversation_id: str | None = resp1.headers.get("X-Conversation-Id") or None

        # Turn 2 — text refinement resuming the same conversation.
        # The id is sent back via X-Resume-Conversation-Id (the server's resume
        # contract).  A conversation_id form field would be ignored by the server.
        if conversation_id:
            self.client.post(
                "/chat",
                data={
                    "text": f"show me similar {item.category}s in a different colour",
                },
                headers={
                    "X-Forwarded-For": self._forwarded_for,
                    "X-Resume-Conversation-Id": conversation_id,
                },
                name="/chat [multi_turn/2]",
            )


# ---------------------------------------------------------------------------
# EmbedUser —  /embed model-QPS task set (realistic images)
# ---------------------------------------------------------------------------


class EmbedUser(HttpUser):
    """Companion embed task set with real Fashion200k images.

    Drives the Rust batcher ``/embed`` endpoint at full saturation (no
    wait_time), like BatcherUser, but sends real test-split JPEGs
    (>=224x224 px) so the measured QPS reflects realistic multipart cost.

    Activation
    ----------
    Opt-in via ``LOCUST_TASKS=chat_embed`` — same selector as
    ChatUser.  The two classes share the selector so a single
    ``locust --class-picker`` invocation can run both.

    Run example::

        LOCUST_TASKS=chat_embed locust -f locustfile.py \\
            -L EmbedUser                       \\
            -u 8 -r 4 -t 60s                           \\
            --host http://localhost:8081
    """

    host = _BATCHER_HOST
    wait_time = constant(0)

    @task
    def embed_real_image(self) -> None:
        """POST a real test-split JPEG (base64-encoded) to /embed."""
        corpus = _get_corpus()
        item = random.choice(corpus)
        img_bytes = _read_image_bytes(item)
        b64 = base64.b64encode(img_bytes).decode()
        self.client.post(
            "/embed",
            json={"image_bytes": b64},
            name="/embed [chat_embed]",
        )
