defmodule ElixirAnthropic.Client do
  @moduledoc """
  Low-level HTTP client for the Anthropic API.

  Uses Finch to execute HTTP requests and Jason to decode JSON responses.
  Emits telemetry via `ElixirAnthropic.Telemetry` after each request.
  """

  @doc """
  Issues an HTTP POST request to the Anthropic API.

  Builds the required headers (`x-api-key`, `anthropic-version`, `content-type`),
  encodes `body` as JSON, and returns the decoded response map on success.

  Returns `{:ok, map()}` on 2xx, `{:error, %ElixirAnthropic.Error{}}` otherwise.
  """
  @spec post(ElixirAnthropic.t(), String.t(), map()) ::
          {:ok, map()} | {:error, ElixirAnthropic.Error.t()}
  def post(%ElixirAnthropic{} = client, path, body) when is_binary(path) and is_map(body) do
    url = client.base_url <> path

    headers = [
      {"x-api-key", client.api_key},
      {"anthropic-version", client.version},
      {"content-type", "application/json"}
    ]

    encoded_body = Jason.encode!(body)
    start_time = :erlang.monotonic_time()
    model = Map.get(body, :model) || Map.get(body, "model")

    request = Finch.build(:post, url, headers, encoded_body)

    case Finch.request(request, ElixirAnthropic.Finch) do
      {:ok, %Finch.Response{status: status, body: response_body}} ->
        duration = :erlang.monotonic_time() - start_time
        decoded = Jason.decode!(response_body)

        ElixirAnthropic.Telemetry.execute(:stop, %{duration: duration}, %{
          model: model,
          status: status
        })

        if status in 200..299 do
          {:ok, decoded}
        else
          message = get_in(decoded, ["error", "message"])

          {:error,
           %ElixirAnthropic.Error{
             type: :api_error,
             message: message,
             status: status
           }}
        end

      {:error, reason} ->
        duration = :erlang.monotonic_time() - start_time

        ElixirAnthropic.Telemetry.execute(:stop, %{duration: duration}, %{
          model: model,
          status: nil
        })

        {type, message} =
          case reason do
            %Mint.TransportError{reason: :timeout} -> {:timeout, "Request timed out"}
            _ -> {:network_error, inspect(reason)}
          end

        {:error, %ElixirAnthropic.Error{type: type, message: message, status: nil}}
    end
  end
end
