defmodule AVSA.LLMTest do
  use ExUnit.Case, async: true

  describe "stub_override/2 — AVSA_LLM_STUB bypass precedence" do
    test "AVSA_LLM_STUB=1 with no Anthropic key selects the Mock" do
      assert AVSA.LLM.stub_override("1", nil) == AVSA.LLM.Mock
      assert AVSA.LLM.stub_override("1", "") == AVSA.LLM.Mock
    end

    test "a present Anthropic key disables the stub bypass (real client wins)" do
      # The crux of the requested change: a provided key must never be silently
      # bypassed by the stub — even with AVSA_LLM_STUB=1, the Mock is not selected.
      assert AVSA.LLM.stub_override("1", "sk-ant-realkey") == nil
    end

    test "no stub flag leaves the configured default regardless of the key" do
      assert AVSA.LLM.stub_override(nil, nil) == nil
      assert AVSA.LLM.stub_override("0", "sk-ant-realkey") == nil
      assert AVSA.LLM.stub_override(nil, "sk-ant-realkey") == nil
    end
  end
end
