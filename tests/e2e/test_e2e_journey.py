"""E2E integration tests -- full AVSA stack journey tests.

All test cases exercise the real wire path:
  API (8080) -> orchestrator (gRPC 50051) -> batcher (8001) -> model (8002) -> pgvector

Prerequisites:
  - AVSA_E2E=1 environment variable set.
  - The full local stack running (`just stack-up`): real ViT, real Anthropic,
    the local catalog DB on :5434, and the API on :8080.

Test cases:
  1. /chat happy path -- SSE stream emits product_card from seeded catalog.
  2. Batcher error propagation -- model-down SSE terminates with error event.
  3. Proto contract smoke -- buf lint + generated-stub drift check.

The external MCP surface (find_similar / extract_attributes) is the conformant
JSON-RPC server (AVSA.MCP.Server); it is covered by the orchestrator
Elixir tests and the smoke suite, not this API-level journey.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

# ---------------------------------------------------------------------------
# Shared test constants
# ---------------------------------------------------------------------------

# A real 224x224 RGB product JPEG (committed fixture). A 1x1 synthetic JPEG
# passes the upload guard but the REAL ViT rejects it (the image processor needs
# a usable 3-channel image), so the embed path 502s and no product_card is ever
# emitted. These journey tests run against a live stack with the real model, so
# they upload a real image. (tests/fixtures/product-photo.jpg, 224x224 = the ViT
# input size.)
_PRODUCT_JPEG: bytes = (
    Path(__file__).resolve().parent.parent / "fixtures" / "product-photo.jpg"
).read_bytes()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_sse_events(raw_text: str) -> list[dict[str, Any]]:
    """Parse SSE `data:` lines from a raw SSE response body into dicts.

    Ignores blank lines, comment lines (`:`) and lines without a `data:` prefix.
    Returns a list of parsed JSON objects in order of appearance.
    """
    events: list[dict[str, Any]] = []
    for line in raw_text.splitlines():
        if line.startswith("data: "):
            payload = line[len("data: ") :].strip()
            if payload:
                try:
                    events.append(json.loads(payload))
                except json.JSONDecodeError as exc:
                    pytest.fail(
                        f"SSE data line is not valid JSON: {payload!r}. "
                        f"JSONDecodeError: {exc}"
                    )
    return events


def _assert_valid_uuid(value: str, label: str) -> None:
    """Assert `value` is parseable as a UUID4; fail with a descriptive message."""
    try:
        parsed = uuid.UUID(value)
        if parsed.version != 4:
            pytest.fail(
                f"{label}: expected UUID4 but got version {parsed.version}: {value!r}"
            )
    except ValueError as exc:
        pytest.fail(f"{label}: not a valid UUID: {value!r}. Error: {exc}")


def _product_card_ids(events: list[dict[str, Any]]) -> list[str]:
    """Extract the non-empty `card.id`s from product_card SSE events, in order."""
    ids: list[str] = []
    for event in events:
        if event.get("type") != "product_card":
            continue
        card = event.get("card")
        if isinstance(card, dict):
            cid = card.get("id")
            if isinstance(cid, str) and cid:
                ids.append(cid)
    return ids


# ---------------------------------------------------------------------------
# Test case 1 -- /chat happy path
# ---------------------------------------------------------------------------


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_chat_sse_emits_product_card_from_seeded_catalog(
    e2e_client: httpx.AsyncClient,
) -> None:
    """/chat happy path: SSE stream must emit at least one product_card event.

    Asserts:
    - HTTP 200 with content-type: text/event-stream.
    - X-Conversation-Id header is a valid UUID4.
    - At least one SSE event with type=product_card is present.
    - Each product_card has a non-empty `card.id` that is a valid UUID (i.e.
      it came from the catalog, not a stub).
    - At least one product_card carries a non-empty `card.title`.

    Failure mode (stack not up): httpx.ConnectError / RemoteProtocolError.
    The test does NOT catch network errors -- they propagate so pytest reports
    the connection refusal as a test failure, not a skip.
    """
    response = await e2e_client.post(
        "/chat",
        files={"image": ("product.jpg", io.BytesIO(_PRODUCT_JPEG), "image/jpeg")},
        data={"text": "find me something similar"},
        headers={"X-Forwarded-For": "10.0.0.1"},
    )

    assert response.status_code == 200, (
        f"Expected HTTP 200 from /chat, got {response.status_code}. "
        f"Body: {response.text[:500]!r}"
    )

    content_type = response.headers.get("content-type", "")
    assert "text/event-stream" in content_type, (
        f"Expected content-type: text/event-stream, got {content_type!r}"
    )

    conv_id = response.headers.get("X-Conversation-Id", "")
    assert conv_id, "Expected X-Conversation-Id header to be present and non-empty"
    _assert_valid_uuid(conv_id, "X-Conversation-Id")

    events = _parse_sse_events(response.text)
    assert events, (
        f"Expected at least one SSE data: event in the response body. "
        f"Raw body: {response.text[:1000]!r}"
    )

    card_events = [e for e in events if e.get("type") == "product_card"]
    assert card_events, (
        f"Expected at least one SSE event with type=product_card. "
        f"Got event types: {[e.get('type') for e in events]}"
    )

    # Validate structural shape of each product_card event.
    for card_event in card_events:
        card = card_event.get("card")
        assert isinstance(card, dict), (
            f"product_card event missing 'card' dict. Event: {card_event!r}"
        )
        card_id = card.get("id", "")
        assert card_id, f"product_card.card.id is empty. Card: {card!r}"
        # All catalog IDs are UUIDs. A stub returns 'stub-001' which fails
        # this assertion -- exactly the failure we need.
        _assert_valid_uuid(card_id, "product_card.card.id")
        assert card.get("title"), f"product_card.card.title is empty. Card: {card!r}"


# ---------------------------------------------------------------------------
# Test case 1b -- text-only /chat happy path (the /embed_text path)
# ---------------------------------------------------------------------------


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_chat_text_only_emits_product_card_from_seeded_catalog(
    e2e_client: httpx.AsyncClient,
) -> None:
    """Text-only /chat: SSE stream must emit a product_card from the seeded catalog.

    The symmetric counterpart to the image journey above, exercising the TEXT
    modality end-to-end: a text-only query routes through the orchestrator's
    ``AVSA.TextTool`` -> ``POST /embed_text`` (512-d CLIP) -> pgvector text-kNN ->
    product cards. This is the only full-stack test of the /embed_text path.

    Requires the e2e catalog seeded with ``text_embedding`` vectors (just as the
    image journey requires ``image_embedding``): against a text-embedded catalog
    the text-kNN returns rows, so at least one product_card with a UUID id must
    appear. If the e2e seed lacks text vectors the text-kNN returns nothing and
    this test fails -- pointing at the seed, exactly as intended.

    Asserts:
    - HTTP 200 with content-type: text/event-stream.
    - X-Conversation-Id is a valid UUID4.
    - No error event (the text path completed, did not 502/error).
    - At least one product_card with a non-empty UUID ``card.id`` and title.

    Failure mode (stack not up): httpx.ConnectError / RemoteProtocolError, which
    propagate as a failure (not a skip), matching the image journey.
    """
    response = await e2e_client.post(
        "/chat",
        data={"text": "a red floral summer dress"},
        headers={"X-Forwarded-For": "10.0.0.2"},
    )

    assert response.status_code == 200, (
        f"Expected HTTP 200 from text-only /chat, got {response.status_code}. "
        f"Body: {response.text[:500]!r}"
    )
    content_type = response.headers.get("content-type", "")
    assert "text/event-stream" in content_type, (
        f"Expected content-type: text/event-stream, got {content_type!r}"
    )

    conv_id = response.headers.get("X-Conversation-Id", "")
    assert conv_id, "Expected X-Conversation-Id header to be present and non-empty"
    _assert_valid_uuid(conv_id, "X-Conversation-Id")

    events = _parse_sse_events(response.text)
    assert events, (
        f"Expected at least one SSE data: event. Raw body: {response.text[:1000]!r}"
    )

    error_events = [e for e in events if e.get("type") == "error"]
    assert not error_events, (
        f"text-only /chat emitted error event(s) -- the /embed_text text path failed: "
        f"{error_events!r}"
    )

    card_events = [e for e in events if e.get("type") == "product_card"]
    assert card_events, (
        "Expected at least one product_card from the text-kNN over the seeded catalog. "
        "If the e2e catalog lacks text_embedding vectors, text-kNN returns nothing -- "
        "seed text embeddings into the catalog. Got event types: "
        f"{[e.get('type') for e in events]}"
    )
    for card_event in card_events:
        card = card_event.get("card")
        assert isinstance(card, dict), (
            f"product_card missing 'card' dict: {card_event!r}"
        )
        card_id = card.get("id", "")
        assert card_id, f"product_card.card.id is empty: {card!r}"
        _assert_valid_uuid(card_id, "product_card.card.id")
        assert card.get("title"), f"product_card.card.title is empty: {card!r}"


# ---------------------------------------------------------------------------
# Test case 1c -- multi-turn refinement (conversation resume + prior-id exclusion)
# ---------------------------------------------------------------------------


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_chat_resume_excludes_previously_shown_results(
    e2e_client: httpx.AsyncClient,
) -> None:
    """A resumed turn must NOT repeat the products shown in the prior turn.

    This is the end-to-end proof that the streaming path now routes through the
    ``AVSA.Conversation`` GenServer: ``prior_result_ids`` from turn 1 are carried
    into turn 2's kNN exclusion (``WHERE id != ALL($2::uuid[])``). Both turns
    submit the SAME image with the SAME text, so without exclusion the kNN
    ordering is identical and turn 2 would return the EXACT same set. A disjoint
    second set therefore proves the exclusion is wired into production — not just
    the previously-unreachable unary StartConversation RPC.

    Resume is requested via ``X-Resume-Conversation-Id`` (the API ignores a client
    ``X-Conversation-Id`` as a session-fixation guard).

    Asserts:
    - Turn 1 returns at least one product_card (the flow works).
    - Turn 2 resumes the SAME conversation (``X-Conversation-Id`` echoes turn 1).
    - Turn 2 returns at least one product_card (the seeded catalog has more than
      one kNN page of results; if this fails, seed a larger e2e catalog).
    - Turn 2's product ids are DISJOINT from turn 1's (prior-id exclusion).

    Failure mode (stack not up): httpx.ConnectError / RemoteProtocolError, which
    propagate as a failure (not a skip), matching the other journey tests.
    """
    resp1 = await e2e_client.post(
        "/chat",
        files={"image": ("product.jpg", io.BytesIO(_PRODUCT_JPEG), "image/jpeg")},
        data={"text": "find me something similar"},
        headers={"X-Forwarded-For": "10.0.0.4"},
    )
    assert resp1.status_code == 200, (
        f"turn 1 /chat failed with {resp1.status_code}: {resp1.text[:500]!r}"
    )
    conv_id = resp1.headers.get("X-Conversation-Id", "")
    _assert_valid_uuid(conv_id, "turn 1 X-Conversation-Id")

    ids_turn1 = _product_card_ids(_parse_sse_events(resp1.text))
    assert ids_turn1, (
        f"turn 1 returned no product_card events. Raw body: {resp1.text[:1000]!r}"
    )

    # ── Turn 2: resume the SAME conversation with an identical query. ──────────
    resp2 = await e2e_client.post(
        "/chat",
        files={"image": ("product.jpg", io.BytesIO(_PRODUCT_JPEG), "image/jpeg")},
        data={"text": "find me something similar"},
        headers={"X-Forwarded-For": "10.0.0.4", "X-Resume-Conversation-Id": conv_id},
    )
    assert resp2.status_code == 200, (
        f"turn 2 /chat failed with {resp2.status_code}: {resp2.text[:500]!r}"
    )

    # Resume took effect: the API reused the conversation rather than minting a
    # fresh id. (If this fails, the resume header was not honoured end-to-end.)
    assert resp2.headers.get("X-Conversation-Id", "") == conv_id, (
        "turn 2 did not resume the conversation — expected X-Conversation-Id "
        f"{conv_id!r}, got {resp2.headers.get('X-Conversation-Id', '')!r}"
    )

    ids_turn2 = _product_card_ids(_parse_sse_events(resp2.text))
    assert ids_turn2, (
        "turn 2 returned no product_card events. For a follow-up turn to return "
        "fresh items the seeded e2e catalog must hold more than one kNN page "
        "(LIMIT 20) of matches; the `just stack-up` catalog (~5000 rows) satisfies this."  # noqa: E501
    )

    overlap = set(ids_turn1) & set(ids_turn2)
    assert not overlap, (
        "a resumed turn repeated products already shown in the prior turn — "
        "prior_result_ids exclusion is NOT taking effect end-to-end. "
        f"Overlapping ids: {sorted(overlap)}"
    )


# ---------------------------------------------------------------------------
# Test case 2 -- Batcher error propagation
# ---------------------------------------------------------------------------


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_chat_sse_terminates_with_error_event_when_model_service_down(
    e2e_client: httpx.AsyncClient,
) -> None:
    """Batcher error propagation: model-service-down causes SSE error, not a hang.

    Uses docker compose stop/start to take the model service down then restore
    it. Requires the `docker` CLI on PATH. Skipped if docker is unavailable.

    Asserts (model down):
    - HTTP 200 is still returned (SSE errors are in-band per the OpenAPI spec).
    - The SSE stream terminates with at least one event with type=error.
    - The stream does NOT hang past the configured e2e timeout (30 s).

    Asserts (model recovered):
    - After `docker compose start model`, a subsequent /chat call returns at
      least one product_card event again.

    Failure mode (stack not up): httpx.RemoteProtocolError / ConnectError.
    Failure mode (model is up): zero error events -- assertion fails.
    """
    compose_file = str(
        Path(__file__).parent.parent.parent / "infra" / "e2e" / "docker-compose.yml"
    )

    # ── Check docker is available ─────────────────────────────────────────────
    docker_check = subprocess.run(
        ["docker", "compose", "version"],
        capture_output=True,
        timeout=10,
    )
    if docker_check.returncode != 0:
        pytest.skip(
            "Skipping batcher error propagation test: `docker compose` CLI not "
            "available. This test requires docker-compose to stop/start the model."
        )

    if not Path(compose_file).is_file():
        pytest.skip(
            "Skipping batcher error propagation test: "
            f"{compose_file} not found. "
            "This test requires the compose-based local stack (`just stack-up`), "
            "not the process-based CI stack."
        )

    # ── Stop the model service ────────────────────────────────────────────────
    stop_result = subprocess.run(
        ["docker", "compose", "-f", compose_file, "stop", "model"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if stop_result.returncode != 0:
        pytest.fail(
            f"Failed to stop the model service via docker compose. "
            f"stdout: {stop_result.stdout!r} stderr: {stop_result.stderr!r}"
        )

    try:
        # ── POST /chat with model down ────────────────────────────────────────
        response = await e2e_client.post(
            "/chat",
            files={"image": ("product.jpg", io.BytesIO(_PRODUCT_JPEG), "image/jpeg")},
            data={"text": "find something similar"},
            headers={"X-Forwarded-For": "10.0.0.2"},
        )

        # HTTP 200 must be returned -- SSE errors are in-band.
        assert response.status_code == 200, (
            f"Expected HTTP 200 even when model is down (SSE errors are in-band). "
            f"Got {response.status_code}: {response.text[:500]!r}"
        )

        events = _parse_sse_events(response.text)
        error_events = [e for e in events if e.get("type") == "error"]

        assert error_events, (
            f"Expected at least one SSE event with type=error when the model "
            f"service is down. "
            f"Got event types: {[e.get('type') for e in events]}. "
            f"Raw body: {response.text[:1000]!r}"
        )

        # Verify the error event has the required fields from the OpenAPI spec.
        for err_event in error_events:
            assert err_event.get("code"), (
                f"SSE error event missing 'code' field. Event: {err_event!r}"
            )
            assert err_event.get("message"), (
                f"SSE error event missing 'message' field. Event: {err_event!r}"
            )

    finally:
        # ── Restart the model service regardless of test outcome ─────────────
        start_result = subprocess.run(
            ["docker", "compose", "-f", compose_file, "start", "model"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if start_result.returncode != 0:
            # Not a pytest.fail -- we're in finally. Log for debugging.
            sys.stderr.write(
                f"[WARNING] Failed to restart model service: {start_result.stderr!r}\n"
            )

    # ── Wait for model recovery, then assert happy path works again. ──────────
    # Poll /health on the model container (port 8002) to confirm it is up
    # before sending a recovery assertion request.
    model_url = os.environ.get("AVSA_E2E_MODEL_URL", "http://localhost:8002")
    recovered = False
    async with httpx.AsyncClient(base_url=model_url, timeout=5.0) as model_client:
        for _ in range(12):  # up to 60 s (12 x 5 s)
            try:
                health_resp = await model_client.get("/health")
                if health_resp.status_code == 200:
                    recovered = True
                    break
            except httpx.ConnectError:
                pass
            await asyncio.sleep(5)

    assert recovered, (
        "Model service did not recover within 60 s after restart. "
        "The batcher error propagation test cannot verify recovery."
    )

    # ── Confirm recovery: a fresh /chat must return at least one product_card. ─
    recovery_response = await e2e_client.post(
        "/chat",
        files={"image": ("product.jpg", io.BytesIO(_PRODUCT_JPEG), "image/jpeg")},
        data={"text": "find something similar again"},
        headers={"X-Forwarded-For": "10.0.0.3"},
    )

    assert recovery_response.status_code == 200, (
        f"Recovery check: expected HTTP 200 after model restart, got "
        f"{recovery_response.status_code}: {recovery_response.text[:500]!r}"
    )

    recovery_events = _parse_sse_events(recovery_response.text)
    recovery_cards = [e for e in recovery_events if e.get("type") == "product_card"]

    assert recovery_cards, (
        f"Recovery check: expected at least one product_card after model restart. "
        f"Got event types: {[e.get('type') for e in recovery_events]}"
    )


# ---------------------------------------------------------------------------
# Test case 3 -- Proto contract smoke test
# ---------------------------------------------------------------------------


@pytest.mark.e2e
def test_proto_contract_buf_lint_and_stub_no_drift() -> None:
    """Proto contract smoke: buf lint passes and Python stubs have no drift.

    Two sub-checks:
    a) `buf lint specs/orchestrator/avsa.proto` exits 0 (proto is well-formed).
    b) `python -m grpc_tools.protoc ...` regenerates stubs into a temp dir;
       the regenerated bytes match the committed stubs byte-for-byte
       (header comments stripped, which contain grpcio version noise).

    Does NOT require the full service stack -- only `buf` and `grpcio-tools`
    on PATH. Marked @pytest.mark.e2e because it belongs to the integration
    phase; run without a stack by setting AVSA_E2E=1 directly for that job.

    Failure mode (buf not on PATH): subprocess.FileNotFoundError + pytest.skip.
    Failure mode (proto lint fails): assertion fails with buf stdout/stderr.
    Failure mode (drift): assertion fails showing the first differing line.
    """
    repo_root = Path(__file__).parent.parent.parent
    proto_file = repo_root / "specs" / "orchestrator" / "avsa.proto"
    buf_yaml = repo_root / "specs" / "orchestrator" / "buf.yaml"

    assert proto_file.exists(), f"Proto file not found at expected path: {proto_file}."

    # ── Sub-check a: buf lint ─────────────────────────────────────────────────
    try:
        lint_result = subprocess.run(
            ["buf", "lint", str(proto_file), f"--config={buf_yaml}"],
            capture_output=True,
            text=True,
            cwd=str(repo_root),
            timeout=30,
        )
    except FileNotFoundError:
        pytest.skip(
            "Skipping proto lint: `buf` CLI not found on PATH. "
            "Install buf (https://buf.build/docs/installation) to run locally. "
            "In CI, buf is installed by the ci.yml workflow."
        )

    assert lint_result.returncode == 0, (
        f"`buf lint` failed for {proto_file}.\n"
        f"stdout: {lint_result.stdout}\n"
        f"stderr: {lint_result.stderr}"
    )

    # ── Sub-check b: generated Python stub drift check ────────────────────────
    committed_pb2 = (
        repo_root / "apps" / "api" / "src" / "avsa_api" / "proto" / "avsa_pb2.py"
    )
    committed_pb2_grpc = (
        repo_root / "apps" / "api" / "src" / "avsa_api" / "proto" / "avsa_pb2_grpc.py"
    )

    assert committed_pb2.exists(), (
        f"Committed avsa_pb2.py not found at {committed_pb2}. "
        f"Run `just proto-gen-python` to generate it."
    )
    assert committed_pb2_grpc.exists(), (
        f"Committed avsa_pb2_grpc.py not found at {committed_pb2_grpc}. "
        f"Run `just proto-gen-python` to generate it."
    )

    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            gen_result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "grpc_tools.protoc",
                    f"--proto_path={proto_file.parent}",
                    f"--python_out={tmpdir}",
                    f"--grpc_python_out={tmpdir}",
                    proto_file.name,
                ],
                capture_output=True,
                text=True,
                cwd=str(repo_root),
                timeout=60,
            )
        except FileNotFoundError:
            pytest.skip(
                "Skipping Python stub drift check: `grpc_tools.protoc` not available. "
                "Install grpcio-tools (`pip install grpcio-tools`) to run this check."
            )

        if gen_result.returncode != 0:
            pytest.fail(
                f"grpc_tools.protoc failed to generate Python stubs.\n"
                f"stdout: {gen_result.stdout}\n"
                f"stderr: {gen_result.stderr}"
            )

        # Compare generated vs committed, stripping leading header comment blocks
        # (which include a grpcio version that changes on each install).
        generated_pb2 = Path(tmpdir) / "avsa_pb2.py"
        generated_pb2_grpc = Path(tmpdir) / "avsa_pb2_grpc.py"

        for generated, committed in [
            (generated_pb2, committed_pb2),
            (generated_pb2_grpc, committed_pb2_grpc),
        ]:
            if not generated.exists():
                pytest.fail(
                    f"grpc_tools.protoc did not produce {generated.name}. "
                    f"Contents of tmpdir: {list(Path(tmpdir).iterdir())}"
                )

            # `just proto-gen-python` rewrites grpc_tools' top-level
            # `import avsa_pb2` to a package-absolute import so the stub resolves
            # inside the avsa_api.proto package. Apply the same rewrite to the
            # freshly generated lines so the committed (fixed-up) stub compares
            # equal — drift is still caught for every other line.
            generated_lines = [
                "from avsa_api.proto import avsa_pb2 as avsa__pb2"
                if line == "import avsa_pb2 as avsa__pb2"
                else line
                for line in generated.read_text().splitlines()
            ]
            committed_lines = committed.read_text().splitlines()

            # Strip leading comment blocks from both sides.
            def _strip_header(lines: list[str]) -> list[str]:
                for i, line in enumerate(lines):
                    if line and not line.startswith("#"):
                        return lines[i:]
                return lines

            gen_body = _strip_header(generated_lines)
            com_body = _strip_header(committed_lines)

            differing_idx = next(
                (
                    i
                    for i, (g, c) in enumerate(zip(gen_body, com_body, strict=False))
                    if g != c
                ),
                "length differs" if len(gen_body) != len(com_body) else None,
            )

            assert gen_body == com_body, (
                f"Proto stub drift detected in {committed.name}.\n"
                f"Committed stubs are out of sync with {proto_file.name}.\n"
                f"Run `just proto-gen-python` to regenerate and commit.\n"
                f"First differing line index: {differing_idx}"
            )
