"""Tests for ci_truth_serum/check_unpaged_all.py — the lint that bans an
all-shaped verdict over one unpaged page of a GitHub listing.

Each case is a whole file, because the lint's scope is the file: the defect it
was written from splits the fetch and the reduction across two functions, so a
per-function rule would answer the real case wrong. The cases pin all four
signals it needs, both languages, and the shapes that must stay clean.
"""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests._helpers import load_hook

mod = load_hook("check_unpaged_all.py", "check_unpaged_all")

# The defect, in the shape it shipped: one function fetches a page, a second
# reduces the list it is handed, and neither one holds every signal.
SPLIT_PY = """\
def run_jobs(repository, run_id):
    answer = gh_api(f"repos/{repository}/actions/runs/{run_id}/jobs?per_page=100")
    return answer.get("jobs", [])


def verified_head(jobs, anchor):
    return all(job["conclusion"] == "success" for job in jobs)
"""


def violations(source, path="script.py"):
    return mod.violations(source, path)


# ─── the four signals ────────────────────────────────────────────────────────


def test_the_split_fetch_and_verdict_is_reported():
    assert violations(SPLIT_PY) == [7]


def test_a_reduction_in_the_fetching_function_is_reported():
    source = (
        "def verified(repo, run_id):\n"
        '    answer = gh_api(f"repos/{repo}/actions/runs/{run_id}/jobs")\n'
        '    return all(j["conclusion"] == "success" for j in answer["jobs"])\n'
    )
    assert violations(source) == [3]


def test_any_is_the_same_question_in_the_other_direction():
    # "did any job fail?" over one page misses the failures on page two.
    source = (
        "def failed(repo, run_id):\n"
        '    answer = gh_api(f"repos/{repo}/actions/runs/{run_id}/jobs")\n'
        '    return any(j["conclusion"] == "failure" for j in answer["jobs"])\n'
    )
    assert violations(source) == [3]


def test_a_reduction_with_no_github_read_is_clean():
    # The listing key alone is the workflow YAML's own `jobs:` mapping, which is
    # what every false finding of the first dogfood run turned out to be.
    source = (
        "def every_job_named(doc):\n"
        '    return all(job.get("name") for job in doc["jobs"].values())\n'
    )
    assert violations(source) == []


def test_a_github_read_with_no_listing_key_is_clean():
    source = (
        "def approved(repo, number):\n"
        '    reviews = gh_api(f"repos/{repo}/pulls/{number}/reviews")\n'
        '    return all(r["state"] == "APPROVED" for r in reviews)\n'
    )
    assert violations(source) == []


def test_a_github_read_named_only_in_a_docstring_is_clean():
    # A docstring is prose ABOUT the code, so it is not a read. This was the one
    # finding left over agent-glovebox's 1751 files before docstrings came out.
    source = (
        '"""Reads api.github.com elsewhere."""\n'
        "\n"
        "def every_job_named(doc):\n"
        '    return all(job.get("name") for job in doc["jobs"].values())\n'
    )
    assert violations(source) == []


# ─── paging evidence ─────────────────────────────────────────────────────────


def test_a_total_count_comparison_clears_the_file():
    source = SPLIT_PY.replace(
        '    return answer.get("jobs", [])\n',
        '    listed = answer.get("jobs", [])\n'
        '    if len(listed) != answer.get("total_count", len(listed)):\n'
        "        return []\n"
        "    return listed\n",
    )
    assert violations(source) == []


@pytest.mark.parametrize(
    "pager",
    ["gh.paged_json(path)", "octokit.paginate(path)", "read_all_pages(path)"],
    ids=["paged", "paginate", "pages"],
)
def test_a_call_that_names_paging_clears_the_file(pager):
    source = SPLIT_PY.replace("gh_api(", f"{pager} or gh_api(")
    assert violations(source) == []


def test_a_while_loop_clears_the_file():
    source = SPLIT_PY.replace(
        "def verified_head(jobs, anchor):\n",
        "def wait():\n    while True:\n        break\n\n\ndef verified_head(jobs, anchor):\n",
    )
    assert violations(source) == []


def test_per_page_alone_is_not_paging():
    # `per_page=100` sets the page size and leaves the truncation where it was,
    # so the file carrying it is exactly the defect.
    assert "per_page=100" in SPLIT_PY
    assert violations(SPLIT_PY) == [7]


# A test module that reads a workflow YAML's `jobs` key and also names a GitHub
# path, then asks a question of a fixed tuple of words. No listing is reduced.
LITERAL_PY = """\
RUN_URL = "https://github.example/o/r/actions/runs/7"
STATUS_FUNCTIONS = ("always()", "!cancelled()")


def gated(workflow):
    condition = workflow["jobs"]["publish"]["if"]
    return any(f in condition for f in STATUS_FUNCTIONS)
"""


@pytest.mark.parametrize(
    ("iterated", "expected"),
    [
        pytest.param("STATUS_FUNCTIONS", [], id="a-module-constant-tuple"),
        pytest.param('("always()", "!cancelled()")', [], id="an-inline-tuple"),
        pytest.param('["always()", "!cancelled()"]', [], id="an-inline-list"),
        pytest.param('workflow["jobs"]', [7], id="the-listing-itself"),
    ],
)
def test_a_reduction_over_a_literal_collection_reads_no_listing(iterated, expected):
    source = LITERAL_PY.replace("in STATUS_FUNCTIONS)", f"in {iterated})")
    assert violations(source) == expected


def test_a_constant_rebound_later_is_not_literal():
    source = (
        LITERAL_PY
        + "\n\ndef widen(more):\n    global STATUS_FUNCTIONS\n    STATUS_FUNCTIONS = more\n"
    )
    assert violations(source) == [7]


def test_a_parameter_that_shadows_the_constant_is_not_literal():
    source = LITERAL_PY.replace(
        "def gated(workflow):", "def gated(workflow, STATUS_FUNCTIONS):"
    )
    assert violations(source) == [7]


def test_a_literal_generator_beside_a_listing_generator_is_reported():
    source = LITERAL_PY.replace(
        "for f in STATUS_FUNCTIONS)",
        'for f in STATUS_FUNCTIONS for j in workflow["jobs"])',
    )
    assert violations(source) == [7]


# ─── JavaScript ──────────────────────────────────────────────────────────────


JS_DEFECT = """\
export async function verifiedHead(runId) {
  const answer = await ghApi(`repos/${owner}/${repo}/actions/runs/${runId}/jobs`);
  return answer.jobs.every((job) => job.conclusion === "success");
}
"""


def test_the_javascript_defect_is_reported():
    assert violations(JS_DEFECT, "decide.mjs") == [3]


def test_javascript_some_is_reported_too():
    source = JS_DEFECT.replace(
        'jobs.every((job) => job.conclusion === "success")',
        'jobs.some((job) => job.conclusion === "failure")',
    )
    assert violations(source, "decide.mjs") == [3]


def test_javascript_paging_clears_the_file():
    source = JS_DEFECT.replace("ghApi(", "octokit.paginate(")
    assert violations(source, "decide.mjs") == []


def test_javascript_reads_the_bracket_form_of_the_key():
    source = JS_DEFECT.replace("answer.jobs", 'answer["jobs"]')
    assert violations(source, "decide.mjs") == [3]


def test_a_listing_key_inside_a_javascript_string_is_not_a_read():
    source = (
        "export function label(runId) {\n"
        "  const url = `https://api.github.com/repos/o/r/actions/runs/${runId}`;\n"
        '  return ["jobs"].every((word) => url.includes(word));\n'
        "}\n"
    )
    assert violations(source, "decide.mjs") == []


@pytest.mark.parametrize(
    ("receiver", "expected"),
    [
        pytest.param('["always()", "!cancelled()"]', [], id="an-inline-array"),
        pytest.param("WORDS", [], id="a-top-level-const-array"),
        pytest.param("answer.jobs", [4], id="the-listing-itself"),
    ],
)
def test_javascript_reduction_over_a_literal_array_reads_no_listing(receiver, expected):
    source = (
        'const WORDS = ["always()", "!cancelled()"];\n'
        "export async function gated(runId) {\n"
        "  const answer = await ghApi(`repos/o/r/actions/runs/${runId}/jobs`);\n"
        f"  return {receiver}.some((word) => answer.jobs[0].name.includes(word));\n"
        "}\n"
    )
    assert violations(source, "decide.mjs") == expected


def test_javascript_shadowed_const_is_not_literal():
    source = (
        'const WORDS = ["a"];\n'
        "export async function gated(runId, WORDS) {\n"
        "  const answer = await ghApi(`repos/o/r/actions/runs/${runId}/jobs`);\n"
        "  return WORDS.some((word) => answer.jobs[0].name === word);\n"
        "}\n"
    )
    assert violations(source, "decide.mjs") == [4]


# ─── the opt-out, and paths of neither language ──────────────────────────────


def test_a_reason_annotated_line_is_allowed():
    source = SPLIT_PY.replace(
        "    return all(",
        "    # allow-unpaged-all: the caller already refused a truncated page\n"
        "    return all(",
    )
    assert violations(source) == []


def test_an_annotation_without_a_reason_does_not_excuse_it():
    source = SPLIT_PY.replace(
        "    return all(", "    # allow-unpaged-all\n    return all("
    )
    assert violations(source) == [8]


def test_a_path_of_neither_language_is_not_scanned():
    assert violations(SPLIT_PY, "notes.md") == []
    assert violations(JS_DEFECT, "run.sh") == []


# ─── properties: crash-resistance and in-range line numbers ──────────────────

_FRAGMENTS = st.sampled_from(
    [
        'answer = gh_api("repos/o/r/actions/runs/1/jobs?per_page=100")\n',
        'return all(j["conclusion"] == "success" for j in answer["jobs"])\n',
        "return any(j.ok for j in rows)\n",
        'doc["jobs"].values()\n',
        "listing.jobs.every((j) => j.ok);\n",
        "octokit.paginate(path);\n",
        "while True:\n    break\n",
        '"""api.github.com in prose."""\n',
        "# allow-unpaged-all: reason\n",
        "// allow-unpaged-all: reason\n",
        "def f(:\n",
        "`unterminated\n",
        "\r\n",
        " ",
        "\U0001f600",
        "\\",
        "'",
    ]
)


@given(
    st.lists(_FRAGMENTS, max_size=40).map("".join),
    st.sampled_from(["x.mjs", "x.ts", "x.py"]),
)
def test_violations_never_raises_and_reports_real_lines(text: str, path: str) -> None:
    found = mod.violations(text, path)
    assert all(1 <= lineno <= max(len(text.splitlines()), 1) for lineno in found)


@given(
    st.lists(_FRAGMENTS, max_size=40).map("".join), st.sampled_from(["x.mjs", "x.py"])
)
def test_violations_is_deterministic(text: str, path: str) -> None:
    assert mod.violations(text, path) == mod.violations(text, path)
