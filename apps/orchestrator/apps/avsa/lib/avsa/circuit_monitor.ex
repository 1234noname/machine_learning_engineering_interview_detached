defmodule AVSA.CircuitMonitor do
  @poll_interval_ms 5_000
  @circuits [:batcher_circuit, :anthropic_circuit, :text_encoder_circuit]

  @moduledoc """
  Polls :fuse circuit-breaker state every #{@poll_interval_ms}ms and emits:

    [:avsa, :circuit, :state]   — gauge: 0 = closed (ok), 1 = blown
      measurements: %{value: 0 | 1}
      metadata:     %{breaker: string()}

    [:avsa, :circuit, :reset]   — counter: fired once per blown→ok transition
      measurements: %{count: 1}
      metadata:     %{breaker: string()}

  Bounded cardinality: breaker ∈ [:batcher_circuit, :anthropic_circuit, :text_encoder_circuit].
  """

  use GenServer

    defstruct prev_states: %{}

  def start_link(opts \\ []) do
    GenServer.start_link(__MODULE__, :ok, Keyword.put_new(opts, :name, __MODULE__))
  end

  @impl true
  def init(:ok) do
    send(self(), :poll)
    {:ok, %__MODULE__{}}
  end

  @impl true
  def handle_info(:poll, state) do
    new_prev =
      Enum.reduce(@circuits, state.prev_states, fn circuit, acc ->
        current = fuse_state(circuit)
        breaker = Atom.to_string(circuit)

        value = if current == :blown, do: 1, else: 0
        :telemetry.execute([:avsa, :circuit, :state], %{value: value}, %{breaker: breaker})

        prev = Map.get(acc, circuit, :ok)
        if prev == :blown and current == :ok do
          :telemetry.execute([:avsa, :circuit, :reset], %{count: 1}, %{breaker: breaker})
        end

        Map.put(acc, circuit, current)
      end)

    schedule_poll()
    {:noreply, %{state | prev_states: new_prev}}
  end

  defp fuse_state(circuit) do
    case :fuse.ask(circuit, :sync) do
      :ok -> :ok
      :blown -> :blown
    end
  rescue
    _ -> :ok
  end

  defp schedule_poll do
    Process.send_after(self(), :poll, @poll_interval_ms)
  end
end
