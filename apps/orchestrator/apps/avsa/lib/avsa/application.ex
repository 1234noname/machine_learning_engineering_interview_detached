defmodule AVSA.Application do
  @moduledoc false
  use Application

  require Logger

  @impl true
  def start(_type, _args) do
    AVSA.TomlConfig.apply_overlay()

   :fuse.install(:batcher_circuit, {{:standard, 5, 10_000}, {:reset, 60_000}})
    :fuse.install(:anthropic_circuit, {{:standard, 3, 10_000}, {:reset, 60_000}})
    :fuse.install(:text_encoder_circuit, {{:standard, 5, 10_000}, {:reset, 60_000}})

    children =
      [
        {Finch, name: :avsa_batcher_pool},
        {Finch,
         name: :avsa_model_pool,
         pools: %{
           Application.get_env(:avsa, :model_url, "http://localhost:8001") => [size: 4]
         }},
        {Registry, keys: :unique, name: AVSA.ConversationRegistry},
        AVSA.ConversationSupervisor,
        AVSA.LLM.Mock,
        AVSA.EmbedCache,
        AVSA.CircuitMonitor,
        AVSA.AttributeTool,
        AVSA.RetrievalTool,
        AVSA.Verifier,
        {TelemetryMetricsPrometheus.Core,
         [metrics: AVSA.Telemetry.metrics(), name: :avsa_prometheus]}
      ] ++
        repo_children() ++
        grpc_children() ++
        metrics_children() ++
        mcp_children()

    Supervisor.start_link(children, strategy: :one_for_one, name: AVSA.Supervisor)
  end

  defp repo_children do
    if Application.get_env(:avsa, :start_repo, true) do
      [AVSA.Repo]
    else
      []
    end
  end

  defp grpc_children do
    if Application.get_env(:avsa, :start_grpc, true) do
      port = Application.get_env(:avsa, :grpc_port, 50051)

      [
        {GRPC.Server.Supervisor, endpoint: AVSA.GrpcEndpoint, port: port, start_server: true}
      ]
    else
      []
    end
  end

  defp metrics_children do
    if Application.get_env(:avsa, :start_metrics, true) do
      port = Application.get_env(:avsa, :metrics_port, 9568)
      [{Plug.Cowboy, scheme: :http, plug: AVSA.MetricsPlug, options: [port: port]}]
    else
      []
    end
  end

  @doc """
  Build the `Plug.Cowboy` child for the external MCP HTTP server.

  Returns `[]` when `:start_mcp` is false (the test/CI default — a foreground
  Plug.Cowboy listener must never block the suite). When `:start_mcp` is true it
  returns one `Plug.Cowboy` child serving `AVSA.MCP.Server` on `:mcp_port`.

  ## Exposed-without-key boot guard

  The MCP server is AVSA's publicly-routable external tool surface. When it would
  be **exposed** off-localhost — `config_env() == :prod`, or the explicit
  `AVSA_MCP_EXPOSED=1` escape hatch (`:mcp_exposed` config) — AND no
  `:mcp_api_key` is set, the auth check is open: an unauthenticated tool endpoint
  reachable from the network. That is refused at boot (we raise) so it can never
  happen by accident. Local/dev with no key is acceptable (the listener binds
  loopback only), so the guard fires solely on the exposed-without-key case.

  Public so the mount decision + guard are unit-testable without opening a
  socket.
  """
  @spec mcp_children() :: [tuple()]
  def mcp_children do
    if Application.get_env(:avsa, :start_mcp, false) do
      guard_mcp_exposure!()
      port = Application.get_env(:avsa, :mcp_port, 8082)
      [{Plug.Cowboy, scheme: :http, plug: AVSA.MCP.Server, options: [port: port]}]
    else
      []
    end
  end

  @spec guard_mcp_exposure!() :: :ok
  defp guard_mcp_exposure! do
    env = Application.get_env(:avsa, :config_env, :prod)
    exposed? = env == :prod or Application.get_env(:avsa, :mcp_exposed, false)
    key = Application.get_env(:avsa, :mcp_api_key)

    if exposed? and (is_nil(key) or key == "") do
      raise """
      Refusing to start the MCP server: it would be EXPOSED off-localhost \
      (config_env=#{inspect(env)}, mcp_exposed=#{inspect(Application.get_env(:avsa, :mcp_exposed))}) \
      with NO :mcp_api_key set, leaving an unauthenticated public tool endpoint. \
      Set AVSA_MCP_API_KEY, or unset AVSA_MCP_EXPOSED for a localhost-only dev server.
      """
    end

    :ok
  end
end
