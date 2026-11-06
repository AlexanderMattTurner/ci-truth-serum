"""Property/fuzz tests for `_cts_cache_scope.cache_scope`: any cache or setup
step, with any text in its inputs, gives a `CacheScope` or None, never an
error, and the same step always gives the same answer."""

from hypothesis import given
from hypothesis import strategies as st

from tests._helpers import load_hook

cs = load_hook("_cts_cache_scope.py", "fuzz_cache_scope")

_PIECES = st.sampled_from(
    [
        "${{ ",
        " }}",
        "env.A",
        "inputs.v",
        "steps.p.outputs.v",
        "steps.p.outputs.cache-primary-key",
        "hashFiles('a/*.json')",
        "format('{0}', 'x')",
        "runner.os",
        "fromJSON(x).y",
        " || ",
        "'",
        "~/",
        "/tmp/",
        "!",
        "\n",
        "false",
        "true",
        "pip",
        "-",
    ]
)
_TEXT = st.one_of(
    st.text(max_size=40),
    st.lists(_PIECES, max_size=12).map("".join),
    st.booleans(),
    st.none(),
    st.integers(),
)
_ACTIONS = st.sampled_from(
    [
        "actions/cache@v4",
        "actions/cache/restore@v4",
        "buildjet/cache@v4",
        "actions/setup-python@v5",
        "actions/setup-node@v4",
        "astral-sh/setup-uv@v6",
        "Swatinem/rust-cache@v2",
        "actions/checkout@v4",
    ]
)
_WITH = st.dictionaries(
    st.sampled_from(["key", "path", "cache", "enable-cache", "prune-cache"]), _TEXT
)


@given(_ACTIONS, _WITH, _TEXT, _TEXT, st.booleans())
def test_cache_scope_reads_any_step(action, with_block, env_value, script, inputs):
    producer = {"id": "p", "run": script if isinstance(script, str) else "true"}
    step = {"uses": action, "with": with_block, "env": {"A": env_value}}
    context = cs.Context(
        (producer, step),
        {"v": cs.Text("1.0")} if inputs else None,
        {"A": "${{ env.A }}/x"},
    )
    scope = cs.cache_scope(step, context)
    assert scope is None or isinstance(scope, cs.CacheScope)
    if scope is not None:
        assert scope.key is None or isinstance(scope.key, str)
        assert all(isinstance(path, str) for path in scope.paths)
    assert cs.cache_scope(step, context) == scope
