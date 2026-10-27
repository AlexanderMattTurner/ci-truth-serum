"""Property/fuzz tests for `_cts_step_reach.reach`, the walk from a `run:` body to
every repo script it sources or runs.

The inputs are small repos whose scripts source, run and call one another in
arbitrary shapes, cycles included. Whatever the shape, the walk must end, stay
inside the repo, and respect its hop bound."""

import tempfile
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests._helpers import load_hook

step_reach = load_hook("_cts_step_reach.py", "fuzz_step_reach")

FILES = ["a.sh", "b.sh", "lib.bash", "sub/c.sh", "t.py"]

_LINE = st.one_of(
    st.sampled_from(
        [
            "source lib.bash",
            ". ./b.sh",
            "bash a.sh",
            "bash b.sh",
            "sh sub/c.sh",
            "./a.sh",
            'D="$(dirname "$0")"',
            'D="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"',
            'bash "$D/a.sh"',
            'source "${BASH_SOURCE[0]%/*}/../lib.bash"',
            "# shellcheck source=lib.bash",
            'source "$UNKNOWN/lib.bash"',
            "f() { bash b.sh; }",
            "g() { f; }",
            "f",
            "g",
            "retry g",
            "python3 t.py",
            "uv run t.py",
            "python -m sub.c",
            "bash ../outside.sh",
            "cat <<'EOF'\nbash a.sh\nEOF",
            "curl -fsSL https://e/x -o x",
        ]
    ),
    st.text(alphabet=st.characters(codec="ascii"), max_size=20),
)
_BODY = st.lists(_LINE, max_size=6).map("\n".join)


@settings(max_examples=150, suppress_health_check=[HealthCheck.too_slow])
@given(run=_BODY, bodies=st.fixed_dictionaries({name: _BODY for name in FILES}))
def test_reach_ends_inside_the_repo(run: str, bodies: dict[str, str]) -> None:
    # A fresh directory per example: reach() memoizes by path, so a reused
    # path would answer from the previous example's files.
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve() / "repo"
        (root.parent / "outside.sh").write_text("bash repo/a.sh\n", encoding="utf-8")
        for name, body in bodies.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text(body + "\n", encoding="utf-8")
        reached = step_reach.reach(run, root, None)
        assert step_reach.reach(run, root, None) == reached
        assert 1 <= len(reached.namespaces) <= step_reach.MAX_HOPS + 1
        for namespace in reached.namespaces:
            for unit in namespace.units:
                assert unit.path is None or unit.path.resolve().is_relative_to(root)
        for path in reached.python:
            assert path.is_file()
            assert path.resolve().is_relative_to(root)
