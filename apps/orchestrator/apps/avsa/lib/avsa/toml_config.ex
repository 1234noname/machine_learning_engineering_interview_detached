defmodule AVSA.TomlConfig do
  @moduledoc """
  Runtime TOML config overlay loader. Reads config/avsa.{profile}.toml
  (selected via AVSA_PROFILE env var) and deep-merges it onto the base
  config/avsa.toml. Called from AVSA.Application.start/2 before supervision
  tree starts; updates Application env for :avsa keys that exist in the overlay.

  The parser handles a restricted subset of TOML sufficient for avsa.toml:
    - [section] and [section.subsection] headers
    - key = "string", key = 123, key = 123.0, key = true/false
  No arrays-of-tables, inline tables, or multi-line strings are supported.
  """

  require Logger

  # ---------------------------------------------------------------------------
  # Public API
  # ---------------------------------------------------------------------------

  @doc "Load and merge base + profile overlay; apply :avsa Application env overrides."
  def apply_overlay do
    root = find_repo_root()

    {:ok, base} = load_profile(Path.join(root, "config/avsa.toml"))

    overlay =
      case System.get_env("AVSA_PROFILE", "") do
        "" ->
          %{}

        profile ->
          path = Path.join(root, "config/avsa.#{profile}.toml")

          case load_profile(path) do
            {:ok, map} ->
              map

            {:error, reason} ->
              Logger.warning("TomlConfig: could not load profile #{profile}: #{inspect(reason)}")
              %{}
          end
      end

    merged = deep_merge(base, overlay)
    apply_to_app_env(merged)
  end

  @doc "Deep-merge two maps; overlay wins at every level for scalar values."
  def deep_merge(base, overlay) when is_map(base) and is_map(overlay) do
    Map.merge(base, overlay, fn _key, base_val, overlay_val ->
      if is_map(base_val) and is_map(overlay_val) do
        deep_merge(base_val, overlay_val)
      else
        overlay_val
      end
    end)
  end

  @doc "Load a TOML file at path. Returns {:ok, map} or {:ok, %{}} if not found."
  def load_profile(path) do
    case File.read(path) do
      {:ok, content} ->
        {:ok, parse_toml(content)}

      {:error, :enoent} ->
        {:ok, %{}}

      {:error, reason} ->
        {:error, reason}
    end
  end

  @doc "Parse a minimal TOML string. Handles [section], [section.sub], key = value."
  def parse_toml(content) do
    lines = String.split(content, ~r/\r?\n/)

    {result, _section} =
      Enum.reduce(lines, {%{}, []}, fn line, {acc, current_section} ->
        trimmed = String.trim(line)

        cond do
          # blank line or comment
          trimmed == "" or String.starts_with?(trimmed, "#") ->
            {acc, current_section}

          # section header e.g. [db.pool]
          String.starts_with?(trimmed, "[") ->
            section_str =
              trimmed
              |> String.trim_leading("[")
              |> String.trim_trailing("]")
              |> String.trim()

            new_section = String.split(section_str, ".")
            {acc, new_section}

          # key = value
          String.contains?(trimmed, "=") ->
            case parse_key_value(trimmed) do
              {key, value} ->
                path = current_section ++ [key]
                {put_in_path(acc, path, value), current_section}

              nil ->
                {acc, current_section}
            end

          true ->
            {acc, current_section}
        end
      end)

    result
  end

  # ---------------------------------------------------------------------------
  # Private helpers
  # ---------------------------------------------------------------------------

  defp find_repo_root do
    start = Path.dirname(__DIR__)
    find_repo_root(start)
  end

  defp find_repo_root(dir) do
    if File.exists?(Path.join(dir, "config/avsa.toml")) do
      dir
    else
      parent = Path.dirname(dir)

      if parent == dir do
        File.cwd!()
      else
        find_repo_root(parent)
      end
    end
  end

  defp parse_key_value(line) do
    case String.split(line, "=", parts: 2) do
      [raw_key, raw_value] ->
        key = String.trim(raw_key)
        value = parse_value(String.trim(raw_value))
        {key, value}

      _ ->
        nil
    end
  end

  defp parse_value(raw) do
    cond do
      String.starts_with?(raw, "\"") and String.ends_with?(raw, "\"") ->
        raw |> String.trim("\"")

      raw == "true" ->
        true

      raw == "false" ->
        false

      String.contains?(raw, ".") ->
        case Float.parse(raw) do
          {f, ""} -> f
          {f, _rest} -> f
          :error -> raw
        end

      true ->
        case Integer.parse(raw) do
          {i, ""} -> i
          {i, _rest} -> i
          :error -> raw
        end
    end
  end

  defp put_in_path(map, [key], value) do
    Map.put(map, key, value)
  end

  defp put_in_path(map, [key | rest], value) do
    nested = Map.get(map, key, %{})
    Map.put(map, key, put_in_path(nested, rest, value))
  end

  defp apply_to_app_env(merged) do
    apply_db(merged)
    apply_api(merged)
    apply_latency(merged)
    apply_verifier(merged)
    :ok
  end

  defp apply_db(%{"db" => db}) when is_map(db) do
    if url = Map.get(db, "url") do
      Application.put_env(:avsa, :db_url, url)
    end

    if pool_size = get_in(db, ["pool", "size"]) do
      Application.put_env(:avsa, :db_pool_size, pool_size)
    end
  end

  defp apply_db(_), do: :ok

  defp apply_api(%{"api" => api}) when is_map(api) do
    if url = Map.get(api, "batcher_url") do
      # runtime.exs / AVSA_BATCHER_URL takes precedence; only apply the TOML
      # default when the key has not already been set (e.g. by a runtime env var).
      unless Application.get_env(:avsa, :batcher_url) do
        Application.put_env(:avsa, :batcher_url, url)
      end
    end
  end

  defp apply_api(_), do: :ok

  defp apply_latency(%{"latency" => latency}) when is_map(latency) do
    if ms = Map.get(latency, "retrieval_knn_ms") do
      Application.put_env(:avsa, :retrieval_knn_ms, ms)
    end
  end

  defp apply_latency(_), do: :ok

  defp apply_verifier(%{"verifier" => verifier}) when is_map(verifier) do
    if threshold = Map.get(verifier, "pii_threshold") do
      Application.put_env(:avsa, :pii_threshold, threshold)
    end
  end

  defp apply_verifier(_), do: :ok
end
