"""Real Cypher tokenizer and safety validator for CEREBELLUM.

Replaces the regex-based keyword filter with a proper state-machine tokenizer
that understands Cypher token boundaries. This eliminates false positives from
keywords embedded in string literals, comments, and identifiers.

Architecture:
- CypherTokenizer: State-machine lexer that emits typed tokens
- CypherValidator: Walks the token stream and enforces read-only constraints
- Public API: is_cypher_safe(query: str) -> bool
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import Final


class TokenType(Enum):
    """Cypher token types emitted by the state-machine lexer."""

    KEYWORD = auto()
    IDENTIFIER = auto()
    STRING_LITERAL = auto()
    NUMBER = auto()
    PARAMETER = auto()  # $param
    PUNCTUATION = auto()
    OPERATOR = auto()
    COMMENT = auto()
    WHITESPACE = auto()
    LABEL = auto()  # :Label
    EOF = auto()


@dataclass(frozen=True)
class Token:
    """A single Cypher token."""

    type: TokenType
    value: str
    position: int = 0


# ── Read-only policy constants ─────────────────────────────────────────────

READ_ONLY_QUERY_PREFIXES: Final = frozenset({
    "MATCH", "CALL", "UNWIND", "WITH", "RETURN", "EXPLAIN", "PROFILE",
})

READ_ONLY_BLOCKED_KEYWORDS: Final = frozenset({
    "CREATE", "MERGE", "DELETE", "DETACH", "SET", "DROP",
    "COPY", "LOAD", "REMOVE", "INSTALL", "ALTER",
    "ATTACH", "USE", "IMPORT", "EXPORT", "UPDATE", "UPSERT",
})

ALLOWED_CALL_PROCEDURES: Final = frozenset({
    "db.schema",
    "db.show_tables",
    "db.show_connections",
})

# Cypher keywords for tokenizer recognition (uppercase canonical form)
CYPHER_KEYWORDS: Final = frozenset({
    *READ_ONLY_QUERY_PREFIXES,
    *READ_ONLY_BLOCKED_KEYWORDS,
    "WHERE", "ORDER", "BY", "LIMIT", "SKIP", "ASC", "DESC",
    "AS", "ON", "CREATE", "OPTIONAL", "FOREACH", "YIELD",
    "TRUE", "FALSE", "NULL", "AND", "OR", "XOR", "NOT", "IN",
    "IS", "CASE", "WHEN", "THEN", "ELSE", "END",
    "ALL", "ANY", "SINGLE", "NONE", "SHORTEST", "ALLSHORTEST",
    "PATH", "NODE", "REL", "RELATIONSHIP", "EDGE",
})


# ── State-machine tokenizer ────────────────────────────────────────────────

class CypherTokenizer:
    """State-machine lexer for Cypher queries.

    Handles:
    - Single/double-quoted strings with escape sequences
    - Block comments (/* ... */) and line comments (-- ...)
    - Parameters ($name)
    - Labels (:Label)
    - Operators (>=, <=, <>, =, etc.)
    - Punctuation ((), [], {}, ,, ., :, ;, *, +, -, /)
    - Numbers (integers and floats)
    - Identifiers (bare and backtick-quoted)
    """

    def __init__(self, query: str) -> None:
        self.query = query
        self.pos = 0
        self.length = len(query)

    def tokenize(self) -> list[Token]:
        """Tokenize the entire query into a list of tokens."""
        tokens: list[Token] = []
        while self.pos < self.length:
            ch = self.query[self.pos]

            # Skip whitespace
            if ch in " \t\r\n":
                self._skip_whitespace()
                continue

            # Line comment
            if ch == "-" and self.pos + 1 < self.length and self.query[self.pos + 1] == "-":
                tokens.append(self._read_line_comment())
                continue

            # Block comment
            if ch == "/" and self.pos + 1 < self.length and self.query[self.pos + 1] == "*":
                tokens.append(self._read_block_comment())
                continue

            # String literals
            if ch in ('"', "'"):
                tokens.append(self._read_string(ch))
                continue

            # Parameter
            if ch == "$":
                tokens.append(self._read_parameter())
                continue

            # Label
            if ch == ":":
                # Check if next char starts a label (not just a colon operator)
                if self.pos + 1 < self.length and (self.query[self.pos + 1].isalpha() or self.query[self.pos + 1] == "_"):
                    tokens.append(self._read_label())
                    continue
                tokens.append(Token(TokenType.PUNCTUATION, ":", self.pos))
                self.pos += 1
                continue

            # Numbers
            if ch.isdigit() or (ch == "." and self.pos + 1 < self.length and self.query[self.pos + 1].isdigit()):
                tokens.append(self._read_number())
                continue

            # Operators (multi-char first)
            if ch in ("!", "<", ">", "=") and self.pos + 1 < self.length:
                two = self.query[self.pos : self.pos + 2]
                if two in ("<=", ">=", "<>", "!="):
                    tokens.append(Token(TokenType.OPERATOR, two, self.pos))
                    self.pos += 2
                    continue

            # Single-char operators and punctuation
            if ch in "=<>!+-*/%().[],{}|":
                tokens.append(
                    Token(
                        TokenType.OPERATOR if ch in "=<>!+-*/%|" else TokenType.PUNCTUATION,
                        ch,
                        self.pos,
                    )
                )
                self.pos += 1
                continue

            # Backtick-quoted identifiers
            if ch == "`":
                tokens.append(self._read_backtick_ident())
                continue

            # Bare identifiers / keywords
            if ch.isalpha() or ch == "_":
                tokens.append(self._read_ident())
                continue

            # Unknown character — skip
            tokens.append(Token(TokenType.PUNCTUATION, ch, self.pos))
            self.pos += 1

        tokens.append(Token(TokenType.EOF, "", self.pos))
        return tokens

    # ── Readers ──────────────────────────────────────────────────────────

    def _skip_whitespace(self) -> None:
        while self.pos < self.length and self.query[self.pos] in " \t\r\n":
            self.pos += 1

    def _read_line_comment(self) -> Token:
        start = self.pos
        self.pos += 2  # skip "--"
        while self.pos < self.length and self.query[self.pos] != "\n":
            self.pos += 1
        return Token(TokenType.COMMENT, self.query[start:self.pos], start)

    def _read_block_comment(self) -> Token:
        start = self.pos
        self.pos += 2  # skip "/*"
        while self.pos + 1 < self.length and not (
            self.query[self.pos] == "*" and self.query[self.pos + 1] == "/"
        ):
            self.pos += 1
        if self.pos + 1 < self.length:
            self.pos += 2  # skip "*/"
        return Token(TokenType.COMMENT, self.query[start:self.pos], start)

    def _read_string(self, quote: str) -> Token:
        start = self.pos
        self.pos += 1  # skip opening quote
        while self.pos < self.length:
            ch = self.query[self.pos]
            if ch == "\\":
                self.pos += 2  # skip escape sequence
                continue
            if ch == quote:
                self.pos += 1  # skip closing quote
                return Token(TokenType.STRING_LITERAL, self.query[start:self.pos], start)
            self.pos += 1
        # Unterminated string — return what we have
        return Token(TokenType.STRING_LITERAL, self.query[start:], start)

    def _read_parameter(self) -> Token:
        start = self.pos
        self.pos += 1  # skip "$"
        while self.pos < self.length and (self.query[self.pos].isalnum() or self.query[self.pos] == "_"):
            self.pos += 1
        return Token(TokenType.PARAMETER, self.query[start:self.pos], start)

    def _read_label(self) -> Token:
        start = self.pos
        self.pos += 1  # skip ":"
        while self.pos < self.length and (self.query[self.pos].isalnum() or self.query[self.pos] == "_"):
            self.pos += 1
        return Token(TokenType.LABEL, self.query[start:self.pos], start)

    def _read_number(self) -> Token:
        start = self.pos
        while self.pos < self.length and (self.query[self.pos].isdigit() or self.query[self.pos] == "."):
            self.pos += 1
        return Token(TokenType.NUMBER, self.query[start:self.pos], start)

    def _read_backtick_ident(self) -> Token:
        start = self.pos
        self.pos += 1  # skip opening backtick
        while self.pos < self.length and self.query[self.pos] != "`":
            if self.query[self.pos] == "\\":
                self.pos += 1
            self.pos += 1
        if self.pos < self.length:
            self.pos += 1  # skip closing backtick
        return Token(TokenType.IDENTIFIER, self.query[start:self.pos], start)

    def _read_ident(self) -> Token:
        start = self.pos
        while self.pos < self.length and (self.query[self.pos].isalnum() or self.query[self.pos] == "_"):
            self.pos += 1
        raw = self.query[start:self.pos]
        upper = raw.upper()
        if upper in CYPHER_KEYWORDS:
            return Token(TokenType.KEYWORD, upper, start)
        return Token(TokenType.IDENTIFIER, raw, start)


# ── Validator ──────────────────────────────────────────────────────────────

class CypherValidator:
    """Validates a tokenized Cypher query against read-only safety policy.

    Rules:
    1. Query must not be empty
    2. Query must not contain semicolons (prevents statement chaining)
    3. First non-whitespace keyword must be in READ_ONLY_QUERY_PREFIXES
    4. No blocked keywords (CREATE, MERGE, DELETE, etc.)
    5. CALL procedures must be in ALLOWED_CALL_PROCEDURES whitelist
    """

    def validate(self, tokens: list[Token]) -> bool:
        """Return True if the token stream passes all safety checks."""
        # Find first meaningful token (skip whitespace, comments, EOF)
        meaningful = [
            t
            for t in tokens
            if t.type not in (TokenType.WHITESPACE, TokenType.COMMENT, TokenType.EOF)
        ]

        if not meaningful:
            return False

        # Check for semicolons (statement chaining)
        for token in meaningful:
            if token.value == ";":
                return False

        # First keyword must be allowed prefix
        first_keyword = self._first_keyword(tokens)
        if first_keyword is None:
            return False
        if first_keyword.value not in READ_ONLY_QUERY_PREFIXES:
            return False

        # No blocked keywords anywhere in the stream
        for token in meaningful:
            if token.type == TokenType.KEYWORD and token.value in READ_ONLY_BLOCKED_KEYWORDS:
                return False

        # CALL whitelist check
        return self._validate_calls(tokens)

    def _first_keyword(self, tokens: list[Token]) -> Token | None:
        """Find the first KEYWORD token (skipping whitespace/comments)."""
        for token in tokens:
            if token.type == TokenType.KEYWORD:
                return token
        return None

    def _validate_calls(self, tokens: list[Token]) -> bool:
        """Validate CALL statements use whitelisted procedures."""
        for i, token in enumerate(tokens):
            if token.type == TokenType.KEYWORD and token.value == "CALL":
                # Find the procedure name (next identifier or dotted name)
                proc = self._extract_call_procedure(tokens, i + 1)
                if proc is not None and proc not in ALLOWED_CALL_PROCEDURES:
                    return False
        return True

    def _extract_call_procedure(self, tokens: list[Token], start: int) -> str | None:
        """Extract dotted procedure name after CALL keyword.

        Handles: db.schema, db.show_tables, db.schema()
        """
        parts: list[str] = []
        i = start
        while i < len(tokens):
            token = tokens[i]
            if token.type == TokenType.IDENTIFIER:
                parts.append(token.value)
                i += 1
            elif token.type == TokenType.PUNCTUATION and token.value == ".":
                i += 1
            elif token.type == TokenType.PUNCTUATION and token.value == "(":
                break
            else:
                break
        return ".".join(parts) if parts else None


# ── Public API ─────────────────────────────────────────────────────────────

def is_cypher_safe(query: str) -> bool:
    """Check if a Cypher query is safe for read-only execution.

    Uses a real tokenizer instead of regex keyword matching.
    Returns True only if the query passes all safety checks.
    """
    if not query or not query.strip():
        return False

    tokenizer = CypherTokenizer(query)
    tokens = tokenizer.tokenize()
    validator = CypherValidator()
    return validator.validate(tokens)
