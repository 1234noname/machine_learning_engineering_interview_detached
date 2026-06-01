defmodule AVSA.LLM do
  @moduledoc """
  Behaviour defining the interface for LLM providers.

  Every implementation must return `{:ok, %AVSA.LLM.ToolUse{}}` on success so
  consumers can pattern-match on atom-keyed struct fields rather than opaque
  string-keyed maps, preventing shape-drift bugs across implementations.
  """

  @callback call(messages :: [map()], tool_manifest :: map()) ::
              {:ok, AVSA.LLM.ToolUse.t()} | {:error, term()}

  @doc """
  Resolve whether the `AVSA_LLM_STUB` bypass should select the in-memory
  `AVSA.LLM.Mock` implementation.

  `AVSA_LLM_STUB=1` opts into the network-free Mock. A real Anthropic key always
  wins, though: when `AVSA_ANTHROPIC_API_KEY` is set the stub bypass is disabled
  so a provided key is never silently bypassed by the stub. In that case — and
  whenever the stub flag is unset — this returns `nil`, meaning "leave the
  configured default" (`AVSA.LLM.Anthropic` from config.exs).

  Pure, so the precedence is unit-testable without booting `config/runtime.exs`.
  """
  @spec stub_override(stub_flag :: String.t() | nil, anthropic_key :: String.t() | nil) ::
          module() | nil
  def stub_override("1", key) when key in [nil, ""], do: AVSA.LLM.Mock
  def stub_override(_stub_flag, _anthropic_key), do: nil
end
