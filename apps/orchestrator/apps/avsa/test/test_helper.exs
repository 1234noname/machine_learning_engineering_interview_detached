defmodule AVSA.RepoTestHelper do
  @moduledoc false

  @doc """
  Start `AVSA.Repo` ONCE for the whole test run so it OUTLIVES any transient
  caller (e.g. a `setup_all` process) and is available to every `:integration`
  module's per-test Sandbox checkout.

  `Repo.start_link/1` links the Repo to the calling process. When called from a
  short-lived process (a module `setup_all`), ExUnit tears that process down at
  module teardown — which then kills the linked Repo for every sibling
  `:integration` module that already saw `{:error, {:already_started, _}}` and
  reused it, producing `DBConnection.Holder.checkout … no process` failures.
  Starting it here from the long-lived test_helper process (and unlinking for
  safety) keeps the Repo alive for the whole run (the BEAM reclaims it at VM
  exit).

  Idempotent: a second call returns `:ok` without restarting.
  """
  @spec ensure_started!() :: :ok
  def ensure_started! do
    case AVSA.Repo.start_link([]) do
      {:ok, pid} ->
        Process.unlink(pid)
        :ok

      {:error, {:already_started, _}} ->
        :ok
    end
  end

  @doc """
  Per-`:integration`-test setup: check a Sandbox connection out and make it
  SHARED, then guarantee deterministic kNN recall for transaction-local rows.

  ## Why SHARED mode

  The kNN/verifier queries run through long-lived, *named* app GenServers
  (`AVSA.RetrievalTool`, `AVSA.Verifier`) — not the test process. They must use
  the test's checked-out connection so they see the rows the test just seeded.
  We use `{:shared, self()}` rather than per-process `allow/3`: those GenServers
  are persistent singletons reused across every test, and a stale `allow` from a
  prior (checked-in) connection contaminates the next test (variable low counts,
  intermittent `DBConnection.OwnershipError`). Shared mode routes *every* process
  to the owner's connection and is the supported pattern for an `async: false`
  suite — all `:integration` tests here are serial.

  ## Why `SET LOCAL hnsw.ef_search`

  `catalog.products.embedding` is indexed with an **HNSW** ANN index. HNSW is
  *approximate*: with the default `ef_search` (40), an `ORDER BY embedding <=> $1
  LIMIT 20` over rows inserted *inside the current (uncommitted) transaction*
  returns FEWER than 20 — and a non-deterministic count (observed 2/5/7/10). The
  rows are present (a plain SELECT sees them) but the index graph traversal of
  transaction-local entries under-recalls. This — NOT an MVCC/pool-snapshot
  effect — was the true source of the flaky `length(results) == 20` failures.
  `SET LOCAL` raises `ef_search` for the duration of the Sandbox transaction
  (auto-reset on rollback, no leak to other tests), making recall exhaustive
  enough that the kNN deterministically returns the full `LIMIT`. Production is
  unaffected: it reads a large, long-committed catalog whose index is fully built.
  """
  @spec checkout_shared!() :: :ok
  def checkout_shared! do
    :ok = Ecto.Adapters.SQL.Sandbox.checkout(AVSA.Repo)
    Ecto.Adapters.SQL.Sandbox.mode(AVSA.Repo, {:shared, self()})
    Ecto.Adapters.SQL.query!(AVSA.Repo, "SET LOCAL hnsw.ef_search = 1000", [])
    :ok
  end
end

# When DATABASE_URL is set, runtime.exs flips `start_repo: true` and configures
# AVSA.Repo with `pool: Ecto.Adapters.SQL.Sandbox`. Start the Repo once here and
# put the Sandbox into :manual mode. Each `:integration` test then calls
# `AVSA.RepoTestHelper.checkout_shared!/0` in its `setup` to own a transaction
# (rolled back at teardown) shared with the app's DB-querying GenServers.
#
# In the hermetic path (no DATABASE_URL → `start_repo: false`), the Repo is NOT
# started and Sandbox is never engaged — unit tests run exactly as before.
if Application.get_env(:avsa, :start_repo, false) do
  :ok = AVSA.RepoTestHelper.ensure_started!()
  Ecto.Adapters.SQL.Sandbox.mode(AVSA.Repo, :manual)
end

ExUnit.start()
ExUnit.configure(exclude: [:integration])
