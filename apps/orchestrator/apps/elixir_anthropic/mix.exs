defmodule ElixirAnthropic.MixProject do
  use Mix.Project

  def project do
    [
      app: :elixir_anthropic,
      version: "0.1.0",
      elixir: "~> 1.16",
      start_permanent: Mix.env() == :prod,
      deps: deps()
    ]
  end

  def application do
    [
      mod: {ElixirAnthropic.Application, []},
      extra_applications: [:logger]
    ]
  end

  defp deps do
    [
      {:finch, "~> 0.19"},
      {:jason, "~> 1.4"},
      {:telemetry, "~> 1.2"},
      {:bypass, github: "PSPDFKit-labs/bypass", only: :test}
    ]
  end
end
