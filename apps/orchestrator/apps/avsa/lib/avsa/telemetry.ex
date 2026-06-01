defmodule AVSA.Telemetry do
  @moduledoc """
  Metric declarations for the AVSA orchestrator.

  This module declares the telemetry metrics consumed by the reporting
  pipeline using `Telemetry.Metrics` structs. Metrics are scraped by
  `TelemetryMetricsPrometheus.Core` and exposed at `/metrics` on port 9568.

  Events emitted by the orchestrator:
    [:avsa, :conversation, :complete]
      measurements: %{latency_ms: integer()}
      metadata:     %{outcome: "success" | "error", modality: string()}

    [:avsa, :embed, :complete]
      measurements: %{latency_ms: integer()}
      metadata:     %{modality: string()}

    [:avsa, :embed, :error]
      measurements: %{}
      metadata:     %{reason: string()}

    [:avsa, :orch, :tool, :retrieval, :stop]
      measurements: %{duration: integer(), monotonic_time: integer()}
      metadata:     %{}
      emitted by :telemetry.span/3 in RetrievalTool.call/2

    [:avsa, :orch, :tool, :attribute, :stop]
      measurements: %{duration: integer(), monotonic_time: integer()}
      metadata:     %{}
      emitted by :telemetry.span/3 in AttributeTool.call/2

    [:avsa, :embed, :error]
      measurements: %{}
      metadata:     %{reason: string()}
      emitted by EmbedStep when a batch embed fails

    [:avsa, :tool_dispatch, :find_similar]
      measurements: %{}
      metadata:     %{}
      emitted by AVSA.MCP.Tools per tool type

    [:avsa, :tool_dispatch, :extract_attributes]
      measurements: %{}
      metadata:     %{}
      emitted by AVSA.MCP.Tools per tool type

    [:avsa, :orch, :verifier, :check]
      measurements: %{outcome: :pass | :fail}
      metadata:     %{name: atom(), outcome: :pass | :fail, conversation_id: string()}
      emitted by AVSA.Verifier for each of the 6 post-generation checks.
      `outcome` is duplicated into metadata because Telemetry.Metrics tag_values
      is metadata-only; `name` becomes the `check_name` Prometheus label.

    [:avsa, :text_embed, :complete]
      measurements: %{latency_ms: integer()}
      metadata:     %{}
      emitted by AVSA.TextTool after a successful /embed_text call

    [:avsa, :text_embed, :error]
      measurements: %{}
      metadata:     %{reason: string()}
      emitted by AVSA.TextTool when a /embed_text call fails

    [:avsa, :attribute, :source]
      measurements: %{count: 1}
      metadata:     %{attribute: "category"|"colour"|"formality"|"occasion",
                      source: "vit"|"llm"|"text"}
      emitted by AVSA.AttributeTool.compose_attrs/2 — one event per attribute,
      tagging which head produced it. Proves the ViT offload (category/colour
      should come from "vit", formality/occasion from "llm"). source="text" marks
      an explicit text override ("this but green") where the shopper-named
      colour/category wins over the ViT value. Bounded cardinality: source ∈ 3.

    [:avsa, :attribute, :llm_call]
      measurements: %{count: 1}
      metadata:     %{narrowed: boolean()}
      emitted by AVSA.AttributeTool before each extract_attributes LLM call.
      narrowed=true when ViT supplied category/colour (the LLM tool schema is
      scoped to formality/occasion); false on the full four-attr fallback.
      Quantifies the LLM-work reduction.

    [:avsa, :attribute, :prediction]
      measurements: %{count: 1}
      metadata:     %{attribute: "category"|"colour", label: string()}
      emitted by AVSA.EmbedStep when the batcher surfaces ViT attribute-head
      output. The label distribution reveals prediction drift. Bounded
      cardinality: head label maps (category/colour vocabularies) are small.

    [:avsa, :attribute, :confidence]
      measurements: %{confidence: float()}
      metadata:     %{attribute: "category"|"colour"}
      emitted by AVSA.EmbedStep alongside :prediction. Confidence distribution;
      a downward spike = model degradation.

    [:avsa, :circuit, :melt]
      measurements: %{count: 1}
      metadata:     %{breaker: "batcher_circuit"|"text_encoder_circuit"|"anthropic_circuit"}
      emitted by AVSA.EmbedStep / AVSA.TextTool / AVSA.LLM.Anthropic each time the
      :fuse circuit melts. An open breaker is otherwise-silent degradation.
      Bounded: breaker ∈ 3.

    [:avsa, :retrieval, :results]
      measurements: %{count: integer()}
      metadata:     %{}
      emitted by AVSA.RetrievalTool per successful query — the result-count
      distribution (in-flow retrieval quality signal).

    [:avsa, :retrieval, :empty]
      measurements: %{count: 1}
      metadata:     %{}
      emitted by AVSA.RetrievalTool when a successful query returns zero rows.
      Empty results = bad shopper experience.

    [:avsa, :retrieval, :constraint_relaxed]
      measurements: %{count: 1}
      metadata:     %{}
      emitted by AVSA.RetrievalTool when an explicit colour constraint yields
      zero in-style matches and the hard filter is relaxed to the unconstrained
      pure-kNN best-effort query. A rising rate flags a colour vocabulary that
      is too narrow for shopper intent.

    [:avsa, :embed_cache, :hit]
      measurements: %{count: 1}
      metadata:     %{modality: "image" | "text"}
      emitted by AVSA.EmbedCache when a content-addressed lookup finds an existing
      embedding. A hit rate climbing toward 1.0 means repeat queries are being
      served without burning GPU time. Bounded: modality ∈ 2.

    [:avsa, :embed_cache, :miss]
      measurements: %{count: 1}
      metadata:     %{modality: "image" | "text"}
      emitted by AVSA.EmbedCache when a content-addressed lookup finds no cached
      embedding and a fresh forward pass is required. A high miss rate means the
      L1 cache isn't helping. Bounded: modality ∈ 2.

    [:avsa, :circuit, :state]
      measurements: %{value: 0 | 1}
      metadata:     %{breaker: string()}
      polled every 5 s by AVSA.CircuitMonitor. 0 = closed (healthy), 1 = blown.
      A nonzero value means requests are short-circuited with {:error, :circuit_open}.
      Bounded: breaker ∈ 3.

    [:avsa, :circuit, :reset]
      measurements: %{count: 1}
      metadata:     %{breaker: string()}
      emitted by AVSA.CircuitMonitor on each blown→ok transition. Complements
      melt: melt counts failures, reset counts healings. Bounded: breaker ∈ 3.
  """

  import Telemetry.Metrics

  @doc """
  Returns the list of `Telemetry.Metrics` structs for this application.

  These are passed to `TelemetryMetricsPrometheus.Core` at startup so it can
  register the appropriate Prometheus metrics and attach the necessary
  `:telemetry` handlers.
  """
  @spec metrics() :: [Telemetry.Metrics.t()]
  def metrics do
    [
      distribution("avsa.conversation.latency.seconds",
        event_name: [:avsa, :conversation, :complete],
        measurement: :latency_ms,
        unit: {:millisecond, :second},
        tags: [:outcome, :modality],
        reporter_options: [buckets: [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5]]
      ),
      counter("avsa.chat.outcome.total",
        event_name: [:avsa, :conversation, :complete],
        tags: [:outcome]
      ),
      counter("avsa.vit.qps.total",
        event_name: [:avsa, :embed, :complete],
        tags: [:modality]
      ),
      distribution("avsa.embed.latency.seconds",
        event_name: [:avsa, :embed, :complete],
        measurement: :latency_ms,
        unit: {:millisecond, :second},
        tags: [:modality],
        reporter_options: [buckets: [0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 1.0]]
      ),

      distribution("avsa.orch.tool.retrieval.duration.seconds",
        event_name: [:avsa, :orch, :tool, :retrieval, :stop],
        measurement: :duration,
        unit: {:native, :second},
        reporter_options: [buckets: [0.01, 0.025, 0.05, 0.1, 0.15, 0.2, 0.3, 0.5, 1.0]]
      ),

      distribution("avsa.orch.tool.attribute.duration.seconds",
        event_name: [:avsa, :orch, :tool, :attribute, :stop],
        measurement: :duration,
        unit: {:native, :second},
        reporter_options: [buckets: [0.1, 0.25, 0.5, 1.0, 1.5, 2.0, 3.0, 5.0]]
      ),

      counter("avsa.embed.error.total",
        event_name: [:avsa, :embed, :error]
      ),

      counter("avsa.tool.dispatch.find_similar.total",
        event_name: [:avsa, :tool_dispatch, :find_similar]
      ),
      counter("avsa.tool.dispatch.extract_attributes.total",
        event_name: [:avsa, :tool_dispatch, :extract_attributes]
      ),

      counter("avsa.verifier.outcome.total",
        event_name: [:avsa, :orch, :verifier, :check],
        tags: [:check_name, :outcome],
        tag_values: fn metadata ->
          %{check_name: metadata.name, outcome: metadata.outcome}
        end
      ),

      distribution("avsa.text_embed.latency.seconds",
        event_name: [:avsa, :text_embed, :complete],
        measurement: :latency_ms,
        unit: {:millisecond, :second},
        reporter_options: [buckets: [0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0]]
      ),

      counter("avsa.text_embed.error.total",
        event_name: [:avsa, :text_embed, :error]
      ),

      # ---------------------------------------------------------------------
      # Attribute Pipeline observability
      # ---------------------------------------------------------------------
      counter("avsa.attribute.source.total",
        event_name: [:avsa, :attribute, :source],
        measurement: :count,
        tags: [:attribute, :source]
      ),

      counter("avsa.attribute.llm_calls.total",
        event_name: [:avsa, :attribute, :llm_call],
        measurement: :count,
        tags: [:narrowed]
      ),

      counter("avsa.attribute.prediction.total",
        event_name: [:avsa, :attribute, :prediction],
        measurement: :count,
        tags: [:attribute, :label]
      ),

      distribution("avsa.attribute.confidence",
        event_name: [:avsa, :attribute, :confidence],
        measurement: :confidence,
        tags: [:attribute],
        reporter_options: [buckets: [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]]
      ),

      counter("avsa.circuit.melt.total",
        event_name: [:avsa, :circuit, :melt],
        measurement: :count,
        tags: [:breaker]
      ),

      distribution("avsa.retrieval.results",
        event_name: [:avsa, :retrieval, :results],
        measurement: :count,
        reporter_options: [buckets: [0, 1, 2, 5, 10, 15, 20]]
      ),

      counter("avsa.retrieval.empty.total",
        event_name: [:avsa, :retrieval, :empty],
        measurement: :count
      ),

      counter("avsa.embed_cache.hit.total",
        event_name: [:avsa, :embed_cache, :hit],
        measurement: :count,
        tags: [:modality]
      ),
      counter("avsa.embed_cache.miss.total",
        event_name: [:avsa, :embed_cache, :miss],
        measurement: :count,
        tags: [:modality]
      ),

      counter("avsa.retrieval.constraint_relaxed.total",
        event_name: [:avsa, :retrieval, :constraint_relaxed],
        measurement: :count
      ),

      last_value("avsa.circuit.state",
        event_name: [:avsa, :circuit, :state],
        measurement: :value,
        tags: [:breaker]
      ),

      counter("avsa.circuit.reset.total",
        event_name: [:avsa, :circuit, :reset],
        measurement: :count,
        tags: [:breaker]
      )
    ]
  end

  @doc """
  Attaches a simple Logger handler for each declared metric event.

  This handler logs each event at debug level and is intended for local
  development alongside the Prometheus reporter.
  """
  @spec attach_logger_handlers() :: :ok
  def attach_logger_handlers do
    events =
      metrics()
      |> Enum.map(& &1.event_name)
      |> Enum.uniq()

    :telemetry.attach_many(
      "avsa-telemetry-logger",
      events,
      &__MODULE__.handle_event/4,
      nil
    )

    :ok
  end

  @doc false
  def handle_event(event_name, measurements, metadata, _config) do
    require Logger

    Logger.debug(fn ->
      "telemetry #{inspect(event_name)} measurements=#{inspect(measurements)} metadata=#{inspect(metadata)}"
    end)
  end
end
