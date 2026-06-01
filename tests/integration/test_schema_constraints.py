"""Integration tests for CHECK constraints and FK behavior in the
conversations schema.

Schema existence (verified in test_db_setup.py) is necessary but not
sufficient. These tests confirm the constraints are *enforced* — invalid
inserts are rejected and FK cascade behaves as the specs define.

Skipped when DATABASE_URL is unset (same pattern as test_db_setup.py).
"""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import psycopg.errors
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATE_SCRIPT = REPO_ROOT / "infra" / "migrations" / "migrate.sh"
DATABASE_URL = os.environ.get("DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="DATABASE_URL unset; integration test requires a live Postgres",
)


@pytest.fixture(scope="module", autouse=True)
def _migrated() -> None:
    subprocess.run(
        ["bash", str(MIGRATE_SCRIPT)],
        env=dict(os.environ),
        capture_output=True,
        text=True,
        check=True,
    )


@pytest.fixture
def db_conn() -> Iterator[psycopg.Connection[Any]]:
    """Fresh connection, rolled back on teardown so constraint-violation tests
    leave the database clean for subsequent tests."""
    conn: psycopg.Connection[Any] = psycopg.connect(DATABASE_URL)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


def _insert_conversation(
    cur: psycopg.Cursor[Any], conn_id: uuid.UUID | None = None
) -> uuid.UUID:
    cid = conn_id or uuid.uuid4()
    cur.execute(
        "INSERT INTO conversations.conversations (id, expires_at) "
        "VALUES (%s, now() + interval '1 hour') RETURNING id",
        (cid,),
    )
    row = cur.fetchone()
    assert row is not None
    return row[0]  # type: ignore[no-any-return]


def _json(value: object) -> str:
    return json.dumps(value)


class TestConversationConstraints:
    """conversations.turns.role CHECK (role IN ('user', 'assistant', 'tool'))
    must reject any other value. CASCADE DELETE on conversation_id must remove
    turns when their parent conversation is deleted."""

    @pytest.mark.parametrize("role", ["user", "assistant", "tool"])
    def test_valid_role_values_are_accepted(
        self, db_conn: psycopg.Connection[Any], role: str
    ) -> None:
        with db_conn.cursor() as cur:
            conv_id = _insert_conversation(cur)
            cur.execute(
                "INSERT INTO conversations.turns (conversation_id, role, content) "
                "VALUES (%s, %s, %s::jsonb)",
                (conv_id, role, _json({"text": "hello"})),
            )
        db_conn.rollback()

    def test_invalid_role_is_rejected(self, db_conn: psycopg.Connection[Any]) -> None:
        with db_conn.cursor() as cur:
            conv_id = _insert_conversation(cur)
            with pytest.raises(psycopg.errors.CheckViolation):
                cur.execute(
                    "INSERT INTO conversations.turns (conversation_id, role, content) "
                    "VALUES (%s, 'system', %s::jsonb)",
                    (conv_id, _json({"text": "hello"})),
                )
        db_conn.rollback()

    def test_cascade_delete_removes_child_turns(
        self, db_conn: psycopg.Connection[Any]
    ) -> None:
        """ON DELETE CASCADE: deleting a conversation must delete its turns."""
        conv_id = uuid.uuid4()
        with db_conn.cursor() as cur:
            _insert_conversation(cur, conv_id)
            cur.execute(
                "INSERT INTO conversations.turns (conversation_id, role, content) "
                "VALUES (%s, 'user', %s::jsonb)",
                (conv_id, _json({"text": "hello"})),
            )
            cur.execute(
                "SELECT COUNT(*) FROM conversations.turns WHERE conversation_id = %s",
                (conv_id,),
            )
            row = cur.fetchone()
            assert row is not None
            count_before: int = row[0]

            cur.execute(
                "DELETE FROM conversations.conversations WHERE id = %s", (conv_id,)
            )

            cur.execute(
                "SELECT COUNT(*) FROM conversations.turns WHERE conversation_id = %s",
                (conv_id,),
            )
            row = cur.fetchone()
            assert row is not None
            count_after: int = row[0]

        assert count_before == 1
        assert count_after == 0, (
            "turns must be deleted when their parent conversation is deleted "
            "(ON DELETE CASCADE on conversations.turns.conversation_id)"
        )
        db_conn.rollback()
