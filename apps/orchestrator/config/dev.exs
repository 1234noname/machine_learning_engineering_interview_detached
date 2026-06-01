import Config

config :logger, level: :debug

config :avsa, AVSA.Repo, url: "postgresql://avsa:avsa@localhost:5434/avsa"

# Local dev/stack serves the MCP tool bus so an external MCP client (the
# Inspector / a future Solenya client) can reach it on :mcp_port. `start_mcp` is
# otherwise gated on DATABASE_URL (runtime.exs), which the dev stack doesn't set
# (the DB URL comes from the line above), so the listener would never mount
# under `just stack-up`. Open (no key) is fine locally — the runtime.exs
# exposure guard still requires a key when config_env==:prod or
# AVSA_MCP_EXPOSED=1.
config :avsa, start_mcp: true
