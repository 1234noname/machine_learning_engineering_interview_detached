defmodule AVSA.TomlConfigTest do
  use ExUnit.Case, async: false

  describe "load_profile/1" do
    test "load_profile/1 returns {:ok, map} for existing profile" do
      tmp = System.tmp_dir!()
      path = Path.join(tmp, "avsa_test_#{System.unique_integer([:positive])}.toml")

      File.write!(path, """
      [latency]
      retrieval_knn_ms = 150

      [db]
      url = "postgresql://localhost/avsa"
      """)

      try do
        assert {:ok, map} = AVSA.TomlConfig.load_profile(path)
        assert get_in(map, ["latency", "retrieval_knn_ms"]) == 150
        assert get_in(map, ["db", "url"]) == "postgresql://localhost/avsa"
      after
        File.rm(path)
      end
    end

    test "load_profile/1 returns {:ok, %{}} when file does not exist" do
      path = "/tmp/avsa_nonexistent_#{System.unique_integer([:positive])}.toml"
      assert {:ok, %{}} = AVSA.TomlConfig.load_profile(path)
    end
  end

  describe "deep_merge/2" do
    test "deep_merge/2 overlay keys win" do
      assert %{a: 1, b: 99} = AVSA.TomlConfig.deep_merge(%{a: 1, b: 2}, %{b: 99})
    end

    test "deep_merge/2 merges nested maps recursively" do
      base = %{x: %{a: 1, b: 2}}
      overlay = %{x: %{b: 99}}
      assert %{x: %{a: 1, b: 99}} = AVSA.TomlConfig.deep_merge(base, overlay)
    end
  end

  describe "parse_toml/1" do
    test "parse_toml/1 parses section and scalar values" do
      content = """
      [server]
      host = "localhost"
      port = 4000
      debug = true

      [server.pool]
      size = 10
      """

      result = AVSA.TomlConfig.parse_toml(content)
      assert get_in(result, ["server", "host"]) == "localhost"
      assert get_in(result, ["server", "port"]) == 4000
      assert get_in(result, ["server", "debug"]) == true
      assert get_in(result, ["server", "pool", "size"]) == 10
    end
  end
end
