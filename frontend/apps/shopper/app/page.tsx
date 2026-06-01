"use client";

import { useState } from "react";
import Link from "next/link";
import HealthBadge from "@/components/HealthBadge";
import ChatInput from "@/components/ChatInput";
import MessageStream from "@/components/MessageStream";
import BrowseGrid from "@/components/BrowseGrid";
import type { components } from "@avsa/shared";

type ChatProductCard = components["schemas"]["ProductCard"];

export default function Page() {
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [productCards, setProductCards] = useState<ChatProductCard[]>([]);
  const [sessionId, setSessionId] = useState<string | null>(null);

  // A new search replaces the previous results (changing the text or the
  // image(s) and re-submitting updates the result set rather than appending
  // to it). Called by ChatInput at submit time, before the SSE stream starts.
  function handleSearchStart() {
    setErrorMessage(null);
    setProductCards([]);
  }

  function handleError(message: string) {
    setErrorMessage(message);
  }

  function handleProductCard(card: ChatProductCard) {
    setProductCards((prev) => [...prev, card]);
  }

  return (
    <div className="page">
      <header className="site-header">
        <div className="site-header__inner">
          <Link href="/" className="site-header__brand">
            <h1 className="site-header__name">AVSA Shopper</h1>
            <span className="site-header__tagline">
              AI-powered visual search
            </span>
          </Link>

          <nav className="site-header__nav" aria-label="Main navigation">
            <Link href="/" className="site-header__nav-link site-header__nav-link--active">
              Search
            </Link>
            {process.env.NEXT_PUBLIC_GRAFANA_URL && (
              <a
                href={process.env.NEXT_PUBLIC_GRAFANA_URL}
                target="_blank"
                rel="noopener noreferrer"
                className="site-header__nav-link site-header__nav-link--external"
              >
                Metrics
              </a>
            )}
            {process.env.NEXT_PUBLIC_API_DOCS_URL && (
              <a
                href={process.env.NEXT_PUBLIC_API_DOCS_URL}
                target="_blank"
                rel="noopener noreferrer"
                className="site-header__nav-link site-header__nav-link--external"
              >
                API Docs
              </a>
            )}
          </nav>

          <HealthBadge />
        </div>
      </header>

      <main className="site-main">
        <div className="search-card">
          <ChatInput
            onSearchStart={handleSearchStart}
            onError={handleError}
            onProductCard={handleProductCard}
            onSessionId={setSessionId}
            resumeConversationId={sessionId}
          />
        </div>

        {errorMessage && (
          <p role="alert" className="chat-error" data-testid="chat-error">
            {errorMessage}
          </p>
        )}

        <MessageStream productCards={productCards} />

        <BrowseGrid />

        {sessionId !== null && (
          <div data-testid="session-id-display" style={{ marginTop: "1rem", fontSize: "0.875rem" }}>
            <span>Session ID: </span>
            <code>{sessionId}</code>{" "}
            <button
              data-testid="copy-session-id"
              type="button"
              onClick={() => void navigator.clipboard.writeText(sessionId)}
            >
              Copy
            </button>
          </div>
        )}
      </main>
    </div>
  );
}
