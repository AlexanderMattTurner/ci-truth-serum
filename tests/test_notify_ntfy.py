"""Behavior tests for .github/actions/notify-ntfy/notify-ntfy.sh.

The script has two paths that notify nobody and still exit 0: an unset topic,
and a POST that fails. Both are deliberate — a repo that never opted in must not
go red, and a dead ntfy server must not add a second red to a workflow that
already failed. The cost of that choice is that a green notifier run is not
evidence anybody was told.

These tests pin the annotation that closes the gap. Each no-op path must write a
`::warning::` line, which GitHub renders on the run summary, so "the alert did
not arrive" is visible without opening a log. The exit status must stay 0 on
every path, because reddening the notifier is what the annotation exists to
avoid.

The script is driven for real against a stubbed `curl`, so the exit status and
the output are observed rather than asserted about the source text.
"""

import subprocess
from pathlib import Path

import pytest

from tests._helpers import REPO_ROOT

SCRIPT = REPO_ROOT / ".github" / "actions" / "notify-ntfy" / "notify-ntfy.sh"
TOPIC = "a-secret-topic"


def _run(
    tmp_path: Path, *, curl_exit: int | None = 0, **env
) -> subprocess.CompletedProcess:
    """Run the script with `curl` stubbed to exit CURL_EXIT.

    `curl_exit=None` installs no stub at all, for the paths that must return
    before they reach curl.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    if curl_exit is not None:
        curl = bindir / "curl"
        curl.write_text(
            f'#!/usr/bin/env bash\necho "$@" >>"{tmp_path}/curl.log"\nexit {curl_exit}\n',
            encoding="utf-8",
        )
        curl.chmod(0o755)
    return subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True,
        text=True,
        env={"PATH": f"{bindir}:/usr/bin:/bin", **env},
        check=False,
    )


def _curl_argv(tmp_path: Path) -> str:
    log = tmp_path / "curl.log"
    return log.read_text(encoding="utf-8") if log.exists() else ""


# ── the two paths that notify nobody ────────────────────────────────────


def test_an_unset_topic_annotates_the_run_instead_of_only_stderr(tmp_path):
    proc = _run(tmp_path, curl_exit=None)
    assert proc.returncode == 0, "a repo that never opted in must not go red"
    assert "::warning title=ntfy is not configured::" in proc.stdout
    assert "notified nobody" in proc.stdout
    # It returned before curl: no stub was installed, so reaching curl would
    # have been a command-not-found rather than a silent pass.
    assert _curl_argv(tmp_path) == ""


def test_a_failed_delivery_annotates_the_run_and_names_the_exit_status(tmp_path):
    proc = _run(tmp_path, curl_exit=7, NTFY_TOPIC=TOPIC, NTFY_MESSAGE="m")
    assert proc.returncode == 0, "a dead ntfy server must not add a second red"
    assert "::warning title=ntfy delivery failed::" in proc.stdout
    assert "curl exited 7" in proc.stdout
    assert "notified nobody" in proc.stdout


@pytest.mark.parametrize("curl_exit", [1, 6, 28])
def test_every_delivery_failure_is_annotated_not_just_one_code(tmp_path, curl_exit):
    proc = _run(tmp_path, curl_exit=curl_exit, NTFY_TOPIC=TOPIC, NTFY_MESSAGE="m")
    assert f"curl exited {curl_exit}" in proc.stdout


# ── the path that does notify somebody ──────────────────────────────────


def test_a_delivered_notification_raises_no_warning(tmp_path):
    proc = _run(tmp_path, curl_exit=0, NTFY_TOPIC=TOPIC, NTFY_MESSAGE="m")
    assert proc.returncode == 0
    # Non-vacuity for the two tests above: the marker they assert on is absent
    # exactly when the message did arrive, so it tracks delivery and not merely
    # the script having run.
    assert "::warning" not in proc.stdout
    assert "notification sent" in proc.stdout


def test_the_message_and_headers_reach_curl(tmp_path):
    _run(
        tmp_path,
        curl_exit=0,
        NTFY_TOPIC=TOPIC,
        NTFY_MESSAGE="the body",
        NTFY_TITLE="the title",
        NTFY_PRIORITY="3",
        NTFY_TAGS="warning",
        NTFY_CLICK="https://example.invalid/run",
    )
    argv = _curl_argv(tmp_path)
    assert "Title: the title" in argv
    assert "Priority: 3" in argv
    assert "Tags: warning" in argv
    assert "Click: https://example.invalid/run" in argv
    assert "the body" in argv
    assert f"https://ntfy.sh/{TOPIC}" in argv


# ── the topic is a secret ───────────────────────────────────────────────


@pytest.mark.parametrize("curl_exit", [0, 7])
def test_the_topic_never_appears_in_the_output(tmp_path, curl_exit):
    # The annotation names the server and the exit code, never the topic. An
    # annotation is rendered on the run summary, so leaking the topic there is
    # worse than leaking it into a log.
    proc = _run(tmp_path, curl_exit=curl_exit, NTFY_TOPIC=TOPIC, NTFY_MESSAGE="m")
    assert TOPIC not in proc.stdout
    assert TOPIC not in proc.stderr


def test_a_self_hosted_base_url_is_used_and_its_trailing_slash_dropped(tmp_path):
    _run(
        tmp_path,
        curl_exit=0,
        NTFY_TOPIC=TOPIC,
        NTFY_MESSAGE="m",
        NTFY_BASE_URL="https://ntfy.example.invalid/",
    )
    assert f"https://ntfy.example.invalid/{TOPIC}" in _curl_argv(tmp_path)


def test_a_multiline_title_cannot_smuggle_a_header(tmp_path):
    # ntfy carries metadata in HTTP headers, which must be single-line. A newline
    # in the title would otherwise start a header of the caller's choosing.
    _run(
        tmp_path,
        curl_exit=0,
        NTFY_TOPIC=TOPIC,
        NTFY_MESSAGE="m",
        NTFY_TITLE="first\nPriority: 5",
    )
    argv = _curl_argv(tmp_path)
    assert "Title: first  Priority: 5" in argv or "Title: first Priority: 5" in argv
    assert "\nPriority: 5" not in argv
