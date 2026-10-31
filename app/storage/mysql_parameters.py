"""Bind positional values through structural rewrites without duplicating secrets."""
from __future__ import annotations

from collections.abc import Sequence

from app.storage.mysql_dialect import MySQLDialectError


def name_parameters(statement: str, parameters: Sequence):
    from sqlglot import Tokenizer
    from sqlglot.tokens import TokenType

    tokens = [token for token in Tokenizer(dialect="sqlite").tokenize(statement)
              if token.token_type == TokenType.PLACEHOLDER and token.text == "?"]
    if len(tokens) != len(parameters):
        raise MySQLDialectError("parameter count does not match SQL markers")
    values = {f"lka_p{index}": value for index, value in enumerate(parameters)}
    for index, token in reversed(list(enumerate(tokens))):
        statement = statement[:token.start] + f":lka_p{index}" + statement[token.end + 1:]
    return statement, values


def format_named_sql(statement: str, values: dict) -> str:
    """Convert only parsed bind markers and escape other DB-API percent signs."""
    from sqlglot import Tokenizer
    from sqlglot.tokens import TokenType

    tokens = Tokenizer(dialect="mysql").tokenize(statement)
    parts, offset = [], 0
    for index, token in enumerate(tokens[:-1]):
        following = tokens[index + 1]
        if token.token_type != TokenType.COLON or following.start != token.end + 1:
            continue
        if following.text not in values:
            raise MySQLDialectError("unknown named SQL parameter")
        parts.append(statement[offset:token.start].replace("%", "%%"))
        parts.append("%(" + following.text + ")s")
        offset = following.end + 1
    parts.append(statement[offset:].replace("%", "%%"))
    return "".join(parts)
