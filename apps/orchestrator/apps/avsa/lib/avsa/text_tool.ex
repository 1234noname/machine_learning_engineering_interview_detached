defmodule AVSA.TextTool do
  @moduledoc """
  Calls the model server /embed_text endpoint via Finch to obtain a 512-dim
  text embedding.

  Uses a circuit breaker (:fuse) to prevent cascading failures when the model
  server is unavailable, and emits telemetry events for latency and error tracking.
  """

  require Logger

  @spec call(binary()) :: {:ok, [float()]} | {:error, term()}
  def call(text) when is_binary(text) do
    model_url = Application.get_env(:avsa, :model_url, "http://localhost:8001")
    start = :erlang.monotonic_time(:millisecond)
    body = Jason.encode!(%{"texts" => [text]})
    url = model_url <> "/embed_text"

    case :fuse.ask(:text_encoder_circuit, :sync) do
      :ok ->
        result = do_request(url, body)

        if match?({:error, _}, result) do
          :fuse.melt(:text_encoder_circuit)
          :telemetry.execute(
            [:avsa, :circuit, :melt],
            %{count: 1},
            %{breaker: "text_encoder_circuit"}
          )
        end

        elapsed = :erlang.monotonic_time(:millisecond) - start

        case result do
          {:ok, embedding} ->
            :telemetry.execute(
              [:avsa, :text_embed, :complete],
              %{latency_ms: elapsed},
              %{}
            )

            {:ok, embedding}

          {:error, reason} ->
            :telemetry.execute(
              [:avsa, :text_embed, :error],
              %{},
              %{reason: inspect(reason)}
            )

            {:error, reason}
        end

      :blown ->
        {:error, :circuit_open}
    end
  end

  defp do_request(url, body) do
    req = Finch.build(:post, url, [{"content-type", "application/json"}], body)

    case Finch.request(req, :avsa_model_pool) do
      {:ok, %Finch.Response{status: 200, body: resp_body}} ->
        case Jason.decode(resp_body) do
          {:ok, %{"embeddings" => [embedding | _]}} -> {:ok, embedding}
          _ -> {:error, :bad_response}
        end

      {:ok, %Finch.Response{status: status}} ->
        {:error, {:http_error, status}}

      {:error, reason} ->
        {:error, reason}
    end
  end
end
