"""Tests for the  spec sprint: `specs/api/chat.openapi.yaml`
(HTTP API contract) and `specs/orchestrator/avsa.proto` (gRPC orchestrator
contract).

Written before implementation (test-first). The specs are the
single source of truth that the API gateway and orchestrator scaffold
 must implement against — these tests pin the contract surface.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
OPENAPI_SPEC = REPO_ROOT / "specs" / "api" / "chat.openapi.yaml"
PROTO_SPEC = REPO_ROOT / "specs" / "orchestrator" / "avsa.proto"
BUF_CONFIG = REPO_ROOT / "specs" / "orchestrator" / "buf.yaml"

SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?(?:\+[A-Za-z0-9.-]+)?$")


# ----- OpenAPI structural assertions ---------------------------------------


class TestOpenAPISpec:
    """The OpenAPI spec is the contract for the API gateway. Pins the
    endpoints (POST /chat, GET /products/{id}) and the discriminated-union
    ChatEvent schema. The external MCP tool surface is the JSON-RPC server
    (AVSA.MCP.Server), not this spec."""

    def test_openapi_spec_file_exists(self) -> None:
        assert OPENAPI_SPEC.is_file(), (
            f"expected OpenAPI spec at {OPENAPI_SPEC.relative_to(REPO_ROOT)}; "
            "implementation hasn't landed yet"
        )

    def test_openapi_is_3_1(self) -> None:
        doc = yaml.safe_load(OPENAPI_SPEC.read_text())
        version = doc.get("openapi", "")
        assert version.startswith("3.1"), (
            f"spec must declare OpenAPI 3.1.x; got {version!r}.  "
            "pins 3.1 because the JSON Schema draft 2020-12 contract is "
            "load-bearing for the discriminated union (ChatEvent)."
        )

    def test_chat_endpoint_defined(self) -> None:
        doc = yaml.safe_load(OPENAPI_SPEC.read_text())
        chat = doc.get("paths", {}).get("/chat", {})
        post = chat.get("post")
        assert post, "POST /chat must be defined in paths"
        # Multipart request body.
        content = post.get("requestBody", {}).get("content", {})
        assert "multipart/form-data" in content, (
            "POST /chat must accept multipart/form-data (image + text fields)"
        )
        # SSE response.
        ok = post.get("responses", {}).get("200", {}).get("content", {})
        assert "text/event-stream" in ok, (
            "POST /chat must return text/event-stream (SSE); a typed event "
            "stream is the orchestrator-to-client contract"
        )

    def test_chat_endpoint_documents_rate_limit_and_size(self) -> None:
        doc = yaml.safe_load(OPENAPI_SPEC.read_text())
        post = doc["paths"]["/chat"]["post"]
        assert "x-rate-limit" in post, (
            "POST /chat must declare x-rate-limit extension; load-bearing "
            "for the API gateway's rate-limiter config"
        )
        assert "x-max-request-size" in post, (
            "POST /chat must declare x-max-request-size extension; "
            "load-bearing for the gateway's 10 MB request guard"
        )

    def test_products_endpoint_defined(self) -> None:
        doc = yaml.safe_load(OPENAPI_SPEC.read_text())
        product = doc.get("paths", {}).get("/products/{id}", {})
        assert product.get("get"), "GET /products/{id} must be defined"

    def test_required_schemas_defined(self) -> None:
        doc = yaml.safe_load(OPENAPI_SPEC.read_text())
        schemas = doc.get("components", {}).get("schemas", {})
        required = {
            "ChatRequest",
            "ChatEvent",
            "ProductCard",
        }
        missing = required - schemas.keys()
        assert not missing, (
            f"components/schemas must define {sorted(required)}; missing "
            f"{sorted(missing)}"
        )

    def test_chat_event_is_discriminated_union(self) -> None:
        doc = yaml.safe_load(OPENAPI_SPEC.read_text())
        chat_event = doc["components"]["schemas"]["ChatEvent"]
        # Discriminator on `type` per the issue's reviewer context.
        discriminator = chat_event.get("discriminator")
        assert discriminator and discriminator.get("propertyName") == "type", (
            "ChatEvent must be a discriminated union on `type`; product_card is "
            "the only variant after  ( cut)"
        )
        # product_card is the only event type that flows end-to-end.
        mapping = discriminator.get("mapping", {})
        assert "product_card" in mapping, (
            "ChatEvent discriminator.mapping must include 'product_card'"
        )


# ----- proto structural assertions -----------------------------------------


class TestProtoSpec:
    """The proto is the contract for the orchestrator scaffold. Pins
    the Conversation service, both RPCs, and the ConversationEvent oneof."""

    def test_proto_spec_file_exists(self) -> None:
        assert PROTO_SPEC.is_file(), (
            f"expected proto spec at {PROTO_SPEC.relative_to(REPO_ROOT)}; "
            "implementation hasn't landed yet"
        )

    def test_proto_syntax_is_proto3(self) -> None:
        body = PROTO_SPEC.read_text()
        assert re.search(r'^syntax\s*=\s*"proto3"\s*;', body, re.MULTILINE), (
            'proto must declare `syntax = "proto3"` on its own line'
        )

    def test_proto_package_is_avsa_orchestrator_v1(self) -> None:
        body = PROTO_SPEC.read_text()
        assert re.search(
            r"^package\s+avsa\.orchestrator\.v1\s*;", body, re.MULTILINE
        ), (
            "proto package must be `avsa.orchestrator.v1`; the v1 suffix "
            "lets us evolve the contract without re-importing every caller"
        )

    def test_proto_declares_go_or_java_package(self) -> None:
        body = PROTO_SPEC.read_text()
        has_go = re.search(r'option\s+go_package\s*=\s*"[^"]+"\s*;', body)
        has_java = re.search(r'option\s+java_package\s*=\s*"[^"]+"\s*;', body)
        assert has_go or has_java, (
            "proto must declare an `option go_package` or `option "
            "java_package` for future codegen targets"
        )

    def test_proto_defines_conversation_service_with_both_rpcs(self) -> None:
        body = PROTO_SPEC.read_text()
        assert re.search(r"service\s+Conversation\s*\{", body), (
            "proto must define `service Conversation`"
        )
        # StartConversation: unary
        assert re.search(
            r"rpc\s+StartConversation\s*\(\s*StartConversationRequest\s*\)"
            r"\s+returns\s*\(\s*ConversationEvent\s*\)\s*;",
            body,
        ), (
            "service Conversation must declare `rpc StartConversation"
            "(StartConversationRequest) returns (ConversationEvent);`"
        )
        # StreamConversationEvents: server-streaming
        assert re.search(
            r"rpc\s+StreamConversationEvents\s*\(\s*StartConversationRequest"
            r"\s*\)\s+returns\s*\(\s*stream\s+ConversationEvent\s*\)\s*;",
            body,
        ), (
            "service Conversation must declare `rpc "
            "StreamConversationEvents(StartConversationRequest) returns "
            "(stream ConversationEvent);`"
        )

    def test_proto_defines_required_messages(self) -> None:
        body = PROTO_SPEC.read_text()
        for msg in (
            "StartConversationRequest",
            "ConversationEvent",
            "ProductResultEvent",
        ):
            assert re.search(rf"message\s+{msg}\s*\{{", body), (
                f"proto must define `message {msg}`"
            )

    def test_conversation_event_has_oneof_payload(self) -> None:
        body = PROTO_SPEC.read_text()
        # Locate the ConversationEvent message body.
        match = re.search(
            r"message\s+ConversationEvent\s*\{([^}]*(?:\{[^}]*\}[^}]*)*)\}",
            body,
            re.DOTALL,
        )
        assert match, "ConversationEvent message body not found"
        msg_body = match.group(1)
        assert re.search(r"oneof\s+\w+\s*\{", msg_body), (
            "ConversationEvent must declare a `oneof` payload carrying "
            "ProductResultEvent"
        )
        assert "ProductResultEvent" in msg_body, (
            "ConversationEvent.oneof must include ProductResultEvent"
        )
        # : the dead tool_call / AG-UI variants were removed; their
        # field numbers and names must stay reserved (wire-safety invariant).
        assert "reserved" in msg_body, (
            "ConversationEvent must reserve the removed field numbers/names"
        )


# ----- semver version headers ----------------------------------------------


class TestSpecVersions:
    """Both specs must carry an explicit semver version. Drift between
    them and the implementations is the most common Phase 1 failure mode;
    the version field is the human-visible signal that the contract moved."""

    def test_openapi_info_version_is_semver(self) -> None:
        doc = yaml.safe_load(OPENAPI_SPEC.read_text())
        version = doc.get("info", {}).get("version", "")
        assert SEMVER_RE.match(version), (
            f"info.version must be semver (e.g. 0.1.0); got {version!r}"
        )

    def test_proto_header_carries_semver_comment(self) -> None:
        body = PROTO_SPEC.read_text()
        # Per issue: `// proto-version: 0.1.0` style header comment.
        match = re.search(r"^//\s*proto-version:\s*(\S+)\s*$", body, re.MULTILINE)
        assert match, (
            "proto file must include a `// proto-version: <semver>` header "
            "comment; this is the human-visible version signal because "
            "proto3 has no native version field"
        )
        assert SEMVER_RE.match(match.group(1)), (
            f"proto-version must be semver; got {match.group(1)!r}"
        )


# ----- external-validator round-trips (skip when tooling not present) -------


class TestExternalValidators:
    """Validates the spec files using the same tools as CI.
    openapi-spec-validator is always available (it is a project dev dep);
    buf may or may not be installed locally so its test skips gracefully."""

    def test_openapi_spec_is_valid_openapi_31(self) -> None:
        result = subprocess.run(
            ["openapi-spec-validator", str(OPENAPI_SPEC)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, (
            f"openapi-spec-validator rejected the spec (exit {result.returncode}):\n"
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )

    def test_proto_lints_cleanly_with_buf(self) -> None:
        if shutil.which("buf") is None:
            pytest.skip("buf not on PATH; CI runs it directly")
        assert BUF_CONFIG.is_file(), (
            f"buf.yaml expected at {BUF_CONFIG.relative_to(REPO_ROOT)}; "
            "buf lint refuses to run without a config file"
        )
        result = subprocess.run(
            ["buf", "lint"],
            cwd=BUF_CONFIG.parent,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, (
            f"buf lint failed (exit {result.returncode}):\n"
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )
