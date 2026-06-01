"""Failing tests for — train ViT attribute heads (linear probe).

Authored at step 2A-i (pre-implementation). The module under test
(``avsa_data.attribute_heads``) does not yet exist; it is imported inside a
try/except so *collection succeeds* and each test fails with a meaningful
assertion failure (``pytest.fail`` / ``AssertionError`` / domain-exception
assertion) — per docs/agents/standards/testing.md § "Test-first protocol".
A failure that says "the feature isn't there yet" proves nothing; these tests
call the not-yet-written functions with concrete inputs and assert on concrete
outputs, so once the module lands they exercise real behaviour.

---------------------------------------------------------------------------
Design decisions encoded by these tests (Pre-implementation Flags in report)
---------------------------------------------------------------------------

Probe method — **numpy ridge / least-squares linear probe** (one weight
matrix per attribute, argmax at inference). The tests assert only the
*observable contract* of a frozen linear probe:

  - it produces a ``(768, n_classes)`` (or ``(n_classes, 768)``) weight
    matrix + a length-``n_classes`` bias — a pure linear map, no backbone
    params (``test_linear_probe_is_linear_768_in``);
  - trained on a fixture with a learnable class signal it beats the
    ``1/n_classes`` chance floor (``test_train_linear_probe_beats_chance``).

These hold for ridge least-squares OR a numpy softmax/logistic GD — the test
does not pin the optimiser, only the probe's shape + the "it actually learns"
property, so the implementer may choose either. Document the final choice.

numpy-not-torch — the probe is a single linear layer over pre-computed 768-d
features; numpy covers the matrix algebra and keeps the training step inside
the ``apps/api`` env without pulling torch (torch lives only in
``apps/model``'s opt-in ``[model]`` extra). The backbone is NOT loaded by
#067 — it consumes the frozen features emitted by #062.

Feature source — the 768-d ``image_embedding`` rows in the #062 artifact ARE
the frozen ViT features; the real CLI reuses
``avsa_data.embedding_pipeline.load_embedding_artifact`` (the single sanctioned
artifact reader). These tests pass synthetic 768-d numpy features directly so
the assertions focus on the probe, not on artifact I/O.

Colour labels — derived from the title with the same keyword vocab +
``multicolour`` fallback as ``avsa_data.catalog_fashion200k`` (reused here, not
re-implemented, so the two label derivations cannot drift). ``category`` comes
straight from the manifest.

Split-by-product — the leakage boundary is the numeric-ID directory
(``women/dresses/.../56037632/...``): every image whose id sits under the same
numeric directory is one *product* and must land wholly in train or wholly in
test. The split is seeded so it is reproducible.

numpy availability flag — numpy is a dependency of the repo-root pyproject
(``numpy>=1.26,<3``) but is NOT yet in ``apps/api/pyproject.toml``. The probe
runs in the ``apps/api`` env, so the implementation phase must add numpy to
``apps/api`` deps (or vendor a stdlib least-squares). numpy is imported inside
a guard here so its current absence in ``apps/api`` does not turn these into
collection errors — they stay assertion-shaped. See completion-report flags.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    import numpy as np

try:
    import numpy as np

    _NUMPY_AVAILABLE = True
except ImportError:
    _NUMPY_AVAILABLE = False

try:
    from avsa_data.attribute_heads import (
        AttributeHeadError,
        LinearHead,
        compute_head_config_hash,
        evaluate,
        extract_labels,
        split_by_product,
        train_linear_probe,
        write_head_artifact,
    )

    _HEADS_AVAILABLE = True
except ImportError:
    _HEADS_AVAILABLE = False

try:
    from avsa_core.storage.local import LocalStorageBackend

    _STORAGE_AVAILABLE = True
except ImportError:
    _STORAGE_AVAILABLE = False


# ----------------------------------------------------------------------------
# Guards — keep failures assertion-shaped, not ImportError collection crashes
# ----------------------------------------------------------------------------


def _require_heads() -> None:
    if not _HEADS_AVAILABLE:
        pytest.fail(
            "avsa_data.attribute_heads (LinearHead / extract_labels / "
            "split_by_product / train_linear_probe / evaluate / "
            "write_head_artifact / compute_head_config_hash) not implemented yet "
            "— expected during 2A-i pre-implementation. Implement the numpy "
            "linear probe / ."
        )


def _require_numpy() -> None:
    if not _NUMPY_AVAILABLE:
        pytest.fail(
            "numpy is not importable in the apps/api env. The linear probe runs "
            "in apps/api and needs numpy; the implementation phase must add "
            "numpy (already in the repo-root pyproject as numpy>=1.26,<3) to "
            "apps/api/pyproject.toml. See completion-report Pre-implementation Flags."
        )


def _require_storage() -> None:
    if not _STORAGE_AVAILABLE:
        pytest.fail(
            "avsa_core.storage.local.LocalStorageBackend not importable — the "
            "head-artifact write test reuses the StorageBackend (as #062 does)."
        )


# ----------------------------------------------------------------------------
# Fixtures — synthetic 768-d features with a learnable class signal
# ----------------------------------------------------------------------------

_DIM = 768


def _manifest_dict() -> dict[str, object]:
    """A minimal #061-shaped manifest covering both label derivation paths.

    - ``category`` is taken verbatim from the manifest entry.
    - ``colour`` is derived from ``title``: "black dress" → black (a vocab
      word), "silk blouse" → multicolour (no vocab word → fallback).

    The numeric-ID directory in each ``id`` is the product key for the
    split-by-product test: ids 56037632 and 56037632 are the *same* product
    (two images), 70001111 is a different product.
    """
    return {
        "seed": 17,
        "dataset_version": "fashion200k-v1.0",
        "items": [
            {
                "id": "women/dresses/casual/56037632/56037632_0.jpeg",
                "category": "dress",
                "title": "black short dress",
            },
            {
                "id": "women/dresses/casual/56037632/56037632_1.jpeg",
                "category": "dress",
                "title": "black knee-length dress",
            },
            {
                "id": "women/tops/blouses/70001111/70001111_0.jpeg",
                "category": "blouse",
                "title": "silk blouse",
            },
            {
                "id": "women/tops/blouses/80002222/80002222_0.jpeg",
                "category": "blouse",
                "title": "navy pleated blouse",
            },
        ],
    }


def _write_manifest(tmp_path: Path) -> Path:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(_manifest_dict()), encoding="utf-8")
    return path


def _separable_features(
    n_classes: int,
    per_class: int,
    *,
    seed: int = 0,
) -> tuple[np.ndarray, list[int], list[str]]:
    """Build a linearly-separable fixture: features, int labels, string labels.

    Each class gets a distinct "prototype" direction in 768-d space plus small
    Gaussian noise, so a linear probe CAN separate them well above the
    ``1/n_classes`` chance floor — proving the probe learns, not memorises.
    Returns ``(features[N, 768], int_labels[N], class_names[n_classes])``.
    """
    rng = np.random.default_rng(seed)
    prototypes = rng.normal(scale=5.0, size=(n_classes, _DIM))
    feats: list[np.ndarray] = []
    int_labels: list[int] = []
    for cls in range(n_classes):
        for _ in range(per_class):
            feats.append(prototypes[cls] + rng.normal(scale=0.5, size=_DIM))
            int_labels.append(cls)
    features = np.asarray(feats, dtype=np.float64)
    class_names = [f"class_{c}" for c in range(n_classes)]
    return features, int_labels, class_names


def _head_config() -> dict[str, object]:
    """Baseline config that identifies a head-weights artifact (hash input)."""
    return {
        "model_version_image": "vit-b-16@2026-05-01",
        "embedding_artifact_hash": (
            "7decaeecdc769a1a4ab2e758684f740c5607bef0c07e9ac3cc027936f25cb899"
        ),
        "attributes": ["category", "colour"],
        "seed": 17,
        "test_frac": 0.2,
        "probe": "ridge",
    }


# ----------------------------------------------------------------------------
# split_by_product — no leakage across the train/test boundary
# ----------------------------------------------------------------------------


def test_split_by_product_no_leakage() -> None:
    """All images of one product land in the same split; train ∩ test (by product) = ∅."""
    _require_heads()
    ids = [
        "women/dresses/casual/56037632/56037632_0.jpeg",
        "women/dresses/casual/56037632/56037632_1.jpeg",
        "women/dresses/casual/56037632/56037632_2.jpeg",
        "women/tops/blouses/70001111/70001111_0.jpeg",
        "women/tops/blouses/80002222/80002222_0.jpeg",
        "women/tops/blouses/90003333/90003333_0.jpeg",
        "women/skirts/midi/11110000/11110000_0.jpeg",
        "women/skirts/midi/22220000/22220000_0.jpeg",
    ]
    train_ids, test_ids = split_by_product(ids, seed=17, test_frac=0.5)

    # Partition is exhaustive and disjoint at the image level.
    assert set(train_ids) | set(test_ids) == set(ids), (
        "split_by_product must assign every id to exactly one split; "
        f"train|test={sorted(set(train_ids) | set(test_ids))!r} vs ids={sorted(ids)!r}"
    )
    assert set(train_ids).isdisjoint(set(test_ids)), (
        f"an id must not appear in both splits; overlap={sorted(set(train_ids) & set(test_ids))!r}"
    )

    def _product(image_id: str) -> str:
        # The product key is the numeric-ID directory component.
        return next(part for part in image_id.split("/") if part.isdigit())

    train_products = {_product(i) for i in train_ids}
    test_products = {_product(i) for i in test_ids}
    assert train_products.isdisjoint(test_products), (
        "a product's images must never straddle train+test (leakage); shared "
        f"products={sorted(train_products & test_products)!r}"
    )


def test_split_by_product_is_seeded_reproducible() -> None:
    """Same ids + same seed → identical split; a different seed may differ."""
    _require_heads()
    ids = [f"women/dresses/x/{p}/{p}_0.jpeg" for p in range(10000000, 10000020)]
    first = split_by_product(ids, seed=17, test_frac=0.3)
    second = split_by_product(ids, seed=17, test_frac=0.3)
    assert first == second, (
        "split_by_product must be deterministic for a fixed seed so the "
        "train/test partition is reproducible across runs"
    )


# ----------------------------------------------------------------------------
# extract_labels — category verbatim; colour via the keyword vocab + fallback
# ----------------------------------------------------------------------------


def test_extract_labels_category_and_colour(tmp_path: Path) -> None:
    """Category from manifest; colour from the title vocab with multicolour fallback."""
    _require_heads()
    manifest_path = _write_manifest(tmp_path)

    labels = extract_labels(manifest_path)

    black_dress = "women/dresses/casual/56037632/56037632_0.jpeg"
    silk_blouse = "women/tops/blouses/70001111/70001111_0.jpeg"
    navy_blouse = "women/tops/blouses/80002222/80002222_0.jpeg"

    assert black_dress in labels, (
        f"extract_labels must key by manifest id; {black_dress!r} missing from "
        f"{sorted(labels.keys())!r}"
    )
    # Category comes straight from the manifest entry.
    assert labels[black_dress]["category"] == "dress", (
        f"category must be taken verbatim from the manifest; got "
        f"{labels[black_dress]['category']!r}"
    )
    assert labels[silk_blouse]["category"] == "blouse", (
        f"category must be taken verbatim from the manifest; got "
        f"{labels[silk_blouse]['category']!r}"
    )
    # Colour derived from the title: a vocab word wins.
    assert labels[black_dress]["colour"] == "black", (
        '"black short dress" must derive colour "black" via the keyword vocab; '
        f"got {labels[black_dress]['colour']!r}"
    )
    assert labels[navy_blouse]["colour"] == "navy", (
        f'"navy pleated blouse" must derive colour "navy"; got {labels[navy_blouse]["colour"]!r}'
    )
    # No colour word in the title → multicolour fallback (reuses catalog vocab).
    assert labels[silk_blouse]["colour"] == "multicolour", (
        '"silk blouse" has no colour word and must fall back to "multicolour"; '
        f"got {labels[silk_blouse]['colour']!r}"
    )


# ----------------------------------------------------------------------------
# train_linear_probe — it actually learns (non-vacuous)
# ----------------------------------------------------------------------------


def test_train_linear_probe_beats_chance() -> None:
    """A clear class signal → trained head top-1 accuracy > 1/n_classes."""
    _require_heads()
    _require_numpy()
    n_classes = 4
    features, int_labels, class_names = _separable_features(n_classes, per_class=25, seed=1)
    string_labels = [class_names[i] for i in int_labels]

    head = train_linear_probe(features, string_labels)
    accuracy = evaluate(head, features, string_labels)

    chance = 1.0 / n_classes
    assert accuracy > chance, (
        f"a linear probe on a linearly-separable fixture must beat the "
        f"{chance:.3f} chance floor; got top-1 accuracy {accuracy:.3f}. If it "
        "does not, the probe is not learning from the features."
    )


def test_train_linear_probe_label_map_covers_classes() -> None:
    """The trained head exposes a label map covering exactly the seen classes."""
    _require_heads()
    _require_numpy()
    features, int_labels, class_names = _separable_features(3, per_class=10, seed=2)
    string_labels = [class_names[i] for i in int_labels]

    head = train_linear_probe(features, string_labels)

    assert set(head.label_map.values()) == set(class_names), (
        "the head's label_map must cover exactly the classes present in the "
        f"training labels {sorted(set(class_names))!r}; got map {head.label_map!r}"
    )


# ----------------------------------------------------------------------------
# linear-map shape — 768 in, n_classes out; no backbone params
# ----------------------------------------------------------------------------


def test_linear_probe_is_linear_768_in() -> None:
    """The head is a linear map: (768, n_classes) weights + length-n_classes bias."""
    _require_heads()
    _require_numpy()
    n_classes = 5
    features, int_labels, class_names = _separable_features(n_classes, per_class=8, seed=3)
    string_labels = [class_names[i] for i in int_labels]

    head: LinearHead = train_linear_probe(features, string_labels)

    weights = np.asarray(head.weights)
    bias = np.asarray(head.bias)

    # A linear probe over 768-d features maps to n_classes logits. Accept either
    # orientation of the weight matrix; the load-bearing facts are (a) it is 2-D,
    # (b) one axis is 768 (the frozen feature dim), (c) the other is n_classes.
    assert weights.ndim == 2, (
        f"head weights must be a 2-D matrix (a linear map); got ndim={weights.ndim}"
    )
    assert set(weights.shape) == {_DIM, n_classes}, (
        f"head weights must be shaped (768, n_classes) or (n_classes, 768); "
        f"got {weights.shape} for n_classes={n_classes}"
    )
    assert bias.shape == (n_classes,), (
        f"head bias must have one entry per class (length {n_classes}); got shape {bias.shape}"
    )
    # No backbone: the only learned parameters are the linear weights + bias.
    assert weights.size == _DIM * n_classes, (
        "a frozen-backbone linear probe has exactly 768 * n_classes weight "
        f"params; got {weights.size} (any extra implies non-linear / backbone params)"
    )


# ----------------------------------------------------------------------------
# evaluate — top-1 accuracy computed correctly on a known head + known labels
# ----------------------------------------------------------------------------


def test_evaluate_top1_accuracy() -> None:
    """Accuracy is the fraction of argmax-correct rows for a hand-built head."""
    _require_heads()
    _require_numpy()
    # Two classes, 768-d. Build a head whose argmax is decided by which of the
    # first two feature dims is larger, with the rest of the matrix zeroed.
    weights = np.zeros((_DIM, 2), dtype=np.float64)
    weights[0, 0] = 1.0  # dim 0 → class "a"
    weights[1, 1] = 1.0  # dim 1 → class "b"
    bias = np.zeros(2, dtype=np.float64)
    head = LinearHead(weights=weights, bias=bias, label_map={0: "a", 1: "b"})

    # 4 rows: 3 should classify correctly, 1 deliberately wrong → 0.75 accuracy.
    feats = np.zeros((4, _DIM), dtype=np.float64)
    feats[0, 0] = 3.0  # → a   (label a)  correct
    feats[1, 1] = 3.0  # → b   (label b)  correct
    feats[2, 0] = 2.0  # → a   (label a)  correct
    feats[3, 0] = 5.0  # → a   (label b)  WRONG
    truth = ["a", "b", "a", "b"]

    accuracy = evaluate(head, feats, truth)
    assert accuracy == pytest.approx(0.75), (
        f"evaluate must report top-1 accuracy = correct/total; expected 0.75 on "
        f"this 3-of-4-correct fixture, got {accuracy!r}"
    )


# ----------------------------------------------------------------------------
# write_head_artifact — weights + label maps + manifest, round-trippable
# ----------------------------------------------------------------------------


def test_write_head_artifact_shape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Writing emits head weights, label maps, and a manifest with the required keys."""
    _require_heads()
    _require_numpy()
    _require_storage()
    monkeypatch.setenv("AVSA_STORAGE_HMAC_SECRET", "test-secret")
    backend = LocalStorageBackend(root=tmp_path)

    cat_feats, cat_int, cat_names = _separable_features(3, per_class=6, seed=4)
    col_feats, col_int, col_names = _separable_features(2, per_class=6, seed=5)
    category_head = train_linear_probe(cat_feats, [cat_names[i] for i in cat_int])
    colour_head = train_linear_probe(col_feats, [col_names[i] for i in col_int])

    heads = {"category": category_head, "colour": colour_head}
    out_dir = Path("data/attribute_heads/hash-abc")

    write_head_artifact(
        out_dir=out_dir,
        heads=heads,
        manifest={
            "model_version_image": "vit-b-16@2026-05-01",
            "image_dim": _DIM,
            "class_counts": {"category": 3, "colour": 2},
            "content_hash": "hash-abc",
            "generated_at": "2026-05-25T00:00:00Z",
        },
        backend=backend,
    )

    listed = sorted(backend.list_objects("data/attribute_heads/hash-abc"))
    assert listed, f"write_head_artifact must emit at least one file under <hash>/; got {listed!r}"
    manifest_keys = [p for p in listed if p.endswith("manifest.json")]
    assert manifest_keys, (
        f"a manifest.json must be written alongside the head weights; got {listed!r}"
    )

    raw = backend.get_object(manifest_keys[0])
    parsed = json.loads(raw.decode("utf-8"))
    required = {"model_version_image", "image_dim", "class_counts", "content_hash", "generated_at"}
    missing = required - set(parsed.keys())
    assert not missing, (
        f"head-artifact manifest is missing required keys {sorted(missing)!r}; "
        f"got {sorted(parsed.keys())!r}"
    )
    assert parsed["image_dim"] == _DIM, (
        f"manifest.image_dim must record the 768-d feature dim; got {parsed['image_dim']!r}"
    )
    assert parsed["class_counts"]["category"] == 3, (
        f"manifest must record per-attribute class counts; got {parsed['class_counts']!r}"
    )


def test_write_head_artifact_npz_round_trips_via_numpy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The persisted .npz weights/bias + labels.json round-trip back exactly.

    There is no avsa_api reader (the model service reads the artifact itself via
    ``np.load`` — ``apps/model`` must not import ``avsa_api``), so this verifies
    the write path the model consumer actually depends on by reading the bytes
    back the same way: ``np.load(..., allow_pickle=False)`` on the stored .npz.
    """
    _require_heads()
    _require_numpy()
    _require_storage()
    monkeypatch.setenv("AVSA_STORAGE_HMAC_SECRET", "test-secret")
    backend = LocalStorageBackend(root=tmp_path)

    feats, int_labels, names = _separable_features(3, per_class=6, seed=6)
    head = train_linear_probe(feats, [names[i] for i in int_labels])
    out_dir = Path("data/attribute_heads/hash-rt")

    write_head_artifact(
        out_dir=out_dir,
        heads={"category": head},
        manifest={
            "model_version_image": "vit-b-16@2026-05-01",
            "image_dim": _DIM,
            "class_counts": {"category": 3},
            "content_hash": "hash-rt",
            "generated_at": "2026-05-25T00:00:00Z",
        },
        backend=backend,
    )

    # Read the .npz back exactly as the model service does and assert it is the
    # in-memory head, value for value — the artifact IS the trained head.
    npz_raw = backend.get_object("data/attribute_heads/hash-rt/category.npz")
    with np.load(io.BytesIO(npz_raw), allow_pickle=False) as data:
        np.testing.assert_array_equal(data["weights"], head.weights)
        np.testing.assert_array_equal(data["bias"], head.bias)

    labels_raw = backend.get_object("data/attribute_heads/hash-rt/category.labels.json")
    labels = json.loads(labels_raw.decode("utf-8"))
    assert {int(idx): name for idx, name in labels.items()} == head.label_map, (
        "the persisted labels.json must round-trip the head's {index: class} map; "
        f"got {labels!r} vs {head.label_map!r}"
    )


# ----------------------------------------------------------------------------
# fail-fast guards — malformed inputs raise the domain error, never silently pass
# ----------------------------------------------------------------------------


def test_split_by_product_rejects_malformed_id() -> None:
    """An id with no numeric product directory fails fast at the split boundary."""
    _require_heads()
    # "lookbook/teaser.jpeg" has no numeric directory, so its product key is
    # undefined; collapsing it with real products would silently corrupt the
    # leakage boundary, so the split must raise rather than guess.
    with pytest.raises(AttributeHeadError):
        split_by_product(
            ["women/dresses/casual/56037632/56037632_0.jpeg", "lookbook/teaser.jpeg"],
            seed=17,
            test_frac=0.5,
        )


def test_train_linear_probe_rejects_wrong_feature_dim() -> None:
    """Features that are not 768-wide violate the frozen-ViT contract and raise."""
    _require_heads()
    _require_numpy()
    # 64-d, not the 768-d ViT-b-16 embedding the probe is defined over.
    features = np.zeros((4, 64), dtype=np.float64)
    with pytest.raises(AttributeHeadError):
        train_linear_probe(features, ["a", "b", "a", "b"])


def test_extract_labels_rejects_manifest_without_items(tmp_path: Path) -> None:
    """A manifest with no 'items' list is corrupt — raise, don't return an empty map."""
    _require_heads()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps({"seed": 17, "dataset_version": "fashion200k-v1.0"}), encoding="utf-8"
    )
    with pytest.raises(AttributeHeadError):
        extract_labels(manifest_path)


# ----------------------------------------------------------------------------
# compute_head_config_hash — deterministic; sensitive to every field
# ----------------------------------------------------------------------------


def test_head_config_hash_deterministic() -> None:
    """Same config → same hash; changing any field changes the hash."""
    _require_heads()
    base = _head_config()
    base_hash = compute_head_config_hash(base)

    assert compute_head_config_hash(base) == base_hash, (
        "compute_head_config_hash must be deterministic for a fixed config"
    )
    assert isinstance(base_hash, str) and len(base_hash) > 0, (
        f"the config hash must be a non-empty string; got {base_hash!r}"
    )

    bumped_seed = dict(base)
    bumped_seed["seed"] = 99
    assert compute_head_config_hash(bumped_seed) != base_hash, (
        "changing the split seed must change the config hash (different artifact)"
    )

    bumped_frac = dict(base)
    bumped_frac["test_frac"] = 0.3
    assert compute_head_config_hash(bumped_frac) != base_hash, (
        "changing test_frac must change the config hash"
    )

    bumped_artifact = dict(base)
    bumped_artifact["embedding_artifact_hash"] = "deadbeef"
    assert compute_head_config_hash(bumped_artifact) != base_hash, (
        "changing the source embedding artifact hash must change the config hash"
    )


def test_head_config_hash_key_order_independent() -> None:
    """Insertion order of config keys does not affect the hash."""
    _require_heads()
    left = {"a": 1, "b": 2, "seed": 17}
    right = {"seed": 17, "b": 2, "a": 1}
    assert compute_head_config_hash(left) == compute_head_config_hash(right), (
        "compute_head_config_hash must sort keys before hashing so a config "
        "built in a different order produces the same hash"
    )


# ----------------------------------------------------------------------------
# committed accuracy report — parses + has the required fields & caveat
# ----------------------------------------------------------------------------

# The placeholder report committed alongside these tests. The orchestrator
# calibrates the real numbers against the trained artifact after impl.
_REPORT_PATH = (
    Path(__file__).resolve().parents[3] / "evals" / "attributes" / "story-020" / "baseline.toml"
)


def test_accuracy_report_committed_shape() -> None:
    """The committed baseline parses and carries category + colour top-1 + the caveat."""
    import tomllib

    assert _REPORT_PATH.exists(), (
        f"a committed accuracy report must exist at {_REPORT_PATH}; the held-out "
        "category baseline lands in evals/attributes/story-020/ DoD"
    )

    parsed = tomllib.loads(_REPORT_PATH.read_text(encoding="utf-8"))

    assert "category_top1" in parsed, (
        f"the report must record category top-1 accuracy; got keys {sorted(parsed.keys())!r}"
    )
    assert "colour_top1" in parsed, (
        f"the report must record colour top-1 accuracy; got keys {sorted(parsed.keys())!r}"
    )
    assert isinstance(parsed["category_top1"], int | float), (
        f"category_top1 must be numeric; got {parsed['category_top1']!r}"
    )
    assert isinstance(parsed["colour_top1"], int | float), (
        f"colour_top1 must be numeric; got {parsed['colour_top1']!r}"
    )
    # The report MUST carry the colour-is-noisier caveat — colour labels are
    # description-derived, not ground truth ( acceptance criterion).
    caveat = parsed.get("colour_caveat")
    assert isinstance(caveat, str) and caveat.strip(), (
        "the report must carry a non-empty 'colour_caveat' noting colour labels "
        f"are description-derived and noisier than category; got {caveat!r}"
    )
