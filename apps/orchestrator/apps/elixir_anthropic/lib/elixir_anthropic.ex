defmodule ElixirAnthropic do
  @moduledoc """
  Public facade for the ElixirAnthropic Anthropic API client library.

  ## Usage

      client = ElixirAnthropic.new(api_key: "sk-ant-...")
      {:ok, response} = ElixirAnthropic.messages(client, %{
        model: "claude-haiku-4-5-20251001",
        max_tokens: 1024,
        messages: [%{role: "user", content: "Hello!"}]
      })
  """

  @typedoc "An ElixirAnthropic client struct."
  @type t :: %__MODULE__{
          api_key: String.t(),
          base_url: String.t(),
          version: String.t()
        }

  defstruct [:api_key, base_url: "https://api.anthropic.com", version: "2023-06-01"]

  @doc """
  Creates a new ElixirAnthropic client struct.

  ## Options

  - `:api_key` (required) — your Anthropic API key.
  - `:base_url` (optional) — base URL for the API. Defaults to `"https://api.anthropic.com"`.
    Override in tests to point at a Bypass server.
  - `:version` (optional) — the `anthropic-version` header value. Defaults to `"2023-06-01"`.
  """
  @spec new(keyword()) :: t()
  def new(opts) do
    struct!(__MODULE__, opts)
  end

  @doc """
  Calls the Anthropic Messages API (`POST /v1/messages`).

  Delegates to `ElixirAnthropic.Messages.call/2`.

  ## Parameters

  - `client` — an `ElixirAnthropic` struct created with `new/1`.
  - `params` — a map with at minimum `:model` and `:messages` keys.

  ## Returns

  - `{:ok, map()}` — the decoded API response on success.
  - `{:error, %ElixirAnthropic.Error{}}` — a structured error on failure.
  """
  @spec messages(t(), map()) :: {:ok, map()} | {:error, ElixirAnthropic.Error.t()}
  def messages(client, params) do
    ElixirAnthropic.Messages.call(client, params)
  end
end
