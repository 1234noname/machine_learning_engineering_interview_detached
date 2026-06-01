defmodule AVSA.Umbrella.MixProject do
  use Mix.Project

  def project do
    [
      apps_path: "apps",
      version: "0.1.0",
      elixir: "~> 1.16",
      start_permanent: Mix.env() == :prod,
      deps: [],
      releases: [
        avsa: [
          applications: [avsa: :permanent]
        ]
      ]
    ]
  end
end
