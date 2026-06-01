import Config

# Stamp the runtime env so the MCP exposure guard in AVSA.Application is correct
# in a release (no Mix at runtime). config.exs sets this too; runtime wins.
config :avsa, config_env: config_env()

# ── MCP external tool surface ────────────────────────────────────────────────
# AVSA_MCP_API_KEY  — bearer key required for the external HTTP tool surface.
#                     Unset on localhost = open (acceptable for a local demo);
#                     REQUIRED whenever the server is exposed off-localhost.
# AVSA_MCP_PORT     — listener port (default 8082).
# AVSA_MCP_EXPOSED  — "1" marks a non-prod server as network-exposed, arming the
#                     boot guard (refuse to start exposed-without-key).
# start_mcp         — mount the listener when DATABASE_URL is set (the stack is
#                     serving), mirroring how the other listeners come up. In
#                     :prod the guard additionally requires a key.
config :avsa, mcp_api_key: System.get_env("AVSA_MCP_API_KEY")

config :avsa, mcp_port: String.to_integer(System.get_env("AVSA_MCP_PORT", "8082"))

config :avsa, mcp_exposed: System.get_env("AVSA_MCP_EXPOSED") == "1"

# Mount the MCP listener when the stack is serving (DATABASE_URL set), EXCEPT
# under :test — the suite must never run a foreground listener, so test.exs's
# `start_mcp: false` is preserved even when integration tests set DATABASE_URL.
if System.get_env("DATABASE_URL") && config_env() != :test do
  config :avsa, start_mcp: true
end

# Headless eval mode — `AVSA_EVAL_MODE=1` boots the app with NO network listeners
# (gRPC :50051, metrics :9568, MCP :8082) so the verifier eval can run alongside a
# live stack (e.g. alongside `just stack-up`) without a port clash. The Repo
# still starts (DATABASE_URL set), so AVSA.Verifier.check/2 runs in-process against
# the live catalog. Must come AFTER the start_mcp auto-mount above so it wins.
if System.get_env("AVSA_EVAL_MODE") == "1" do
  config :avsa, start_grpc: false
  config :avsa, start_metrics: false
  config :avsa, start_mcp: false
end

if url = System.get_env("DATABASE_URL") do
  config :avsa, start_repo: true

  # Integration tests (MIX_ENV=test with DATABASE_URL set) run each test inside
  # an Ecto SQL.Sandbox transaction that is rolled back at test teardown. Each
  # test checks the connection out in SHARED mode so the app's named DB-querying
  # GenServers (AVSA.RetrievalTool, AVSA.Verifier) see the rows it seeded, and
  # the inserts vanish for the next test — so determinism is INDEPENDENT of pool
  # size. We therefore keep a realistic `pool_size` (mirroring config.exs's 5)
  # rather than pinning to 1. (The per-test setup also raises hnsw.ef_search; see
  # AVSA.RepoTestHelper.checkout_shared!/0 — determinism on transaction-local
  # rows depends on approximate-index recall, not pool snapshots.)
  if config_env() == :test do
    config :avsa, AVSA.Repo,
      url: url,
      pool: Ecto.Adapters.SQL.Sandbox,
      pool_size: 5
  else
    config :avsa, AVSA.Repo, url: url
  end
end

# AVSA_LLM_STUB=1 selects the in-memory Mock LLM (no network). A real Anthropic
# key always wins: when AVSA_ANTHROPIC_API_KEY is set the stub bypass is disabled
# and the configured real client (AVSA.LLM.Anthropic) is used — a provided key is
# never silently bypassed by the stub. Precedence lives in AVSA.LLM.stub_override/2.
case AVSA.LLM.stub_override(
       System.get_env("AVSA_LLM_STUB"),
       System.get_env("AVSA_ANTHROPIC_API_KEY")
     ) do
  nil -> :ok
  llm_module -> config(:avsa, llm_module: llm_module)
end

# AVSA_MODEL_URL: override the default model service URL used by TextTool for
# /embed_text (text encoding). Defaults to http://localhost:8001 in text_tool.ex
# but stack-up places the real ViT service on :8090.
if model_url = System.get_env("AVSA_MODEL_URL") do
  config :avsa, model_url: model_url
end

# AVSA_BATCHER_URL: override the batcher service URL used by EmbedStep for
# POST /embed (image encoding). Defaults to http://localhost:8081 in embed_step.ex
# but in-cluster the batcher runs at batcher-service:8001.
if batcher_url = System.get_env("AVSA_BATCHER_URL") do
  config :avsa, batcher_url: batcher_url
end

if config_env() == :prod do
  # Cloud SQL (public IP, no private network) requires SSL for connections
  # originating from GCP-hosted clients (pg_hba rejects no-SSL from GCP CIDRs).
  # verify: :verify_none skips CA validation — Cloud SQL uses a self-signed cert
  # and we do not distribute its CA bundle to the release image.
  config :avsa, AVSA.Repo,
    url: System.fetch_env!("DATABASE_URL"),
    ssl: true,
    ssl_opts: [verify: :verify_none],
    pool_size: String.to_integer(System.get_env("POOL_SIZE", "10"))

  config :avsa, AVSAWeb.Endpoint,
    secret_key_base: System.fetch_env!("SECRET_KEY_BASE"),
    url: [host: System.fetch_env!("PHX_HOST"), port: 443, scheme: "https"],
    http: [ip: {0, 0, 0, 0, 0, 0, 0, 0}, port: String.to_integer(System.get_env("PORT", "4000"))]
end
