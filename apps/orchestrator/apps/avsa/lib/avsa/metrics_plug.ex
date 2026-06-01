defmodule AVSA.MetricsPlug do
  @moduledoc """
  A minimal Plug router that exposes a `/metrics` endpoint for Prometheus
  scraping. Served on port 9568 (configurable via `:avsa, :metrics_port`).
  """

  use Plug.Router

  plug(:match)
  plug(:dispatch)

  get "/metrics" do
    metrics = TelemetryMetricsPrometheus.Core.scrape(:avsa_prometheus)

    conn
    |> put_resp_content_type("text/plain; version=0.0.4; charset=utf-8")
    |> send_resp(200, metrics)
  end

  match _ do
    send_resp(conn, 404, "not found")
  end
end
