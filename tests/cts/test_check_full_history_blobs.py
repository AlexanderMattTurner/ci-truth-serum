"""Tests for ci_truth_serum/check_full_history_blobs.py — the lint that refuses a
full-history checkout with no blob filter.

Each test drives `violations()` over real workflow text, so each rule is checked
alone. The `main()` tests run discovery over a scratch repository.
"""

from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests._helpers import load_hook

fhb = load_hook("check_full_history_blobs.py", "check_full_history_blobs")

_CHECKOUT = (
    "      - uses: actions/checkout@abc\n        with:\n          fetch-depth: 0\n"
)
_PUBLIC_ONLY = "${{ !github.event.repository.private && 'blob:none' || '' }}"


def _workflow(job_body: str = _CHECKOUT, *, key: str = "  build:\n") -> str:
    """A one-job workflow whose job key sits on line 2 and whose first
    `fetch-depth:` sits on line 7."""
    return f"jobs:\n{key}    runs-on: ubuntu-latest\n    steps:\n{job_body}"


def _filtered(value: str) -> str:
    return _CHECKOUT + f"          filter: {value}\n"


def _lines(text: str, required: str | None = None) -> list[int]:
    return [line for line, _message in fhb.violations(text, required)]


def test_an_unfiltered_full_history_checkout_fires_on_its_fetch_depth_line() -> None:
    assert _lines(_workflow()) == [7]


@pytest.mark.parametrize("value", ["blob:none", "tree:0", f'"{_PUBLIC_ONLY}"'])
def test_any_filter_passes_by_default(value: str) -> None:
    assert _lines(_workflow(_filtered(value))) == []


def test_a_required_filter_passes_only_its_exact_value() -> None:
    text = _workflow(_filtered(f'"{_PUBLIC_ONLY}"'))
    assert _lines(text, _PUBLIC_ONLY) == []
    assert _lines(_workflow(_filtered("blob:none")), _PUBLIC_ONLY) == [7]
    assert _lines(_workflow(), _PUBLIC_ONLY) == [7]


def test_a_quoted_zero_reads_as_full_history() -> None:
    assert _lines(_workflow().replace("fetch-depth: 0", 'fetch-depth: "0"')) == [7]


@pytest.mark.parametrize("depth", ["1", "50", '"${{ inputs.depth }}"'])
def test_a_shallow_checkout_is_ignored(depth: str) -> None:
    assert _lines(_workflow().replace("fetch-depth: 0", f"fetch-depth: {depth}")) == []


def test_a_checkout_with_no_fetch_depth_is_ignored() -> None:
    assert _lines(_workflow("      - uses: actions/checkout@abc\n")) == []


def test_two_unfiltered_checkouts_report_separately() -> None:
    assert _lines(_workflow(_CHECKOUT + _CHECKOUT)) == [7, 10]


def test_a_step_that_is_not_checkout_is_ignored() -> None:
    body = _CHECKOUT.replace("actions/checkout@abc", "acme/checkout-cache@abc")
    assert _lines(_workflow(body)) == []


@pytest.mark.parametrize("text", ["name: not-a-workflow\n", "jobs: []\n", "- a\n"])
def test_a_document_with_no_jobs_mapping_is_clean(text: str) -> None:
    assert _lines(text) == []


def test_unparseable_yaml_is_itself_a_finding() -> None:
    [(line, message)] = fhb.violations("jobs: [\n")
    assert line == 1
    assert message.startswith("could not parse as YAML")


# ── the opt-out ──────────────────────────────────────────────────────────────

_REASON = "it runs git range-diff"


@pytest.mark.parametrize(
    "key",
    [
        f"  build: # full-blobs-ok: {_REASON}\n",
        f"  # full-blobs-ok: {_REASON}\n  build:\n",
        f"  # full-blobs-ok: {_REASON}\n  # and pushes to a mirror\n  build:\n",
    ],
)
def test_a_reasoned_marker_exempts_the_job(key: str) -> None:
    assert _lines(_workflow(key=key)) == []


def test_a_bare_marker_does_not_exempt() -> None:
    assert _lines(_workflow(key="  build: # full-blobs-ok\n")) == [7]


def test_a_marker_exempts_only_its_own_job() -> None:
    text = _workflow(key=f"  a: # full-blobs-ok: {_REASON}\n")
    other = "  b:\n    runs-on: ubuntu-latest\n    steps:\n" + _CHECKOUT
    assert _lines(text + other) == [13]


def test_a_marker_inside_a_quoted_value_does_not_exempt() -> None:
    key = f'  build:\n    name: "x # full-blobs-ok: {_REASON}"\n'
    assert _lines(_workflow(key=key)) == [8]


def test_the_message_names_the_remedy_and_the_opt_out() -> None:
    [(_line, default)] = fhb.violations(_workflow())
    [(_line, required)] = fhb.violations(_workflow(), _PUBLIC_ONLY)
    assert "`filter: blob:none`" in default
    assert f"`filter: {_PUBLIC_ONLY}`" in required
    assert "# full-blobs-ok: <reason>" in default


# ── main ─────────────────────────────────────────────────────────────────────


def _point_at(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    monkeypatch.setattr(fhb, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(fhb, "WORKFLOWS_DIR", wf)
    return wf


def test_main_reports_each_finding_and_exits_nonzero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    wf = _point_at(tmp_path, monkeypatch)
    (wf / "ci.yaml").write_text(_workflow(_filtered("blob:none")), encoding="utf-8")
    assert fhb.main([]) is None
    with pytest.raises(SystemExit) as stop:
        fhb.main(["--require-filter", _PUBLIC_ONLY])
    assert stop.value.code == 1
    assert "::error file=.github/workflows/ci.yaml,line=7::" in capsys.readouterr().out


def test_main_refuses_an_unknown_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _point_at(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as stop:
        fhb.main(["--require-filtre", "blob:none"])
    assert stop.value.code == 2


@given(st.text(max_size=300))
def test_fuzz_violations_returns_line_findings_or_a_known_error(text) -> None:
    """Fuzz: any text yields findings on real lines, or one of the declared errors."""
    try:
        found = fhb.violations(text)
    except Exception as err:
        raise AssertionError(f"unexpected {type(err).__name__}: {err}") from err
    assert all(1 <= line <= text.count("\n") + 1 for line, _message in found)
