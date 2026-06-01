defmodule AVSA.MCP.CapturingRetrievalTool do
  @moduledoc false
  # Deterministic retrieval stub that captures the embedding + attrs passed to
  # call/2 (image kNN) and call_text/2 (text kNN), so MCP.Tools tests can assert
  # modality routing and constraint propagation without a live DB.
  use Agent

  def start do
    case Agent.start(fn -> %{image: nil, text: nil} end, name: __MODULE__) do
      {:ok, _} -> :ok
      {:error, {:already_started, _}} -> Agent.update(__MODULE__, fn _ -> %{image: nil, text: nil} end)
    end
  end

  def last_image_call, do: Agent.get(__MODULE__, & &1.image)
  def last_text_call, do: Agent.get(__MODULE__, & &1.text)

  def call(embedding, attrs) do
    Agent.update(__MODULE__, fn s -> %{s | image: {embedding, attrs}} end)
    {:ok, results()}
  end

  def call_text(embedding, attrs) do
    Agent.update(__MODULE__, fn s -> %{s | text: {embedding, attrs}} end)
    {:ok, results()}
  end

  defp results do
    [
      %AVSA.ProductResult{
        # Binary UUID — matches what Postgrex returns from catalog.products (the
        # real producer). A string id here would mask the to_card Jason-encode
        # contract: to_string/1 on a 16-byte binary yields invalid UTF-8 that
        # crashes the external HTTP wire. The wire result_id round-trips back to
        # this string via Ecto.UUID.cast!.
        id: Ecto.UUID.dump!("b0000000-0000-0000-0000-000000000001"),
        title: "Red Sundress",
        category: "dress",
        price_cents: 4999,
        score: 0.91,
        image_url: "/images/sundress-001"
      }
    ]
  end
end

defmodule AVSA.MCP.CountingEmbedStep do
  @moduledoc false
  # Counts how many times the (expensive) ViT forward ran. Used to prove the
  # one-forward-per-turn invariant of the embed cache.
  use Agent

  def start do
    case Agent.start(fn -> 0 end, name: __MODULE__) do
      {:ok, _} -> :ok
      {:error, {:already_started, _}} -> Agent.update(__MODULE__, fn _ -> 0 end)
    end
  end

  def count, do: Agent.get(__MODULE__, & &1)

  def call(_image_bytes) do
    Agent.update(__MODULE__, &(&1 + 1))
    {:ok, %{embedding: List.duplicate(0.5, 768), attributes: nil}}
  end
end

defmodule AVSA.MCP.CountingAttributeTool do
  @moduledoc false
  # Counts calls to the attribute tool (the LLM-invoking tool) so the MCP server
  # tests can prove the external Verifier pre-check halts BEFORE the LLM is
  # reached on a rejected text arg.
  use Agent

  def start do
    case Agent.start(fn -> 0 end, name: __MODULE__) do
      {:ok, _} -> :ok
      {:error, {:already_started, _}} -> Agent.update(__MODULE__, fn _ -> 0 end)
    end
  end

  def count, do: Agent.get(__MODULE__, & &1)

  def call(_image_description, _user_text, _vit_attributes) do
    Agent.update(__MODULE__, &(&1 + 1))
    {:ok, %{"category" => "dress", "colour" => "red", "formality" => "casual", "occasion" => "everyday"}}
  end
end

defmodule AVSA.MCP.CountingTextTool do
  @moduledoc false
  # Counts text-encoder forwards; returns a fixed 512-dim text embedding.
  use Agent

  def start do
    case Agent.start(fn -> 0 end, name: __MODULE__) do
      {:ok, _} -> :ok
      {:error, {:already_started, _}} -> Agent.update(__MODULE__, fn _ -> 0 end)
    end
  end

  def count, do: Agent.get(__MODULE__, & &1)

  def call(_text) do
    Agent.update(__MODULE__, &(&1 + 1))
    {:ok, List.duplicate(0.3, 512)}
  end
end
