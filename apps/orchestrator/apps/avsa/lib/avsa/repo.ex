defmodule AVSA.Repo do
  use Ecto.Repo,
    otp_app: :avsa,
    adapter: Ecto.Adapters.Postgres
end
