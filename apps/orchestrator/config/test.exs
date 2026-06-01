import Config

config :logger, level: :warning

config :avsa, http_client: :stub

config :avsa, start_repo: false

config :avsa, start_grpc: false

config :avsa, start_metrics: false

# CRITICAL: never mount the MCP HTTP listener in the test suite — a foreground
# Plug.Cowboy listener would block the run.
config :avsa, start_mcp: false

config :avsa, config_env: :test

config :avsa, llm_module: AVSA.LLM.Mock

config :avsa, :max_context_turns, 3
