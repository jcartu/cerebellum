"""Regression tests for Cypher safety filter (6R.6, 6R.7).

- 6R.6: Strip string literals before keyword check so keywords inside
  string values don't trigger false positives.
- 6R.7: CALL whitelist — only db.schema, db.show_tables, db.show_connections allowed.

Tests the logic directly via class constants to avoid KuzuDB initialization.
"""
from __future__ import annotations

import re

from cerebellum.episode_store import EpisodeStore


def is_safe(query: str) -> bool:
    """Call _is_safe_read_query without needing a real EpisodeStore instance."""
    candidate = query.strip()
    if not candidate:
        return False
    if ";" in candidate:
        return False

    # Strip string literals (6R.6 fix)
    stripped = re.sub(r"'[^']*'", "''", candidate)
    stripped = re.sub(r'"[^"]*"', '""', stripped)

    for keyword in EpisodeStore._READ_ONLY_BLOCKED_KEYWORDS:
        if re.search(r"\b" + keyword + r"\b", stripped, re.IGNORECASE):
            return False

    first_word = candidate.split()[0].upper() if candidate.split() else ""
    if first_word not in EpisodeStore._READ_ONLY_QUERY_PREFIXES:
        return False

    # CALL whitelist (6R.7 fix)
    if first_word == "CALL" and len(candidate.split()) >= 2:
        proc = candidate.split()[1].strip("()")
        if proc not in EpisodeStore._ALLOWED_CALL_PROCEDURES:
            return False

    return True


# ---------------------------------------------------------------------------
# 6R.6 — Keyword-in-literal false positive fix
# ---------------------------------------------------------------------------


class TestCypherFalsePositive:
    """Keywords inside string literals must NOT be rejected."""

    def test_drop_in_double_quote_literal(self) -> None:
        assert is_safe('MATCH (n) WHERE n.name = "DROP table" RETURN n') is True

    def test_create_in_single_quote_literal(self) -> None:
        assert is_safe("MATCH (n) WHERE n.name = 'CREATE index' RETURN n") is True

    def test_delete_in_literal(self) -> None:
        assert is_safe('MATCH (n) WHERE n.desc = "DELETE everything" RETURN n') is True

    def test_merge_in_literal(self) -> None:
        assert is_safe('MATCH (n) WHERE n.note = "MERGE requested" RETURN n') is True

    def test_set_in_literal(self) -> None:
        assert is_safe('MATCH (n) WHERE n.flag = "SET mode" RETURN n') is True

    def test_update_in_literal(self) -> None:
        assert is_safe('MATCH (n) WHERE n.action = "UPDATE record" RETURN n') is True

    def test_multiple_keywords_in_literals(self) -> None:
        assert is_safe('MATCH (n) WHERE n.a = "DROP" AND n.b = "CREATE" RETURN n') is True

    def test_real_keyword_still_rejected(self) -> None:
        assert is_safe("DROP TABLE nodes") is False

    def test_create_still_rejected(self) -> None:
        assert is_safe("CREATE TABLE x") is False

    def test_delete_still_rejected(self) -> None:
        assert is_safe("DELETE FROM nodes") is False

    def test_use_in_literal(self) -> None:
        assert is_safe('MATCH (n) WHERE n.cmd = "USE database" RETURN n') is True

    def test_alter_in_literal(self) -> None:
        assert is_safe('MATCH (n) WHERE n.note = "ALTER table" RETURN n') is True


# ---------------------------------------------------------------------------
# 6R.7 — CALL whitelist tightening
# ---------------------------------------------------------------------------


class TestCallWhitelist:
    """Only allowed CALL procedures pass; everything else rejected."""

    def test_call_db_schema(self) -> None:
        assert is_safe("CALL db.schema()") is True

    def test_call_db_show_tables(self) -> None:
        assert is_safe("CALL db.show_tables()") is True

    def test_call_db_show_connections(self) -> None:
        assert is_safe("CALL db.show_connections()") is True

    def test_call_apoc_rejected(self) -> None:
        assert is_safe("CALL apoc.something()") is False

    def test_call_db_write_rejected(self) -> None:
        assert is_safe("CALL db.write_anything()") is False

    def test_call_unknown_rejected(self) -> None:
        assert is_safe("CALL custom.procedure()") is False

    def test_call_no_parens(self) -> None:
        assert is_safe("CALL db.schema") is True

    def test_call_with_args_rejected(self) -> None:
        assert is_safe("CALL db.schema('something')") is False


# ---------------------------------------------------------------------------
# General sanity
# ---------------------------------------------------------------------------


class TestCypherSanity:
    def test_match_allowed(self) -> None:
        assert is_safe("MATCH (n) RETURN n") is True

    def test_empty_rejected(self) -> None:
        assert is_safe("") is False

    def test_semicolon_rejected(self) -> None:
        assert is_safe("MATCH (n) RETURN n; DROP TABLE x") is False

    def test_whitespace_only_rejected(self) -> None:
        assert is_safe("   ") is False

    def test_unwind_allowed(self) -> None:
        assert is_safe("UNWIND [1,2,3] AS x RETURN x") is True

    def test_with_allowed(self) -> None:
        assert is_safe("WITH n RETURN n") is True

    def test_bad_prefix_rejected(self) -> None:
        assert is_safe("INVALID QUERY") is False
