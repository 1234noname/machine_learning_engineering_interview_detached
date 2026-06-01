defmodule AVSA.ConversationSupervisor do
  @moduledoc """
  Dynamic supervisor for AVSA.Conversation processes.

  Each conversation is registered in AVSA.ConversationRegistry under its
  conversation_id. Dead conversations are not restarted (restart: :temporary).
  """

  use DynamicSupervisor

  def start_link(init_arg) do
    DynamicSupervisor.start_link(__MODULE__, init_arg, name: __MODULE__)
  end

  @impl DynamicSupervisor
  def init(_init_arg) do
    DynamicSupervisor.init(strategy: :one_for_one)
  end

  @doc "Returns the via-tuple used to register and look up a conversation by id."
  def via_tuple(conversation_id) do
    {:via, Registry, {AVSA.ConversationRegistry, conversation_id}}
  end

  @doc """
  Start or resume a conversation process under this supervisor.

  If a process is already registered for conversation_id (resume case), returns
  {:ok, existing_pid}. Otherwise starts a new child and returns {:ok, new_pid}.

  `llm_module` defaults to the **configured** implementation (`configured_llm_module/0`)
  so the GenServer that the gRPC streaming path drives uses the SAME LLM as the
  rest of the system — real `AVSA.LLM.Anthropic` in production, the Mock only in
  test / under `AVSA_LLM_STUB` (see `AVSA.LLM.stub_override/2`). It is read at call
  time, and only takes effect when a NEW process is started (resume ignores it).
  """
  def start_conversation(conversation_id, llm_module \\ configured_llm_module()) do
    child_spec = %{
      id: {AVSA.Conversation, conversation_id},
      start:
        {AVSA.Conversation, :start_link,
         [
           [
             conversation_id: conversation_id,
             llm_module: llm_module,
             name: via_tuple(conversation_id)
           ]
         ]},
      restart: :temporary
    }

    case DynamicSupervisor.start_child(__MODULE__, child_spec) do
      {:ok, pid} -> {:ok, pid}
      {:error, {:already_started, pid}} -> {:ok, pid}
      error -> error
    end
  end

  defp configured_llm_module do
    Application.get_env(:avsa, :llm_module, AVSA.LLM.Anthropic)
  end
end
