"""Unit tests for the  external-agent MCP discovery loop.

``scripts/demo_mcp_session.py`` is a real JSON-RPC 2.0 / Streamable-HTTP client
that drives ``AVSA.MCP.Server`` (ADR 0008). These tests pin the *pure* request
builders, response parsers, image-eliding, secret-handling, and the boundary
turn-classification — everything that does not need a live server. The live
round-trip (real catalog kNN, real LLM, real boundary screening) is the separate
``just mcp-demo`` verification step.

The script is importable as a module (underscore filename), but it imports
``httpx`` at module scope; httpx is present in the repo-root env via
``fastapi[standard]``. We load it directly so the pure helpers are exercised.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "demo_mcp_session.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("demo_mcp_session", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before exec: the @dataclass machinery (with `from __future__
    # import annotations`) resolves cls.__module__ via sys.modules during the
    # decorator run, which fails if the module is not yet registered.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


demo = _load()


# ── request builders ──────────────────────────────────────────────────────────


class TestRequestBuilders:
    def test_tools_list_is_valid_jsonrpc(self) -> None:
        req = demo.build_tools_list(1)
        assert req == {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}

    def test_find_similar_image_modality(self) -> None:
        req = demo.build_find_similar(2, image_b64="QUJD")
        assert req["method"] == "tools/call"
        assert req["params"]["name"] == "find_similar"
        assert req["params"]["arguments"] == {"image_b64": "QUJD"}

    def test_find_similar_text_modality(self) -> None:
        req = demo.build_find_similar(3, text="red dress")
        assert req["params"]["arguments"] == {"text": "red dress"}

    def test_find_similar_attaches_attrs_for_colour_constraint(self) -> None:
        # The "this but green" refinement: attrs.colour flows into kNN.
        req = demo.build_find_similar(4, image_b64="QUJD", attrs={"colour": "green"})
        assert req["params"]["arguments"]["attrs"] == {"colour": "green"}

    def test_find_similar_omits_empty_attrs(self) -> None:
        req = demo.build_find_similar(5, image_b64="QUJD", attrs={})
        assert "attrs" not in req["params"]["arguments"]

    def test_extract_attributes_requires_user_text_key(self) -> None:
        # specs/mcp/tools.json marks user_text required; always emit it.
        req = demo.build_extract_attributes(6, image_b64="QUJD")
        assert req["params"]["name"] == "extract_attributes"
        assert req["params"]["arguments"]["user_text"] == ""
        assert req["params"]["arguments"]["image_b64"] == "QUJD"

    def test_injection_payload_matches_a_corpus_pattern(self) -> None:
        # The recorded boundary turn must actually carry an injection string;
        # this guards against the payload silently becoming benign.
        assert "ignore all previous instructions" in demo.INJECTION_PAYLOAD.lower()


# ── image eliding (artifact must never embed a giant base64 blob) ─────────────


class TestImageEliding:
    def test_request_for_trace_elides_image_bytes(self) -> None:
        req = demo.build_find_similar(7, image_b64="A" * 5000)
        traced = demo.request_for_trace(req)
        elided = traced["params"]["arguments"]["image_b64"]
        assert "elided" in elided
        assert "5000" in elided
        assert "A" * 100 not in elided

    def test_request_for_trace_does_not_mutate_original(self) -> None:
        req = demo.build_find_similar(8, image_b64="A" * 100)
        demo.request_for_trace(req)
        assert req["params"]["arguments"]["image_b64"] == "A" * 100

    def test_request_for_trace_passes_through_non_image_requests(self) -> None:
        req = demo.build_tools_list(9)
        assert demo.request_for_trace(req) == req


# ── response parsing ──────────────────────────────────────────────────────────


def _tool_result(payload: dict[str, Any]) -> dict[str, Any]:
    """Mirror the server envelope: result.content[0].text = JSON.encode(payload)."""
    import json

    return {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"content": [{"type": "text", "text": json.dumps(payload)}]},
    }


class TestResponseParsing:
    def test_extract_products_flattens_cards(self) -> None:
        resp = _tool_result(
            {
                "results": [
                    {
                        "result_id": "id-1",
                        "title": "Green Tee",
                        "score": 0.91,
                        "category": "tops",
                    },
                    {
                        "result_id": "id-2",
                        "title": "Olive Shirt",
                        "score": 0.88,
                        "category": "tops",
                    },
                ]
            }
        )
        products = demo.extract_products(resp)
        assert [p["result_id"] for p in products] == ["id-1", "id-2"]
        assert products[0]["title"] == "Green Tee"
        assert products[0]["score"] == pytest.approx(0.91)

    def test_extract_products_empty_on_error_response(self) -> None:
        resp = {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32602, "message": "rejected"},
        }
        assert demo.extract_products(resp) == []

    def test_extract_products_empty_on_malformed_inner_json(self) -> None:
        resp = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"content": [{"type": "text", "text": "not json {"}]},
        }
        assert demo.extract_products(resp) == []

    def test_extract_attrs_decodes_attribute_map(self) -> None:
        resp = _tool_result(
            {
                "attrs": {
                    "category": "tops",
                    "colour": "blue",
                    "formality": "casual",
                    "occasion": "everyday",
                }
            }
        )
        attrs = demo.extract_attrs(resp)
        assert attrs == {
            "category": "tops",
            "colour": "blue",
            "formality": "casual",
            "occasion": "everyday",
        }

    def test_parse_error_returns_error_object(self) -> None:
        resp = {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32602, "message": "rejected"},
        }
        err = demo.parse_error(resp)
        assert err is not None and err["code"] == demo.JSONRPC_INVALID_PARAMS

    def test_parse_error_none_on_success(self) -> None:
        assert demo.parse_error(_tool_result({"results": []})) is None


# ── headers / secret handling ─────────────────────────────────────────────────


class TestAuthHeaders:
    def test_no_key_yields_no_authorization_header(self) -> None:
        client = demo.MCPClient("http://localhost:8082/", None)
        assert "Authorization" not in client.headers()

    def test_key_yields_bearer_header(self) -> None:
        client = demo.MCPClient("http://localhost:8082/", "sekret")
        assert client.headers()["Authorization"] == "Bearer sekret"

    def test_override_key_used_for_negative_auth_turn(self) -> None:
        client = demo.MCPClient("http://localhost:8082/", "sekret")
        headers = client.headers(override_key="wrong")
        assert headers["Authorization"] == "Bearer wrong"


# ── artifact assembly (secrets must never leak in) ────────────────────────────


class TestArtifact:
    def _records(self) -> list[Any]:
        # tools/list ok, find_similar ok, two boundary turns (one rejected, one not).
        ok = demo.TurnRecord(
            turn=1, label="tools/list", method="tools/list", request={}, response={}
        )
        rejected = demo.TurnRecord(
            turn=2,
            label="BOUNDARY injection",
            method="tools/call",
            request={},
            response={"http_status": 200},
            error={
                "code": demo.JSONRPC_INVALID_PARAMS,
                "message": "rejected by input screening",
            },
            boundary_rejection=True,
        )
        not_rejected = demo.TurnRecord(
            turn=3,
            label="BOUNDARY auth",
            method="tools/list",
            request={},
            response={"http_status": 200},
            error=None,
            boundary_rejection=True,
        )
        return [ok, rejected, not_rejected]

    def test_artifact_never_contains_the_api_key(self) -> None:
        import json

        artifact = demo.build_artifact(
            self._records(),
            mcp_url="http://localhost:8082/",
            keyed=True,
            image_source="img.jpg",
        )
        blob = json.dumps(artifact)
        assert "super-secret-key-value" not in blob
        # Only the boolean presence flag is recorded, not a key.
        assert artifact["auth"]["bearer"] is True

    def test_artifact_summarises_boundary_turns(self) -> None:
        artifact = demo.build_artifact(
            self._records(),
            mcp_url="http://localhost:8082/",
            keyed=False,
            image_source="img.jpg",
        )
        summary = artifact["summary"]
        assert summary["turns"] == 3
        assert summary["boundary_rejection_turns"] == [2, 3]
        # Only turn 2 carries an actual JSON-RPC error.
        assert summary["boundary_turns_actually_rejected"] == [2]

    def test_artifact_has_schema_and_endpoint(self) -> None:
        artifact = demo.build_artifact(
            self._records(),
            mcp_url="http://example.test:8082/",
            keyed=False,
            image_source="img.jpg",
        )
        assert artifact["schema"] == "avsa.mcp.discovery-session/v1"
        assert artifact["endpoint"] == "http://example.test:8082/"


# ── image loading ─────────────────────────────────────────────────────────────


class TestImageLoading:
    def test_placeholder_used_without_image_path(self) -> None:
        b64, source = demo.load_image_b64(None)
        assert "placeholder" in source.lower()
        assert isinstance(b64, str) and len(b64) > 0

    def test_real_file_is_base64_encoded(self, tmp_path: Path) -> None:
        import base64

        raw = b"\x89PNG fake bytes"
        path = tmp_path / "p.png"
        path.write_bytes(raw)
        b64, source = demo.load_image_b64(str(path))
        assert base64.b64decode(b64) == raw
        assert source == str(path)


# ── env-only secret in main() ─────────────────────────────────────────────────


class TestMainSecretSource:
    def test_no_api_key_cli_flag_exists(self) -> None:
        # Regression guard: the key must come from env only, never a flag (which
        # would risk it landing in shell history / process args / the artifact).
        ns = demo.parse_args([])
        assert not hasattr(ns, "api_key")
