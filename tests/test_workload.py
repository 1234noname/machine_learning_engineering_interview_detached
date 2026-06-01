"""Held-out corpus + realistic workload profile tests.

These tests are CI-safe (no live stack, no real Anthropic, no heavy load run).
They verify the four testable surfaces:

1. **Corpus selection** — only ``split=="test"`` items; count == 733; IDs map
   to on-disk image paths; no ``train`` item leaks in.
2. **Query-mix config** — weights load from ``config/avsa.toml`` and parse;
   the task-weight integers exposed by the locustfile reflect the config values.
3. **Task-set registration** — importing the locustfile registers
   ``ChatUser`` and ``EmbedUser``; the default (unset env)
   does NOT replace BatcherUser behaviour.
4. **Realistic image** — the image payload used by ChatUser/EmbedUser is a real
   >=224x224 JPEG, not ``_JPEG_1PX``.
5. **Multi-turn resume contract** — the ``multi_turn`` task must capture the
   conversation id from the ``X-Conversation-Id`` **response header** and send
   it back in the ``X-Resume-Conversation-Id`` **request header** for turn 2;
   it must NOT use a ``conversation_id`` form field or SSE-event parsing.

Marked ``loadtest`` to match the project convention for locust-importing tests
(see root ``pyproject.toml`` ``addopts``).  Run in isolation::

    uv run pytest tests/test_workload.py -m loadtest

"""

from __future__ import annotations

import json
import tomllib
import uuid
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Repo-root helpers
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[1]
_MANIFEST_PATH = _REPO_ROOT / "evals" / "catalog" / "fashion200k" / "manifest.json"
_AVSA_TOML = _REPO_ROOT / "config" / "avsa.toml"
_DATA_ROOT = _REPO_ROOT / "data" / "fashion200k" / "images"

pytestmark = pytest.mark.loadtest


# ===========================================================================
# 1 — Corpus selection
# ===========================================================================


class TestCorpusSelection:
    """The held-out corpus must contain ONLY test-split items, count == 733."""

    def test_load_test_corpus_returns_only_test_split(self) -> None:
        """No train-split item may appear in the held-out corpus."""
        from evals.workload.chat_embed.corpus import load_test_corpus

        items = load_test_corpus()
        splits = {i.item_id for i in items}
        # Cross-reference against the raw manifest to find any train leaks.
        with _MANIFEST_PATH.open() as fh:
            raw = json.load(fh)
        train_ids = {i["id"] for i in raw["items"] if i["split"] == "train"}
        leaked = splits & train_ids
        assert not leaked, (
            f"{len(leaked)} train-split item(s) leaked into the test corpus: "
            f"{sorted(leaked)[:5]!r}"
        )

    def test_load_test_corpus_count_is_733(self) -> None:
        """Exactly 733 held-out items (the test split) must be returned."""
        from evals.workload.chat_embed.corpus import (
            EXPECTED_CORPUS_SIZE,
            load_test_corpus,
        )

        items = load_test_corpus()
        assert len(items) == EXPECTED_CORPUS_SIZE, (
            f"Expected {EXPECTED_CORPUS_SIZE} test items; got {len(items)}.  "
            "If the manifest changed, update EXPECTED_CORPUS_SIZE and re-validate."
        )

    def test_load_test_corpus_rejects_wrong_split(self) -> None:
        """A manifest with only train items yields an empty corpus — no train leak."""
        import tempfile

        from evals.workload.chat_embed.corpus import load_test_corpus

        fake_manifest = {
            "seed": 0,
            "criteria": {},
            "dataset_version": "test",
            "items": [
                {
                    "id": "women/tops/t-shirts/00000001/00000001_0.jpeg",
                    "category": "top",
                    "title": "blue t-shirt",
                    "source_url": "https://example.com",
                    "split": "train",
                },
                {
                    "id": "women/tops/t-shirts/00000002/00000002_0.jpeg",
                    "category": "top",
                    "title": "red t-shirt",
                    "source_url": "https://example.com",
                    "split": "train",
                },
            ],
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tf:
            json.dump(fake_manifest, tf)
            tmp_path = Path(tf.name)

        try:
            items = load_test_corpus(manifest_path=tmp_path)
            assert items == [], (
                f"Expected empty corpus when manifest has only train items; got {items}"
            )
        finally:
            tmp_path.unlink(missing_ok=True)

    def test_corpus_item_local_paths_are_derived_from_item_ids(self) -> None:
        """Each corpus item's local_path must encode the item_id under data_root."""
        from evals.workload.chat_embed.corpus import load_test_corpus

        fake_root = Path("/fake/data/root")
        items = load_test_corpus(data_root=fake_root)
        for item in items[:10]:  # spot-check first 10
            expected_suffix = Path(f"{item.item_id}.jpg")
            assert item.local_path == fake_root / expected_suffix, (
                f"item_id {item.item_id!r}: expected path "
                f"{fake_root / expected_suffix}, got {item.local_path}"
            )

    def test_corpus_item_ids_map_to_on_disk_paths(self) -> None:
        """At least one item's local_path must exist on this machine.

        If the data directory has not been acquired, this test is skipped
        rather than failing — corpus selection is correct regardless of
        local storage.
        """
        from evals.workload.chat_embed.corpus import load_test_corpus

        items = load_test_corpus()
        on_disk = [i for i in items if i.local_path.exists()]
        if not on_disk:
            pytest.skip(
                "Fashion200k images not present locally; skipping on-disk check"
            )
        # Just assert the path resolves and is a file.
        assert on_disk[0].local_path.is_file(), (
            f"Expected a JPEG file at {on_disk[0].local_path}"
        )


# ===========================================================================
# 2 — Query-mix config
# ===========================================================================


class TestQueryMixConfig:
    """Weights must be present in config/avsa.toml and sum to 1.0."""

    def test_config_has_workload_chat_embed_section(self) -> None:
        """config/avsa.toml must contain a [workload.chat_embed] section."""
        with _AVSA_TOML.open("rb") as fh:
            cfg = tomllib.load(fh)
        assert "workload" in cfg, "config/avsa.toml must have a [workload] table"
        wl = cfg["workload"]
        assert "chat_embed" in wl, (
            "config/avsa.toml must have a [workload.chat_embed] section; "
            f"found workload keys: {sorted(wl)}"
        )

    def test_config_has_all_four_weights(self) -> None:
        """All four weight keys must be present."""
        with _AVSA_TOML.open("rb") as fh:
            cfg = tomllib.load(fh)
        cfg_entry = cfg["workload"]["chat_embed"]
        required_keys = {
            "image_only_weight",
            "text_only_weight",
            "image_text_weight",
            "multi_turn_weight",
        }
        missing = required_keys - set(cfg_entry)
        assert not missing, f"[workload.chat_embed] is missing keys: {sorted(missing)}"

    def test_config_weights_sum_to_one(self) -> None:
        """The four weights must sum to 1.0 (within floating-point tolerance)."""
        with _AVSA_TOML.open("rb") as fh:
            cfg = tomllib.load(fh)
        cfg_entry = cfg["workload"]["chat_embed"]
        total = (
            cfg_entry["image_only_weight"]
            + cfg_entry["text_only_weight"]
            + cfg_entry["image_text_weight"]
            + cfg_entry["multi_turn_weight"]
        )
        assert abs(total - 1.0) < 1e-9, (
            f"[workload.chat_embed] weights must sum to 1.0; got {total:.9f}"
        )

    def test_config_weights_are_floats_in_0_1(self) -> None:
        """Each weight must be a TOML float in (0, 1)."""
        with _AVSA_TOML.open("rb") as fh:
            cfg = tomllib.load(fh)
        cfg_entry = cfg["workload"]["chat_embed"]
        for key in (
            "image_only_weight",
            "text_only_weight",
            "image_text_weight",
            "multi_turn_weight",
        ):
            val = cfg_entry[key]
            assert isinstance(val, float), (
                f"[workload.chat_embed] {key} must be a TOML float; "
                f"got {type(val).__name__}"
            )
            assert 0.0 < val < 1.0, (
                f"[workload.chat_embed] {key} must be in (0, 1); got {val}"
            )

    def test_locustfile_weights_reflect_config(self) -> None:
        """The locustfile's integer task weights must match the config floats."""
        import importlib

        import locustfile

        importlib.reload(locustfile)  # ensure fresh config read

        with _AVSA_TOML.open("rb") as fh:
            cfg = tomllib.load(fh)
        cfg_entry = cfg["workload"]["chat_embed"]

        # The locustfile scales floats * 100 and rounds; verify the round-trip.
        scale = 100
        assert (
            round(cfg_entry["image_only_weight"] * scale) == locustfile._TW_IMAGE_ONLY
        )
        assert round(cfg_entry["text_only_weight"] * scale) == locustfile._TW_TEXT_ONLY
        assert (
            round(cfg_entry["image_text_weight"] * scale) == locustfile._TW_IMAGE_TEXT
        )
        assert (
            round(cfg_entry["multi_turn_weight"] * scale) == locustfile._TW_MULTI_TURN
        )

    def test_load_chat_embed_weights_rejects_nonunit_sum(self) -> None:
        """_load_chat_embed_weights raises ValueError when weights don't sum to 1.0."""
        import locustfile

        bad_config: dict[str, object] = {
            "workload": {
                "chat_embed": {
                    "image_only_weight": 0.5,
                    "text_only_weight": 0.5,
                    "image_text_weight": 0.5,
                    "multi_turn_weight": 0.5,
                }
            }
        }
        with pytest.raises(ValueError, match="sum to 1.0"):
            locustfile._load_chat_embed_weights(bad_config)


# ===========================================================================
# 3 — Task-set registration
# ===========================================================================


class TestTaskSetRegistration:
    """Importing the locustfile registers the ChatUser and EmbedUser classes."""

    def test_chat_user_class_exists(self) -> None:
        """ChatUser must be importable from the root locustfile."""
        import locustfile

        assert hasattr(locustfile, "ChatUser"), "locustfile must define ChatUser"

    def test_embed_user_class_exists(self) -> None:
        """EmbedUser must be importable from the root locustfile."""
        import locustfile

        assert hasattr(locustfile, "EmbedUser"), "locustfile must define EmbedUser"

    def test_batcher_user_class_still_exists(self) -> None:
        """BatcherUser must still be present (not replaced by chat_embed work)."""
        import locustfile

        assert hasattr(locustfile, "BatcherUser"), (
            "BatcherUser must not be removed; it drives the QPS bench"
        )

    def test_chat_user_is_http_user_subclass(self) -> None:
        """ChatUser must subclass locust.HttpUser."""
        from locust import HttpUser

        import locustfile

        assert issubclass(locustfile.ChatUser, HttpUser)

    def test_embed_user_is_http_user_subclass(self) -> None:
        """EmbedUser must subclass locust.HttpUser."""
        from locust import HttpUser

        import locustfile

        assert issubclass(locustfile.EmbedUser, HttpUser)

    def test_chat_user_has_task_methods(self) -> None:
        """ChatUser must expose the four task methods."""
        import locustfile

        for method_name in ("image_only", "text_only", "image_text", "multi_turn"):
            assert hasattr(locustfile.ChatUser, method_name), (
                f"ChatUser must have a '{method_name}' task method"
            )

    def test_embed_user_has_embed_task(self) -> None:
        """EmbedUser must expose embed_real_image."""
        import locustfile

        assert hasattr(locustfile.EmbedUser, "embed_real_image"), (
            "EmbedUser must have 'embed_real_image' task"
        )

    def test_chat_user_has_wait_time(self) -> None:
        """ChatUser must configure a wait_time (not None)."""
        import locustfile

        assert locustfile.ChatUser.wait_time is not None, "ChatUser must set wait_time"

    def test_embed_user_has_constant_0_wait_time(self) -> None:
        """EmbedUser must saturate the endpoint (constant(0) wait_time)."""
        import locustfile

        # constant(0) returns a callable returning 0 for any user.
        user = locustfile.EmbedUser.__new__(locustfile.EmbedUser)
        wait = locustfile.EmbedUser.wait_time(user)
        assert wait == 0, "EmbedUser.wait_time must return 0 (saturate the endpoint)"


# ===========================================================================
# 4 — Realistic image sizes
# ===========================================================================


class TestRealisticImageSizes:
    """ChatUser/EmbedUser must use real >=224x224 images, not the 1-px fixture."""

    def test_corpus_item_not_1px_fixture(self) -> None:
        """The first on-disk corpus image must be larger than the 1px fixture.

        The 1px JPEG fixture is ~81 bytes; any real Fashion200k image is
        orders of magnitude larger.  Skipped if images are not present.
        """
        from evals.workload.chat_embed.corpus import load_test_corpus

        items = load_test_corpus()
        on_disk = [i for i in items if i.local_path.exists()]
        if not on_disk:
            pytest.skip("Fashion200k images not present locally; skipping size check")

        sample = on_disk[0]
        size_bytes = sample.local_path.stat().st_size
        assert size_bytes > 1000, (
            f"Expected a real image (>1000 bytes); got {size_bytes} bytes at "
            f"{sample.local_path}"
        )

    def test_corpus_item_dimensions_gte_224(self) -> None:
        """A real test-split image must be at least 224x224 pixels.

        Skipped if images are not present locally or Pillow is unavailable.
        """
        try:
            from PIL import Image
        except ImportError:
            pytest.skip("Pillow not installed; skipping dimension check")

        from evals.workload.chat_embed.corpus import load_test_corpus

        items = load_test_corpus()
        on_disk = [i for i in items if i.local_path.exists()]
        if not on_disk:
            pytest.skip(
                "Fashion200k images not present locally; skipping dimension check"
            )

        sample = on_disk[0]
        img = Image.open(sample.local_path)
        w, h = img.size
        assert w >= 224 and h >= 224, (
            f"Expected >=224x224; got {w}x{h} at {sample.local_path}"
        )

    def test_embed_user_does_not_use_1px_payload(self) -> None:
        """EmbedUser.embed_real_image must call _read_image_bytes."""
        import inspect

        import locustfile

        source = inspect.getsource(locustfile.EmbedUser.embed_real_image)
        assert "_ONE_PIXEL_JPEG_B64" not in source, (
            "EmbedUser.embed_real_image must not use the 1-pixel fixture; "
            "it must read from the real corpus."
        )
        assert "_read_image_bytes" in source, (
            "EmbedUser.embed_real_image must call _read_image_bytes to use "
            "real corpus images."
        )

    def test_chat_tasks_do_not_use_1px_payload(self) -> None:
        """None of the ChatUser image tasks may reference the 1-px fixture."""
        import inspect

        import locustfile

        for method_name in ("image_only", "image_text", "multi_turn"):
            source = inspect.getsource(getattr(locustfile.ChatUser, method_name))
            assert "_JPEG_1PX" not in source and "_ONE_PIXEL_JPEG_B64" not in source, (
                f"ChatUser.{method_name} must not reference the 1-px fixture"
            )


# ===========================================================================
# 5 — Multi-turn resume contract
# ===========================================================================


class TestMultiTurnResumeContract:
    """multi_turn must use the header-based resume contract, not SSE/form-field.

    The real ``/chat`` API contract (``apps/api/src/avsa_api/routes/chat.py``):
    - Turn-1 response: conversation id is in ``X-Conversation-Id`` response
      header only.  There is no ``conversation_id`` SSE event.
    - Turn-2 request: the client must pass the id in ``X-Resume-Conversation-Id``
      request header.  A ``conversation_id`` form field is silently ignored.
    """

    def _make_fake_response(
        self, status_code: int = 200, conv_id: str | None = None
    ) -> MagicMock:
        """Return a fake requests.Response-like object."""
        resp = MagicMock()
        resp.status_code = status_code
        if conv_id is not None:
            resp.headers = {"X-Conversation-Id": conv_id}
        else:
            resp.headers = {}
        return resp

    def _run_multi_turn(
        self, turn1_response: MagicMock
    ) -> list[tuple[str, dict[str, object]]]:
        """Execute multi_turn with a mocked corpus + HTTP client.

        Returns a list of (name, kwargs) for each POST call made by the task
        so the test can inspect the arguments of each turn.
        """
        import locustfile

        calls: list[tuple[str, dict[str, object]]] = []

        # Fake corpus item with minimal attributes.
        fake_item = MagicMock()
        fake_item.item_id = "women/tops/shirts/00000001/00000001_0.jpeg"
        fake_item.title = "blue shirt"
        fake_item.category = "shirt"

        # Fake locust HTTP client.
        fake_client = MagicMock()
        # First POST returns turn1_response; subsequent POSTs return a 200 stub.
        turn2_response = MagicMock()
        turn2_response.status_code = 200
        fake_client.post.side_effect = [turn1_response, turn2_response]

        # Build a minimal ChatUser instance (no real locust env needed).
        user = locustfile.ChatUser.__new__(locustfile.ChatUser)
        user.client = fake_client
        user._forwarded_for = "10.0.0.2"

        with (
            patch.object(locustfile, "_get_corpus", return_value=[fake_item]),
            patch.object(locustfile, "_read_image_bytes", return_value=b"\xff\xd8\xff"),
            patch("random.choice", return_value=fake_item),
        ):
            user.multi_turn()

        # Collect call arguments.  cast() is needed because mock-stubs type
        # call_args.kwargs as ``object``; we know it is a dict at runtime.
        for call_args in fake_client.post.call_args_list:
            kw = cast(dict[str, object], call_args.kwargs)
            name = cast(str, kw.get("name")) or (
                str(call_args.args[0]) if call_args.args else ""
            )
            calls.append((name, kw))

        return calls

    # -----------------------------------------------------------------------
    # Positive: header-based contract is used correctly
    # -----------------------------------------------------------------------

    def test_turn2_sends_resume_header_when_conv_id_present(self) -> None:
        """Turn 2 must send X-Resume-Conversation-Id with the turn-1 response id."""
        test_uuid = str(uuid.uuid4())
        turn1_resp = self._make_fake_response(status_code=200, conv_id=test_uuid)
        calls = self._run_multi_turn(turn1_resp)

        assert len(calls) == 2, (
            f"Expected exactly 2 POST calls (turn 1 + turn 2); got {len(calls)}"
        )
        _name, turn2_kwargs = calls[1]
        turn2_headers = cast(dict[str, str], turn2_kwargs.get("headers", {}))
        assert "X-Resume-Conversation-Id" in turn2_headers, (
            "Turn 2 must include X-Resume-Conversation-Id in its request headers; "
            f"got headers: {turn2_headers}"
        )
        assert turn2_headers["X-Resume-Conversation-Id"] == test_uuid, (
            "X-Resume-Conversation-Id must equal the turn-1 response id "
            f"{test_uuid!r}; got "
            f"{turn2_headers['X-Resume-Conversation-Id']!r}"
        )

    def test_turn2_does_not_send_conversation_id_form_field(self) -> None:
        """Turn 2 must NOT include a conversation_id form field (ignored by server)."""
        test_uuid = str(uuid.uuid4())
        turn1_resp = self._make_fake_response(status_code=200, conv_id=test_uuid)
        calls = self._run_multi_turn(turn1_resp)

        assert len(calls) == 2, "Expected 2 POST calls"
        _name, turn2_kwargs = calls[1]
        form_data = cast(dict[str, str], turn2_kwargs.get("data", {}))
        assert "conversation_id" not in form_data, (
            "Turn 2 must NOT send a conversation_id form field; "
            "the server ignores it (session-fixation guard). "
            f"Got data: {form_data}"
        )

    # -----------------------------------------------------------------------
    # Negative: graceful skip when header is absent
    # -----------------------------------------------------------------------

    def test_turn2_skipped_when_header_missing(self) -> None:
        """Turn 2 is skipped when turn-1 response lacks X-Conversation-Id."""
        turn1_resp = self._make_fake_response(status_code=200, conv_id=None)
        calls = self._run_multi_turn(turn1_resp)

        assert len(calls) == 1, (
            "Turn 2 must be skipped when X-Conversation-Id is absent from the "
            f"turn-1 response; got {len(calls)} POST call(s)"
        )

    # -----------------------------------------------------------------------
    # Source-level assertion: no SSE-event parsing in multi_turn
    # -----------------------------------------------------------------------

    def test_multi_turn_does_not_parse_sse_events_for_conv_id(self) -> None:
        """multi_turn source must not contain SSE conversation_id event parsing."""
        import inspect

        import locustfile

        source = inspect.getsource(locustfile.ChatUser.multi_turn)
        assert 'evt.get("type") == "conversation_id"' not in source, (
            "multi_turn must not parse a 'conversation_id' SSE event type; "
            "there is no such event in the real API response."
        )
        assert '"type"] == "conversation_id"' not in source, (
            "multi_turn must not parse a 'conversation_id' SSE event type."
        )

    def test_multi_turn_reads_response_header_not_sse_body(self) -> None:
        """multi_turn source must reference resp.headers (the real capture path)."""
        import inspect

        import locustfile

        source = inspect.getsource(locustfile.ChatUser.multi_turn)
        assert "headers.get" in source or ".headers[" in source, (
            "multi_turn must read the conversation id from resp.headers "
            "(e.g. resp1.headers.get('X-Conversation-Id')); "
            "the current source does not reference .headers"
        )
