"""Tests for the  spec sprint: `specs/db/catalog.sql`,
`specs/db/conversations.sql`, and `specs/db/audit.sql`.

These three SQL files are the data contracts every service and migration
must agree on before any application query code is written.

The tests are structural — they assert each SQL file contains the
required CREATE EXTENSION / CREATE SCHEMA / CREATE TABLE / CREATE INDEX
statements with the correct columns, types, constraints, and design
decisions. Runtime parse-correctness (does this SQL actually execute on
Postgres?) is validated by the `spec-validate-sql` CI job in
`.github/workflows/ci.yml`, which spins up an ephemeral pgvector-enabled
Postgres and runs the files through `psql --set ON_ERROR_STOP=1`.

Written before implementation (test-first). Mirrors the
structural pattern used in `tests/test_specs.py`.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CATALOG_SQL = REPO_ROOT / "specs" / "db" / "catalog.sql"
CONVERSATIONS_SQL = REPO_ROOT / "specs" / "db" / "conversations.sql"

# Match a CREATE TABLE body — the columns chunk between the first `(` and
# the closing `);` at the end of the statement. `re.DOTALL` so newlines
# inside the body don't break the match.
TABLE_BODY_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?{table}\s*\((?P<body>.*?)\)\s*;",
    re.IGNORECASE | re.DOTALL,
)


def _table_body(sql: str, qualified_table: str) -> str:
    """Return the column-list body of `CREATE TABLE qualified_table (...);`
    so individual column / constraint assertions don't have to repeat
    the regex."""
    pattern = TABLE_BODY_RE.pattern.format(table=re.escape(qualified_table))
    match = re.search(pattern, sql, re.IGNORECASE | re.DOTALL)
    assert match, f"CREATE TABLE {qualified_table} (...) not found in SQL"
    return match.group("body")


# ---------------------------------------------------------------------------
# specs/db/catalog.sql
# ---------------------------------------------------------------------------


class TestCatalogSchema:
    """Catalog schema: pgvector extension, products table, both HNSW indexes.
    The 768-dim image embedding column and 512-dim text embedding column
    are pinned here because they're the contract every ingest job and
    query layer compiles against."""

    def test_catalog_sql_exists(self) -> None:
        assert CATALOG_SQL.is_file(), (
            f"expected {CATALOG_SQL.relative_to(REPO_ROOT)}; "
            " implementation hasn't landed yet"
        )

    def test_pgvector_extension_created_first(self) -> None:
        # Strip `-- …` line comments first; doc comments routinely mention
        # `CREATE TABLE` and would trip the order check below.
        sql = re.sub(r"--[^\n]*", "", CATALOG_SQL.read_text())
        # `CREATE EXTENSION IF NOT EXISTS vector;` must precede any CREATE
        # TABLE that declares a `vector(N)` column — the column type is
        # only resolvable once the extension is loaded.
        ext_match = re.search(
            r"CREATE\s+EXTENSION\s+IF\s+NOT\s+EXISTS\s+vector\s*;",
            sql,
            re.IGNORECASE,
        )
        assert ext_match, (
            "catalog.sql must begin with "
            "`CREATE EXTENSION IF NOT EXISTS vector;` per issue Technical "
            "Requirements"
        )
        first_table = re.search(r"CREATE\s+TABLE\b", sql, re.IGNORECASE)
        assert first_table, "no CREATE TABLE found in catalog.sql"
        assert ext_match.start() < first_table.start(), (
            "CREATE EXTENSION vector must appear before any CREATE TABLE "
            "that uses the vector type"
        )

    def test_catalog_schema_created(self) -> None:
        sql = CATALOG_SQL.read_text()
        assert re.search(
            r"CREATE\s+SCHEMA\s+IF\s+NOT\s+EXISTS\s+catalog\s*;",
            sql,
            re.IGNORECASE,
        ), (
            "catalog.sql must `CREATE SCHEMA IF NOT EXISTS catalog;` "
            "before defining catalog.products; otherwise a fresh DB errors"
        )

    def test_products_table_required_columns(self) -> None:
        body = _table_body(CATALOG_SQL.read_text(), "catalog.products")
        # Spot-check each required column by name + a key type/constraint
        # token. We don't normalise whitespace — the SQL formatter will.
        expected_column_fragments = [
            (
                r"\bid\s+UUID\b.*\bPRIMARY\s+KEY\b.*\bgen_random_uuid\s*\(\s*\)",
                "id UUID PRIMARY KEY DEFAULT gen_random_uuid()",
            ),
            (r"\btitle\s+TEXT\s+NOT\s+NULL\b", "title TEXT NOT NULL"),
            (r"\bcategory\s+TEXT\s+NOT\s+NULL\b", "category TEXT NOT NULL"),
            (r"\bcolour\s+TEXT\s+NOT\s+NULL\b", "colour TEXT NOT NULL"),
            (r"\bformality\s+TEXT\s+NOT\s+NULL\b", "formality TEXT NOT NULL"),
            (r"\boccasion\s+TEXT\s+NOT\s+NULL\b", "occasion TEXT NOT NULL"),
            (r"\bprice_cents\s+INTEGER\s+NOT\s+NULL\b", "price_cents INTEGER NOT NULL"),
            (r"\bimage_url\s+TEXT\s+NOT\s+NULL\b", "image_url TEXT NOT NULL"),
            (
                r"\bembedding\s+vector\s*\(\s*768\s*\)\s+NOT\s+NULL\b",
                "embedding vector(768) NOT NULL",
            ),
            # No trailing `\b` — `)` and `,` are both non-word chars so a
            # word boundary between them never matches.
            (
                r"\btext_embedding\s+vector\s*\(\s*512\s*\)",
                "text_embedding vector(512)",
            ),
            (
                r"\bcreated_at\s+TIMESTAMPTZ\s+NOT\s+NULL\s+DEFAULT\s+now\s*\(\s*\)",
                "created_at TIMESTAMPTZ NOT NULL DEFAULT now()",
            ),
        ]
        for pattern, description in expected_column_fragments:
            assert re.search(pattern, body, re.IGNORECASE | re.DOTALL), (
                f"catalog.products is missing column: {description}"
            )

    def test_products_text_embedding_is_nullable(self) -> None:
        """`text_embedding` is the optional text-tower column; making it
        NOT NULL would block ingest of catalog items that lack a usable
        text description. The contract is explicit: vector(512) without
        NOT NULL."""
        body = _table_body(CATALOG_SQL.read_text(), "catalog.products")
        # Find the text_embedding line specifically and assert NOT NULL
        # doesn't appear before the next column / end-of-line.
        line_match = re.search(
            r"\btext_embedding\s+vector\s*\(\s*512\s*\)(?P<rest>[^,)]*)",
            body,
            re.IGNORECASE,
        )
        assert line_match, "text_embedding column definition not found"
        assert "NOT NULL" not in line_match.group("rest").upper(), (
            "text_embedding must be nullable; making it NOT NULL would "
            "block ingest of items without usable text descriptions"
        )

    def test_hnsw_index_on_image_embedding(self) -> None:
        sql = CATALOG_SQL.read_text()
        # `\s` here (vs `[^\n]`) so an index name + newline + `ON …` form
        # like the multi-line pgvector idiom matches as readily as the
        # single-line form.
        assert re.search(
            r"CREATE\s+INDEX\b[\s\S]*?\bON\s+catalog\.products\s+USING\s+hnsw\s*\(\s*embedding\s+vector_cosine_ops\s*\)",
            sql,
            re.IGNORECASE,
        ), (
            "catalog.sql must declare an HNSW index on catalog.products "
            "embedding column using vector_cosine_ops"
        )

    def test_hnsw_index_on_text_embedding(self) -> None:
        sql = CATALOG_SQL.read_text()
        assert re.search(
            r"CREATE\s+INDEX\b[\s\S]*?\bON\s+catalog\.products\s+USING\s+hnsw\s*\(\s*text_embedding\s+vector_cosine_ops\s*\)",
            sql,
            re.IGNORECASE,
        ), (
            "catalog.sql must declare a second HNSW index on the "
            "text_embedding column using vector_cosine_ops"
        )


# ---------------------------------------------------------------------------
# specs/db/conversations.sql
# ---------------------------------------------------------------------------


class TestConversationsSchema:
    """Conversations schema: two tables with a cascading FK, and a comment
    pinning the TTL design (periodic cleanup, no trigger or pg_cron)."""

    def test_conversations_sql_exists(self) -> None:
        assert CONVERSATIONS_SQL.is_file(), (
            f"expected {CONVERSATIONS_SQL.relative_to(REPO_ROOT)}; "
            " implementation hasn't landed yet"
        )

    def test_conversations_schema_created(self) -> None:
        sql = CONVERSATIONS_SQL.read_text()
        assert re.search(
            r"CREATE\s+SCHEMA\s+IF\s+NOT\s+EXISTS\s+conversations\s*;",
            sql,
            re.IGNORECASE,
        ), "conversations.sql must `CREATE SCHEMA IF NOT EXISTS conversations;`"

    def test_conversations_table_columns(self) -> None:
        body = _table_body(
            CONVERSATIONS_SQL.read_text(),
            "conversations.conversations",
        )
        for pattern, description in [
            (
                r"\bid\s+UUID\b.*\bPRIMARY\s+KEY\b.*\bgen_random_uuid\s*\(\s*\)",
                "id UUID PRIMARY KEY DEFAULT gen_random_uuid()",
            ),
            (
                r"\bcreated_at\s+TIMESTAMPTZ\s+NOT\s+NULL\s+DEFAULT\s+now\s*\(\s*\)",
                "created_at TIMESTAMPTZ NOT NULL DEFAULT now()",
            ),
            (
                r"\bexpires_at\s+TIMESTAMPTZ\s+NOT\s+NULL\b",
                "expires_at TIMESTAMPTZ NOT NULL",
            ),
        ]:
            assert re.search(pattern, body, re.IGNORECASE | re.DOTALL), (
                f"conversations.conversations is missing column: {description}"
            )

    def test_turns_table_columns_and_role_constraint(self) -> None:
        body = _table_body(CONVERSATIONS_SQL.read_text(), "conversations.turns")
        for pattern, description in [
            (
                r"\bid\s+UUID\b.*\bPRIMARY\s+KEY\b.*\bgen_random_uuid\s*\(\s*\)",
                "id UUID PRIMARY KEY DEFAULT gen_random_uuid()",
            ),
            (
                r"\bconversation_id\s+UUID\s+NOT\s+NULL\b",
                "conversation_id UUID NOT NULL",
            ),
            (r"\brole\s+TEXT\s+NOT\s+NULL\b", "role TEXT NOT NULL"),
            (r"\bcontent\s+JSONB\s+NOT\s+NULL\b", "content JSONB NOT NULL"),
            (
                r"\bcreated_at\s+TIMESTAMPTZ\s+NOT\s+NULL\s+DEFAULT\s+now\s*\(\s*\)",
                "created_at TIMESTAMPTZ NOT NULL DEFAULT now()",
            ),
        ]:
            assert re.search(pattern, body, re.IGNORECASE | re.DOTALL), (
                f"conversations.turns is missing column: {description}"
            )
        # The role check constraint must list exactly the three legal
        # values; an open enum would let the orchestrator persist garbage.
        role_check = re.search(
            r"CHECK\s*\(\s*role\s+IN\s*\(\s*'user'\s*,\s*'assistant'\s*,\s*'tool'\s*\)\s*\)",
            body,
            re.IGNORECASE,
        )
        assert role_check, (
            "conversations.turns.role must carry "
            "`CHECK (role IN ('user', 'assistant', 'tool'))` — open enum "
            "lets the orchestrator persist garbage"
        )

    def test_turns_has_cascading_fk_to_conversations(self) -> None:
        body = _table_body(CONVERSATIONS_SQL.read_text(), "conversations.turns")
        assert re.search(
            r"REFERENCES\s+conversations\.conversations\s*\(\s*id\s*\)\s+ON\s+DELETE\s+CASCADE",
            body,
            re.IGNORECASE,
        ), (
            "conversations.turns.conversation_id must REFERENCE "
            "conversations.conversations(id) ON DELETE CASCADE so deleting "
            "a conversation reaps its turns"
        )

    def test_ttl_design_comment_present(self) -> None:
        sql = CONVERSATIONS_SQL.read_text()
        # The contract pins a *periodic cleanup* design; readers must not
        # mistakenly add a trigger or pg_cron dependency.
        assert re.search(r"periodic\s+cleanup", sql, re.IGNORECASE), (
            "conversations.sql must include a SQL comment explaining the "
            "TTL approach (periodic cleanup job, not trigger / pg_cron)"
        )
        assert re.search(r"expires_at\s*<\s*now\s*\(\s*\)", sql, re.IGNORECASE), (
            "TTL comment must show the deletion predicate "
            "`expires_at < now()` so future readers know what the cleanup "
            "job actually runs"
        )
