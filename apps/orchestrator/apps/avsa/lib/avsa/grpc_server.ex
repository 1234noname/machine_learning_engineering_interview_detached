defmodule AVSA.GrpcServer do
  @moduledoc """
  gRPC server handler for the avsa.orchestrator.v1.Conversation service.

  Implements StreamConversationEvents and StartConversation RPCs. Each call
  (MCP-native flow):
    1. Builds the uploaded image into a tool ARGUMENT (`%{"image_b64" => ...}`),
       or routes text-only turns by `user_text`. There is NO pre-embed step here
       anymore — the embed happens INSIDE the image-native MCP tools, once per
       turn, through `AVSA.EmbedCache`.
    2. Starts/retrieves a Conversation GenServer for the given conversation_id.
    3. Runs the conversation turn, which drives the image-native MCP tools
       (`AVSA.MCP.Tools.extract_attributes` → `find_similar_results`) — plan →
       attribute extraction → kNN retrieval → verify.
    4. Purges the per-turn embed cache, then streams ProductResultEvents back.
    5. Emits telemetry for latency and outcome.

  The embed step / text tool / retrieval tool modules are resolved (inside the
  MCP tools) via `Application.get_env(:avsa, <key>, <default>)` at call time,
  allowing test overrides without restarting the app.
  """

  use GRPC.Server, service: Avsa.Orchestrator.V1.Conversation.Service

  require Logger

  alias Avsa.Orchestrator.V1.ConversationEvent

  @turn_timeout_ms 120_000

  @spec stream_conversation_events(
          Avsa.Orchestrator.V1.StartConversationRequest.t(),
          GRPC.Server.Stream.t()
        ) :: any()
  def stream_conversation_events(request, stream) do
    events = events_for_turn(request)

    for event <- events do
      GRPC.Server.send_reply(stream, event)
    end

    stream
  rescue
    e in GRPC.RPCError ->
      raise e

    e ->
      Logger.error("GrpcServer stream_conversation_events unexpected error: #{inspect(e)}")
      raise GRPC.RPCError, status: :internal, message: "internal error"
  end

  @spec start_conversation(
          Avsa.Orchestrator.V1.StartConversationRequest.t(),
          GRPC.Server.Stream.t()
        ) :: Avsa.Orchestrator.V1.ConversationEvent.t()
  def start_conversation(request, _stream) do
    {result, _elapsed} = run_conversation_turn(request)

    case result do
      {:ok, response} ->
        case Map.get(response, :results, []) do
          [first | _] -> build_product_event(first)
          [] -> %ConversationEvent{payload: nil}
        end

      {:error, reason} ->
        Logger.error("GrpcServer start_conversation error: #{inspect(reason)}")
        raise GRPC.RPCError, status: :internal, message: "internal error"
    end
  end

  @doc """
  Produces the ordered list of `ConversationEvent`s for a conversation turn.

  Delegates to `run_conversation_turn/1` — the single turn driver that routes
  through the `AVSA.Conversation` GenServer: plan → extract_attributes →
  find_similar → **verify/re-plan** → persist turn history, carrying
  `prior_result_ids` across turns of the same conversation_id so a follow-up turn
  excludes the previously-shown results. The GenServer's result list is mapped to
  `product_result` events — the single event type that flows end-to-end to the
  shopper.

  Telemetry and the per-turn embed-cache purge are owned by `run_conversation_turn/1`.

  Exposed as a public function so tests can call it without a live gRPC stream.
  """
  @spec events_for_turn(Avsa.Orchestrator.V1.StartConversationRequest.t()) ::
          [ConversationEvent.t()]
  def events_for_turn(request) do
    {result, _elapsed} = run_conversation_turn(request)

    case result do
      {:ok, response} ->
        response
        |> Map.get(:results, [])
        |> Enum.map(&build_product_event/1)

      {:error, :no_tool_use} ->
        Logger.warning("GrpcServer events_for_turn: LLM returned no tool_use (no results emitted)")
        []

      {:error, :safety, reason} ->
        Logger.warning(
          "GrpcServer events_for_turn: turn refused by safety check: #{inspect(reason)}"
        )

        []

      {:error, :verification_failed, check_name, reason} ->
        Logger.warning(
          "GrpcServer events_for_turn: turn failed verification " <>
            "(#{inspect(check_name)}): #{inspect(reason)}"
        )

        []

      {:error, reason} ->
        Logger.error("GrpcServer events_for_turn error: #{inspect(reason)}")
        raise GRPC.RPCError, status: :internal, message: "internal error"
    end
  end

  @doc """
  Runs the full conversation turn: embed → conversation start → tool dispatch → verify.

  Returns `{result, elapsed_ms}` where result is `{:ok, response}` or `{:error, reason}`.
  Also emits the `[:avsa, :conversation, :complete]` telemetry event with outcome and modality tags.

  Exposed as a public function so it can be exercised directly in unit tests without
  needing a live gRPC stream.
  """
  @spec run_conversation_turn(Avsa.Orchestrator.V1.StartConversationRequest.t()) ::
          {{:ok, map()} | {:error, term()}, non_neg_integer()}
  def run_conversation_turn(request) do
    conversation_id = resolve_conversation_id(request.conversation_id)
    start = :erlang.monotonic_time(:millisecond)
    modality = detect_modality(request.image_bytes)
    request_id = "turn-" <> Ecto.UUID.generate()

    result =
      try do
        case modality do
          :text ->
            with {:ok, pid} <- AVSA.ConversationSupervisor.start_conversation(conversation_id),
                 {:ok, response} <-
                   GenServer.call(
                     pid,
                     {:start_text, request.user_text, request_id},
                     @turn_timeout_ms
                   ) do
              {:ok, response}
            end

          :image ->
            image_arg = image_args(request.image_bytes)

            with {:ok, pid} <- AVSA.ConversationSupervisor.start_conversation(conversation_id),
                 {:ok, response} <-
                   GenServer.call(
                     pid,
                     {:start_image, image_arg, request.user_text, request_id},
                     @turn_timeout_ms
                   ) do
              {:ok, response}
            end
        end
      after
        AVSA.EmbedCache.purge_request(request_id)
      end

    elapsed = :erlang.monotonic_time(:millisecond) - start
    outcome = if match?({:ok, _}, result), do: "success", else: "error"
    modality_tag = if modality == :text, do: "text", else: "image"

    :telemetry.execute(
      [:avsa, :conversation, :complete],
      %{latency_ms: elapsed},
      %{outcome: outcome, modality: modality_tag}
    )

    {result, elapsed}
  end

  # ---------------------------------------------------------------------------
  # Private helpers
  # ---------------------------------------------------------------------------

  @spec resolve_conversation_id(String.t()) :: String.t()
  defp resolve_conversation_id(""), do: Ecto.UUID.generate()
  defp resolve_conversation_id(id), do: id

  @spec detect_modality([binary()] | binary()) :: :image | :text
  defp detect_modality([]), do: :text
  defp detect_modality(list) when is_list(list), do: :image
  defp detect_modality(image_bytes) when image_bytes == "" or image_bytes == <<>>, do: :text
  defp detect_modality(_image_bytes), do: :image

  @spec image_args([binary()] | binary()) :: %{String.t() => term()}
  defp image_args(images) when is_list(images) do
    encoded = Enum.map(images, &Base.encode64/1)
    %{"image_b64" => List.first(encoded) || "", "image_b64_list" => encoded}
  end

  defp image_args(image_bytes) when is_binary(image_bytes) do
    image_args([image_bytes])
  end

  @spec build_product_event(AVSA.ProductResult.t()) :: ConversationEvent.t()
  defp build_product_event(product_result) do
    %ConversationEvent{
      payload:
        {:product_result,
         %Avsa.Orchestrator.V1.ProductResultEvent{
           product_id: Ecto.UUID.cast!(product_result.id),
           score: product_result.score || 0.0,
           metadata_json:
             Jason.encode!(%{
               title: product_result.title,
               category: product_result.category,
               price_cents: product_result.price_cents,
               image_url: product_result.image_url
             })
         }}
    }
  end
end

defmodule AVSA.GrpcEndpoint do
  @moduledoc """
  gRPC endpoint that wires AVSA.GrpcServer (Conversation service) into the
  cowboy adapter.

  External MCP callers do NOT use this endpoint: they reach AVSA's tools through
  the conformant JSON-RPC MCP server (AVSA.MCP.Server), which invokes
  AVSA.MCP.Tools in-process. This endpoint serves only the Conversation RPCs the
  API gateway proxies for `POST /chat`.

  Used by GRPC.Server.Supervisor in AVSA.Application.
  """

  use GRPC.Endpoint

  run(AVSA.GrpcServer)
end
