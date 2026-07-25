"""Behavior tests for bin/lib/release-model-call.bash.

The library is what keeps the release pipeline alive when one credential is
exhausted, so every test drives the real bash functions under a stubbed `curl`
and asserts on what the caller can observe — the exit code, the credential the
request actually authenticated with, and the text that reaches the log. Nothing
here greps the library's source.

No test makes a network call: `curl` is a stub on PATH that replays a scripted
sequence of HTTP statuses and records the headers it was handed.
"""

import json
import subprocess
from pathlib import Path

import pytest

from tests._helpers import REPO_ROOT

LIB = REPO_ROOT / "bin" / "lib" / "release-model-call.bash"

# A credential-shaped needle: if any log line interpolates a credential VALUE
# instead of its variable NAME, this string shows up in the captured output.
NEEDLE = "sk-ant-api03-NEEDLE-c0ffee-must-never-be-logged"
OAUTH_NEEDLE = "sk-ant-oat01-NEEDLE-c0ffee-must-never-be-logged"

# What the API returns on the statuses the stub replays.
OK_BODY = json.dumps(
    {
        "content": [
            {"type": "tool_use", "input": {"recommended_bump": "patch"}},
        ],
        "stop_reason": "tool_use",
    }
)
ERR_BODY = json.dumps({"error": {"message": "credit balance is too low"}})

# Writes the scripted status for this invocation, honouring `-o FILE` (the body)
# and `-w %{http_code}` (the status on stdout) exactly as real curl does, and
# appending every `-H` value to a header log so a test can tell WHICH rung the
# request authenticated with.
_CURL_STUB = r"""#!/usr/bin/env bash
out=""
prev=""
for a in "$@"; do
  [[ "$prev" == "-o" ]] && out="$a"
  [[ "$prev" == "-H" ]] && printf '%s\n' "$a" >>"$STUB_HEADER_LOG"
  prev="$a"
done
printf -- '--- end of request ---\n' >>"$STUB_HEADER_LOG"
n=$(cat "$STUB_INDEX" 2>/dev/null)
n=$(("${n:-0}" + 1))
printf '%s' "$n" >"$STUB_INDEX"
status=$(sed -n "${n}p" "$STUB_STATUSES")
# Past the end of the script, keep replaying the last status: a test that sets
# one status means "every rung answers this".
[[ -n "$status" ]] || status=$(tail -n 1 "$STUB_STATUSES")
# The 000 sentinel is a transport failure: real curl never connected, so it
# writes NOTHING to the output file. Leaving the file untouched here is what
# lets a test catch a stale body being misattributed to this rung.
if [[ -n "$out" && "$status" != "000" ]]; then
  if [[ "$status" == "200" ]]; then printf '%s' "$STUB_OK_BODY" >"$out"; else printf '%s' "$STUB_ERR_BODY" >"$out"; fi
fi
printf '%s' "$status"
"""


def _sandbox(tmp_path: Path, statuses: list[str]) -> dict[str, str]:
    """Install the curl stub and return the env vars wiring it up."""
    bindir = tmp_path / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    stub = bindir / "curl"
    stub.write_text(_CURL_STUB)
    stub.chmod(0o755)

    status_file = tmp_path / "statuses.txt"
    status_file.write_text("\n".join(statuses) + "\n")
    return {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "STUB_HEADER_LOG": str(tmp_path / "headers.log"),
        "STUB_INDEX": str(tmp_path / "index.txt"),
        "STUB_STATUSES": str(status_file),
        "STUB_OK_BODY": OK_BODY,
        "STUB_ERR_BODY": ERR_BODY,
        # One attempt per rung and no sleeping: the retry budget has its own
        # tests, and every other test wants the ladder, not the backoff.
        "ANTHROPIC_MAX_ATTEMPTS": "1",
        "ANTHROPIC_RETRY_DELAY": "0",
    }


def _run(body: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    """Source the library in a strict-mode shell and run `body`.

    `set -euo pipefail` matches how the release scripts source it, so an unset
    variable or an unguarded non-zero inside the library fails the test rather
    than silently taking a branch.
    """
    script = f'set -euo pipefail\nsource "{LIB}"\n{body}\n'
    return subprocess.run(
        ["bash", "-c", script], env=env, capture_output=True, text=True
    )


def _headers(env: dict[str, str]) -> list[str]:
    log = Path(env["STUB_HEADER_LOG"])
    return log.read_text().splitlines() if log.exists() else []


def _requests(env: dict[str, str]) -> list[list[str]]:
    """The header log split into one list per request."""
    out: list[list[str]] = [[]]
    for line in _headers(env):
        if line == "--- end of request ---":
            out.append([])
        else:
            out[-1].append(line)
    return [r for r in out if r]


# --------------------------------------------------------------------------
# release_credential_ladder
# --------------------------------------------------------------------------


def test_ladder_lists_only_non_empty_rungs_in_order(tmp_path: Path) -> None:
    env = _sandbox(tmp_path, ["200"])
    env |= {
        "ANTHROPIC_API_KEY": "",  # set but empty: still skipped
        "CLAUDE_CODE_OAUTH_TOKEN": "b",
        # CLAUDE_CODE_OAUTH_TOKEN_FALLBACK deliberately unset
        "CLAUDE_CODE_OAUTH_TOKEN_FALLBACK_3": "d",
        "CLAUDE_CODE_OAUTH_TOKEN_FALLBACK_5": "e",
    }
    result = _run("release_credential_ladder", env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == [
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN_FALLBACK_3",
        "CLAUDE_CODE_OAUTH_TOKEN_FALLBACK_5",
    ]


def test_ladder_is_empty_with_no_credentials(tmp_path: Path) -> None:
    env = _sandbox(tmp_path, ["200"])
    result = _run("release_credential_ladder", env)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_ladder_emits_names_never_values(tmp_path: Path) -> None:
    env = _sandbox(tmp_path, ["200"]) | {"ANTHROPIC_API_KEY": NEEDLE}
    result = _run("release_credential_ladder", env)
    assert result.stdout.strip() == "ANTHROPIC_API_KEY"
    assert NEEDLE not in result.stdout


# --------------------------------------------------------------------------
# auth_headers_for
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "credential, expected",
    [
        (
            "sk-ant-oat01-abc",
            [
                "-H",
                "authorization: Bearer sk-ant-oat01-abc",
                "-H",
                "anthropic-beta: oauth-2025-04-20",
                "-H",
                "anthropic-version: 2023-06-01",
            ],
        ),
        (
            "sk-ant-api03-abc",
            [
                "-H",
                "x-api-key: sk-ant-api03-abc",
                "-H",
                "anthropic-version: 2023-06-01",
            ],
        ),
        # Anything that is not an OAuth token takes the API-key form, including
        # a value with no recognizable prefix at all.
        (
            "nonsense",
            ["-H", "x-api-key: nonsense", "-H", "anthropic-version: 2023-06-01"],
        ),
    ],
    ids=["oauth-token", "api-key", "unrecognized"],
)
def test_auth_headers_pick_the_scheme_for_the_credential(
    tmp_path: Path, credential: str, expected: list[str]
) -> None:
    env = _sandbox(tmp_path, ["200"])
    result = _run(
        f'auth_headers_for "{credential}"\nprintf "%s\\n" "${{AUTH_HEADERS[@]}}"', env
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == expected


def test_auth_headers_keep_a_credential_with_whitespace_in_one_argument(
    tmp_path: Path,
) -> None:
    # A credential is arbitrary bytes. A space- or newline-delimited round-trip
    # would split it across two curl arguments and send a truncated secret; the
    # array form must keep it in exactly one.
    env = _sandbox(tmp_path, ["200"])
    weird = "tok en\twith  spaces"
    result = _run(
        f'auth_headers_for "{weird}"\nprintf "%s\\n" "${{#AUTH_HEADERS[@]}}" "${{AUTH_HEADERS[1]}}"',
        env,
    )
    assert result.returncode == 0, result.stderr
    count, header = result.stdout.splitlines()
    assert count == "4"
    assert header == f"x-api-key: {weird}"


# --------------------------------------------------------------------------
# anthropic_call — ladder traversal
# --------------------------------------------------------------------------


def test_first_healthy_rung_answers_and_no_later_rung_is_tried(tmp_path: Path) -> None:
    env = _sandbox(tmp_path, ["200"]) | {
        "ANTHROPIC_API_KEY": "sk-ant-api03-first",
        "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-second",
    }
    out = tmp_path / "response.json"
    result = _run(f'anthropic_call "{{}}" "{out}"', env)
    assert result.returncode == 0, result.stderr
    assert len(_requests(env)) == 1
    assert "x-api-key: sk-ant-api03-first" in _requests(env)[0]
    assert "Model call succeeded on credential ANTHROPIC_API_KEY." in result.stderr
    assert json.loads(out.read_text())["stop_reason"] == "tool_use"


def test_rejected_first_rung_falls_through_to_the_second_and_succeeds(
    tmp_path: Path,
) -> None:
    # The whole point of the ladder: rung 1 is over its usage cap (401), so the
    # call must continue rather than end the run.
    env = _sandbox(tmp_path, ["401", "200"]) | {
        "ANTHROPIC_API_KEY": "sk-ant-api03-exhausted",
        "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-healthy",
    }
    out = tmp_path / "response.json"
    result = _run(f'anthropic_call "{{}}" "{out}"', env)
    assert result.returncode == 0, result.stderr

    requests = _requests(env)
    assert len(requests) == 2
    assert "x-api-key: sk-ant-api03-exhausted" in requests[0]
    assert "authorization: Bearer sk-ant-oat01-healthy" in requests[1]
    assert "anthropic-beta: oauth-2025-04-20" in requests[1]

    assert "Credential ANTHROPIC_API_KEY rejected (HTTP 401)" in result.stderr
    assert (
        "Model call succeeded on credential CLAUDE_CODE_OAUTH_TOKEN." in result.stderr
    )


def test_every_rung_rejected_returns_non_zero(tmp_path: Path) -> None:
    env = _sandbox(tmp_path, ["401"]) | {
        "ANTHROPIC_API_KEY": "a",
        "CLAUDE_CODE_OAUTH_TOKEN": "b",
        "CLAUDE_CODE_OAUTH_TOKEN_FALLBACK": "c",
        "CLAUDE_CODE_OAUTH_TOKEN_FALLBACK_3": "d",
        "CLAUDE_CODE_OAUTH_TOKEN_FALLBACK_5": "e",
    }
    result = _run(f'anthropic_call "{{}}" "{tmp_path / "r.json"}"', env)
    assert result.returncode == 1
    assert len(_requests(env)) == 5
    # Every rung is named in the log, so the operator can see which secrets to fix.
    for name in (
        "ANTHROPIC_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN_FALLBACK",
        "CLAUDE_CODE_OAUTH_TOKEN_FALLBACK_3",
        "CLAUDE_CODE_OAUTH_TOKEN_FALLBACK_5",
    ):
        assert f"Credential {name} rejected (HTTP 401)" in result.stderr
    assert (
        "Every one of the 5 configured credential rungs was rejected" in result.stderr
    )


def test_no_credentials_at_all_returns_non_zero_and_makes_no_request(
    tmp_path: Path,
) -> None:
    env = _sandbox(tmp_path, ["200"])
    result = _run(f'anthropic_call "{{}}" "{tmp_path / "r.json"}"', env)
    assert result.returncode == 1
    assert _requests(env) == []
    assert "No release credential is set" in result.stderr


@pytest.mark.parametrize("code", ["400", "401", "403"])
def test_terminal_status_is_not_retried_on_the_same_rung(
    tmp_path: Path, code: str
) -> None:
    env = _sandbox(tmp_path, [code]) | {
        "ANTHROPIC_API_KEY": "a",
        "ANTHROPIC_MAX_ATTEMPTS": "3",
    }
    result = _run(f'anthropic_call "{{}}" "{tmp_path / "r.json"}"', env)
    assert result.returncode == 1
    # One request, not three: a revoked/capped credential fails identically on
    # every retry, so the budget must go to the next rung instead.
    assert len(_requests(env)) == 1
    assert "moving to the next rung" in result.stderr


@pytest.mark.parametrize("code", ["000", "429", "500", "503"])
def test_retryable_status_is_retried_on_the_same_rung(
    tmp_path: Path, code: str
) -> None:
    env = _sandbox(tmp_path, [code, code, "200"]) | {
        "ANTHROPIC_API_KEY": "a",
        "ANTHROPIC_MAX_ATTEMPTS": "3",
    }
    result = _run(f'anthropic_call "{{}}" "{tmp_path / "r.json"}"', env)
    assert result.returncode == 0, result.stderr
    assert len(_requests(env)) == 3
    assert "Model call succeeded on credential ANTHROPIC_API_KEY." in result.stderr


def test_retry_budget_is_spent_per_rung_then_the_ladder_advances(
    tmp_path: Path,
) -> None:
    env = _sandbox(tmp_path, ["500", "500", "200"]) | {
        "ANTHROPIC_API_KEY": "sk-ant-api03-flaky",
        "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-healthy",
        "ANTHROPIC_MAX_ATTEMPTS": "2",
    }
    result = _run(f'anthropic_call "{{}}" "{tmp_path / "r.json"}"', env)
    assert result.returncode == 0, result.stderr
    requests = _requests(env)
    assert len(requests) == 3
    assert "x-api-key: sk-ant-api03-flaky" in requests[0]
    assert "x-api-key: sk-ant-api03-flaky" in requests[1]
    assert "authorization: Bearer sk-ant-oat01-healthy" in requests[2]


# --------------------------------------------------------------------------
# Credential values must never reach a log
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "credential", [NEEDLE, OAUTH_NEEDLE], ids=["api-key", "oauth-token"]
)
def test_rung_failure_report_names_the_variable_not_its_value(
    tmp_path: Path, credential: str
) -> None:
    env = _sandbox(tmp_path, ["401"]) | {"ANTHROPIC_API_KEY": credential}
    (tmp_path / "err.json").write_text(ERR_BODY)
    result = _run(
        f'_report_rung_failure ANTHROPIC_API_KEY 401 "{tmp_path / "err.json"}"',
        env,
    )
    assert result.returncode == 0, result.stderr
    assert "Credential ANTHROPIC_API_KEY rejected (HTTP 401)" in result.stderr
    # The API's own reason is quoted, so an exhausted cap reads as one.
    assert "credit balance is too low" in result.stderr
    assert credential not in result.stderr + result.stdout


@pytest.mark.parametrize(
    "credential", [NEEDLE, OAUTH_NEEDLE], ids=["api-key", "oauth-token"]
)
def test_full_ladder_traversal_never_logs_a_credential_value(
    tmp_path: Path, credential: str
) -> None:
    env = _sandbox(tmp_path, ["401"]) | {
        "ANTHROPIC_API_KEY": credential,
        "CLAUDE_CODE_OAUTH_TOKEN": credential + "-second",
    }
    result = _run(f'anthropic_call "{{}}" "{tmp_path / "r.json"}"', env)
    assert result.returncode == 1
    combined = result.stdout + result.stderr
    assert credential not in combined
    # Non-vacuity: the run really did report both rungs, so the absence above is
    # a scrubbed log rather than an empty one.
    assert "Credential ANTHROPIC_API_KEY rejected" in combined
    assert "Credential CLAUDE_CODE_OAUTH_TOKEN rejected" in combined


def test_a_rung_never_inherits_the_previous_rungs_error_message(
    tmp_path: Path,
) -> None:
    # Rung 1 is rejected with a real API message; rung 2's request never reaches
    # the API, so curl writes no body. Quoting the leftover body would pin rung
    # 1's reason to rung 2's name in the log a human is told to go read.
    env = _sandbox(tmp_path, ["401", "000"]) | {
        "ANTHROPIC_API_KEY": "a",
        "CLAUDE_CODE_OAUTH_TOKEN": "b",
    }
    result = _run(f'anthropic_call "{{}}" "{tmp_path / "r.json"}"', env)
    assert result.returncode == 1
    assert (
        "Credential ANTHROPIC_API_KEY rejected (HTTP 401): credit balance is too low"
        in result.stderr
    )
    assert (
        "Credential CLAUDE_CODE_OAUTH_TOKEN failed (HTTP 000); response body was not Anthropic-shaped."
        in result.stderr
    )
    assert "CLAUDE_CODE_OAUTH_TOKEN rejected (HTTP 000)" not in result.stderr


def test_non_json_error_body_still_reports_the_rung(tmp_path: Path) -> None:
    env = _sandbox(tmp_path, ["502"])
    body = tmp_path / "gateway.html"
    body.write_text("<html>502 Bad Gateway</html>")
    result = _run(f'_report_rung_failure CLAUDE_CODE_OAUTH_TOKEN 502 "{body}"', env)
    assert result.returncode == 0, result.stderr
    assert (
        "Credential CLAUDE_CODE_OAUTH_TOKEN failed (HTTP 502); response body was not Anthropic-shaped."
        in result.stderr
    )


# --------------------------------------------------------------------------
# bump_from_fragments — the deterministic floor
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fragments, expected",
    [
        ([], "patch"),
        (["README.md"], "patch"),
        (["a.fixed.md"], "patch"),
        (["a.security.md"], "patch"),
        (["a.fixed.md", "b.security.md"], "patch"),
        (["a.added.md"], "minor"),
        (["a.changed.md"], "minor"),
        (["a.removed.md"], "minor"),
        (["a.deprecated.md"], "minor"),
        # One surface-changing fragment carries the whole set to minor.
        (["a.fixed.md", "b.security.md", "c.added.md"], "minor"),
        # A malformed name has no category to read, so it takes the floor.
        (["nocategory.md"], "patch"),
        # Not a fragment at all: the *.md glob never sees it.
        (["notes.txt"], "patch"),
    ],
    ids=[
        "empty-dir",
        "readme-only",
        "fixed",
        "security",
        "fixed-and-security",
        "added",
        "changed",
        "removed",
        "deprecated",
        "mixed-with-added",
        "no-category",
        "non-markdown",
    ],
)
def test_bump_from_fragments(
    tmp_path: Path, fragments: list[str], expected: str
) -> None:
    env = _sandbox(tmp_path, ["200"])
    frag_dir = tmp_path / "changelog.d"
    frag_dir.mkdir()
    for name in fragments:
        (frag_dir / name).write_text("- an entry\n")
    result = _run(f'bump_from_fragments "{frag_dir}"', env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


def test_bump_from_fragments_floors_at_patch_for_a_missing_directory(
    tmp_path: Path,
) -> None:
    # The floor must survive the worst input the caller can hand it: under
    # `set -euo pipefail` an unmatched glob must not abort the release run.
    env = _sandbox(tmp_path, ["200"])
    result = _run(f'bump_from_fragments "{tmp_path / "does-not-exist"}"', env)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "patch"


def test_bump_from_fragments_ignores_the_readme_alongside_real_fragments(
    tmp_path: Path,
) -> None:
    # README.md sits in changelog.d/ permanently; reading "md" as its category
    # would be a silent misclassification, so pin that it is skipped rather than
    # merely absent from the minor set.
    env = _sandbox(tmp_path, ["200"])
    frag_dir = tmp_path / "changelog.d"
    frag_dir.mkdir()
    (frag_dir / "README.md").write_text("how to write fragments\n")
    (frag_dir / "a.fixed.md").write_text("- a fix\n")
    result = _run(f'bump_from_fragments "{frag_dir}"', env)
    assert result.stdout.strip() == "patch"
