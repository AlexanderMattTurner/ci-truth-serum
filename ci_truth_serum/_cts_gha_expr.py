"""A parser for the GitHub Actions expression language — the text inside `${{ }}`.

GitHub's grammar has literals (a single-quoted string with `''` as its escape,
a number, `true`, `false`, `null`), property access (`a.b`, `a['b']`, `a.*`),
function calls, parentheses, and the operators `!`, `<`, `<=`, `>`, `>=`, `==`,
`!=`, `&&` and `||`. This module parses everything except the operators into a
tree. An operator computes its value by GitHub's own coercion rules, which a
lint must not re-implement, so `parse` raises `Unsupported` for an expression
that holds one. A caller then treats that expression as a value it cannot know.
Property access reads only from a name, so `fromJSON(x).version` raises too.

Identifiers may hold `-` (`steps.pnpm-store.outputs.cache-primary-key`), which
is why Python's `ast` cannot stand in for this grammar.
"""

import re
from typing import NamedTuple


class Unsupported(ValueError):
    """The expression holds syntax this parser does not read."""


class Literal(NamedTuple):
    """A literal value: a string, a number, a boolean or null."""

    value: str | float | bool | None


class Ref(NamedTuple):
    """A property access: `steps.pin.outputs.uv` is ('steps', 'pin', 'outputs', 'uv')."""

    path: tuple[str, ...]


class Call(NamedTuple):
    """A function call: `hashFiles('a', 'b')`."""

    name: str
    args: tuple["Literal | Ref | Call", ...]


Expression = Literal | Ref | Call

_TOKEN = re.compile(
    r"\s*(?:(?P<string>'(?:[^']|'')*')"
    r"|(?P<number>-?(?:0x[0-9a-fA-F]+|\d+(?:\.\d+)?(?:[eE][+-]?\d+)?))"
    r"|(?P<name>[A-Za-z_][A-Za-z0-9_-]*)"
    r"|(?P<punct>[.,()\[\]*])"
    r"|(?P<other>\S))"
)
_KEYWORDS: dict[str, bool | None] = {"true": True, "false": False, "null": None}
# The deepest nesting `parse` reads, so a long run of `(` ends in `Unsupported`
# rather than in Python's recursion limit.
_MAX_DEPTH = 64


def _lex(text: str) -> list[tuple[str, str]]:
    tokens = []
    for match in _TOKEN.finditer(text):
        kind = match.lastgroup
        if kind == "other":
            raise Unsupported(f"unsupported syntax {match[kind]!r} in {text!r}")
        if kind:
            tokens.append((kind, match[kind]))
    return tokens


class _ExpressionParser:
    def __init__(self, tokens: list[tuple[str, str]]) -> None:
        self.tokens = tokens
        self.at = 0
        self.depth = 0

    def peek(self) -> tuple[str, str] | None:
        return self.tokens[self.at] if self.at < len(self.tokens) else None

    def take(self) -> tuple[str, str]:
        token = self.peek()
        if token is None:
            raise Unsupported("the expression ends early")
        self.at += 1
        return token

    def expect(self, punct: str) -> None:
        if self.take() != ("punct", punct):
            raise Unsupported(f"expected {punct!r}")

    def expression(self) -> Expression:
        self.depth += 1
        if self.depth > _MAX_DEPTH:
            raise Unsupported("the expression nests too deeply")
        tree = self.term()
        self.depth -= 1
        return tree

    def term(self) -> Expression:
        kind, text = self.take()
        if kind == "string":
            return Literal(text[1:-1].replace("''", "'"))
        if kind == "number":
            return Literal(float(int(text, 16)) if "x" in text.lower() else float(text))
        if (kind, text) == ("punct", "("):
            inner = self.expression()
            self.expect(")")
            return inner
        if kind != "name":
            raise Unsupported(f"unexpected {text!r}")
        if text.lower() in _KEYWORDS:
            return Literal(_KEYWORDS[text.lower()])
        if self.peek() == ("punct", "("):
            return Call(text, self.arguments())
        return Ref(self.properties(text))

    def arguments(self) -> tuple[Expression, ...]:
        self.expect("(")
        args: list[Expression] = []
        while self.peek() != ("punct", ")"):
            if args:
                self.expect(",")
            args.append(self.expression())
        self.take()
        return tuple(args)

    def properties(self, head: str) -> tuple[str, ...]:
        path = [head]
        while self.peek() in (("punct", "."), ("punct", "[")):
            if self.take() == ("punct", "."):
                kind, text = self.take()
                if kind != "name" and text != "*":
                    raise Unsupported(f"unexpected {text!r} after '.'")
                path.append(text)
                continue
            index = self.expression()
            if not isinstance(index, Literal) or not isinstance(index.value, str):
                raise Unsupported("an index that is not a string literal")
            path.append(index.value)
            self.expect("]")
        return tuple(path)


def parse(text: str) -> Expression:
    """The tree of the expression TEXT, the body of one `${{ }}`. Raises
    `Unsupported` for an operator or for text that is not an expression."""
    parser = _ExpressionParser(_lex(text))
    tree = parser.expression()
    if parser.peek() is not None:
        raise Unsupported(f"unsupported syntax after the first term of {text!r}")
    return tree


_FORMAT_FIELD = re.compile(r"\{\{|\}\}|\{(?P<index>\d+)\}|[{}]")


def format_string(template: str, args: list[str]) -> str:
    """GitHub's `format()`: `{N}` is argument N, `{{` and `}}` are braces.
    Raises `Unsupported` for an index past the arguments or a lone brace, which
    GitHub refuses too."""

    def field(match: re.Match[str]) -> str:
        if match[0] in ("{{", "}}"):
            return match[0][0]
        if match["index"] is None or int(match["index"]) >= len(args):
            raise Unsupported(f"format() cannot fill {match[0]!r}")
        return args[int(match["index"])]

    return _FORMAT_FIELD.sub(field, template)
