"""Example tests for `_cts_cache_scope`: what one cache or setup step can hold."""

import pytest

from tests._helpers import load_hook

cs = load_hook("_cts_cache_scope.py", "cts_cache_scope")

PRODUCER_RUN = 'echo "v=$TOOL_VERSION" >> "$GITHUB_OUTPUT"'


def _cache(key=None, path=None, uses="actions/cache@v4"):
    with_block = {k: v for k, v in (("key", key), ("path", path)) if v is not None}
    return {"uses": uses, "with": with_block}


def _scope(step, *, before=(), after=(), inputs=None, env=None):
    context = cs.Context((*before, step, *after), inputs, env or {})
    return cs.cache_scope(step, context)


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("deps-1", cs.CacheScope("deps-1")),
        (
            "t-${{ hashFiles('a/*.json', 'b.lock') }}",
            cs.CacheScope(
                "t-${{ hashFiles('a/*.json', 'b.lock') }}", ("a/*.json", "b.lock")
            ),
        ),
        ("t-${{ env.TOOL_VERSION }}", cs.CacheScope("t-${{ env.TOOL_VERSION }}")),
        (
            "t-${{ matrix['node-version'] }}",
            cs.CacheScope("t-${{ matrix.node-version }}"),
        ),
        ("t-${{ format('{0}-x', 'v1') }}", cs.CacheScope("t-v1-x")),
        ("t-${{ 3 }}-${{ true }}-${{ null }}", cs.CacheScope("t-3-true-")),
        ("t-${{ inputs.a || 'x' }}", cs.CacheScope(None)),
        ("t-${{ fromJSON('[]') }}", cs.CacheScope(None)),
        ("t-${{ format('{0}', env.V) }}", cs.CacheScope(None)),
        ("t-${{ secrets.TOKEN }}", cs.CacheScope(None)),
        ("t-${{ steps.none.outputs.v }}", cs.CacheScope(None)),
        (["a", "b"], cs.CacheScope(None)),
    ],
    ids=[
        "literal",
        "hash-files",
        "env-kept",
        "index-kept",
        "format",
        "literals",
        "operator",
        "unknown-function",
        "format-of-a-computed-value",
        "context-that-is-not-kept",
        "output-of-no-step",
        "not-text",
    ],
)
def test_a_key_reads_each_expression_it_can(key, expected):
    """A key keeps each value the text fixes, and the spelling of a context
    that can hold a pin. A value the module cannot read makes the key None."""
    assert _scope(_cache(key)) == expected


def test_a_step_without_inputs_has_an_unreadable_key():
    assert _scope({"uses": "actions/cache@v4"}) == cs.CacheScope(None)


@pytest.mark.parametrize(
    "uses", ["actions/checkout@v4", "actions/cachex@v1", None, "./local-action"]
)
def test_a_step_that_caches_nothing_has_no_scope(uses):
    assert _scope({"uses": uses, "with": {"key": "k"}}) is None


@pytest.mark.parametrize(
    "uses", ["actions/cache@v4", "actions/cache/restore@v4", "buildjet/cache@v4"]
)
def test_each_cache_action_has_a_scope(uses):
    assert _scope(_cache("k", uses=uses)) == cs.CacheScope("k")


@pytest.mark.parametrize(
    ("inputs", "key", "expected"),
    [
        ({"v": cs.Text("1.2")}, "t-${{ inputs.v }}", "t-1.2"),
        ({}, "t-${{ inputs.v }}", "t-"),
        (None, "t-${{ inputs.v }}", "t-${{ inputs.v }}"),
        ({"v": cs.Text("1.2")}, "t-${{ format('{0}', inputs.v) }}", "t-1.2"),
    ],
    ids=["passed", "not-passed", "workflow-input", "through-format"],
)
def test_a_composite_input_reads_what_the_caller_passes(inputs, key, expected):
    """In a composite action an input the caller does not pass is empty. In a
    workflow, `inputs.*` is the run's own and stays as written."""
    assert _scope(_cache(key), inputs=inputs) == cs.CacheScope(expected)


def test_an_input_the_caller_computes_stays_unreadable_in_format():
    inputs = {"v": cs.Text("${{ steps.x.outputs.v }}", literal=False)}
    key = "t-${{ format('{0}', inputs.v) }}"
    assert _scope(_cache(key), inputs=inputs) == cs.CacheScope(None)


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        (PRODUCER_RUN, "t-${{ env.TOOL_VERSION }}"),
        ('gha_output "v=${TOOL_VERSION}"', "t-${{ env.TOOL_VERSION }}"),
        ('echo "v=2.7.0" >> "$GITHUB_OUTPUT"', "t-2.7.0"),
        ('echo "v=$(cat f)" >> "$GITHUB_OUTPUT"', None),
        ('printf \'v=%s\\n\' "$X" >> "$GITHUB_OUTPUT"', None),
        ('echo "v=$A" >> "$GITHUB_OUTPUT"\necho "v=$B" >> "$GITHUB_OUTPUT"', None),
        (
            'echo "v=$A" >> "$GITHUB_OUTPUT"\necho "v=$A" >> "$GITHUB_OUTPUT"',
            "t-${{ env.A }}",
        ),
        ('echo "w=$A" >> "$GITHUB_OUTPUT"', None),
    ],
    ids=[
        "echo",
        "helper",
        "literal",
        "computed",
        "printf-format",
        "two-values",
        "one-value-twice",
        "no-such-output",
    ],
)
def test_a_step_output_reads_what_the_run_step_writes(script, expected):
    producer = {"id": "p", "run": script}
    key = cs.CacheScope(expected) if expected else cs.CacheScope(None)
    assert _scope(_cache("t-${{ steps.p.outputs.v }}"), before=(producer,)) == key


def test_only_a_step_before_the_cache_sets_its_output():
    producer = {"id": "p", "run": PRODUCER_RUN}
    step = _cache("t-${{ steps.p.outputs.v }}")
    assert _scope(step, after=(producer,)) == cs.CacheScope(None)


ENV_WRITER = {"run": 'gha_env "CACHE_KEY=gate-$digest"'}


@pytest.mark.parametrize(
    ("before", "after", "env", "key", "expected"),
    [
        ((ENV_WRITER,), (), {}, "${{ env.CACHE_KEY }}", None),
        (
            ({"run": 'echo "CACHE_KEY=x" >> "$GITHUB_ENV"'},),
            (),
            {},
            "t-${{ env.CACHE_KEY }}",
            None,
        ),
        ((), (ENV_WRITER,), {}, "${{ env.CACHE_KEY }}", "${{ env.CACHE_KEY }}"),
        ((), (), {}, "${{ env.CACHE_KEY }}", "${{ env.CACHE_KEY }}"),
        (
            (ENV_WRITER,),
            (),
            {"CACHE_KEY": "k"},
            "${{ env.CACHE_KEY }}",
            "${{ env.CACHE_KEY }}",
        ),
        (
            ({"run": 'gha_env "TOOL_VERSION=$v"'},),
            (),
            {},
            "t-${{ env.TOOL_VERSION }}",
            "t-${{ env.TOOL_VERSION }}",
        ),
    ],
    ids=[
        "written-by-an-earlier-step",
        "written-to-github-env",
        "written-only-after-the-cache",
        "written-by-no-step",
        "set-in-an-env-block",
        "pin-name-stays",
    ],
)
def test_an_env_key_an_earlier_step_computes_is_unreadable(
    before, after, env, key, expected
):
    """A key that reads `env.NAME` names no pin when an earlier step computes
    NAME and the name says nothing about a pin, so the key is unreadable."""
    scope = _scope(_cache(key), before=before, after=after, env=env)
    assert scope == cs.CacheScope(expected)


def test_a_step_output_of_a_step_that_is_not_a_run_step_is_unreadable():
    producer = {"id": "p", "uses": "actions/checkout@v4"}
    step = _cache("t-${{ steps.p.outputs.v }}")
    assert _scope(step, before=(producer,)) == cs.CacheScope(None)


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("cache-primary-key", cs.CacheScope("tool-2.7.0")),
        ("cache-hit", cs.CacheScope("${{ steps.c.outputs.cache-hit }}")),
        (
            "cache-matched-key",
            cs.CacheScope("${{ steps.c.outputs.cache-matched-key }}"),
        ),
        ("other", cs.CacheScope(None)),
    ],
)
def test_a_cache_steps_outputs_read_as_its_key_or_its_status(output, expected):
    """A cache step's primary key is its own key. Its status outputs hold no
    pin, so they stay as written. Any other output is unknown."""
    producer = dict(_cache("tool-2.7.0"), id="c")
    step = _cache(f"${{{{ steps.c.outputs.{output} }}}}", uses="actions/cache/save@v4")
    assert _scope(step, before=(producer,)) == expected


@pytest.mark.parametrize(
    ("path", "env", "expected"),
    [
        ("~/.cache/x", {}, ("~/.cache/x",)),
        ("$HOME/.npm\n${HOME}/.cache/pip", {}, ("~/.npm", "~/.cache/pip")),
        ("a/./b/", {}, ("a/b",)),
        ("!a/skip\n  \nkeep", {}, ("keep",)),
        ("${{ runner.temp }}/tool-cache", {}, ("**/tool-cache",)),
        ("${{ steps.none.outputs.d }}/tc", {}, ("**/tc",)),
        ("${{ runner.temp }}", {}, ()),
        ("${{ env.D }}", {"D": "/opt/d"}, ("/opt/d",)),
        ("${{ env.D }}", {"D": "${{ runner.temp }}/x"}, ("**/x",)),
        ("${{ env.D }}", {"D": "${{ env.D }}/x"}, ("**/x",)),
        ("${{ env.D }}", {}, ()),
        (["a"], {}, ()),
    ],
    ids=[
        "home",
        "home-variable",
        "normalized",
        "exclusion-and-blank",
        "computed-root",
        "unreadable-root",
        "computed-whole",
        "env",
        "env-computed-root",
        "env-names-itself",
        "env-not-set",
        "not-text",
    ],
)
def test_a_path_reads_env_and_globs_the_rest(path, env, expected):
    """A part of a path the text does not fix is `**`. A line the text fixes no
    part of is left out, because it could be any directory."""
    assert _scope(_cache("k", path), env=env).paths == expected


def test_a_step_env_overrides_the_job_env_in_a_path():
    step = dict(_cache("k", "${{ env.D }}"), env={"D": "/step"})
    assert _scope(step, env={"D": "/job"}).paths == ("/step",)


def _setup(uses, **inputs):
    return {"uses": uses, "with": inputs} if inputs else {"uses": uses}


@pytest.mark.parametrize(
    ("step", "installers"),
    [
        (_setup("actions/setup-python@v5", cache="pip"), {"pip"}),
        (_setup("actions/setup-node@v4", cache="pnpm"), {"pnpm"}),
        (_setup("actions/setup-node@v4", cache=" npm "), {"npm"}),
        (_setup("actions/setup-node@v4"), None),
        (_setup("actions/setup-node@v4", cache=""), None),
        (_setup("actions/setup-node@v4", cache=None), None),
        (_setup("actions/setup-node@v4", cache="false"), None),
        (_setup("actions/setup-node@v4", cache="${{ inputs.m }}"), {"*"}),
        (_setup("actions/setup-node@v4", cache="${{ a || b }}"), {"*"}),
        (_setup("actions/setup-go@v5"), {"go"}),
        (_setup("actions/setup-go@v5", cache=False), None),
        (_setup("actions/setup-dotnet@v4", cache=True), {"dotnet"}),
        (_setup("ruby/setup-ruby@v1", **{"bundler-cache": True}), {"bundle"}),
        (_setup("Swatinem/rust-cache@v2"), {"cargo"}),
        (_setup("astral-sh/setup-uv@v6"), None),
        (_setup("astral-sh/setup-uv@v6", **{"prune-cache": False}), {"uv"}),
        (_setup("astral-sh/setup-uv@v6", **{"prune-cache": "false"}), {"uv"}),
        (
            _setup(
                "astral-sh/setup-uv@v6", **{"enable-cache": False, "prune-cache": False}
            ),
            None,
        ),
        (_setup("astral-sh/setup-uv@v6", **{"prune-cache": "${{ inputs.p }}"}), {"uv"}),
    ],
    ids=[
        "python-pip",
        "node-pnpm",
        "node-padded",
        "node-no-input",
        "node-empty",
        "node-no-value",
        "node-false",
        "node-expression",
        "node-operator",
        "go-on-by-default",
        "go-off",
        "dotnet",
        "ruby",
        "rust-cache",
        "uv-prunes-by-default",
        "uv-no-prune",
        "uv-no-prune-string",
        "uv-off",
        "uv-prune-unknown",
    ],
)
def test_a_setup_action_caches_its_installers_store(step, installers):
    """A setup action caches one installer's store when its input is on. An
    input the module cannot read may name any installer. setup-uv prunes the
    wheels it downloaded unless `prune-cache` is false."""
    expected = (
        None
        if installers is None
        else cs.CacheScope("", installers=frozenset(installers))
    )
    assert _scope(step) == expected


@pytest.mark.parametrize(
    ("path", "expected"),
    [("$HOME/x", "~/x"), ("${HOME}/x", "~/x"), ("/a/$HOME", "/a/$HOME"), ("x", "x")],
)
def test_home_spells_the_home_directory_as_a_tilde(path, expected):
    assert cs.home(path) == expected


@pytest.mark.parametrize(
    ("container", "expected"),
    [
        ({"env": {"A": "1", "__line__": 3}}, {"A": "1"}),
        ({"env": "A=1"}, {}),
        ({}, {}),
        ("not-a-mapping", {}),
    ],
)
def test_env_of_reads_an_env_block(container, expected):
    assert cs.env_of(container) == expected


@pytest.mark.parametrize(
    ("step", "expected"),
    [
        ({"uses": "actions/cache@0057852"}, "actions/cache"),
        ({"uses": " ./local "}, "./local"),
        ({"uses": 3}, ""),
        ({}, ""),
    ],
)
def test_action_name_drops_the_ref(step, expected):
    assert cs.action_name(step) == expected


def test_rendered_joins_the_pieces_of_a_template():
    text = cs.rendered(
        "a-${{ hashFiles('x') }}-${{ 'b' }}", {}, cs.Context((), None, {})
    )
    assert text == cs.Text("a-${{ hashFiles('x') }}-b", ("x",), False)


@pytest.mark.parametrize("template", [None, ["a"], {"a": 1}])
def test_rendered_reads_no_template_that_is_not_text(template):
    assert cs.rendered(template, {}, cs.Context((), None, {})) is None
