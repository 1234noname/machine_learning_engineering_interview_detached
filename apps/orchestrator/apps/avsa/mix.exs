defmodule AVSA.MixProject do
  use Mix.Project

  def project do
    [
      app: :avsa,
      version: "0.1.0",
      elixir: "~> 1.16",
      start_permanent: Mix.env() == :prod,
      elixirc_paths: elixirc_paths(Mix.env()),
      deps: deps()
    ]
  end

  defp elixirc_paths(:test), do: ["lib", "test/support"]
  defp elixirc_paths(_), do: ["lib"]

  def application do
    [
      mod: {AVSA.Application, []},
      extra_applications: [:logger]
    ]
  end

  defp deps do
    [
      {:phoenix, "~> 1.7"},
      {:phoenix_live_view, "~> 1.0"},
      {:grpc, "~> 0.11"},
      {:castore, "~> 1.0"},
      {:protobuf, "~> 0.14"},
      {:elixir_anthropic, in_umbrella: true},
      {:telemetry, "~> 1.2"},
      {:telemetry_metrics, "~> 1.0"},
      {:telemetry_metrics_prometheus_core, "~> 1.1"},
      {:plug_cowboy, "~> 2.7"},
      {:ecto_sql, "~> 3.10"},
      {:postgrex, "~> 0.17"},
      {:fuse, "~> 2.5"},
      {:pgvector, "~> 0.3"},
      {:finch, "~> 0.22"},
      {:jason, "~> 1.4"},
      {:bypass, github: "PSPDFKit-labs/bypass", only: :test},
      {:yaml_elixir, "~> 2.9"}
    ]
  end
end
