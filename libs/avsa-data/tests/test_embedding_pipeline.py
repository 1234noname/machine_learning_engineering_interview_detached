"""Failing tests for — Offline embedding pre-compute pipeline.

Authored at step 2A-i (pre-implementation). The module under test does not
yet exist; we import inside a try/except so collection succeeds and each
test fails with a meaningful assertion failure (``pytest.fail`` /
``AssertionError`` / domain-exception assertion) — per
docs/agents/standards/testing.md § "Test-first protocol".

Module-location choice: ``avsa_data.embedding_pipeline`` (in-app module),
mirroring the placement of ``avsa_data.acquisition`` and
``avsa_data.fashion200k_metadata`` from .

Artifact format choice: **JSONL** (one row per line) rather than parquet.
Rationale: ``apps/api/pyproject.toml`` has neither ``pyarrow`` nor
``polars`` listed as deps (grep confirms zero hits); adding a heavyweight
columnar dep just to round-trip ~15k rows of float lists is not warranted
for an offline pipeline. JSONL preserves byte-equality semantics for
round-trip tests without a new dep. The implementation should emit
``embeddings.jsonl`` under ``<hash>/`` and a sibling ``manifest.json``.

Model-service contract (verified against
``apps/model/src/avsa_model/embed.py`` and
``apps/model/src/avsa_model/embed_text.py``):

- ``POST /embed`` — request ``{"images": [base64-string, ...]}``;
  response ``{"embeddings": [[f32; 768], ...]}``.
- ``POST /embed_text`` — request ``{"texts": ["string", ...]}``;
  response ``{"embeddings": [[f32; 512], ...]}``.

NB: The route is ``/embed_text`` (underscore) at the model service. The
issue brief used ``/embed-text`` (hyphen) — see Pre-implementation Flags
in the completion report; the canonical route name is the underscore
form because that is what the model service exposes today.

The fetch boundary is mocked with ``respx`` (already a dev dep). No live
network calls.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

try:
    from avsa_data.embedding_pipeline import (
        EmbeddingArtifactManifest,
        compute_content_hash,
        compute_embeddings,
        load_embedding_artifact,
        write_embedding_artifact,
    )

    _PIPELINE_AVAILABLE = True
except ImportError:
    _PIPELINE_AVAILABLE = False

try:
    from avsa_core.storage.local import LocalStorageBackend

    _STORAGE_AVAILABLE = True
except ImportError:
    _STORAGE_AVAILABLE = False


def _require_pipeline() -> None:
    if not _PIPELINE_AVAILABLE:
        pytest.fail(
            "avsa_data.embedding_pipeline (EmbeddingArtifactManifest / "
            "compute_content_hash / compute_embeddings / write_embedding_artifact / "
            "load_embedding_artifact) not implemented yet — expected during 2A-i "
            "pre-implementation. Implement per "
            "plans/061-071-real-catalog-and-dual-head-plan.md § Phase 2a."
        )


def _require_storage() -> None:
    if not _STORAGE_AVAILABLE:
        pytest.fail(
            "avsa_core.storage.local.LocalStorageBackend not importable — "
            "embedding-pipeline tests depend on a StorageBackend implementation "
            "(landed in )."
        )


# ----------------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------------


_MODEL_URL = "http://model.test"


def _three_items() -> list[dict[str, object]]:
    """Three minimal item rows shaped like the #061 manifest entries.

    Each item carries an ``id``, a ``title`` / ``description`` for the text
    embed call, and an ``image_bytes`` payload (the implementation will
    fetch these from storage in the real pipeline; tests pre-supply them
    inline so the assertion focuses on the embedding contract, not on
    storage I/O).
    """
    return [
        {
            "id": "women/dresses/A/a_0.jpeg",
            "title": "red dress",
            "description": "red dress",
            "image_bytes": b"a-bytes",
        },
        {
            "id": "women/dresses/B/b_0.jpeg",
            "title": "blue dress",
            "description": "blue dress",
            "image_bytes": b"b-bytes",
        },
        {
            "id": "women/dresses/C/c_0.jpeg",
            "title": "green dress",
            "description": "green dress",
            "image_bytes": b"c-bytes",
        },
    ]


def _zeros(n: int) -> list[float]:
    """Stand-in vector for mocked embed responses; length is what matters."""
    return [0.0] * n


def _config_baseline() -> dict[str, object]:
    return {
        "model_version_image": "vit-b-16@2026-05-01",
        "model_version_text": "minilm-l6-v2@2026-05-01",
        "subset_count": 15_000,
        "dataset_version": "fashion200k-v1.0",
    }


# ----------------------------------------------------------------------------
# compute_content_hash
# ----------------------------------------------------------------------------


def test_compute_content_hash_is_deterministic() -> None:
    """Same config → same hash across two calls."""
    _require_pipeline()
    config = _config_baseline()
    first = compute_content_hash(config)
    second = compute_content_hash(config)
    assert first == second, (
        f"compute_content_hash must be deterministic; got {first!r} then {second!r}"
    )
    assert isinstance(first, str) and len(first) > 0, (
        f"hash must be a non-empty string; got {first!r}"
    )


def test_compute_content_hash_changes_with_config_change() -> None:
    """Mutating any key (model_version, subset_count, dataset_version) changes the hash."""
    _require_pipeline()
    base = _config_baseline()
    base_hash = compute_content_hash(base)

    bumped_image = dict(base)
    bumped_image["model_version_image"] = "vit-b-16@2026-06-01"
    assert compute_content_hash(bumped_image) != base_hash, (
        "changing model_version_image must change the content hash"
    )

    bumped_text = dict(base)
    bumped_text["model_version_text"] = "minilm-l6-v2@2026-06-01"
    assert compute_content_hash(bumped_text) != base_hash, (
        "changing model_version_text must change the content hash"
    )

    bumped_count = dict(base)
    bumped_count["subset_count"] = 14_999
    assert compute_content_hash(bumped_count) != base_hash, (
        "changing subset_count must change the content hash"
    )

    bumped_dataset = dict(base)
    bumped_dataset["dataset_version"] = "fashion200k-v1.1"
    assert compute_content_hash(bumped_dataset) != base_hash, (
        "changing dataset_version must change the content hash"
    )


def test_compute_content_hash_is_key_order_independent() -> None:
    """``{'a': 1, 'b': 2}`` hashes the same as ``{'b': 2, 'a': 1}``."""
    _require_pipeline()
    left = {"a": 1, "b": 2, "c": 3}
    right = {"c": 3, "a": 1, "b": 2}
    assert compute_content_hash(left) == compute_content_hash(right), (
        "compute_content_hash must sort keys before hashing so a config built "
        "in a different insertion order produces the same hash"
    )


# ----------------------------------------------------------------------------
# compute_embeddings — wire contract
# ----------------------------------------------------------------------------


def _mock_model_service(
    router: respx.MockRouter,
    image_dim: int = 768,
    text_dim: int = 512,
) -> tuple[respx.Route, respx.Route]:
    """Wire up the ``POST /embed`` and ``POST /embed_text`` mocks.

    The model service accepts BATCHED requests (per
    ``apps/model/src/avsa_model/embed.py``), so the mock inspects the
    request payload and returns N vectors of the requested dim.
    """

    def _image_handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode())
        n = len(payload.get("images", []))
        return httpx.Response(200, json={"embeddings": [_zeros(image_dim)] * n})

    def _text_handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode())
        n = len(payload.get("texts", []))
        return httpx.Response(200, json={"embeddings": [_zeros(text_dim)] * n})

    image_route = router.post(f"{_MODEL_URL}/embed").mock(side_effect=_image_handler)
    text_route = router.post(f"{_MODEL_URL}/embed_text").mock(side_effect=_text_handler)
    return image_route, text_route


async def test_compute_embeddings_emits_one_row_per_item() -> None:
    """Three input items → three output rows."""
    _require_pipeline()
    items = _three_items()

    async with httpx.AsyncClient() as client:
        with respx.mock(assert_all_called=False) as router:
            _mock_model_service(router)
            rows = await compute_embeddings(
                items=items,
                model_url=_MODEL_URL,
                batch_size=2,
                client=client,
            )

    assert len(rows) == len(items), (
        f"compute_embeddings must emit one row per input item; got {len(rows)} for "
        f"{len(items)} items"
    )


async def test_compute_embeddings_image_dim_is_768() -> None:
    """Every row's ``image_embedding`` is exactly 768 floats."""
    _require_pipeline()
    items = _three_items()

    async with httpx.AsyncClient() as client:
        with respx.mock(assert_all_called=False) as router:
            _mock_model_service(router, image_dim=768)
            rows = await compute_embeddings(
                items=items,
                model_url=_MODEL_URL,
                batch_size=2,
                client=client,
            )

    for i, row in enumerate(rows):
        assert "image_embedding" in row, (
            f"row {i} missing 'image_embedding'; got keys {sorted(row.keys())}"
        )
        assert len(row["image_embedding"]) == 768, (
            f"row {i} image_embedding has dim {len(row['image_embedding'])}; "
            "expected 768 per the ViT-b-16 contract"
        )


async def test_compute_embeddings_text_dim_is_512() -> None:
    """Every row's ``text_embedding`` is exactly 512 floats."""
    _require_pipeline()
    items = _three_items()

    async with httpx.AsyncClient() as client:
        with respx.mock(assert_all_called=False) as router:
            _mock_model_service(router, text_dim=512)
            rows = await compute_embeddings(
                items=items,
                model_url=_MODEL_URL,
                batch_size=2,
                client=client,
            )

    for i, row in enumerate(rows):
        assert "text_embedding" in row, (
            f"row {i} missing 'text_embedding'; got keys {sorted(row.keys())}"
        )
        assert len(row["text_embedding"]) == 512, (
            f"row {i} text_embedding has dim {len(row['text_embedding'])}; "
            "expected 512 per the  text encoder contract"
        )


async def test_compute_embeddings_preserves_item_order() -> None:
    """Output ids match the input ids in order — downstream seeder relies on this."""
    _require_pipeline()
    items = _three_items()
    expected_ids = [str(i["id"]) for i in items]

    async with httpx.AsyncClient() as client:
        with respx.mock(assert_all_called=False) as router:
            _mock_model_service(router)
            rows = await compute_embeddings(
                items=items,
                model_url=_MODEL_URL,
                batch_size=2,
                client=client,
            )

    actual_ids = [str(r["id"]) for r in rows]
    assert actual_ids == expected_ids, (
        "compute_embeddings must preserve input order (downstream consumers "
        f"index by position). Expected {expected_ids!r}, got {actual_ids!r}"
    )


async def test_compute_embeddings_calls_model_endpoints_per_item() -> None:
    """Across all batches, every input item is exercised by both endpoints.

    With three items and batch_size=2, the implementation may invoke /embed
    twice (e.g. 2+1) or three times (1+1+1). The robust assertion is the
    total *image count* across calls equals 3 — same for /embed_text.
    """
    _require_pipeline()
    items = _three_items()

    async with httpx.AsyncClient() as client:
        with respx.mock(assert_all_called=True) as router:
            image_route, text_route = _mock_model_service(router)
            await compute_embeddings(
                items=items,
                model_url=_MODEL_URL,
                batch_size=2,
                client=client,
            )

        # Count the items observed across all calls to each route.
        image_items_seen = 0
        for call in image_route.calls:
            payload = json.loads(call.request.content.decode())
            image_items_seen += len(payload.get("images", []))
        text_items_seen = 0
        for call in text_route.calls:
            payload = json.loads(call.request.content.decode())
            text_items_seen += len(payload.get("texts", []))

        assert image_items_seen == len(items), (
            f"/embed must see {len(items)} images across all batched calls; saw {image_items_seen}"
        )
        assert text_items_seen == len(items), (
            f"/embed_text must see {len(items)} texts across all batched calls; "
            f"saw {text_items_seen}"
        )


async def test_compute_embeddings_image_payload_is_base64() -> None:
    """The wire payload sent to /embed must be base64-encoded strings.

    Per ``apps/model/src/avsa_model/embed.py`` the route validates each
    image string is decodable via ``base64.b64decode(..., validate=True)``.
    If the pipeline sends raw bytes (or a non-base64 string), the real
    model service would return 400 — catch that here.
    """
    _require_pipeline()
    items = _three_items()

    async with httpx.AsyncClient() as client:
        with respx.mock(assert_all_called=True) as router:
            image_route, _text_route = _mock_model_service(router)
            await compute_embeddings(
                items=items,
                model_url=_MODEL_URL,
                batch_size=3,
                client=client,
            )

        # Inspect the first /embed call's payload — every image must be a
        # base64-decodable string that round-trips to the raw image bytes.
        assert image_route.call_count >= 1, "expected at least one /embed call"
        payload = json.loads(image_route.calls[0].request.content.decode())
        images_field = payload.get("images")
        assert isinstance(images_field, list), (
            f"/embed payload must carry an 'images' list; got {payload!r}"
        )
        for b64 in images_field:
            assert isinstance(b64, str), (
                f"each image in the /embed payload must be a string (base64); got {type(b64)!r}"
            )
            # Round-trip — raises binascii.Error on non-base64 input, which
            # surfaces here as the test failure with a meaningful message.
            base64.b64decode(b64, validate=True)


# ----------------------------------------------------------------------------
# write_embedding_artifact + load_embedding_artifact
# ----------------------------------------------------------------------------


def _sample_embeddings(n: int = 3) -> list[dict[str, object]]:
    return [
        {
            "id": f"item_{i}",
            "image_embedding": _zeros(768),
            "text_embedding": _zeros(512),
        }
        for i in range(n)
    ]


def _sample_manifest(content_hash: str = "deadbeef") -> dict[str, object]:
    return {
        "model_version_image": "vit-b-16@2026-05-01",
        "model_version_text": "minilm-l6-v2@2026-05-01",
        "image_dim": 768,
        "text_dim": 512,
        "item_count": 3,
        "content_hash": content_hash,
        "generated_at": "2026-05-25T00:00:00Z",
    }


def test_write_embedding_artifact_creates_manifest_and_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Writing emits both ``<hash>/embeddings.jsonl`` and ``<hash>/manifest.json``."""
    _require_pipeline()
    _require_storage()
    monkeypatch.setenv("AVSA_STORAGE_HMAC_SECRET", "test-secret")
    backend = LocalStorageBackend(root=tmp_path)

    embeddings = _sample_embeddings()
    manifest = _sample_manifest("hash-abc")
    out_dir = Path("data/embeddings/hash-abc")

    write_embedding_artifact(
        out_dir=out_dir,
        embeddings=embeddings,
        manifest=manifest,  # type: ignore[arg-type]
        backend=backend,
    )

    # Two files must exist under <hash>/.
    listed = sorted(backend.list_objects("data/embeddings/hash-abc"))
    assert len(listed) >= 2, (
        f"write_embedding_artifact must emit at least 2 files under <hash>/; got {listed!r}"
    )

    manifest_keys = {p for p in listed if p.endswith("manifest.json")}
    bundle_keys = {p for p in listed if p.endswith(".jsonl") or p.endswith(".parquet")}
    assert manifest_keys, f"manifest.json missing from artifact bundle; got {listed!r}"
    assert bundle_keys, (
        f"embeddings bundle (.jsonl or .parquet) missing from artifact bundle; got {listed!r}"
    )


def test_write_embedding_artifact_manifest_has_required_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicit key-set assertion on the persisted manifest JSON."""
    _require_pipeline()
    _require_storage()
    monkeypatch.setenv("AVSA_STORAGE_HMAC_SECRET", "test-secret")
    backend = LocalStorageBackend(root=tmp_path)

    embeddings = _sample_embeddings()
    manifest = _sample_manifest("hash-required-keys")
    out_dir = Path("data/embeddings/hash-required-keys")

    write_embedding_artifact(
        out_dir=out_dir,
        embeddings=embeddings,
        manifest=manifest,  # type: ignore[arg-type]
        backend=backend,
    )

    raw = backend.get_object("data/embeddings/hash-required-keys/manifest.json")
    parsed = json.loads(raw.decode("utf-8"))

    required = {
        "model_version_image",
        "model_version_text",
        "image_dim",
        "text_dim",
        "item_count",
        "content_hash",
        "generated_at",
    }
    missing = required - set(parsed.keys())
    assert not missing, (
        f"persisted manifest is missing required keys {sorted(missing)!r}; "
        f"got keys {sorted(parsed.keys())!r}"
    )
    assert parsed["image_dim"] == 768, (
        f"manifest.image_dim must round-trip the input value 768; got {parsed['image_dim']!r}"
    )
    assert parsed["text_dim"] == 512, (
        f"manifest.text_dim must round-trip the input value 512; got {parsed['text_dim']!r}"
    )
    assert parsed["item_count"] == 3, (
        f"manifest.item_count must round-trip the input value 3; got {parsed['item_count']!r}"
    )


def test_load_embedding_artifact_round_trips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Write then load — embeddings list and manifest match exactly."""
    _require_pipeline()
    _require_storage()
    monkeypatch.setenv("AVSA_STORAGE_HMAC_SECRET", "test-secret")
    backend = LocalStorageBackend(root=tmp_path)

    embeddings = _sample_embeddings()
    manifest = _sample_manifest("hash-roundtrip")
    out_dir = Path("data/embeddings/hash-roundtrip")

    write_embedding_artifact(
        out_dir=out_dir,
        embeddings=embeddings,
        manifest=manifest,  # type: ignore[arg-type]
        backend=backend,
    )

    loaded_embeddings, loaded_manifest = load_embedding_artifact(
        artifact_dir=out_dir,
        backend=backend,
    )

    # Manifest round-trip.
    for key, expected in manifest.items():
        assert loaded_manifest[key] == expected, (  # type: ignore[literal-required]
            f"manifest key {key!r} did not round-trip; expected {expected!r}, "
            f"got {loaded_manifest[key]!r}"  # type: ignore[literal-required]
        )

    # Embeddings round-trip — same length, same per-row contents.
    assert len(loaded_embeddings) == len(embeddings), (
        f"embeddings count mismatch on round-trip; wrote {len(embeddings)}, "
        f"loaded {len(loaded_embeddings)}"
    )
    for i, (orig, loaded) in enumerate(zip(embeddings, loaded_embeddings, strict=True)):
        assert loaded["id"] == orig["id"], (
            f"row {i} id mismatch; wrote {orig['id']!r}, loaded {loaded['id']!r}"
        )
        assert list(loaded["image_embedding"]) == list(orig["image_embedding"]), (
            f"row {i} image_embedding did not round-trip exactly"
        )
        assert list(loaded["text_embedding"]) == list(orig["text_embedding"]), (
            f"row {i} text_embedding did not round-trip exactly"
        )


def test_write_embedding_artifact_creates_parent_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Backend root is empty; ``<hash>/`` doesn't exist — write succeeds."""
    _require_pipeline()
    _require_storage()
    monkeypatch.setenv("AVSA_STORAGE_HMAC_SECRET", "test-secret")
    backend = LocalStorageBackend(root=tmp_path)

    out_dir = Path("data/embeddings/brand-new-hash")
    # Sanity: the directory does not pre-exist.
    assert not (tmp_path / out_dir).exists(), (
        "fixture pre-condition violated: out_dir must not pre-exist for this test"
    )

    write_embedding_artifact(
        out_dir=out_dir,
        embeddings=_sample_embeddings(),
        manifest=_sample_manifest("brand-new-hash"),  # type: ignore[arg-type]
        backend=backend,
    )

    # After the write the artifact directory and at least one file should
    # be present.
    listed = list(backend.list_objects("data/embeddings/brand-new-hash"))
    assert listed, (
        "write_embedding_artifact must create parent directories on demand; "
        f"got empty listing under {out_dir!s}"
    )


def test_embedding_artifact_manifest_typeddict_keys_match_required() -> None:
    """The TypedDict exposed by the module exactly enumerates the manifest schema.

    Asserts the static type that downstream callers (#063 seeder) will
    import has the same key set as the persisted JSON. If the TypedDict
    and the writer drift, the seeder will get a stale contract.
    """
    _require_pipeline()
    # TypedDicts carry their key names on ``__annotations__``.
    annotations: dict[str, Any] = dict(EmbeddingArtifactManifest.__annotations__)
    required = {
        "model_version_image",
        "model_version_text",
        "image_dim",
        "text_dim",
        "item_count",
        "content_hash",
        "generated_at",
    }
    assert set(annotations.keys()) == required, (
        f"EmbeddingArtifactManifest must declare exactly the keys {sorted(required)!r}; "
        f"got {sorted(annotations.keys())!r}"
    )


# Silence unused-import warnings when running under specific configurations.
_ = httpx
