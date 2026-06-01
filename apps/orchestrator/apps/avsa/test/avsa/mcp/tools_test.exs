defmodule AVSA.MCP.ToolsTest do
  @moduledoc """
  Tests for AVSA.MCP.Tools — the image-native MCP tool layer.

  The load-bearing contract: `find_similar` and `extract_attributes` take an
  IMAGE (base64 bytes or a storage ref), NOT a pre-computed 768-d embedding.
  Each tool embeds INTERNALLY by reaching L1 (the embed step) through the
  per-request embed cache, so the ViT forward runs ONCE per turn even when both
  tools are called in the same turn.

  These tests prove:
    * find_similar accepts image_b64 (no `embedding` key anywhere).
    * find_similar is modality-aware: an image arg → 768-d image kNN;
      a text arg → 512-d text kNN.
    * extract_attributes accepts an image and returns the four-attr map.
    * One turn calling BOTH tools embeds the image exactly once (cache seam).
    * The colour constraint still flows through find_similar.
    * A find_similar request with NO image and NO text is rejected
      (image-native: there is no vector fallback).
  """

  use ExUnit.Case, async: false

  alias AVSA.MCP.Tools

  setup do
    # Route find_similar's retrieval through a deterministic stub (no DB) and
    # capture the embedding+attrs it receives so we can assert modality + colour constraint.
    original_retrieval = Application.get_env(:avsa, :retrieval_tool_module)
    Application.put_env(:avsa, :retrieval_tool_module, AVSA.MCP.CapturingRetrievalTool)
    AVSA.MCP.CapturingRetrievalTool.start()

    # Route the embed step through a counting stub so we can prove the
    # one-forward-per-turn invariant without a live batcher.
    original_embed = Application.get_env(:avsa, :embed_step_module)
    Application.put_env(:avsa, :embed_step_module, AVSA.MCP.CountingEmbedStep)
    AVSA.MCP.CountingEmbedStep.start()

    original_text = Application.get_env(:avsa, :text_tool_module)
    Application.put_env(:avsa, :text_tool_module, AVSA.MCP.CountingTextTool)
    AVSA.MCP.CountingTextTool.start()

    on_exit(fn ->
      restore(:retrieval_tool_module, original_retrieval)
      restore(:embed_step_module, original_embed)
      restore(:text_tool_module, original_text)
      Agent.update(AVSA.LLM.Mock, fn _ -> nil end)
    end)

    :ok
  end

  defp restore(key, nil), do: Application.delete_env(:avsa, key)
  defp restore(key, val), do: Application.put_env(:avsa, key, val)

  describe "find_similar with an image" do
    test "accepts image_b64 (no embedding argument) and returns catalog results" do
      args = %{
        "image_b64" => Base.encode64(<<1, 2, 3>>),
        "attrs" => %{
          "category" => "dress",
          "colour" => "red",
          "formality" => "casual",
          "occasion" => "everyday"
        }
      }

      assert {:ok, %{results: results}} = Tools.find_similar(args, request_id: "t-img-1")
      assert is_list(results)
      assert length(results) >= 1

      # The retrieval got a 768-dim IMAGE embedding (modality = image).
      {embedding, _attrs} = AVSA.MCP.CapturingRetrievalTool.last_image_call()
      assert length(embedding) == 768
    end

    test "the colour constraint flows through to retrieval" do
      args = %{
        "image_b64" => Base.encode64(<<4, 5, 6>>),
        "attrs" => %{
          "category" => "dress",
          "colour" => "green",
          "formality" => "casual",
          "occasion" => "everyday"
        }
      }

      assert {:ok, _} = Tools.find_similar(args, request_id: "t-img-2")
      {_embedding, attrs} = AVSA.MCP.CapturingRetrievalTool.last_image_call()
      assert attrs["colour"] == "green"
    end

    test "combines multiple images (image_b64_list) into one mean-pooled query" do
      args = %{
        "image_b64_list" => [
          Base.encode64(<<10, 11, 12>>),
          Base.encode64(<<13, 14, 15>>)
        ],
        "attrs" => %{
          "category" => "dress",
          "colour" => "red",
          "formality" => "casual",
          "occasion" => "everyday"
        }
      }

      assert {:ok, %{results: results}} = Tools.find_similar(args, request_id: "t-img-multi")
      assert is_list(results) and length(results) >= 1

      # Both distinct images were embedded (one ViT forward each — distinct
      # content hashes miss the per-turn cache), then mean-pooled into a single
      # 768-dim query vector handed to retrieval.
      assert AVSA.MCP.CountingEmbedStep.count() == 2
      {embedding, _attrs} = AVSA.MCP.CapturingRetrievalTool.last_image_call()
      assert length(embedding) == 768
    end
  end

  describe "find_similar with text (modality-aware)" do
    test "a text arg uses the 512-d text encoder kNN, not the image path" do
      args = %{
        "text" => "a red summer dress",
        "attrs" => %{
          "category" => "dress",
          "colour" => "red",
          "formality" => "casual",
          "occasion" => "everyday"
        }
      }

      assert {:ok, %{results: results}} = Tools.find_similar(args, request_id: "t-txt-1")
      assert is_list(results)

      {text_embedding, _attrs} = AVSA.MCP.CapturingRetrievalTool.last_text_call()
      assert length(text_embedding) == 512
    end
  end

  describe "extract_attributes with an image" do
    test "accepts an image and returns the four-attribute map" do
      AVSA.LLM.Mock.set_response(
        {:ok,
         %AVSA.LLM.ToolUse{
           name: "extract_attributes",
           id: "mcp-ea-1",
           input: %{
             "category" => "jacket",
             "colour" => "navy",
             "formality" => "smart_casual",
             "occasion" => "work"
           }
         }}
      )

      args = %{"image_b64" => Base.encode64(<<7, 8, 9>>), "user_text" => "business casual"}

      assert {:ok, %{attrs: attrs}} = Tools.extract_attributes(args, request_id: "t-ea-1")
      assert Map.has_key?(attrs, "category")
      assert Map.has_key?(attrs, "colour")
      assert Map.has_key?(attrs, "formality")
      assert Map.has_key?(attrs, "occasion")
    end
  end

  describe "one ViT forward per turn (the cache seam)" do
    test "calling find_similar then extract_attributes on the same image embeds once" do
      AVSA.LLM.Mock.set_response(
        {:ok,
         %AVSA.LLM.ToolUse{
           name: "extract_attributes",
           id: "mcp-ea-2",
           input: %{
             "category" => "dress",
             "colour" => "red",
             "formality" => "casual",
             "occasion" => "everyday"
           }
         }}
      )

      image_b64 = Base.encode64(<<42, 42, 42>>)
      request_id = "turn-shared-1"

      assert {:ok, _} =
               Tools.find_similar(
                 %{
                   "image_b64" => image_b64,
                   "attrs" => %{
                     "category" => "dress",
                     "colour" => "red",
                     "formality" => "casual",
                     "occasion" => "everyday"
                   }
                 },
                 request_id: request_id
               )

      assert {:ok, _} =
               Tools.extract_attributes(
                 %{"image_b64" => image_b64, "user_text" => "red dress"},
                 request_id: request_id
               )

      # The ViT forward (CountingEmbedStep) ran EXACTLY once for the two tools.
      assert AVSA.MCP.CountingEmbedStep.count() == 1
    end
  end

  describe "image-native rejection (no vector fallback)" do
    test "find_similar with neither image nor text is an invalid argument" do
      args = %{"attrs" => %{"category" => "dress", "colour" => "red"}}

      assert {:error, {:invalid_argument, msg}} =
               Tools.find_similar(args, request_id: "t-bad-1")

      assert is_binary(msg)
    end

    test "find_similar does NOT accept an embedding argument (image-native only)" do
      # A caller that supplies a pre-computed vector must be rejected for
      # lacking an image/text — the embedding key is ignored, never used as a
      # retrieval shortcut.
      args = %{
        "embedding" => List.duplicate(0.5, 768),
        "attrs" => %{"category" => "dress", "colour" => "red"}
      }

      assert {:error, {:invalid_argument, _}} = Tools.find_similar(args, request_id: "t-bad-2")
    end
  end
end
