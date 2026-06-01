defmodule AVSA.StubEmbedStep do
  @moduledoc false
  # Returns a fixed 768-dim embedding with nil ViT attributes — fast and
  # deterministic for unit tests. Matches the EmbedStep return shape
  # `{:ok, %{embedding: [...], attributes: map() | nil}}`.
  def call(_image_bytes), do: {:ok, %{embedding: List.duplicate(0.5, 768), attributes: nil}}
end

defmodule AVSA.FailingEmbedStep do
  @moduledoc false
  # Always returns an error — used to test the embed-failure path.
  def call(_image_bytes), do: {:error, :embed_failed}
end

defmodule AVSA.VitAttrEmbedStep do
  @moduledoc false
  # Returns a fixed 768-dim embedding PLUS a populated ViT attribute head map
  # (category/colour + confidences) — exercises the ViT-offload path where
  # extract_attributes sources category/colour from the head.
  def call(_image_bytes) do
    {:ok,
     %{
       embedding: List.duplicate(0.5, 768),
       attributes: %{
         "category" => "skirt",
         "colour" => "navy",
         "category_confidence" => 0.95,
         "colour_confidence" => 0.88
       }
     }}
  end
end

defmodule AVSA.StubTextTool do
  @moduledoc false
  # Returns a fixed 512-dim text embedding — fast and deterministic for unit tests.
  def call(_text), do: {:ok, List.duplicate(0.3, 512)}
end
