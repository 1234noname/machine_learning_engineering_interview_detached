# `specs/` — cross-service contracts

Machine-readable contracts that govern cross-service boundaries in AVSA.
Implementations are generated from (or validated against) these files. The
corresponding rule lives in
[`CLAUDE.md`](../CLAUDE.md#spec-first-contracts): *spec-first contracts*.

| Directory | Contents | Consumer |
|---|---|---|
| `specs/api/` | OpenAPI definitions for HTTP endpoints (e.g. `chat.openapi.yaml`). | Frontend TS types codegen (`openapi-typescript` → committed `frontend/packages/shared/src/generated.ts`). |
| `specs/orchestrator/` | `.proto` files for the orchestrator gRPC surface. | Elixir + Python gRPC stub codegen (`buf generate` / `grpc_tools.protoc` → committed stubs consumed at runtime by the orchestrator and API). |
| `specs/mcp/` | External tool manifest in **MCP 2025-03 format**. | `AVSA.MCP.Server` loads this at runtime and serves it via the JSON-RPC `tools/list` method on `:8082`. External agents (Claude Desktop, partner storefronts, the eval harness's MCP client) call `tools/call` against the same server. |
| `specs/db/` | SQL schema definitions. | `\ir`-included by `infra/migrations/00{1,2}*.sql` and applied by `just db-migrate` (part of `just stack-up`). |
| `specs/verifier/` | Verifier input text corpora. `injection_corpus.txt` and `safety_probes.txt` are loaded at orchestrator boot by `AVSA.Verifier` and drive the MCP boundary screen plus the post-generation injection_pattern and safety checks. |

## MCP `annotations` extension

The `annotations` field on each MCP tool entry is not part of the MCP 2025-03
core spec but is explicitly allowed via `additionalProperties` on the tool
object. We use it for:

- `makesSdkCall` (boolean) — `true` for tools that invoke the LLM
  (`extract_attributes`); `false` for tools that do not (`find_similar`, which
  calls pgvector directly). Budget-conscious external callers can read this
  annotation to avoid unexpected LLM spend.
- `title` (string) — human-readable label for UIs.
- `readOnlyHint` (boolean) — all current tools are read-only.

Extensions live under `annotations` rather than at the top level so the
manifest remains compatible with any strict MCP 2025-03 validator.
