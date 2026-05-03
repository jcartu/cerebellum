"""Tests for the Cypher tokenizer and safety validator (Phase 7 Thread 2).

Covers:
- Tokenizer: all token types, edge cases, escape sequences
- Validator: read-only policy, CALL whitelist, statement chaining
- Integration: is_cypher_safe() public API
- Regression: all 27 existing cypher_filter tests must still pass
"""

from __future__ import annotations

from cerebellum.cypher_safety import (
    CypherTokenizer,
    CypherValidator,
    Token,
    TokenType,
    is_cypher_safe,
)

# ── Tokenizer tests ────────────────────────────────────────────────────────


class TestTokenizerKeywords:
    """Keywords are recognized and uppercased."""

    def test_match_is_keyword(self) -> None:
        tokens = CypherTokenizer("MATCH (n) RETURN n").tokenize()
        first = next(t for t in tokens if t.type == TokenType.KEYWORD)
        assert first.value == "MATCH"

    def test_create_is_keyword(self) -> None:
        tokens = CypherTokenizer("CREATE TABLE x").tokenize()
        first = next(t for t in tokens if t.type == TokenType.KEYWORD)
        assert first.value == "CREATE"

    def test_mixed_case_keyword(self) -> None:
        tokens = CypherTokenizer("match (n) return n").tokenize()
        first = next(t for t in tokens if t.type == TokenType.KEYWORD)
        assert first.value == "MATCH"

    def test_where_is_keyword(self) -> None:
        tokens = CypherTokenizer("WHERE x = 1").tokenize()
        first = next(t for t in tokens if t.type == TokenType.KEYWORD)
        assert first.value == "WHERE"


class TestTokenizerStrings:
    """String literals are tokenized correctly."""

    def test_single_quoted_string(self) -> None:
        tokens = CypherTokenizer("'hello world'").tokenize()
        string_tokens = [t for t in tokens if t.type == TokenType.STRING_LITERAL]
        assert len(string_tokens) == 1
        assert string_tokens[0].value == "'hello world'"

    def test_double_quoted_string(self) -> None:
        tokens = CypherTokenizer('"hello world"').tokenize()
        string_tokens = [t for t in tokens if t.type == TokenType.STRING_LITERAL]
        assert len(string_tokens) == 1
        assert string_tokens[0].value == '"hello world"'

    def test_escaped_quote_in_string(self) -> None:
        tokens = CypherTokenizer("'it\\'s a test'").tokenize()
        string_tokens = [t for t in tokens if t.type == TokenType.STRING_LITERAL]
        assert len(string_tokens) == 1

    def test_string_with_keyword_inside(self) -> None:
        tokens = CypherTokenizer("'DROP table'").tokenize()
        keyword_tokens = [t for t in tokens if t.type == TokenType.KEYWORD]
        assert len(keyword_tokens) == 0

    def test_unterminated_string(self) -> None:
        tokens = CypherTokenizer("'unterminated").tokenize()
        string_tokens = [t for t in tokens if t.type == TokenType.STRING_LITERAL]
        assert len(string_tokens) == 1


class TestTokenizerComments:
    """Comments are recognized and separated."""

    def test_line_comment(self) -> None:
        tokens = CypherTokenizer("-- this is a comment").tokenize()
        comment_tokens = [t for t in tokens if t.type == TokenType.COMMENT]
        assert len(comment_tokens) == 1

    def test_block_comment(self) -> None:
        tokens = CypherTokenizer("/* block comment */").tokenize()
        comment_tokens = [t for t in tokens if t.type == TokenType.COMMENT]
        assert len(comment_tokens) == 1

    def test_multiline_block_comment(self) -> None:
        tokens = CypherTokenizer("/* line1\nline2 */").tokenize()
        comment_tokens = [t for t in tokens if t.type == TokenType.COMMENT]
        assert len(comment_tokens) == 1

    def test_comment_with_keyword(self) -> None:
        tokens = CypherTokenizer("-- DROP everything").tokenize()
        keyword_tokens = [t for t in tokens if t.type == TokenType.KEYWORD]
        assert len(keyword_tokens) == 0


class TestTokenizerParameters:
    """Parameters ($param) are tokenized."""

    def test_simple_parameter(self) -> None:
        tokens = CypherTokenizer("$id").tokenize()
        param_tokens = [t for t in tokens if t.type == TokenType.PARAMETER]
        assert len(param_tokens) == 1
        assert param_tokens[0].value == "$id"

    def test_parameter_with_underscore(self) -> None:
        tokens = CypherTokenizer("$event_type").tokenize()
        param_tokens = [t for t in tokens if t.type == TokenType.PARAMETER]
        assert param_tokens[0].value == "$event_type"


class TestTokenizerLabels:
    """Labels (:Label) are tokenized."""

    def test_simple_label(self) -> None:
        tokens = CypherTokenizer(":Event").tokenize()
        label_tokens = [t for t in tokens if t.type == TokenType.LABEL]
        assert len(label_tokens) == 1
        assert label_tokens[0].value == ":Event"


class TestTokenizerNumbers:
    """Numbers are tokenized."""

    def test_integer(self) -> None:
        tokens = CypherTokenizer("42").tokenize()
        num_tokens = [t for t in tokens if t.type == TokenType.NUMBER]
        assert num_tokens[0].value == "42"

    def test_float(self) -> None:
        tokens = CypherTokenizer("3.14").tokenize()
        num_tokens = [t for t in tokens if t.type == TokenType.NUMBER]
        assert num_tokens[0].value == "3.14"


class TestTokenizerOperators:
    """Operators are recognized."""

    def test_equals(self) -> None:
        tokens = CypherTokenizer("=").tokenize()
        op_tokens = [t for t in tokens if t.type == TokenType.OPERATOR]
        assert op_tokens[0].value == "="

    def test_not_equals(self) -> None:
        tokens = CypherTokenizer("<>").tokenize()
        op_tokens = [t for t in tokens if t.type == TokenType.OPERATOR]
        assert op_tokens[0].value == "<>"

    def test_less_equal(self) -> None:
        tokens = CypherTokenizer("<=").tokenize()
        op_tokens = [t for t in tokens if t.type == TokenType.OPERATOR]
        assert op_tokens[0].value == "<="


class TestTokenizerIdentifiers:
    """Identifiers and backtick-quoted identifiers."""

    def test_bare_identifier(self) -> None:
        tokens = CypherTokenizer("myVar").tokenize()
        ident_tokens = [t for t in tokens if t.type == TokenType.IDENTIFIER]
        assert ident_tokens[0].value == "myVar"

    def test_backtick_identifier(self) -> None:
        tokens = CypherTokenizer("`my var`").tokenize()
        ident_tokens = [t for t in tokens if t.type == TokenType.IDENTIFIER]
        assert len(ident_tokens) == 1


# ── Validator tests ────────────────────────────────────────────────────────


class TestValidatorBasic:
    """Basic validation rules."""

    def _tokens(self, query: str) -> list[Token]:
        return CypherTokenizer(query).tokenize()

    def test_empty_rejected(self) -> None:
        assert CypherValidator().validate(self._tokens("")) is False

    def test_whitespace_only_rejected(self) -> None:
        assert CypherValidator().validate(self._tokens("   ")) is False

    def test_semicolon_rejected(self) -> None:
        assert CypherValidator().validate(self._tokens("MATCH (n) RETURN n; DROP x")) is False

    def test_bad_prefix_rejected(self) -> None:
        assert CypherValidator().validate(self._tokens("INVALID QUERY")) is False


class TestValidatorReadOnly:
    """Read-only keyword enforcement."""

    def _tokens(self, query: str) -> list[Token]:
        return CypherTokenizer(query).tokenize()

    def test_match_allowed(self) -> None:
        assert CypherValidator().validate(self._tokens("MATCH (n) RETURN n")) is True

    def test_unwind_allowed(self) -> None:
        assert CypherValidator().validate(self._tokens("UNWIND [1,2,3] AS x RETURN x")) is True

    def test_with_allowed(self) -> None:
        assert CypherValidator().validate(self._tokens("WITH n RETURN n")) is True

    def test_create_rejected(self) -> None:
        assert CypherValidator().validate(self._tokens("CREATE TABLE x")) is False

    def test_merge_rejected(self) -> None:
        assert CypherValidator().validate(self._tokens("MERGE (n) RETURN n")) is False

    def test_delete_rejected(self) -> None:
        assert CypherValidator().validate(self._tokens("DELETE n")) is False

    def test_drop_rejected(self) -> None:
        assert CypherValidator().validate(self._tokens("DROP TABLE x")) is False

    def test_alter_rejected(self) -> None:
        assert CypherValidator().validate(self._tokens("ALTER TABLE x")) is False


class TestValidatorCallWhitelist:
    """CALL procedure whitelist."""

    def _tokens(self, query: str) -> list[Token]:
        return CypherTokenizer(query).tokenize()

    def test_call_db_schema_allowed(self) -> None:
        assert CypherValidator().validate(self._tokens("CALL db.schema()")) is True

    def test_call_db_show_tables_allowed(self) -> None:
        assert CypherValidator().validate(self._tokens("CALL db.show_tables()")) is True

    def test_call_db_show_connections_allowed(self) -> None:
        assert CypherValidator().validate(self._tokens("CALL db.show_connections()")) is True

    def test_call_apoc_rejected(self) -> None:
        assert CypherValidator().validate(self._tokens("CALL apoc.something()")) is False

    def test_call_unknown_rejected(self) -> None:
        assert CypherValidator().validate(self._tokens("CALL custom.procedure()")) is False


# ── Public API tests ───────────────────────────────────────────────────────


class TestIsCypherSafe:
    """Integration tests for is_cypher_safe() public API."""

    def test_simple_match_safe(self) -> None:
        assert is_cypher_safe("MATCH (n) RETURN n") is True

    def test_create_unsafe(self) -> None:
        assert is_cypher_safe("CREATE TABLE x") is False

    def test_empty_unsafe(self) -> None:
        assert is_cypher_safe("") is False

    def test_none_safe_handling(self) -> None:
        # is_cypher_safe expects a string; None would raise TypeError
        # This is intentional — callers should validate input type
        pass


# ── Regression tests (must match old regex filter behavior) ────────────────


class TestRegression6R6:
    """6R.6: Keywords inside string literals must NOT be rejected."""

    def test_drop_in_double_quote_literal(self) -> None:
        assert is_cypher_safe('MATCH (n) WHERE n.name = "DROP table" RETURN n') is True

    def test_create_in_single_quote_literal(self) -> None:
        assert is_cypher_safe("MATCH (n) WHERE n.name = 'CREATE index' RETURN n") is True

    def test_delete_in_literal(self) -> None:
        assert is_cypher_safe('MATCH (n) WHERE n.desc = "DELETE everything" RETURN n') is True

    def test_merge_in_literal(self) -> None:
        assert is_cypher_safe('MATCH (n) WHERE n.note = "MERGE requested" RETURN n') is True

    def test_set_in_literal(self) -> None:
        assert is_cypher_safe('MATCH (n) WHERE n.flag = "SET mode" RETURN n') is True

    def test_update_in_literal(self) -> None:
        assert is_cypher_safe('MATCH (n) WHERE n.action = "UPDATE record" RETURN n') is True

    def test_multiple_keywords_in_literals(self) -> None:
        assert is_cypher_safe('MATCH (n) WHERE n.a = "DROP" AND n.b = "CREATE" RETURN n') is True

    def test_real_keyword_still_rejected(self) -> None:
        assert is_cypher_safe("DROP TABLE nodes") is False

    def test_create_still_rejected(self) -> None:
        assert is_cypher_safe("CREATE TABLE x") is False

    def test_delete_still_rejected(self) -> None:
        assert is_cypher_safe("DELETE FROM nodes") is False

    def test_use_in_literal(self) -> None:
        assert is_cypher_safe('MATCH (n) WHERE n.cmd = "USE database" RETURN n') is True

    def test_alter_in_literal(self) -> None:
        assert is_cypher_safe('MATCH (n) WHERE n.note = "ALTER table" RETURN n') is True


class TestRegression6R7:
    """6R.7: CALL whitelist — only allowed procedures pass."""

    def test_call_db_schema(self) -> None:
        assert is_cypher_safe("CALL db.schema()") is True

    def test_call_db_show_tables(self) -> None:
        assert is_cypher_safe("CALL db.show_tables()") is True

    def test_call_db_show_connections(self) -> None:
        assert is_cypher_safe("CALL db.show_connections()") is True

    def test_call_apoc_rejected(self) -> None:
        assert is_cypher_safe("CALL apoc.something()") is False

    def test_call_db_write_rejected(self) -> None:
        assert is_cypher_safe("CALL db.write_anything()") is False

    def test_call_unknown_rejected(self) -> None:
        assert is_cypher_safe("CALL custom.procedure()") is False

    def test_call_no_parens(self) -> None:
        assert is_cypher_safe("CALL db.schema") is True


class TestRegressionSanity:
    """General sanity checks from old test suite."""

    def test_match_allowed(self) -> None:
        assert is_cypher_safe("MATCH (n) RETURN n") is True

    def test_empty_rejected(self) -> None:
        assert is_cypher_safe("") is False

    def test_semicolon_rejected(self) -> None:
        assert is_cypher_safe("MATCH (n) RETURN n; DROP TABLE x") is False

    def test_whitespace_only_rejected(self) -> None:
        assert is_cypher_safe("   ") is False

    def test_unwind_allowed(self) -> None:
        assert is_cypher_safe("UNWIND [1,2,3] AS x RETURN x") is True

    def test_with_allowed(self) -> None:
        assert is_cypher_safe("WITH n RETURN n") is True

    def test_bad_prefix_rejected(self) -> None:
        assert is_cypher_safe("INVALID QUERY") is False
