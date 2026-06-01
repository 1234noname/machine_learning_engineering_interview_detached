"""Run with AVSA_MODEL_STUB=1. These tests pin the contract:

  - EmbedResponse gains attributes: list[Attribute] parallel to
    embeddings; each Attribute carries
    {category, colour, category_confidence, colour_confidence}.
  - embeddings (768-d, L2-normalised, SHA-256 stub algorithm) is UNCHANGED.
  - Stub mode returns deterministic, valid attributes with no weights / network.
  - Real-mode head application matches the inference path (preprocessing,
    label-map, argmax) for the same 768-d vector - so apps/api and apps/model cannot drift.
  - The head-artifact location is config-driven ([model] attribute_heads_dir).

These are written BEFORE implementation. They are shaped to fail with
*assertion-style* messages (a missing field / a missing symbol surfaced via
pytest.fail), NOT collection-time ImportError - per
docs/agents/standards/testing.md test-first discipline. The stub-mode
assertions (1-3, 5) fail because the attributes field / loader does not yet
exist; the real-mode consistency test (4) skips when the artifact + loader
are not importable in this env but pins the contract in its body so the
implementation must satisfy it.
"""

import base64
import hashlib
import importlib
import json
import math
import pathlib

import pytest
from httpx import AsyncClient

EMBEDDING_DIM = 768
ATTRIBUTE_FIELDS = ("category", "colour", "category_confidence", "colour_confidence")

_REPO_ROOT = pathlib.Path("/Users/erinversfeld/avsa")
_EMBEDDING_ARTIFACT_DIR = (
    _REPO_ROOT
    / "data"
    / "embeddings"
    / "7decaeecdc769a1a4ab2e758684f740c5607bef0c07e9ac3cc027936f25cb899"
)


def _config_attribute_heads_dir() -> pathlib.Path:
    """Resolve the canonical head-artifact dir from config/avsa.toml.

    Read [model] attribute_heads_dir (the single source of truth the fixed
    CLI writes to and the loader reads from) rather than re-hardcoding the
    config hash here, so this test can never silently key off a stale
    location after a clean regen. The config value is repo-root-relative; resolve
    it against _REPO_ROOT. Falls back to the canonical hardcoded path if the
    config is unreadable so the consistency tests still point at the right place.
    """
    import tomllib

    fallback = (
        _REPO_ROOT
        / "data"
        / "attribute_heads"
        / "e8bcf7394f30b59124167d70b14a7283e28aa614e40e567284c0a8295c2db2de"
    )
    config_path = _REPO_ROOT / "config" / "avsa.toml"
    if not config_path.exists():
        return fallback
    with open(config_path, "rb") as f:
        configured = tomllib.load(f).get("model", {}).get("attribute_heads_dir")
    if not configured:
        return fallback
    return (_REPO_ROOT / configured).resolve()


_HEAD_ARTIFACT_DIR = _config_attribute_heads_dir()


def _l2_normalise(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vector))
    if norm == 0.0:
        return vector
    return [x / norm for x in vector]


def _stub_embedding(image_bytes: bytes) -> list[float]:
    """Reproduce the documented stub-embedding algorithm independently of the app.

    This is the contract the backward-compat test guards: sha256 -> expand to 768
    floats (raw[i % 32] / 255.0) -> L2-normalise. If the implementation ever
    changes the stub algorithm, the byte-for-byte test below fails.
    """
    digest = hashlib.sha256(image_bytes).digest()
    raw = [digest[i % 32] / 255.0 for i in range(EMBEDDING_DIM)]
    return _l2_normalise(raw)


# ---------------------------------------------------------------------------
# Backward-compat: embeddings unchanged (byte-for-byte) alongside attributes.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_embeddings_unchanged_byte_for_byte_with_attributes(
    client: AsyncClient, sample_jpeg: bytes
) -> None:
    """The embeddings field is exactly the documented 768-d L2-normalised stub
    vector even after attributes is added to the response.

    Guards the backward-compat invariant: downstream retrieval depends on the
    embedding bytes, so the new attributes field must not perturb them.
    """
    image_bytes = sample_jpeg
    b64 = base64.b64encode(image_bytes).decode()
    response = await client.post("/embed", json={"images": [b64]})
    assert response.status_code == 200, response.text
    data = response.json()

    assert "embeddings" in data, "response must still carry the 'embeddings' field"
    embeddings = data["embeddings"]
    assert len(embeddings) == 1
    assert len(embeddings[0]) == EMBEDDING_DIM

    expected = _stub_embedding(image_bytes)
    assert embeddings[0] == expected, (
        "stub embedding bytes changed - backward-compat broken: the new attributes "
        "field must not alter the existing embedding algorithm/values"
    )


# ---------------------------------------------------------------------------
# Stub attributes present, parallel to embeddings, correctly shaped.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stub_attributes_present_and_shaped(client: AsyncClient, sample_jpeg: bytes) -> None:
    """/embed (stub) returns attributes parallel to embeddings with the
    four required fields, correct types, and confidences in [0, 1]."""
    image_bytes = [sample_jpeg, sample_jpeg + b"\x00salt"]
    b64s = [base64.b64encode(b).decode() for b in image_bytes]
    response = await client.post("/embed", json={"images": b64s})
    assert response.status_code == 200, response.text
    data = response.json()

    if "attributes" not in data:
        pytest.fail(
            "EmbedResponse is missing the 'attributes' field: expected a "
            "list parallel to 'embeddings' with one entry per image"
        )

    attributes = data["attributes"]
    assert len(attributes) == len(data["embeddings"]) == len(b64s), (
        "attributes must be parallel to embeddings (one per image)"
    )

    for attr in attributes:
        for field in ATTRIBUTE_FIELDS:
            assert field in attr, f"attribute entry missing field {field!r}: {attr!r}"
        assert isinstance(attr["category"], str) and attr["category"], (
            f"category must be a non-empty string; got {attr['category']!r}"
        )
        assert isinstance(attr["colour"], str) and attr["colour"], (
            f"colour must be a non-empty string; got {attr['colour']!r}"
        )
        for conf_field in ("category_confidence", "colour_confidence"):
            value = attr[conf_field]
            assert isinstance(value, int | float) and not isinstance(value, bool), (
                f"{conf_field} must be a number; got {value!r}"
            )
            assert 0.0 <= float(value) <= 1.0, f"{conf_field} must be in [0, 1]; got {value!r}"


# ---------------------------------------------------------------------------
# Stub determinism: same bytes -> same attrs; different bytes may differ.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stub_attributes_deterministic(client: AsyncClient, sample_jpeg: bytes) -> None:
    """Same image bytes yield identical attributes across two calls; a different
    image is allowed to differ. No weights / network required (stub mode)."""
    image_bytes = sample_jpeg
    b64 = base64.b64encode(image_bytes).decode()
    payload = {"images": [b64]}

    r1 = await client.post("/embed", json=payload)
    r2 = await client.post("/embed", json=payload)
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    d1, d2 = r1.json(), r2.json()

    if "attributes" not in d1 or "attributes" not in d2:
        pytest.fail("EmbedResponse is missing 'attributes' - cannot test determinism")

    assert d1["attributes"][0] == d2["attributes"][0], (
        "stub attributes are not deterministic - same image bytes produced different attrs"
    )

    other = base64.b64encode(image_bytes + b"\x01different").decode()
    r3 = await client.post("/embed", json={"images": [other]})
    assert r3.status_code == 200, r3.text
    d3 = r3.json()
    for field in ATTRIBUTE_FIELDS:
        assert field in d3["attributes"][0], f"different-image attribute missing {field!r}"


# ---------------------------------------------------------------------------
# Real-mode head application matches inference (the crux).
# ---------------------------------------------------------------------------


def _load_npz_head(head_dir: pathlib.Path, attribute: str) -> tuple[object, object, dict[int, str]]:
    """Load (weights, bias, label_map) for one head directly from the .npz artifact."""
    import numpy as np

    with np.load(head_dir / f"{attribute}.npz", allow_pickle=False) as data:
        weights = np.asarray(data["weights"], dtype=np.float64)
        bias = np.asarray(data["bias"], dtype=np.float64)
    labels_obj = json.loads((head_dir / f"{attribute}.labels.json").read_text(encoding="utf-8"))
    label_map = {int(idx): str(name) for idx, name in labels_obj.items()}
    return weights, bias, label_map


def _sample_embedding_vectors(artifact_dir: pathlib.Path, n: int) -> list[list[float]]:
    """Read the first n 768-d image-embedding vectors from the artifact.

    The artifact is JSONL - one {"id", "image_embedding", "text_embedding"}
    object per line - so the first n rows are read directly (no avsa_api
    reader needed) to obtain the L2-normalised 768-d vectors /embed returns.
    """
    vectors: list[list[float]] = []
    with open(artifact_dir / "embeddings.jsonl", encoding="utf-8") as f:
        for line in f:
            if len(vectors) >= n:
                break
            vectors.append([float(x) for x in json.loads(line)["image_embedding"]])
    return vectors


@pytest.mark.asyncio
async def test_real_mode_head_application_matches_baseline() -> None:
    """The model service's head application must produce the SAME category/colour
    prediction as's inference path for the same 768-d vector.

    This guards against preprocessing / label-map drift between apps/api,
    avsa_data.attribute_heads and apps/model. The inference is
    argmax(features @ weights + bias) mapped through label_map (see
    attribute_heads.evaluate); the embedding fed in is the L2-normalised 768-d
    vector that /embed already returns (the artifact rows are those
    vectors).
    """
    try:
        import numpy as np
    except ImportError as exc:
        pytest.skip(
            f"numpy not importable (stub-only env, no model extra): {exc}; the consistency "
            "contract is pinned in this test body and runs under --extra model"
        )

    if not _HEAD_ARTIFACT_DIR.exists() or not _EMBEDDING_ARTIFACT_DIR.exists():
        pytest.skip(
            "deferred:  artifacts not present on disk; consistency "
            "contract is pinned in this test body"
        )

    cat_weights, cat_bias, cat_labels = _load_npz_head(_HEAD_ARTIFACT_DIR, "category")
    col_weights, col_bias, col_labels = _load_npz_head(_HEAD_ARTIFACT_DIR, "colour")

    sample = _sample_embedding_vectors(_EMBEDDING_ARTIFACT_DIR, 5)
    assert sample, "embedding artifact has no rows"

    def predict_baseline(
        vec: list[float], weights: object, bias: object, label_map: dict[int, str]
    ) -> str:
        scores = np.asarray(vec, dtype=np.float64) @ weights + bias  # type: ignore[operator]
        return label_map[int(np.argmax(scores))]

    try:
        model_heads = importlib.import_module("avsa_model.heads")
    except ImportError as exc:
        pytest.fail(
            "avsa_model.heads (the model-side head applier) is not implemented yet "
            f"({exc}); it must apply the heads to the L2-normalised 768-d embedding "
            "and return predictions identical to's evaluate (argmax via label_map). "
            "Expected: for each sampled vector, the model service's category/colour "
            "equals predict_baseline(vec)."
        )

    applier = model_heads.AttributeHeads.load(_HEAD_ARTIFACT_DIR)
    for vec in sample:
        expected_cat = predict_baseline(vec, cat_weights, cat_bias, cat_labels)
        expected_col = predict_baseline(vec, col_weights, col_bias, col_labels)
        prediction = applier.predict(vec)
        assert prediction.category == expected_cat, (
            f"category drift: expected={expected_cat!r} actual={prediction.category!r}"
        )
        assert prediction.colour == expected_col, (
            f"colour drift: expected={expected_col!r} actual={prediction.colour!r}"
        )


# ---------------------------------------------------------------------------
# Head-artifact location is config-driven, not hardcoded.
# ---------------------------------------------------------------------------


def test_attribute_heads_dir_is_config_driven(tmp_path: pathlib.Path) -> None:
    """The head-artifact location must come from config ([model] attribute_heads_dir),
    not a hardcoded path.

    Pins the loader contract: the model service reads the configured directory.
    Pre-impl this fails because the loader symbol does not exist yet; once
    implemented, pointing the config at tmp_path must cause the loader to read
    from there (proving the path is config-driven).
    """
    try:
        heads_mod = importlib.import_module("avsa_model.heads")
    except ImportError as exc:
        pytest.fail(
            "avsa_model.heads is not implemented yet "
            f"({exc}); the head-artifact directory must be config-driven via "
            "[model] attribute_heads_dir, read at load time - not hardcoded"
        )

    resolver = getattr(heads_mod, "resolve_attribute_heads_dir", None)
    if resolver is None:
        pytest.fail(
            "avsa_model.heads must expose a config-driven resolver for the head-artifact "
            "directory (e.g. resolve_attribute_heads_dir reading [model] attribute_heads_dir)"
        )

    config = {"model": {"attribute_heads_dir": str(tmp_path)}}
    resolved = resolver(config)
    assert pathlib.Path(resolved) == tmp_path, (
        "resolver did not honour [model] attribute_heads_dir - the path must be "
        f"config-driven; expected {tmp_path}, got {resolved!r}"
    )

    # And the config key must actually be present in the project config so the
    # impl wires a real default, not only the test override.
    project_config = _REPO_ROOT / "config" / "avsa.toml"
    if project_config.exists():
        import tomllib

        with open(project_config, "rb") as f:
            model_cfg = tomllib.load(f).get("model", {})
        assert "attribute_heads_dir" in model_cfg, (
            "config/avsa.toml [model] must define attribute_heads_dir so the head "
            "artifact location is config-driven (currently absent - impl must add it)"
        )


_EXPECTED_CATEGORY_LABELS = {"dress", "jacket", "pants", "skirt", "top"}


def test_head_artifact_label_maps_are_wellformed() -> None:
    """The head artifact on disk has the label maps the consistency test relies on."""
    cat_labels_path = _HEAD_ARTIFACT_DIR / "category.labels.json"
    if not cat_labels_path.exists():
        pytest.skip("deferred (pre-impl): head artifact not present on disk")
    labels = json.loads(cat_labels_path.read_text(encoding="utf-8"))
    assert set(labels.values()) == _EXPECTED_CATEGORY_LABELS, (
        "category label map drifted from the vocabulary the consistency test pins"
    )
