defmodule ElixirAnthropic.Telemetry do
  @moduledoc """
  Telemetry integration for the ElixirAnthropic library.

  Emits the following event:

  - `[:elixir_anthropic, :request, :stop]` — fired after every HTTP request.
    Measurements: `%{duration: non_neg_integer()}` (native time units via `:erlang.monotonic_time/0`).
    Metadata: `%{model: String.t() | nil, status: non_neg_integer() | nil}`.
  """

  @doc """
  Emits the `[:elixir_anthropic, :request, :stop]` telemetry event.

  ## Parameters

  - `event` — reserved for the last segment of the event name (currently unused; always emits `:stop`).
  - `measurements` — a map containing at least `%{duration: native_time}`.
  - `metadata` — a map containing at least `%{model: model, status: status_code}`.
  """
  @spec execute(atom(), map(), map()) :: :ok
  def execute(_event, measurements, metadata) do
    :telemetry.execute([:elixir_anthropic, :request, :stop], measurements, metadata)
    :ok
  end
end
