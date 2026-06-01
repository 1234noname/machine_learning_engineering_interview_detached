defmodule AVSA.MCP.Server do
  @moduledoc """
  Conformant MCP server: Streamable HTTP transport + JSON-RPC 2.0.

  This is AVSA's **external tool surface**. It is a thin in-house Elixir Plug
  (ADR 0008 — thin protocol implementations over heavy frameworks) exposing the
  three MCP methods an external client (the MCP Inspector, Claude Desktop, a
  future Solenya client) relies on:

    * `initialize` — returns `protocolVersion`, `capabilities` (tools) and
      `serverInfo`.
    * `tools/list` — returns the tool descriptors from `specs/mcp/tools.json`.
    * `tools/call` — dispatches to the **image-native** `AVSA.MCP.Tools`
      (`find_similar` / `extract_attributes`), which embed internally through
      L1. The acceptance bar is: Inspector calls `find_similar` WITH AN IMAGE →
      grounded catalog results.

  The internal orchestrator reaches the SAME `AVSA.MCP.Tools` in-process
  (loopback, no socket) for latency; this HTTP server is only the external
  surface. One tool layer, two callers.

  ## Trust boundary (ADR 0008)

  There are two callers of the one `AVSA.MCP.Tools` layer, with different trust:

    * **Internal loopback** — the conversation flow (`AVSA.Conversation`) calls
      `AVSA.MCP.Tools` in-process. That text is already screened by the
      conversation flow's `AVSA.Verifier.check/2` on the proposed response
      (injection + safety, among the 6 checks). This server is NOT on that path,
      so the loopback adds no latency and is not double-screened.
    * **External HTTP** — anything arriving over this Plug is **untrusted**. It
      is screened AT THIS BOUNDARY, before any tool runs:
        1. **Auth** (below) — bearer key when configured.
        2. **Verifier input pre-check** — every inbound text arg (`text`,
           `user_text`, `image_description`) is run through
           `AVSA.Verifier.screen_input/1` (injection_pattern + safety) in
           `tools/call` BEFORE dispatch, so an external caller cannot reach the
           LLM (`extract_attributes`) or the catalog (`find_similar`) with an
           injection/unsafe payload. A failed pre-check is a JSON-RPC
           `invalid_params` (-32602) error, not a tool result.

  ## Auth

  Bearer / API-key, config-driven via `:avsa, :mcp_api_key` (read at request
  time). When unset (local-only default), the server is open — convenient for a
  local Inspector demo. When set, every request must carry
  `Authorization: Bearer <key>`; a missing/wrong token is a JSON-RPC error with
  HTTP 401. The key is compared in constant time. The boot-time mount
  (`AVSA.Application.mcp_children/0`) refuses to start an **exposed** server
  (`:prod`, or `AVSA_MCP_EXPOSED=1`) with no key set, so an accidentally-unauthed
  public tool endpoint cannot come up. Production exposure MUST set
  `:mcp_api_key` (and front it with TLS / a gateway).

  ## Request-body size cap

  `call/2` reads the body with a hard `:length` cap (`:mcp_max_body_bytes`,
  default 16 MiB — enough for a ~12 MB source image as base64 plus the JSON-RPC
  envelope). A body over the cap makes `read_body` return `{:more, _, conn}`,
  which we reject as HTTP 413 + a JSON-RPC `@payload_too_large` error rather than
  buffering an unbounded payload. See ADR 0008.
  """

  @behaviour Plug

  import Plug.Conn

  require Logger

  # JSON-RPC 2.0 standard error codes + one AVSA extension for auth.
  @parse_error -32_700
  @invalid_request -32_600
  @method_not_found -32_601
  @invalid_params -32_602
  @internal_error -32_603
  @unauthorized -32_001
  @payload_too_large -32_010

  @default_max_body_bytes 16 * 1024 * 1024

  @protocol_version "2025-03"
  @server_name "avsa-mcp"
  @server_version "1.0.0"

  @impl Plug
  def init(opts), do: opts

  @impl Plug
  def call(conn, _opts) do
    case read_body(conn, length: max_body_bytes()) do
      {:ok, raw, conn} ->
        case authorize(conn) do
          :ok ->
            handle_body(conn, raw)

          {:error, message} ->
            send_json(conn, 401, error_response(nil, @unauthorized, message))
        end

      {:more, _partial, conn} ->
        send_json(
          conn,
          413,
          error_response(nil, @payload_too_large, "request body exceeds #{max_body_bytes()} bytes")
        )

      {:error, _reason} ->
        send_json(conn, 400, error_response(nil, @parse_error, "could not read request body"))
    end
  end

  @spec max_body_bytes() :: pos_integer()
  defp max_body_bytes, do: Application.get_env(:avsa, :mcp_max_body_bytes, @default_max_body_bytes)

  # ── request handling ────────────────────────────────────────────────────────

  defp handle_body(conn, raw) do
    case Jason.decode(raw) do
      {:ok, %{"jsonrpc" => "2.0", "method" => method} = req} ->
        id = Map.get(req, "id")
        params = Map.get(req, "params", %{})
        {status, body} = dispatch(method, params, id)
        send_json(conn, status, body)

      {:ok, _other} ->
        send_json(conn, 200, error_response(nil, @invalid_request, "invalid JSON-RPC 2.0 request"))

      {:error, _} ->
        send_json(conn, 200, error_response(nil, @parse_error, "parse error: invalid JSON"))
    end
  end

  @spec dispatch(String.t(), map(), term()) :: {non_neg_integer(), map()}
  defp dispatch("initialize", _params, id) do
    {200,
     result_response(id, %{
       "protocolVersion" => @protocol_version,
       "capabilities" => %{"tools" => %{}},
       "serverInfo" => %{"name" => @server_name, "version" => @server_version}
     })}
  end

  defp dispatch("tools/list", _params, id) do
    case load_manifest_tools() do
      {:ok, tools} -> {200, result_response(id, %{"tools" => tools})}
      {:error, reason} -> {200, error_response(id, @internal_error, "manifest error: #{inspect(reason)}")}
    end
  end

  defp dispatch("tools/call", params, id) do
    name = Map.get(params, "name")
    arguments = Map.get(params, "arguments", %{})

    case screen_arguments(arguments) do
      :ok ->
        dispatch_tool(name, arguments, id)

      {:error, check, reason} ->
        Logger.warning("MCP.Server tools/call #{inspect(name)} rejected at boundary: #{check}")
        {200, error_response(id, @invalid_params, "rejected by input screening (#{check}): #{reason}")}
    end
  end

  defp dispatch(method, _params, id) do
    {200, error_response(id, @method_not_found, "method not found: #{method}")}
  end

  @spec dispatch_tool(String.t() | nil, map(), term()) :: {non_neg_integer(), map()}
  defp dispatch_tool(name, arguments, id) do
    case call_tool(name, arguments) do
      {:ok, payload} ->
        {200, result_response(id, %{"content" => [%{"type" => "text", "text" => Jason.encode!(payload)}]})}

      {:error, {:invalid_argument, message}} ->
        {200, error_response(id, @invalid_params, message)}

      {:error, :unknown_tool} ->
        {200, error_response(id, @method_not_found, "unknown tool: #{inspect(name)}")}

      {:error, reason} ->
        Logger.error("MCP.Server tools/call #{inspect(name)} error: #{inspect(reason)}")
        {200, error_response(id, @internal_error, "tool error")}
    end
  end

  # ── external boundary input screening ────────────────────────────────────────

  @text_arg_keys ["text", "user_text", "image_description"]

  @spec screen_arguments(map()) :: :ok | {:error, atom(), String.t()}
  defp screen_arguments(arguments) when is_map(arguments) do
    Enum.reduce_while(@text_arg_keys, :ok, fn key, _acc ->
      value = Map.get(arguments, key)

      case verifier_module().screen_input(value) do
        :ok -> {:cont, :ok}
        {:error, check, reason} -> {:halt, {:error, check, reason}}
      end
    end)
  end

  defp screen_arguments(_), do: :ok

  @spec verifier_module() :: module()
  defp verifier_module, do: Application.get_env(:avsa, :verifier_module, AVSA.Verifier)

  # Each external tool call is its own turn → a fresh request_id scopes the embed
  # cache. We purge it after the call so the cache stays bounded.
  @spec call_tool(String.t() | nil, map()) :: {:ok, map()} | {:error, term()}
  defp call_tool("find_similar", arguments) do
    request_id = "mcp-" <> Ecto.UUID.generate()

    try do
      AVSA.MCP.Tools.find_similar(arguments, request_id: request_id)
    after
      AVSA.EmbedCache.purge_request(request_id)
    end
  end

  defp call_tool("extract_attributes", arguments) do
    request_id = "mcp-" <> Ecto.UUID.generate()

    try do
      AVSA.MCP.Tools.extract_attributes(arguments, request_id: request_id)
    after
      AVSA.EmbedCache.purge_request(request_id)
    end
  end

  defp call_tool(_name, _arguments), do: {:error, :unknown_tool}

  # ── auth ──────────────────────────────────────────────────────────────────────

  @spec authorize(Plug.Conn.t()) :: :ok | {:error, String.t()}
  defp authorize(conn) do
    case Application.get_env(:avsa, :mcp_api_key) do
      nil ->
        :ok

      "" ->
        :ok

      expected when is_binary(expected) ->
        case bearer_token(conn) do
          token when is_binary(token) ->
            if secure_compare(token, expected),
              do: :ok,
              else: {:error, "unauthorized"}

          nil ->
            {:error, "unauthorized: missing bearer token"}
        end
    end
  end

  @spec bearer_token(Plug.Conn.t()) :: String.t() | nil
  defp bearer_token(conn) do
    case get_req_header(conn, "authorization") do
      ["Bearer " <> token | _] -> token
      ["bearer " <> token | _] -> token
      _ -> nil
    end
  end

  # Constant-time comparison to avoid leaking the key via timing.
  @spec secure_compare(binary(), binary()) :: boolean()
  defp secure_compare(a, b), do: Plug.Crypto.secure_compare(a, b)

  # ── manifest ────────────────────────────────────────────────────────────────

  @spec load_manifest_tools() :: {:ok, [map()]} | {:error, term()}
  defp load_manifest_tools do
    path = Path.join([specs_root(), "mcp", "tools.json"])

    with {:ok, raw} <- File.read(path),
         {:ok, %{"tools" => tools}} <- Jason.decode(raw) do
      {:ok, tools}
    else
      {:ok, _decoded} -> {:error, :no_tools_key}
      {:error, reason} -> {:error, reason}
    end
  end

  @spec specs_root() :: String.t()
  defp specs_root do
    Application.get_env(:avsa, :specs_root, Path.expand("../../../../../specs", __DIR__))
  end

  # ── JSON-RPC envelopes ────────────────────────────────────────────────────────

  defp result_response(id, result), do: %{"jsonrpc" => "2.0", "id" => id, "result" => result}

  defp error_response(id, code, message) do
    %{"jsonrpc" => "2.0", "id" => id, "error" => %{"code" => code, "message" => message}}
  end

  defp send_json(conn, status, body) do
    conn
    |> put_resp_content_type("application/json")
    |> send_resp(status, Jason.encode!(body))
  end
end
