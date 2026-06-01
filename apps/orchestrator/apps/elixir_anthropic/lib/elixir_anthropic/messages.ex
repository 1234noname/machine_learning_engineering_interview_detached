defmodule ElixirAnthropic.Messages do
  @moduledoc """
  Handles the `POST /v1/messages` endpoint of the Anthropic API.
  """

  @doc """
  Calls the Anthropic Messages API.

  Validates that `:model` and `:messages` keys are present in `params`.
  Delegates the actual HTTP call to `ElixirAnthropic.Client.post/3`.

  Returns `{:ok, map()}` on success or `{:error, %ElixirAnthropic.Error{}}` on failure.
  """
  @spec call(ElixirAnthropic.t(), map()) ::
          {:ok, map()} | {:error, ElixirAnthropic.Error.t()}
  def call(%ElixirAnthropic{} = client, params) when is_map(params) do
    with :ok <- validate_params(params) do
      ElixirAnthropic.Client.post(client, "/v1/messages", params)
    end
  end

  @spec validate_params(map()) :: :ok | {:error, ElixirAnthropic.Error.t()}
  defp validate_params(params) do
    model_present = Map.has_key?(params, :model) or Map.has_key?(params, "model")
    messages_present = Map.has_key?(params, :messages) or Map.has_key?(params, "messages")

    if model_present and messages_present do
      :ok
    else
      {:error,
       %ElixirAnthropic.Error{
         type: :invalid_params,
         message: "params must include :model and :messages keys",
         status: nil
       }}
    end
  end
end
