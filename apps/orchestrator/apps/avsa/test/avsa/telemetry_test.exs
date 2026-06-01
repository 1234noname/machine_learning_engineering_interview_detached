defmodule AVSA.TelemetryTest do
  use ExUnit.Case, async: true

  describe "metrics/0" do
    # The metric DEFINITIONS (event_name / tags / measurement per spec) are
    # validated by Telemetry.Metrics at build time, and each event is bound to
    # its real handler by the per-emitter tests (embed_step_test, text_tool_test,
    # verifier_test, retrieval_tool_test, …). Re-asserting the metric(...) DSL
    # args here would only detect *edits*, not defects. The one piece of genuine
    # AVSA logic in metrics/0 is the verifier metric's tag_values function, which
    # renames the emitted :name metadata to the :check_name Prometheus label.
    test "verifier metric's tag_values maps :name metadata to the :check_name label" do
      metric =
        Enum.find(AVSA.Telemetry.metrics(), fn m ->
          m.name == [:avsa, :verifier, :outcome, :total]
        end)

      refute is_nil(metric), "expected metric avsa.verifier.outcome.total"

      assert metric.tag_values.(%{name: :pii_filter, outcome: :pass}) ==
               %{check_name: :pii_filter, outcome: :pass}
    end
  end

  describe "attach_logger_handlers/0" do
    test "returns :ok and does not crash on repeated calls" do
      # First attach.
      assert :ok = AVSA.Telemetry.attach_logger_handlers()
      # Re-attaching the same handler_id would raise unless the impl detaches the
      # old handler first; this guards that idempotency.
      assert :ok = AVSA.Telemetry.attach_logger_handlers()
    after
      :telemetry.detach("avsa-telemetry-logger")
    end
  end
end
