defmodule AVSA.MCP.ImageResolver do
  @moduledoc """
  Resolves the image argument of an image-native MCP tool to raw bytes.

  The image-native tools (`AVSA.MCP.Tools`) accept an image in one of two
  transports, supporting both the internal chat flow and an external Inspector
  call cleanly:

    * `"image_b64"` — base64-encoded image bytes inline. This is the primary
      transport: the internal chat flow already holds the uploaded bytes
      (`StartConversationRequest.image_bytes`), and an external MCP client (the
      Inspector, Claude Desktop) can trivially base64 a local file. Resolved
      with zero I/O.

    * `"image_ref"` — a storage key the tool resolves via the storage backend
      (local-only in this branch; project memory: AVSA runs locally). The ref is
      resolved by the configured resolver function (`:avsa, :image_ref_resolver`)
      which defaults to reading the key under the local storage root. The key is
      treated as opaque and path-traversal is rejected.

  Returns `{:ok, bytes}` or `{:error, reason}` — never raises. A pre-computed
  embedding is intentionally NOT a supported transport: the tools are
  image-native and embed internally; a 768-d vector argument is rejected
  upstream in `AVSA.MCP.Tools`.
  """

  @type args :: %{optional(String.t()) => term()}

  @doc """
  Resolve the image bytes from a tool argument map.

  Precedence: `image_b64` (inline) wins over `image_ref` (storage). Returns
  `{:error, :no_image}` when neither is present, `{:error, :bad_base64}` on a
  malformed `image_b64`, or `{:error, reason}` when the ref resolver fails.
  """
  @spec resolve(args()) :: {:ok, binary()} | {:error, term()}
  def resolve(args) when is_map(args) do
    cond do
      is_binary(Map.get(args, "image_b64")) ->
        decode_b64(Map.get(args, "image_b64"))

      is_binary(Map.get(args, "image_ref")) ->
        resolve_ref(Map.get(args, "image_ref"))

      true ->
        {:error, :no_image}
    end
  end

  @doc """
  Resolve ALL image bytes from a tool argument map (multi-image).

  Precedence: a non-empty `"image_b64_list"` (list of base64 strings) resolves to
  every image; otherwise it falls back to the single-image `resolve/1`
  (`image_b64` / `image_ref`) wrapped in a one-element list. Returns
  `{:ok, [bytes]}` (order preserved), or the first decode/resolve `{:error, …}`.
  """
  @spec resolve_all(args()) :: {:ok, [binary()]} | {:error, term()}
  def resolve_all(args) when is_map(args) do
    case Map.get(args, "image_b64_list") do
      list when is_list(list) and list != [] ->
        decode_all(list, [])

      _ ->
        case resolve(args) do
          {:ok, bytes} -> {:ok, [bytes]}
          {:error, reason} -> {:error, reason}
        end
    end
  end

  @spec decode_all([term()], [binary()]) :: {:ok, [binary()]} | {:error, term()}
  defp decode_all([], acc), do: {:ok, Enum.reverse(acc)}

  defp decode_all([b64 | rest], acc) when is_binary(b64) do
    case decode_b64(b64) do
      {:ok, bytes} -> decode_all(rest, [bytes | acc])
      {:error, reason} -> {:error, reason}
    end
  end

  defp decode_all([_non_binary | _rest], _acc), do: {:error, :bad_base64}

  @spec decode_b64(String.t()) :: {:ok, binary()} | {:error, :bad_base64}
  defp decode_b64(b64) do
    case Base.decode64(b64) do
      {:ok, bytes} -> {:ok, bytes}
      :error -> {:error, :bad_base64}
    end
  end

  @spec resolve_ref(String.t()) :: {:ok, binary()} | {:error, term()}
  defp resolve_ref(ref) do
    resolver = Application.get_env(:avsa, :image_ref_resolver, &__MODULE__.default_local_resolver/1)
    resolver.(ref)
  end

  @doc """
  Default local-storage resolver: read `ref` as a path under the configured
  local storage root (`[storage.local] root_path`, default `./data`). Rejects
  path traversal so a ref can never escape the storage root.
  """
  @spec default_local_resolver(String.t()) :: {:ok, binary()} | {:error, term()}
  def default_local_resolver(ref) do
    root = Application.get_env(:avsa, :storage_local_root, "./data")
    safe_root = Path.expand(root)
    candidate = Path.expand(Path.join(safe_root, ref))

    if String.starts_with?(candidate, safe_root <> "/") or candidate == safe_root do
      case File.read(candidate) do
        {:ok, bytes} -> {:ok, bytes}
        {:error, reason} -> {:error, {:image_ref_unreadable, reason}}
      end
    else
      {:error, :image_ref_traversal}
    end
  end
end
