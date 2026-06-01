"""Offline embedding pre-compute pipeline.

Surface:

- ``EmbeddingArtifactManifest`` — TypedDict pinning the manifest schema
  the seeder imports.
- ``compute_content_hash(config)`` — deterministic SHA-256 over the
  config that uniquely identifies the artifact (model versions, subset
  count, dataset version, etc.). Key-order independent.
- ``compute_embeddings(items, model_url, batch_size, client)`` — async
  fan-out over the model service. Batches inputs into groups of
  ``batch_size`` and posts to ``/embed`` (images, 768-d) and
  ``/embed_text`` (texts, 512-d). Returns one row per input item, order
  preserved, so downstream consumers can index by position.
- ``write_embedding_artifact(out_dir, embeddings, manifest, backend)``
  — persists ``embeddings.jsonl`` + ``manifest.json`` under ``out_dir``
  via ``StorageBackend.put_object``.
- ``load_embedding_artifact(artifact_dir, backend)`` — round-trip reader
  for the seeder.

Module-location choice mirrors Phase 1's placement
(``avsa_data.acquisition``, ``avsa_data.fashion200k_metadata``): the
in-app package is the natural home for shared offline-pipeline code.

Artifact-format choice: JSONL rather than parquet — ``apps/api/pyproject.toml``
carries neither ``pyarrow`` nor ``polars``, and a heavyweight columnar
dep just to round-trip ~15k rows is not warranted. The format is
swappable behind ``write_embedding_artifact`` / ``load_embedding_artifact``
without churning callers.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    import httpx
    from avsa_core.storage import StorageBackend


class EmbeddingArtifactManifest(TypedDict):
    """Schema for the persisted ``manifest.json`` sidecar.

    Downstream callers import this
    TypedDict so the manifest schema is statically pinned in two places
    at once. Drift between the TypedDict and the writer would break the
    seeder — the test
    ``test_embedding_artifact_manifest_typeddict_keys_match_required``
    guards against that.
    """

    model_version_image: str
    model_version_text: str
    image_dim: int
    text_dim: int
    item_count: int
    content_hash: str
    generated_at: str


def compute_content_hash(config: dict[str, object]) -> str:
    """Return a deterministic SHA-256 hex digest of ``config``.

    Uses ``json.dumps(..., sort_keys=True)`` so insertion order is
    irrelevant — ``{"a": 1, "b": 2}`` and ``{"b": 2, "a": 1}`` hash to
    the same value. Any change to a key OR value flips the hash, which
    is what makes the artifact directory ``data/embeddings/<hash>/``
    self-identifying.
    """
    payload = json.dumps(config, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def compute_embeddings(
    items: list[dict[str, object]],
    model_url: str,
    batch_size: int,
    client: httpx.AsyncClient,
) -> list[dict[str, object]]:
    """Compute image + text embeddings for ``items`` via the model service.

    Each input item must carry:

    - ``id`` — preserved verbatim in the output row.
    - ``image_bytes`` — raw image bytes (caller loads from storage).
    - ``title`` or ``description`` — the string fed to ``/embed_text``;
      ``title`` wins when both are present.

    Items are processed in batches of ``batch_size``. For each batch
    the function issues two requests:

    - ``POST {model_url}/embed`` with ``{"images": [base64(bytes), ...]}``
      → ``{"embeddings": [[f32; 768], ...]}``
    - ``POST {model_url}/embed_text`` with ``{"texts": [str, ...]}``
      → ``{"embeddings": [[f32; 512], ...]}``

    Output order matches input order — downstream consumers
    (e.g. the seeder) index by position.
    """
    if batch_size < 1:
        raise ValueError(f"compute_embeddings: batch_size must be >= 1; got {batch_size}")

    rows: list[dict[str, object]] = []
    for start in range(0, len(items), batch_size):
        batch = items[start : start + batch_size]

        images_b64 = [
            base64.b64encode(_as_bytes(item["image_bytes"])).decode("ascii") for item in batch
        ]
        texts = [_text_for(item) for item in batch]

        image_resp = await client.post(f"{model_url}/embed", json={"images": images_b64})
        image_resp.raise_for_status()
        image_vectors: list[list[float]] = image_resp.json()["embeddings"]

        text_resp = await client.post(f"{model_url}/embed_text", json={"texts": texts})
        text_resp.raise_for_status()
        text_vectors: list[list[float]] = text_resp.json()["embeddings"]

        if len(image_vectors) != len(batch) or len(text_vectors) != len(batch):
            raise RuntimeError(
                "model service returned a vector count that does not match the batch size; "
                f"batch={len(batch)} image={len(image_vectors)} text={len(text_vectors)}"
            )

        for item, image_vec, text_vec in zip(batch, image_vectors, text_vectors, strict=True):
            rows.append(
                {
                    "id": item["id"],
                    "image_embedding": image_vec,
                    "text_embedding": text_vec,
                }
            )

    return rows


def write_embedding_artifact(
    out_dir: Path,
    embeddings: list[dict[str, object]],
    manifest: EmbeddingArtifactManifest,
    backend: StorageBackend,
) -> None:
    """Persist ``embeddings.jsonl`` + ``manifest.json`` under ``out_dir``.

    Both files land via ``backend.put_object``; ``LocalStorageBackend``
    creates parent directories as needed, so callers do not need to
    pre-create ``out_dir``.

    The JSONL bundle uses one JSON object per line so streaming readers
    can ingest a 15k-row file without holding it all in memory. The
    manifest is pretty-printed (2-space indent) with a trailing newline
    for diff-friendliness.
    """
    out_dir_str = str(out_dir).replace("\\", "/")

    jsonl_lines = [json.dumps(row, sort_keys=False) for row in embeddings]
    jsonl_bytes = ("\n".join(jsonl_lines) + ("\n" if jsonl_lines else "")).encode("utf-8")
    backend.put_object(f"{out_dir_str}/embeddings.jsonl", jsonl_bytes)

    manifest_bytes = (json.dumps(dict(manifest), indent=2, sort_keys=True) + "\n").encode("utf-8")
    backend.put_object(f"{out_dir_str}/manifest.json", manifest_bytes)


def load_embedding_artifact(
    artifact_dir: Path,
    backend: StorageBackend,
) -> tuple[list[dict[str, object]], EmbeddingArtifactManifest]:
    """Round-trip read for the seeder.

    Returns ``(embeddings, manifest)`` — same shapes as
    ``write_embedding_artifact`` was given. The embeddings list
    preserves on-disk order (which itself preserved input order at
    write time).
    """
    artifact_dir_str = str(artifact_dir).replace("\\", "/")

    manifest_raw = backend.get_object(f"{artifact_dir_str}/manifest.json")
    # json.loads returns Any; mypy cannot see across that boundary, so the
    # mapping is typed as ``dict[str, object]`` and each field is narrowed
    # explicitly below.
    manifest_dict: dict[str, object] = json.loads(manifest_raw.decode("utf-8"))
    # ``str()`` accepts ``object`` so the string fields coerce directly. The
    # integer fields cannot be passed to ``int()`` while typed ``object``
    # (no matching overload), so each is asserted to be an ``int`` first.
    # This narrows the type for mypy *and* fails loudly on a malformed
    # manifest rather than silently coercing a bad value.
    image_dim = manifest_dict["image_dim"]
    text_dim = manifest_dict["text_dim"]
    item_count = manifest_dict["item_count"]
    assert isinstance(image_dim, int), "manifest image_dim must be an int"
    assert isinstance(text_dim, int), "manifest text_dim must be an int"
    assert isinstance(item_count, int), "manifest item_count must be an int"
    manifest: EmbeddingArtifactManifest = {
        "model_version_image": str(manifest_dict["model_version_image"]),
        "model_version_text": str(manifest_dict["model_version_text"]),
        "image_dim": image_dim,
        "text_dim": text_dim,
        "item_count": item_count,
        "content_hash": str(manifest_dict["content_hash"]),
        "generated_at": str(manifest_dict["generated_at"]),
    }

    jsonl_raw = backend.get_object(f"{artifact_dir_str}/embeddings.jsonl")
    embeddings: list[dict[str, object]] = []
    for line in jsonl_raw.decode("utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        embeddings.append(json.loads(line))

    return embeddings, manifest


# ----------------------------------------------------------------------------
# Internals
# ----------------------------------------------------------------------------


def _as_bytes(value: object) -> bytes:
    """Coerce a manifest item's ``image_bytes`` field to ``bytes``.

    The pipeline accepts raw bytes (the natural shape) and falls back
    to bytearray/memoryview for callers that batch I/O through a buffer.
    Anything else is a programming error and we raise.
    """
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray | memoryview):
        return bytes(value)
    raise TypeError(
        f"compute_embeddings: item['image_bytes'] must be bytes-like; got {type(value).__name__}"
    )


def _text_for(item: dict[str, object]) -> str:
    """Pick the text field for an item — ``title`` wins, then ``description``.

    Empty string fallback rather than raising: the model service accepts
    any non-empty list of texts, and dropping an item silently would
    desynchronise the image/text batches.
    """
    title = item.get("title")
    if isinstance(title, str) and title:
        return title
    description = item.get("description")
    if isinstance(description, str) and description:
        return description
    return ""
