defmodule ElixirAnthropic.Application do
  @moduledoc """
  OTP Application for the ElixirAnthropic library.

  Starts the `ElixirAnthropic.Finch` HTTP connection pool as the only supervised child.
  """

  use Application

  @impl true
  def start(_type, _args) do
    children = [
      {Finch, name: ElixirAnthropic.Finch}
    ]

    Supervisor.start_link(children, strategy: :one_for_one, name: ElixirAnthropic.Supervisor)
  end
end
