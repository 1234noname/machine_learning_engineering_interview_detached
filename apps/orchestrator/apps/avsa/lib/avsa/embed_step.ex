defmodule AVSA.EmbedStep do
  @moduledoc """
  Calls the batcher /embed endpoint via Finch to obtain a 768-dim embedding.

  Uses a circuit breaker (:fuse) to prevent cascading failures when the batcher
  is unavailable, and emits telemetry events for latency and error tracking.
  """

  require Logger

  @typedoc """
  EmbedStep result: the 768-dim embedding plus the ViT attribute head output.

  `attributes` is the string-keyed ViT attribute map
  (`category`, `colour`, `category_confidence`, `colour_confidence`) when the
  batcher includes the attribute head, or `nil` when it omits it (e.g. a stub
  batcher without heads) — graceful degradation, never a crash.
  """
  @type result :: %{embedding: [float()], attributes: map() | nil}

  @spec call(binary()) :: {:ok, result()} | {:error, term()}
  def call(image_bytes) when is_binary(image_bytes) do
    batcher_url = Application.get_env(:avsa, :batcher_url, "http://localhost:8081")
    start = :erlang.monotonic_time(:millisecond)
    b64 = Base.encode64(image_bytes)
    body = Jason.encode!(%{"image_bytes" => b64})
    url = batcher_url <> "/embed"

    case :fuse.ask(:batcher_circuit, :sync) do
      :ok ->
        result = do_request(url, body)

        if match?({:error, _}, result) do
          :fuse.melt(:batcher_circuit)
          # Circuit-breaker observability: an open breaker is silent degradation.
          # Each melt is counted; bounded cardinality: breaker ∈ 2 fixed values.
          :telemetry.execute(
            [:avsa, :circuit, :melt],
            %{count: 1},
            %{breaker: "batcher_circuit"}
          )
        end

        elapsed = :erlang.monotonic_time(:millisecond) - start

        case result do
          {:ok, embed_result} ->
            :telemetry.execute(
              [:avsa, :embed, :complete],
              %{latency_ms: elapsed},
              %{modality: "image"}
            )

            emit_attribute_metrics(embed_result)

            {:ok, embed_result}

          {:error, reason} ->
            :telemetry.execute(
              [:avsa, :embed, :error],
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

    case Finch.request(req, :avsa_batcher_pool) do
      {:ok, %Finch.Response{status: 200, body: resp_body}} ->
        case Jason.decode(resp_body) do
          {:ok, %{"embedding" => embedding} = decoded} when is_list(embedding) ->
            {:ok, %{embedding: embedding, attributes: Map.get(decoded, "attributes")}}

          _ ->
            {:error, :bad_response}
        end

      {:ok, %Finch.Response{status: status}} ->
        {:error, {:http_error, status}}

      {:error, reason} ->
        {:error, reason}
    end
  end

  defp emit_attribute_metrics(%{attributes: attributes}) when is_map(attributes) do
    for attribute <- ["category", "colour"] do
      label = Map.get(attributes, attribute)

      if is_binary(label) and label != "" do
        :telemetry.execute(
          [:avsa, :attribute, :prediction],
          %{count: 1},
          %{attribute: attribute, label: label}
        )
      end

      confidence = Map.get(attributes, "#{attribute}_confidence")

      if is_number(confidence) do
        :telemetry.execute(
          [:avsa, :attribute, :confidence],
          %{confidence: confidence},
          %{attribute: attribute}
        )
      end
    end

    :ok
  end

  defp emit_attribute_metrics(_embed_result), do: :ok
end
