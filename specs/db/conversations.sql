-- specs/db/conversations.sql
-- ---------------------------------------------------------------------------
-- Conversation state for the  chat surface. The orchestrator's
-- Conversation GenServer persists initial state to conversations.conversations
-- and appends each user/assistant/tool exchange to conversations.turns.
--
-- TTL: a periodic cleanup job (cron / scheduled worker) deletes rows where
-- expires_at < now(). No trigger and no pg_cron dependency — the cleanup
-- predicate lives in application code so it's testable and observable, and
-- we don't take a hard dep on a pg extension that isn't universal.
-- ---------------------------------------------------------------------------

CREATE SCHEMA IF NOT EXISTS conversations;

CREATE TABLE conversations.conversations (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL
);

CREATE TABLE conversations.turns (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id  UUID NOT NULL
                     REFERENCES conversations.conversations (id) ON DELETE CASCADE,
    role             TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'tool')),
    content          JSONB NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
