defmodule ElixirAnthropic.Error do
  @moduledoc """
  Structured error type for the ElixirAnthropic library.

  Error types:
  - `:api_error` — The Anthropic API returned a non-2xx response.
  - `:network_error` — A lower-level network failure occurred (connection refused, DNS, etc.).
  - `:timeout` — The request timed out.
  - `:invalid_params` — The caller supplied invalid or missing parameters.
  """

  @typedoc "A structured error returned by ElixirAnthropic functions."
  @type t :: %__MODULE__{
          type: atom(),
          message: String.t() | nil,
          status: non_neg_integer() | nil
        }

  defstruct [:type, :message, :status]
end
