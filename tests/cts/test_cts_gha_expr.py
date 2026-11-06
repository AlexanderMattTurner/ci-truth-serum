"""Tests for ci_truth_serum/_cts_gha_expr.py — the parser for the text inside a
GitHub Actions `${{ }}`."""

import pytest

from tests._helpers import load_hook

gha = load_hook("_cts_gha_expr.py", "cts_gha_expr")


@pytest.mark.parametrize(
    ("text", "tree"),
    [
        ("runner.os", gha.Ref(("runner", "os"))),
        (
            "steps.pnpm-store.outputs.cache-primary-key",
            gha.Ref(("steps", "pnpm-store", "outputs", "cache-primary-key")),
        ),
        ("matrix['node-version']", gha.Ref(("matrix", "node-version"))),
        ("github.event.*", gha.Ref(("github", "event", "*"))),
        ("'it''s'", gha.Literal("it's")),
        ("3", gha.Literal(3.0)),
        ("0x10", gha.Literal(16.0)),
        ("TRUE", gha.Literal(True)),
        ("null", gha.Literal(None)),
        ("( runner.os )", gha.Ref(("runner", "os"))),
        (
            "hashFiles('a', format('{0}.json', inputs.tool))",
            gha.Call(
                "hashFiles",
                (
                    gha.Literal("a"),
                    gha.Call(
                        "format", (gha.Literal("{0}.json"), gha.Ref(("inputs", "tool")))
                    ),
                ),
            ),
        ),
        ("always()", gha.Call("always", ())),
    ],
)
def test_parse_reads_literals_properties_and_calls(text, tree):
    assert gha.parse(text) == tree


@pytest.mark.parametrize(
    "text",
    [
        "a || b",
        "!cancelled()",
        "inputs.a == 'x'",
        "a.b c",
        "",
        "f(a,",
        "a[b]",
        "'unterminated",
        "a.'b'",
        "fromJSON(x).version",
        "(" * 100 + "a" + ")" * 100,
    ],
)
def test_parse_refuses_an_operator_or_broken_text(text):
    with pytest.raises(gha.Unsupported):
        gha.parse(text)


@pytest.mark.parametrize(
    ("template", "args", "out"),
    [
        ("config/{0}-version.json", ["grype"], "config/grype-version.json"),
        ("{1}{0}", ["a", "b"], "ba"),
        ("{{0}} {0}", ["x"], "{0} x"),
    ],
)
def test_format_string_fills_each_index(template, args, out):
    assert gha.format_string(template, args) == out


@pytest.mark.parametrize("template", ["{1}", "{", "a}b", "{x}"])
def test_format_string_refuses_what_github_refuses(template):
    with pytest.raises(gha.Unsupported):
        gha.format_string(template, ["only"])
