defmodule AVSA.Conversation do
  @moduledoc """
  GenServer representing a single user conversation session.

  Holds state including the conversation_id, turn history,
  the LLM module to use, and the current plan.

  ## MCP-native turn flow

  A turn no longer receives a pre-computed embedding. The image (or text query)
  is threaded in as a tool ARGUMENT and the turn drives the **image-native MCP
  tools** (`AVSA.MCP.Tools`) — the same L2 layer external MCP clients call:

    1. The LLM selects the tool (plan) — unchanged.
    2. `extract_attributes/2` extracts the attribute map. For an image turn it
       embeds the image internally (through the per-turn `AVSA.EmbedCache`) so
       the ViT attribute head (category/colour) is sourced from L1 and the LLM is
       asked only for the text-derived attrs. For a text turn it falls back to
       the LLM for all four attrs.
    3. The merged attrs (with `prior_result_ids` for turn continuity, and the
       explicit `colour` constraint) flow into…
    4. `find_similar_results/2`, which embeds the SAME image through the SAME
       per-turn cache (so the ViT forward runs ONCE per turn) and runs kNN.
    5. `AVSA.Verifier.check/2` — unchanged.

  `find_similar_results/2` is the in-process variant that returns
  `%AVSA.ProductResult{}` structs (not display cards) so the Verifier and gRPC
  layer get an un-lossy result. The embed cache is purged by the caller
  (`AVSA.GrpcServer`) at turn teardown.
  """

  use GenServer

  defstruct [:conversation_id, :llm_module, :max_context_turns, turns: [], plan: nil, results: []]

  # Client API

  def start_link(opts) do
    conversation_id = Keyword.fetch!(opts, :conversation_id)
    llm_module = Keyword.get(opts, :llm_module, AVSA.LLM.Mock)

    gen_server_opts =
      case Keyword.get(opts, :name) do
        nil -> []
        name -> [name: name]
      end

    GenServer.start_link(
      __MODULE__,
      %{conversation_id: conversation_id, llm_module: llm_module},
      gen_server_opts
    )
  end

  # Server callbacks

  @impl GenServer
  def init(%{conversation_id: conversation_id, llm_module: llm_module}) do
    AVSA.ConversationRecord.insert_conversation(conversation_id)

    state = %__MODULE__{
      conversation_id: conversation_id,
      llm_module: llm_module,
      max_context_turns: Application.get_env(:avsa, :max_context_turns, 5),
      turns: [],
      plan: nil
    }

    {:ok, state}
  end

  # ── Text-modality turn ────────────────────────────────────────────────────────
  @impl GenServer
  def handle_call({:start_text, user_text}, from, state) do
    handle_call({:start_text, user_text, Ecto.UUID.generate()}, from, state)
  end

  @impl GenServer
  def handle_call({:start_text, user_text, request_id}, _from, state) do
    do_turn(
      state,
      _image_arg = nil,
      %{"text" => user_text},
      user_text,
      request_id,
      "Text query: #{user_text}. Please use the find_similar tool."
    )
  end

  # ── Image-modality turn ─────────────────────────────────────────────────────
  @impl GenServer
  def handle_call({:start_image, image_arg, user_text}, from, state) do
    handle_call({:start_image, image_arg, user_text, Ecto.UUID.generate()}, from, state)
  end

  @impl GenServer
  def handle_call({:start_image, image_arg, user_text, request_id}, _from, state)
      when is_map(image_arg) do
    do_turn(
      state,
      image_arg,
      image_arg,
      user_text,
      request_id,
      "User provided an image. User text: #{user_text}. Please use the find_similar tool."
    )
  end

  # ── shared turn driver ──────────────────────────────────────────────────────
  defp do_turn(state, attr_image_arg, retrieval_arg, user_text, request_id, planning_content) do
    messages = [%{"role" => "user", "content" => planning_content}]

    case state.llm_module.call(messages, %{}) do
      {:ok, %AVSA.LLM.ToolUse{} = plan} ->
        with {:ok, %{attrs: attrs}} <- extract_attributes(attr_image_arg, user_text, request_id),
             attrs_with_context <- put_prior_ids(attrs, state),
             {:ok, results} <-
               find_similar(retrieval_arg, attrs_with_context, request_id) do
          finalise_turn(state, messages, plan, attrs_with_context, results, user_text)
        else
          {:error, reason} -> {:reply, {:error, reason}, state}
        end

      {:error, reason} ->
        {:reply, {:error, reason}, state}
    end
  end

  defp extract_attributes(attr_image_arg, user_text, request_id) do
    args = (attr_image_arg || %{}) |> Map.put("user_text", user_text)
    AVSA.MCP.Tools.extract_attributes(args, request_id: request_id)
  end

  defp find_similar(retrieval_arg, attrs_with_context, request_id) do
    args = Map.put(retrieval_arg, "attrs", attrs_with_context)
    AVSA.MCP.Tools.find_similar_results(args, request_id: request_id)
  end

  defp put_prior_ids(attrs, state) do
    prior_ids = Enum.map(state.results, & &1.id)
    Map.put(attrs, "prior_result_ids", prior_ids)
  end

  defp finalise_turn(state, messages, plan, attrs_with_context, results, user_text) do
    proposed = %{
      plan: plan,
      attrs: attrs_with_context,
      results: results,
      text: "",
      user_input: user_text,
      tool_calls: []
    }

    case AVSA.Verifier.check(state.conversation_id, proposed) do
      {:ok, _} ->
        {:reply, {:ok, proposed}, commit_turn(state, messages, plan, results, user_text)}

      {:error, :safety, reason} ->
        {:reply, {:error, :safety, reason}, state}

      {:error, _check_name, _reason} ->
        case state.llm_module.call(messages, %{}) do
          {:ok, %AVSA.LLM.ToolUse{} = new_plan} ->
            new_proposed = %{proposed | plan: new_plan, text: ""}

            case AVSA.Verifier.check(state.conversation_id, new_proposed) do
              {:ok, _} ->
                {:reply, {:ok, new_proposed},
                 commit_turn(state, messages, new_plan, results, user_text)}

              {:error, check_name2, reason2} ->
                {:reply, {:error, :verification_failed, check_name2, reason2}, state}
            end

          {:error, reason2} ->
            {:reply, {:error, reason2}, state}
        end
    end
  end

  defp commit_turn(state, messages, plan, results, user_text) do
    turn = %{messages: messages, plan: plan}
    new_turns = Enum.take(state.turns ++ [turn], -state.max_context_turns)

    AVSA.ConversationRecord.insert_turn(
      state.conversation_id,
      "user",
      %{text: user_text, results_count: length(results)}
    )

    %{state | plan: plan, results: results, turns: new_turns}
  end
end
