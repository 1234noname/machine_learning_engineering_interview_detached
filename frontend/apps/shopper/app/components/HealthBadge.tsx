"use client";

import { useEffect, useState } from "react";

export default function HealthBadge() {
  const [status, setStatus] = useState<"ok" | "unreachable" | "pending">(
    "pending",
  );

  useEffect(() => {
    let cancelled = false;

    fetch("/api/health")
      .then((res) => {
        if (!cancelled) {
          setStatus(res.ok ? "ok" : "unreachable");
        }
      })
      .catch(() => {
        if (!cancelled) {
          setStatus("unreachable");
        }
      });

    return () => {
      cancelled = true;
    };
  }, []);

  const label =
    status === "pending"
      ? "API: checking…"
      : status === "ok"
        ? "API: ok"
        : "API: unreachable";

  return (
    <span
      data-testid="health-badge"
      className={`health-badge health-badge--${status}`}
    >
      <span className="health-badge__dot" />
      {label}
    </span>
  );
}
