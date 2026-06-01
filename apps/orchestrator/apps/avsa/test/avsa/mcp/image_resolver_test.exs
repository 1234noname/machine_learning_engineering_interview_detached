defmodule AVSA.MCP.ImageResolverTest do
  @moduledoc """
  Hermetic tests for AVSA.MCP.ImageResolver.

  Pure byte-decoding + a local-file resolver — no DB, no network. The happy
  `image_b64` path is exercised transitively by mcp/tools_test.exs; these pin the
  error + security branches that have no other coverage, in particular the
  path-traversal reject (`:image_ref_traversal`) which guards that an `image_ref`
  can never escape the configured storage root.
  """

  use ExUnit.Case, async: false

  alias AVSA.MCP.ImageResolver

  describe "resolve/1 — image_b64 (inline transport)" do
    test "decodes valid base64 to the original raw bytes" do
      bytes = <<1, 2, 3, 4, 5>>
      assert {:ok, ^bytes} = ImageResolver.resolve(%{"image_b64" => Base.encode64(bytes)})
    end

    test "returns {:error, :bad_base64} on malformed base64" do
      assert {:error, :bad_base64} = ImageResolver.resolve(%{"image_b64" => "not!valid!base64!"})
    end

    test "image_b64 takes precedence over image_ref" do
      bytes = <<9, 9, 9>>
      args = %{"image_b64" => Base.encode64(bytes), "image_ref" => "ignored.jpg"}
      assert {:ok, ^bytes} = ImageResolver.resolve(args)
    end
  end

  describe "resolve/1 — no image present" do
    test "returns {:error, :no_image} when neither image_b64 nor image_ref is present" do
      assert {:error, :no_image} = ImageResolver.resolve(%{})
      assert {:error, :no_image} = ImageResolver.resolve(%{"user_text" => "hello"})
    end
  end

  describe "resolve/1 — image_ref (default local-storage resolver)" do
    setup do
      root = Path.join(System.tmp_dir!(), "avsa-img-resolver-#{System.unique_integer([:positive])}")
      File.mkdir_p!(root)
      prev = Application.get_env(:avsa, :storage_local_root)
      Application.put_env(:avsa, :storage_local_root, root)

      on_exit(fn ->
        File.rm_rf!(root)

        if prev,
          do: Application.put_env(:avsa, :storage_local_root, prev),
          else: Application.delete_env(:avsa, :storage_local_root)
      end)

      {:ok, root: root}
    end

    test "reads a ref located under the storage root", %{root: root} do
      bytes = <<7, 7, 7, 7>>
      File.write!(Path.join(root, "photo.jpg"), bytes)
      assert {:ok, ^bytes} = ImageResolver.resolve(%{"image_ref" => "photo.jpg"})
    end

    test "reads a ref in a nested directory under the root", %{root: root} do
      bytes = <<8, 8>>
      File.mkdir_p!(Path.join(root, "nested/dir"))
      File.write!(Path.join(root, "nested/dir/img.png"), bytes)
      assert {:ok, ^bytes} = ImageResolver.resolve(%{"image_ref" => "nested/dir/img.png"})
    end

    test "rejects a path-traversal ref that would escape the storage root" do
      assert {:error, :image_ref_traversal} =
               ImageResolver.resolve(%{"image_ref" => "../../etc/passwd"})
    end

    test "contains an absolute ref under the storage root (never reads the real file)" do
      # Path.join folds an absolute ref under the root, so "/etc/passwd" resolves
      # to <root>/etc/passwd (absent → unreadable) rather than the real system
      # file. Containment, not a traversal reject — but the escape is still
      # prevented: the resolver never returns the real /etc/passwd bytes.
      assert {:error, {:image_ref_unreadable, :enoent}} =
               ImageResolver.resolve(%{"image_ref" => "/etc/passwd"})
    end

    test "wraps a read failure for a missing ref under the root" do
      assert {:error, {:image_ref_unreadable, :enoent}} =
               ImageResolver.resolve(%{"image_ref" => "does-not-exist.jpg"})
    end
  end

  describe "resolve_all/1 — multi-image" do
    test "decodes a non-empty image_b64_list preserving order" do
      a = <<1>>
      b = <<2, 2>>
      c = <<3, 3, 3>>
      args = %{"image_b64_list" => Enum.map([a, b, c], &Base.encode64/1)}
      assert {:ok, [^a, ^b, ^c]} = ImageResolver.resolve_all(args)
    end

    test "falls back to single-image resolve when no list is given" do
      bytes = <<4, 5, 6>>
      assert {:ok, [^bytes]} = ImageResolver.resolve_all(%{"image_b64" => Base.encode64(bytes)})
    end

    test "returns {:error, :bad_base64} when a list element is not a base64 string" do
      args = %{"image_b64_list" => [Base.encode64(<<1>>), 123]}
      assert {:error, :bad_base64} = ImageResolver.resolve_all(args)
    end

    test "returns the first decode error in the list" do
      args = %{"image_b64_list" => ["!!!not-base64!!!", Base.encode64(<<1>>)]}
      assert {:error, :bad_base64} = ImageResolver.resolve_all(args)
    end
  end
end
