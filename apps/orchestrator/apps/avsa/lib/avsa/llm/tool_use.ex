defmodule AVSA.LLM.ToolUse do
  @moduledoc """
  Typed result struct returned by every `AVSA.LLM` implementation.

  Both `AVSA.LLM.Mock` and `AVSA.LLM.Anthropic` produce `{:ok, %AVSA.LLM.ToolUse{}}` on
  success so downstream consumers can pattern-match on atom-keyed fields rather than
  opaque string-keyed maps.

  ## Fields

  - `:name`  — the tool name the LLM called (e.g. `"find_similar"`, `"extract_attributes"`).
  - `:input` — the structured input map the LLM produced for that tool call.
  - `:id`    — the unique identifier for this tool use block (e.g. `"tu-1"`, `"mock-1"`).
  """

  @enforce_keys [:name, :input, :id]
  defstruct [:name, :input, :id]

  @typedoc """
  A successful LLM tool-use result.

  - `:name`  — tool name string
  - `:input` — tool input map (string-keyed, mirrors the JSON schema)
  - `:id`    — opaque identifier string for this tool_use block
  """
  @type t :: %__MODULE__{
          name: String.t(),
          input: map(),
          id: String.t()
        }
end
