import Config

config :logger, level: :info

config :avsa, specs_root: Path.expand("../../../specs", __DIR__)

# Stamp the compile-time env so AVSA.Application can evaluate the MCP exposure
# guard without referencing Mix at runtime. runtime.exs re-stamps it.
config :avsa, config_env: config_env()

config :avsa, AVSA.Repo,
  url: "postgresql://avsa:avsa@localhost:5434/avsa",
  pool_size: 5,
  types: AVSA.PostgrexTypes

config :avsa, retrieval_knn_ms: 150

config :avsa, :max_context_turns, 5

config :avsa, llm_module: AVSA.LLM.Anthropic

if config_env() == :dev do
  import_config "dev.exs"
end

if config_env() == :test do
  import_config "test.exs"
end
