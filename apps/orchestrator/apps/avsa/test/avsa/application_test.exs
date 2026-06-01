defmodule AVSA.ApplicationTest do
  @moduledoc """
  Tests for the MCP listener mount decision in `AVSA.Application`.

  The MCP HTTP server is AVSA's external tool surface. These pin:

    * the `start_mcp` flag gates whether the `Plug.Cowboy` child is built;
    * the **exposed-without-key guard** raises on boot when the listener would
      be reachable off-localhost (`config_env() == :prod`, or
      `AVSA_MCP_EXPOSED=1`) AND no `:mcp_api_key` is set — so an accidentally
      unauthed *exposed* server can never start;
    * local/dev with no key is allowed (open-on-localhost is acceptable).

  We test the pure `mcp_children/0` builder directly so no socket is opened.
  """

  use ExUnit.Case, async: false

  setup do
    original_start = Application.get_env(:avsa, :start_mcp)
    original_key = Application.get_env(:avsa, :mcp_api_key)
    original_exposed = Application.get_env(:avsa, :mcp_exposed)
    original_port = Application.get_env(:avsa, :mcp_port)
    original_env = Application.get_env(:avsa, :config_env)

    # Default these tests to a non-exposed (local) env unless a test opts into
    # the exposed branch via :mcp_exposed / :config_env.
    Application.put_env(:avsa, :config_env, :test)

    on_exit(fn ->
      restore(:start_mcp, original_start)
      restore(:mcp_api_key, original_key)
      restore(:mcp_exposed, original_exposed)
      restore(:mcp_port, original_port)
      restore(:config_env, original_env)
    end)

    :ok
  end

  defp restore(key, nil), do: Application.delete_env(:avsa, key)
  defp restore(key, val), do: Application.put_env(:avsa, key, val)

  describe "start_mcp flag" do
    test "no MCP child is built when start_mcp is false" do
      Application.put_env(:avsa, :start_mcp, false)
      assert AVSA.Application.mcp_children() == []
    end

    test "a Plug.Cowboy child for AVSA.MCP.Server is built when start_mcp is true (local, no key)" do
      Application.put_env(:avsa, :start_mcp, true)
      Application.delete_env(:avsa, :mcp_api_key)
      Application.delete_env(:avsa, :mcp_exposed)
      Application.put_env(:avsa, :mcp_port, 8099)

      assert [{Plug.Cowboy, opts}] = AVSA.Application.mcp_children()
      assert opts[:scheme] == :http
      assert opts[:plug] == AVSA.MCP.Server
      assert opts[:options][:port] == 8099
    end
  end

  describe "exposed-without-key guard" do
    test "raises when exposed (mcp_exposed: true) and no key is set" do
      Application.put_env(:avsa, :start_mcp, true)
      Application.put_env(:avsa, :mcp_exposed, true)
      Application.delete_env(:avsa, :mcp_api_key)

      assert_raise RuntimeError, ~r/mcp_api_key/i, fn ->
        AVSA.Application.mcp_children()
      end
    end

    test "does NOT raise when exposed and a key IS set" do
      Application.put_env(:avsa, :start_mcp, true)
      Application.put_env(:avsa, :mcp_exposed, true)
      Application.put_env(:avsa, :mcp_api_key, "s3cret")

      assert [{Plug.Cowboy, _opts}] = AVSA.Application.mcp_children()
    end

    test "does NOT raise when NOT exposed (local) and no key is set" do
      Application.put_env(:avsa, :start_mcp, true)
      Application.delete_env(:avsa, :mcp_exposed)
      Application.delete_env(:avsa, :mcp_api_key)

      assert [{Plug.Cowboy, _opts}] = AVSA.Application.mcp_children()
    end

    test "an empty-string key counts as no key when exposed (raises)" do
      Application.put_env(:avsa, :start_mcp, true)
      Application.put_env(:avsa, :mcp_exposed, true)
      Application.put_env(:avsa, :mcp_api_key, "")

      assert_raise RuntimeError, ~r/mcp_api_key/i, fn ->
        AVSA.Application.mcp_children()
      end
    end
  end

  describe "supervision tree" do
    # All workers (gRPC channel, LLM client, Repo, MCP server, …) register under
    # AVSA.Supervisor, so a missing PID here means the application failed to
    # start.
    test "AVSA.Supervisor is running" do
      assert is_pid(Process.whereis(AVSA.Supervisor))
    end
  end
end
