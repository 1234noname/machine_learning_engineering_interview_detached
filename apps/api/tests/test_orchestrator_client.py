"""Unit tests for OrchestratorClient (the API's gRPC client to the orchestrator).

Covers the pure pieces that need no live channel:

  - _product_result_to_card - the real-mode ProductResultEvent → product_card
    mapping (metadata_json parse, price_cents → ZAR major units, score, and the
    malformed-JSON fallback). Exercised with real protos, so this is the only
    fast coverage of the real-gRPC output shape; previously it was reached only
    in e2e, and the stub-mode route tests assert a hand-written card that the
    real path would never emit.
  - _build_grpc_metadata - W3C baggage + the (placeholder) traceparent.
"""

from __future__ import annotations

import json

import pytest

from avsa_api.clients.orchestrator import OrchestratorClient, _product_result_to_card
from avsa_api.proto import avsa_pb2

# ---------------------------------------------------------------------------
# _product_result_to_card
# ---------------------------------------------------------------------------


def test_product_result_to_card_maps_all_fields() -> None:
    """A populated metadata_json maps to the full product_card shape."""
    pr = avsa_pb2.ProductResultEvent(
        product_id="11111111-1111-1111-1111-111111111111",
        score=0.87,
        metadata_json=json.dumps(
            {
                "title": "Black knit dress",
                "category": "dress",
                "price_cents": 12999,
                "image_url": "/images/x.jpg",
            }
        ),
    )
    event = _product_result_to_card(pr)
    assert event["type"] == "product_card"
    card = event["card"]
    assert isinstance(card, dict)
    assert card["id"] == "11111111-1111-1111-1111-111111111111"
    assert card["title"] == "Black knit dress"
    assert card["category"] == "dress"
    assert card["price"] == 129.99, "price_cents must convert to ZAR major units"
    assert card["currency"] == "ZAR"
    assert card["image_url"] == "/images/x.jpg"
    assert card["score"] == pytest.approx(0.87, abs=1e-6)


def test_product_result_to_card_missing_price_defaults_to_zero() -> None:
    """No price_cents in metadata → price 0.0 (no KeyError)."""
    pr = avsa_pb2.ProductResultEvent(
        product_id="p1",
        score=0.0,
        metadata_json=json.dumps({"title": "t", "category": "c", "image_url": "i"}),
    )
    assert _product_result_to_card(pr)["card"]["price"] == 0.0  # type: ignore[index]


def test_product_result_to_card_malformed_json_degrades_to_defaults() -> None:
    """Malformed metadata_json must not fail the turn - empty meta, proto fields kept."""
    pr = avsa_pb2.ProductResultEvent(product_id="x", score=0.5, metadata_json="{not valid json")
    card = _product_result_to_card(pr)["card"]
    assert isinstance(card, dict)
    assert card["title"] == "" and card["category"] == "" and card["image_url"] == ""
    assert card["price"] == 0.0
    assert card["id"] == "x"
    assert card["score"] == pytest.approx(0.5, abs=1e-6)


def test_product_result_to_card_empty_metadata_json_is_empty_meta() -> None:
    """An empty metadata_json string is treated as empty metadata (not a parse error)."""
    pr = avsa_pb2.ProductResultEvent(product_id="y", score=0.0, metadata_json="")
    card = _product_result_to_card(pr)["card"]
    assert isinstance(card, dict)
    assert card["id"] == "y"
    assert card["title"] == "" and card["price"] == 0.0


# ---------------------------------------------------------------------------
# _build_grpc_metadata
# ---------------------------------------------------------------------------


@pytest.fixture
def client() -> OrchestratorClient:
    return OrchestratorClient()


def test_baggage_carries_conversation_id(client: OrchestratorClient) -> None:
    conv = "c0ffee12-3456-7890-abcd-ef0123456789"
    md = dict(client._build_grpc_metadata(conv))
    assert md["baggage"] == f"conversation_id={conv}"


def test_traceparent_is_well_formed_with_derived_trace_id(client: OrchestratorClient) -> None:
    """traceparent = 00-<trace_id>-<16 zero hex>-01, trace_id derived from the conv id."""
    conv = "550e8400-e29b-41d4-a716-446655440000"
    version, trace_id, span_id, flags = dict(client._build_grpc_metadata(conv))[
        "traceparent"
    ].split("-")
    assert version == "00"
    assert trace_id == conv.replace("-", "")  # a UUID fills the 32 hex chars exactly
    assert span_id == "0" * 16
    assert flags == "01"


def test_short_conversation_id_is_zero_padded_to_32_hex(client: OrchestratorClient) -> None:
    trace_id = dict(client._build_grpc_metadata("abc"))["traceparent"].split("-")[1]
    assert trace_id == "abc" + "0" * 29
    assert len(trace_id) == 32
