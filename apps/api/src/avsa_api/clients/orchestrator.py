"""Orchestrator client - the API's gRPC channel to the Elixir orchestrator.

OrchestratorClient.stream_chat opens a real gRPC stream
(Conversation.StreamConversationEvents) and maps each ProductResultEvent to a
product_card SSE event. When AVSA_ORCHESTRATOR_STUB=1 it short-circuits to a
single canned card instead - used by the unit/route test suite and by local dev
without a running orchestrator.
"""

import json
import os
import time
from collections.abc import AsyncGenerator
from typing import Any

from fastapi import HTTPException, Request
from prometheus_client import Histogram

_ORCHESTRATOR_DURATION = Histogram(
    "avsa_api_orchestrator_call_duration_seconds",
    "End-to-end duration of an orchestrator stream_chat call",
    buckets=[0.1, 0.25, 0.5, 1.0, 1.5, 2.0, 3.0, 5.0, 10.0],
)

_STUB_CARD: dict[str, object] = {
    "type": "product_card",
    "card": {
        "id": "stub-001",
        "title": "Stub Product",
        "price": 0.0,
        "currency": "ZAR",
        "image_url": "http://example.com/stub.jpg",
        "category": "stub",
        "score": 1.0,
    },
}


def _product_result_to_card(pr: Any) -> dict[str, object]:
    """Map a ProductResultEvent proto to a product_card SSE event.

    metadata_json is opaque JSON the orchestrator encodes; price_cents is
    converted to ZAR, and malformed JSON degrades to empty metadata
    rather than failing the whole turn. id and score come from
    the proto fields directly.
    """
    try:
        meta = json.loads(pr.metadata_json) if pr.metadata_json else {}
    except json.JSONDecodeError:
        meta = {}
    price_cents = meta.get("price_cents", 0)
    price = price_cents / 100.0 if isinstance(price_cents, int | float) else 0.0
    return {
        "type": "product_card",
        "card": {
            "id": pr.product_id,
            "title": meta.get("title", ""),
            "price": price,
            "currency": "ZAR",
            "image_url": meta.get("image_url", ""),
            "category": meta.get("category", ""),
            "score": pr.score,
        },
    }


class OrchestratorClient:
    """Client that streams a chat turn from the orchestrator (or a stub in tests)."""

    def __init__(self) -> None:
        self._addr: str = os.environ.get("AVSA_ORCHESTRATOR_ADDR", "localhost:50051")

    async def stream_chat(
        self,
        image_bytes: list[bytes],
        text: str,
        conversation_id: str = "",
    ) -> AsyncGenerator[dict[str, object], None]:
        """Yield product_card events for a chat turn.

        image_bytes is a list of raw image payloads (zero for a text-only
        turn, one or more when the shopper uploads images). The orchestrator
        combines multiple images into one query (mean-pooled embedding).

        When AVSA_ORCHESTRATOR_STUB=1, yields a single stub card. Otherwise opens
        a real gRPC channel and streams ConversationEvent messages, mapping each
        product_result to a product_card via :func:_product_result_to_card.
        """
        if os.environ.get("AVSA_ORCHESTRATOR_STUB") == "1":
            yield _STUB_CARD
            return

        t0 = time.perf_counter()
        try:
            async for event in self._stream_grpc(image_bytes, text, conversation_id):
                yield event
        finally:
            _ORCHESTRATOR_DURATION.observe(time.perf_counter() - t0)

    async def _stream_grpc(
        self,
        image_bytes: list[bytes],
        text: str,
        conversation_id: str = "",
    ) -> AsyncGenerator[dict[str, object], None]:
        """Open a real gRPC stream and yield product_card events."""
        import grpc

        from avsa_api.proto import avsa_pb2, avsa_pb2_grpc

        async with grpc.aio.insecure_channel(self._addr) as channel:
            stub = avsa_pb2_grpc.ConversationStub(channel)  # type: ignore[no-untyped-call]
            request = avsa_pb2.StartConversationRequest(  # type: ignore[attr-defined]
                conversation_id=conversation_id,
                image_bytes=image_bytes,
                user_text=text,
            )
            metadata = self._build_grpc_metadata(conversation_id)
            async for event in stub.StreamConversationEvents(request, metadata=metadata):
                if event.HasField("product_result"):
                    yield _product_result_to_card(event.product_result)

    def _build_grpc_metadata(self, conversation_id: str) -> list[tuple[str, str]]:
        """Build W3C baggage + simplified traceparent for gRPC metadata.

        The trace_id is derived from conversation_id (hyphens stripped, zero-padded
        to 32 hex chars). Full OTel SDK propagation is deferred to a later phase.
        """
        trace_id = conversation_id.replace("-", "").ljust(32, "0")[:32]
        return [
            ("baggage", f"conversation_id={conversation_id}"),
            ("traceparent", f"00-{trace_id}-{'0' * 16}-01"),
        ]


def get_orchestrator(request: Request) -> OrchestratorClient:
    """FastAPI dependency - returns the app-scoped client stored in app.state."""
    try:
        return request.app.state.orchestrator  # type: ignore[no-any-return]
    except AttributeError as exc:
        raise HTTPException(status_code=503, detail="Service not ready") from exc
