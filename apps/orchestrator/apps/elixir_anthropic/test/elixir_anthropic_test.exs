defmodule ElixirAnthropicTest do
  use ExUnit.Case, async: true

  # Smoke test — verifies the module compiles and is loaded.
  # Replaced by real behavioural tests when is implemented.
  test "ElixirAnthropic module is defined" do
    assert Code.ensure_loaded?(ElixirAnthropic)
  end
end
