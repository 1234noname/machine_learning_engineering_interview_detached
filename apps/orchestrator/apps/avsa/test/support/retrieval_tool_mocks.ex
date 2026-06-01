defmodule AVSA.StubRetrievalTool do
  @moduledoc false
  # Returns two deterministic fake ProductResult rows from different categories
  # so the product_result emission path can be exercised without a live DB.
  def call(_embedding, _attrs) do
    results = [
      %AVSA.ProductResult{
        id: "a0000000-0000-0000-0000-000000000001",
        title: "Red Sundress",
        category: "dress",
        price_cents: 4999,
        score: 0.92,
        image_url: "/images/sundress-001"
      },
      %AVSA.ProductResult{
        id: "a0000000-0000-0000-0000-000000000002",
        title: "Floral Skirt",
        category: "skirt",
        price_cents: 2999,
        score: 0.87,
        image_url: "/images/skirt-002"
      }
    ]

    {:ok, results}
  end

  def call_text(_embedding, _attrs), do: call(nil, nil)
end

defmodule AVSA.TrackingEmbedStep do
  @moduledoc false
  # Records whether call/1 was invoked — used to verify modality routing.
  # Start via: Agent.start(fn -> false end, name: __MODULE__)

  def reset, do: Agent.update(__MODULE__, fn _ -> false end)
  def called?, do: Agent.get(__MODULE__, & &1)

  def call(_image_bytes) do
    Agent.update(__MODULE__, fn _ -> true end)
    {:ok, %{embedding: List.duplicate(0.5, 768), attributes: nil}}
  end
end

defmodule AVSA.AttrCapturingRetrievalTool do
  @moduledoc false
  # Captures attrs passed to call/2 and call_text/2 — used to verify prior context propagation.
  # Start via: Agent.start(fn -> [] end, name: __MODULE__)

  def captured_attrs, do: Agent.get(__MODULE__, & &1)
  def reset, do: Agent.update(__MODULE__, fn _ -> [] end)

  def call(embedding, attrs) do
    Agent.update(__MODULE__, fn acc -> acc ++ [attrs] end)
    AVSA.StubRetrievalTool.call(embedding, attrs)
  end

  def call_text(embedding, attrs) do
    Agent.update(__MODULE__, fn acc -> acc ++ [attrs] end)
    AVSA.StubRetrievalTool.call_text(embedding, attrs)
  end
end

defmodule AVSA.TrackingTextTool do
  @moduledoc false
  # Records whether call/1 was invoked — used to verify modality routing.
  # Start via: Agent.start(fn -> false end, name: __MODULE__)

  def reset, do: Agent.update(__MODULE__, fn _ -> false end)
  def called?, do: Agent.get(__MODULE__, & &1)

  def call(_text) do
    Agent.update(__MODULE__, fn _ -> true end)
    {:ok, List.duplicate(0.3, 512)}
  end
end
