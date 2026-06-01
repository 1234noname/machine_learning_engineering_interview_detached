defmodule AVSA.Verifier do
  @moduledoc """
  GenServer that runs the 5 post-generation checks before a proposed response
  is returned to the caller.

  Checks (in order):
    1. catalog_resolvability — every result ID must exist in catalog.products
    2. pii_filter           — text must not contain detectable PII
    3. factuality           — result fields must match catalog ground truth
    4. injection_pattern    — text must not match prompt-injection regexes
    5. safety               — safety probes in user input must trigger escalation
  """

  use GenServer

  require Logger

  @pii_email ~r/\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b/i
  @pii_phone ~r/\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b/

  # ---------------------------------------------------------------------------
  # Client API
  # ---------------------------------------------------------------------------

  def start_link(_opts \\ []) do
    GenServer.start_link(__MODULE__, %{}, name: __MODULE__)
  end

  @doc """
  Run all 6 verifier checks against `proposed_response`.

  Returns `{:ok, proposed_response}` if all checks pass, or
  `{:error, check_name, reason}` on the first failing check.
  """
  def check(conversation_id, proposed_response) do
    GenServer.call(__MODULE__, {:check, conversation_id, proposed_response})
  end

  @doc """
  Pre-screen a single piece of inbound text at a trust boundary.

  Used by `AVSA.MCP.Server` (the **external HTTP** tool surface) to screen
  incoming `tools/call` text args BEFORE dispatching to a tool — especially the
  LLM-invoking `extract_attributes`. It runs ONLY the injection_pattern + safety
  checks (the two inbound-text checks); it does NOT touch the DB-dependent
  catalog/factuality checks or the pii_filter (which is for *outbound*
  response text).

  Inbound text cannot carry an escalate/refuse flag, so a safety-probe match is
  always a rejection at the boundary — the correct behaviour for an external
  caller.

  Returns `:ok` when the text is clean (incl. `nil`/`""`), or
  `{:error, :injection_pattern | :safety, reason}` on the first failing check.
  """
  @spec screen_input(String.t() | nil) ::
          :ok | {:error, :injection_pattern | :safety, String.t()}
  def screen_input(text) when is_nil(text) or text == "", do: :ok

  def screen_input(text) when is_binary(text) do
    GenServer.call(__MODULE__, {:screen_input, text})
  end

  # ---------------------------------------------------------------------------
  # Server callbacks
  # ---------------------------------------------------------------------------

  @impl GenServer
  def init(_opts) do
    specs_root = Application.get_env(:avsa, :specs_root)

    injection_patterns =
      load_regexes(Path.join([specs_root, "verifier", "injection_corpus.txt"]))

    safety_patterns =
      load_regexes(Path.join([specs_root, "verifier", "safety_probes.txt"]))

    pii_threshold = Application.get_env(:avsa, :pii_threshold, 0)

    state = %{
      injection_patterns: injection_patterns,
      safety_patterns: safety_patterns,
      pii_threshold: pii_threshold
    }

    {:ok, state}
  end

  @impl GenServer
  def handle_call({:check, conversation_id, proposed_response}, _from, state) do
    result = run_checks(conversation_id, proposed_response, state)
    {:reply, result, state}
  end

  def handle_call({:screen_input, text}, _from, state) do
    candidate = %{text: text, user_input: text}

    result =
      with :ok <- check_injection_pattern(candidate, "mcp-boundary", state),
           :ok <- check_safety(candidate, "mcp-boundary", state) do
        :ok
      end

    {:reply, result, state}
  end

  # ---------------------------------------------------------------------------
  # Private — check runner
  # ---------------------------------------------------------------------------

  defp run_checks(conversation_id, proposed_response, state) do
    checks = [
      {:catalog_resolvability, &check_catalog_resolvability/3},
      {:pii_filter, &check_pii_filter/3},
      {:factuality, &check_factuality/3},
      {:injection_pattern, &check_injection_pattern/3},
      {:safety, &check_safety/3}
    ]

    Enum.reduce_while(checks, {:ok, proposed_response}, fn {name, fun}, _acc ->
      result = fun.(proposed_response, conversation_id, state)

      case result do
        :ok ->
          emit_telemetry(name, :pass, conversation_id)
          {:cont, {:ok, proposed_response}}

        {:error, ^name, _reason} = err ->
          emit_telemetry(name, :fail, conversation_id)
          {:halt, err}

        {:error, _other_name, _reason} = err ->
          emit_telemetry(name, :fail, conversation_id)
          {:halt, err}
      end
    end)
  end

  defp emit_telemetry(name, outcome, conversation_id) do
    :telemetry.execute(
      [:avsa, :orch, :verifier, :check],
      %{outcome: outcome},
      %{name: name, outcome: outcome, conversation_id: conversation_id}
    )
  end

  # ---------------------------------------------------------------------------
  # Check 1 — catalog_resolvability
  # ---------------------------------------------------------------------------

  defp check_catalog_resolvability(proposed_response, _conversation_id, _state) do
    if repo_started?() do
      results = Map.get(proposed_response, :results, [])

      Enum.reduce_while(results, :ok, fn result, _acc ->
        id_param = uuid_param(result.id)

        case Ecto.Adapters.SQL.query(
               AVSA.Repo,
               "SELECT 1 FROM catalog.products WHERE id = $1",
               [id_param]
             ) do
          {:ok, %{rows: []}} ->
            {:halt, {:error, :catalog_resolvability, "product #{result.id} not found in catalog"}}

          {:ok, _} ->
            {:cont, :ok}

          {:error, reason} ->
            {:halt, {:error, :catalog_resolvability, "DB error: #{inspect(reason)}"}}
        end
      end)
    else
      :ok
    end
  end

  # ---------------------------------------------------------------------------
  # Check 2 — pii_filter
  # ---------------------------------------------------------------------------

  defp check_pii_filter(proposed_response, _conversation_id, state) do
    text = Map.get(proposed_response, :text, "")

    email_matches = length(Regex.scan(@pii_email, text))
    phone_matches = length(Regex.scan(@pii_phone, text))
    count = email_matches + phone_matches

    if count > state.pii_threshold do
      {:error, :pii_filter, "#{count} PII match(es) found"}
    else
      :ok
    end
  end

  # ---------------------------------------------------------------------------
  # Check 3 — factuality
  # ---------------------------------------------------------------------------

  defp check_factuality(proposed_response, _conversation_id, _state) do
    if repo_started?() do
      results = Map.get(proposed_response, :results, [])

      Enum.reduce_while(results, :ok, fn result, _acc ->
        %AVSA.ProductResult{
          id: id,
          price_cents: price_cents,
          category: category,
          title: title
        } = result

        case Ecto.Adapters.SQL.query(
               AVSA.Repo,
               "SELECT price_cents, category, title FROM catalog.products WHERE id = $1",
               [uuid_param(id)]
             ) do
          {:ok, %{rows: []}} ->
            {:halt, {:error, :factuality, "product #{id} not in catalog"}}

          {:ok, %{rows: [[row_price_cents, row_category, row_title]]}} ->
            cond do
              String.downcase(row_category) != String.downcase(category) ->
                {:halt, {:error, :factuality, "category mismatch for #{id}"}}

              String.downcase(row_title) != String.downcase(title) ->
                {:halt, {:error, :factuality, "title mismatch for #{id}"}}

              row_price_cents != price_cents ->
                {:halt, {:error, :factuality, "price_cents mismatch for #{id}"}}

              true ->
                {:cont, :ok}
            end

          {:error, reason} ->
            {:halt, {:error, :factuality, "DB error: #{inspect(reason)}"}}
        end
      end)
    else
      :ok
    end
  end

  # ---------------------------------------------------------------------------
  # Check 4 — injection_pattern
  # ---------------------------------------------------------------------------

  defp check_injection_pattern(proposed_response, _conversation_id, state) do
    text = Map.get(proposed_response, :text, "")

    matching =
      Enum.find(state.injection_patterns, fn pattern ->
        Regex.match?(pattern, text)
      end)

    if matching do
      {:error, :injection_pattern, "injection pattern matched"}
    else
      :ok
    end
  end

  # ---------------------------------------------------------------------------
  # Check 5 — safety
  # ---------------------------------------------------------------------------

  defp check_safety(proposed_response, _conversation_id, state) do
    user_input = Map.get(proposed_response, :user_input, "")

    matching =
      Enum.find(state.safety_patterns, fn pattern ->
        Regex.match?(pattern, user_input)
      end)

    if matching do
      escalated = Map.get(proposed_response, :escalate, false)
      refused = Map.get(proposed_response, :refuse, false)

      if escalated or refused do
        :ok
      else
        {:error, :safety, "safety probe matched without escalation"}
      end
    else
      :ok
    end
  end

  # ---------------------------------------------------------------------------
  # Helpers
  # ---------------------------------------------------------------------------

  defp load_regexes(path) do
    path
    |> File.read!()
    |> String.split("\n")
    |> Enum.reject(&(String.starts_with?(&1, "#") or String.trim(&1) == ""))
    |> Enum.map(&Regex.compile!(&1, []))
  end

  defp repo_started? do
    case Process.whereis(AVSA.Repo) do
      nil -> false
      _pid -> true
    end
  end

  defp uuid_param(id) when is_binary(id) and byte_size(id) == 16, do: id

  defp uuid_param(id) when is_binary(id) do
    case Ecto.UUID.dump(id) do
      {:ok, bin} -> bin
      :error -> id
    end
  end
end
