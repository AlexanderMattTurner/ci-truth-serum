"""Tests for ci_truth_serum/check_pinned_downloads.py — the pre-commit lint that demands a
checksum/signature check on every downloaded artifact.

Drives `violations()` directly so each rule is asserted in isolation.
"""

import subprocess
import sys

import pytest

from tests._helpers import HOOKS_DIR, REPO_ROOT, load_hook

_SRC = HOOKS_DIR / "check_pinned_downloads.py"
mod = load_hook("check_pinned_downloads.py", "check_pinned_downloads")


def _flags(text: str) -> list[int]:
    return mod.violations(text)


def test_unverified_curl_with_output_flags() -> None:
    assert _flags('curl -fsSL "$url" -o /usr/local/bin/cosign\nchmod +x x\n') == [1]
    assert _flags("curl -O https://example.com/runsc\ninstall runsc /usr/bin\n") == [1]
    assert _flags("wget -O tool https://example.com/tool\n") == [1]
    assert _flags("curl --output f https://x\ncurl --remote-name https://y\n") == [1, 2]


def test_bare_wget_saves_to_disk_and_is_flagged() -> None:
    # wget (unlike curl) writes to disk by default, so a bare `wget <url>` with no
    # output flag is still an unverified artifact reaching disk.
    assert _flags("wget https://example.com/tool\ninstall tool /usr/bin\n") == [1]
    assert _flags("wget -q https://example.com/x.tar.gz\ntar xf x.tar.gz\n") == [1]


def test_bare_wget_to_stdout_or_null_is_not_an_artifact() -> None:
    # Negative controls: an `-O -`/`-O /dev/null` sink is a probe, not a saved file,
    # and must stay excused even though the bare-wget rule now fires by default.
    # (A stdout sink piped INTO a shell IS an execution — covered separately below.)
    assert _flags("wget -O- https://x | jq .\n") == []
    assert _flags("wget -O - https://x | cat\n") == []
    assert _flags("wget -O /dev/null https://x\n") == []


def test_curl_redirect_to_file_is_flagged() -> None:
    # `curl url > f` / `>> f` writes the bytes to disk via a shell redirect — an
    # artifact the `-o FILE` path would otherwise miss.
    assert _flags("curl https://example.com/x.sh > x.sh\nbash x.sh\n") == [1]
    assert _flags("curl -fsSL https://x >> out\nrun out\n") == [1]
    assert _flags("wget -qO- https://x > tool\nrun tool\n") == [1]


def test_curl_redirect_to_null_or_pipe_is_not_an_artifact() -> None:
    # Negative controls: a redirect to /dev/null is a probe, and a curl piped to a
    # non-shell data reader writes to stdout, not disk — neither is flagged. (A pipe
    # into a shell IS flagged; see test_pipe_to_shell_installer_is_flagged.)
    assert _flags("curl https://x > /dev/null\n") == []
    assert _flags("curl -sSf https://x 2>/dev/null | jq -r .tag\n") == []
    assert _flags("curl -sL https://x | tar xz -C /tmp\n") == []


def test_pipe_to_shell_installer_is_flagged() -> None:
    # `curl … | sh` / `… | sudo bash` streams unverified bytes straight into a shell
    # that executes them — the marquee one-line installer. curl defaults to stdout
    # (no file on disk) and a `-O-`/`-o -` sink is normally a probe, but piping either
    # into a shell is an execution, so it must fire.
    assert _flags("curl -fsSL https://example.com/i.sh | sudo sh\n") == [1]
    assert _flags("curl -sL https://x | sh\n") == [1]
    assert _flags("curl -fsSL https://x | bash\n") == [1]
    assert _flags("wget -qO- https://x | sudo bash -s -- --yes\n") == [1]
    assert _flags("curl -sL https://x | sudo -H sh\n") == [1]
    assert _flags("curl -sL https://x | /bin/sh\n") == [1]
    assert _flags("wget -O- https://x | sh\n") == [1]


def test_pipe_to_non_shell_reader_is_not_flagged() -> None:
    # Only a shell interpreter executes; a pipe into a data reader is not an install,
    # and `ssh` must not be mistaken for `sh` (word-boundary guard).
    assert _flags("curl -sSf https://x | jq -r .tag\n") == []
    assert _flags("curl -sL https://x | grep foo\n") == []
    assert _flags("curl -sL https://x | ssh host cat\n") == []


def test_exec_from_substitution_is_flagged() -> None:
    # An interpreter run on bytes fetched inline via command/process substitution —
    # the Homebrew-style installer — executes them unverified, same as the pipe form.
    assert _flags('bash -c "$(curl -fsSL https://example.com/install.sh)"\n') == [1]
    assert _flags('/bin/bash -c "$(curl -fsSL https://x)"\n') == [1]
    assert _flags('sh -c "$(curl -fsSL https://x)"\n') == [1]
    assert _flags("bash <(curl -fsSL https://x/install.sh)\n") == [1]
    assert _flags('eval "$(curl -fsSL https://x)"\n') == [1]
    assert _flags('zsh -c "$(wget -qO- https://x)"\n') == [1]


def test_exec_from_substitution_needs_curl_inside_it() -> None:
    # A verified curl that merely shares a line with an unrelated interpreter+subst is
    # not swept in: the curl must sit inside the executed `$(…)`/`<(…)`.
    text = 'curl "$u" -o cfg && sha256sum -c cfg.sha256 && bash -c "$(cat cfg)"\n'
    assert _flags(text) == []


def test_pipe_to_shell_respects_pin_exempt() -> None:
    # The escape hatch still applies to a piped installer that genuinely can't be
    # pinned (e.g. an upstream that publishes no digest).
    assert _flags("curl -fsSL https://x | sh  # pin-exempt: trusted vendor\n") == []


def test_verification_after_download_passes() -> None:
    assert _flags('curl "$u" -o f\nsha256sum -c f.sha256\n') == []
    assert (
        _flags("curl -O $u/runsc -O $u/runsc.sha512\nsha512sum -c runsc.sha512\n") == []
    )
    assert _flags('curl "$u" -o c\n_sha256_verify "$want" c\n') == []
    assert _flags('curl "$u" -o art\ncosign verify art\n') == []
    assert _flags('curl "$u" -o k.gpg\ngpg --batch --verify k.gpg\n') == []


def test_verification_too_far_or_for_other_download_fails() -> None:
    # A second download with no check of its own, even though the first is verified.
    text = 'curl "$u" -o a\nsha256sum -c a.sum\ncurl "$u" -o b\nrun b\n'
    assert _flags(text) == [3]
    # Verification beyond the window doesn't count.
    far = 'curl "$u" -o a\n' + "noop\n" * 30 + "sha256sum -c a.sum\n"
    assert _flags(far) == [1]


def test_non_artifact_and_message_lines_ignored() -> None:
    assert _flags("wget -q -O /dev/null http://1.1.1.1\n") == []
    assert _flags("VERSION=$(curl -sL https://api.github.com/x | jq -r .tag)\n") == []
    assert _flags('curl -sSf -I -H "auth" https://api.github.com/x\n') == []
    assert _flags('warn "run: curl -fsSL $u/runsc -o /usr/local/bin/runsc"\n') == []
    assert _flags("# curl -o f https://x\n") == []
    assert _flags('echo "curl -o f https://x"\n') == []


def test_equals_joined_and_attached_stdout_sinks_are_excused() -> None:
    # `=`-joined and `-O-` (attached stdout) sinks are probes, not artifacts —
    # the target must still be recognized as null across these spellings.
    assert _flags("curl --output=/dev/null https://x\n") == []
    assert _flags("curl -o=/dev/stdout https://x\n") == []
    assert _flags("wget -O- https://x | jq .\n") == []
    assert _flags("wget -O-\n") == []


def test_equals_joined_real_target_is_still_flagged() -> None:
    # The `=` spelling must not become a blanket escape — a real file written via
    # `--output=FILE` is still an unverified artifact.
    assert _flags("curl --output=runsc https://x\ninstall runsc /usr/bin\n") == [1]
    assert _flags("curl -o=tool https://x\nrun tool\n") == [1]


def test_pin_exempt_escape_hatch() -> None:
    assert (
        _flags('curl "$u" -o f https://x  # pin-exempt: upstream has no digest\n') == []
    )
    assert _flags('# pin-exempt: see issue 1\ncurl "$u" -o f https://x\n') == []


def test_pin_exempt_only_excuses_the_immediately_preceding_line() -> None:
    # The exemption must sit on the line right above the download (lines[i-1]).
    # A `pin-exempt` two lines up does NOT excuse it -- pins lines[i-1], killing
    # the `i - 1` -> `i >> 1` / `i // 2` index mutants (which alias i-1 only for
    # tiny i). The download here is at index 5, so a wrong index reads a blank.
    far = (
        "# pin-exempt: stale, two lines up\n"
        "noop\n"
        "noop\n"
        "noop\n"
        "noop\n"
        'curl "$u" -o f https://x\n'  # line 6
    )
    assert _flags(far) == [6]
    # ...and exactly one line above DOES excuse it (same download, exemption moved).
    near = "noop\n" * 4 + "# pin-exempt: ok\n" + 'curl "$u" -o f https://x\n'
    assert _flags(near) == []


def test_pin_exempt_on_first_line_download_ignores_wraparound() -> None:
    # A download on line 1 (i == 0) with `pin-exempt` only on the LAST line must
    # still be flagged: the `i > 0` guard blocks the lines[i-1] read, so a
    # negative index can't wrap around to the trailing exemption.
    #
    # NB: `i > 0` -> `i != 0` is an EQUIVALENT mutant (the enumerate index i is
    # always >= 0, so the two agree everywhere) and is left surviving by design;
    # this test pins the i==0 boundary behaviour the real guard exists for.
    text = 'curl "$u" -o f https://x\nnoop\n# pin-exempt: trailing, unrelated\n'
    assert _flags(text) == [1]


# The scan window is 25 lines (ci_truth_serum.check_pinned_downloads._WINDOW). The boundary
# is hardcoded here ON PURPOSE: parametrising on `mod._WINDOW` would let a mutant
# that changes the constant shift the test input in lockstep, so the test could
# never observe the change. Pinning the literal makes the off-by-one mutants
# (the `_WINDOW` NumberReplacer and the `start + _WINDOW + 1` arithmetic) fail.
_WINDOW_LITERAL = 25


@pytest.mark.parametrize(
    ("gap", "expected"),
    [
        (_WINDOW_LITERAL, []),  # verify on the last in-window line -> verified
        (_WINDOW_LITERAL + 1, [1]),  # one line past the window -> unverified
    ],
)
def test_verification_window_boundary_is_exact(gap: int, expected: list[int]) -> None:
    text = 'curl "$u" -o f https://x\n' + "noop\n" * (gap - 1) + "sha256sum -c f\n"
    assert _flags(text) == expected


def test_window_literal_matches_source() -> None:
    # SSOT guard: if _WINDOW changes in the source, the hardcoded boundary above
    # is stale and silently stops testing the real edge. Fail loudly instead.
    assert mod._WINDOW == _WINDOW_LITERAL


def test_same_line_download_and_verify_passes() -> None:
    # A download verified ON ITS OWN LINE is accepted: the window scan starts at
    # `j == start`, and the `j > start` guard must let that first line reach the
    # _VERIFY check rather than treat the download as an immediate "next download".
    # Kills the `j > start` -> `j >= start` mutant (which would abort at j==start,
    # leaving every same-line-verified download wrongly flagged).
    assert _flags('curl "$u" -o f https://x && sha256sum -c f\n') == []


def test_intervening_download_aborts_scan_before_a_later_verify() -> None:
    # The window scan must STOP at the next download so one checksum can't cover an
    # earlier, unrelated fetch. Here download `a` (line 1) is followed by download
    # `b` (line 2), then a checksum (line 3) that only matches `a`. Because `b`
    # intervenes, `a` is unverified and flagged; `b` finds the checksum on line 3
    # and is clean. Kills the `j > start` -> `j < start` / `j is not start`
    # mutants (which never fire the abort, letting `a` reach the later checksum and
    # wrongly pass) -- without it `_flags` would be [].
    text = 'curl "$u" -o a\ncurl "$u" -o b\nsha256sum -c a.sum\n'
    assert _flags(text) == [1]


def test_non_download_line_does_not_halt_the_scan() -> None:
    # A line that is neither a comment/message NOR a download (the `if not
    # _is_artifact_download: continue` branch) must be skipped, not end the loop: a
    # real unverified download AFTER such a line must still be flagged. Kills the
    # `continue` -> `break` mutant on that branch.
    text = "noop\ncurl -o f https://x\nrun f\n"
    assert _flags(text) == [2]


def test_comment_line_does_not_halt_the_scan() -> None:
    # A comment / message line is skipped (continue on the first guard), not a hard
    # stop (break): a real unverified download AFTER a comment line must still be
    # flagged. Kills the `continue` -> `break` mutant on the comment/blank branch.
    text = "# just a note\ncurl -o f https://x\nrun f\n"
    assert _flags(text) == [2]


def test_add_from_url_unverified_flags() -> None:
    # A Dockerfile `ADD <url> <dest>` writes remote bytes into the image and owes a
    # verification exactly like curl/wget, including with build flags like --chmod.
    assert _flags("ADD https://example.com/tool.tar /opt/tool.tar\n") == [1]
    assert _flags("ADD --chmod=755 https://example.com/x /usr/local/bin/x\n") == [1]


def test_add_with_checksum_is_pinned() -> None:
    # Docker's own `ADD --checksum=sha256:<digest>` IS the verification, matched by
    # _VERIFY on the ADD line itself (the window scan starts at j == start).
    digest = "sha256:" + "a" * 64
    assert _flags(f"ADD --checksum={digest} https://example.com/x /opt/x\n") == []


def test_add_followed_by_sha256sum_passes() -> None:
    # A separate checksum within the window verifies the ADD, same as for curl/wget.
    text = "ADD https://example.com/x.tar /tmp/x.tar\nRUN sha256sum -c x.tar.sha256\n"
    assert _flags(text) == []


def test_add_local_source_is_not_a_download() -> None:
    # `ADD` of a local path (build context) fetches nothing — only http(s) URLs do.
    assert _flags("ADD app.tar /opt/\nADD ./src /src\n") == []


def test_add_respects_pin_exempt() -> None:
    assert (
        _flags("ADD https://example.com/x /opt/x  # pin-exempt: vendored mirror\n")
        == []
    )


def test_add_and_curl_each_need_their_own_check() -> None:
    # An ADD and a curl are independent downloads: a checksum for one does not cover
    # the other. The verified curl passes; the trailing unverified ADD is flagged.
    text = 'curl "$u" -o a\nsha256sum -c a.sum\nADD https://x/y /y\n'
    assert _flags(text) == [3]


def test_verification_token_in_comment_does_not_count() -> None:
    # BYPASS FIX (Finding 1): a verification token that lives only in a comment must
    # NOT satisfy the gate — detection runs over a comment-stripped view. Before the
    # fix these returned [] (the TODO comment was read as verification).
    assert _flags("curl -o tool.tar.gz https://x/tool.tar.gz\n# TODO: sha256sum\n") == [
        1
    ]
    assert _flags("curl -o t https://x  # verify: sha256sum -c later\nrun t\n") == [1]
    assert _flags("ADD https://x/y /y\nRUN echo done  # sha256sum was here\n") == [1]


def test_real_verification_still_passes_after_comment_stripping() -> None:
    # Green control for the fix: a genuine sha256sum in executed code (even with a
    # trailing comment on the download line) still verifies.
    assert _flags("curl -o f https://x  # fetch it\nsha256sum -c f.sha256\n") == []
    assert _flags('curl "$u" -o f https://x && sha256sum -c f  # inline check\n') == []


def test_hash_inside_quotes_is_code_not_a_comment() -> None:
    # The AST (not a naive `#` split) decides what a comment is, so a `#` inside a
    # quoted output target is still executed code — the download is a real artifact.
    assert _flags('curl -o "a#b.tar" https://x\nrun a#b.tar\n') == [1]


def test_pin_exempt_bare_substring_does_not_exempt() -> None:
    # BYPASS FIX (Finding 2): `pin-exempt` as a bare substring — in a URL path or a
    # quoted string, not an actual `# pin-exempt:` comment — must NOT opt out.
    assert _flags("curl -o x https://cdn/pin-exempt/x | sh\n") == [1]
    assert _flags('curl -o "pin-exempt" https://x\nrun x\n') == [1]
    # A `#` comment mentioning pin-exempt without a colon-and-reason states nothing.
    assert _flags("curl -o f https://x  # pin-exempt\nrun f\n") == [1]
    assert _flags("curl -o f https://x  # pin-exempt:\nrun f\n") == [1]


def test_main_wires_violations_and_message(
    tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    """main() runs this script's detector through the shared loop with its own
    message. The generic loop behaviour is covered once in test_linecheck.py;
    here we only pin that main() emits THIS message."""
    bad = tmp_path / "bad.sh"
    bad.write_text("curl -o f https://x\nrun f\n")
    assert mod.main([str(bad)]) == 1
    assert "not checksum/signature" in capsys.readouterr().err


def _run_script(*paths: str) -> subprocess.CompletedProcess[str]:
    """Invoke the real script as pre-commit does (paths on argv)."""
    return subprocess.run(
        [sys.executable, str(_SRC), *paths],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    "script",
    [
        'curl -fsSL "$url" -o /usr/local/bin/cosign\nchmod +x cosign\n',  # -o FILE
        "curl -O https://example.com/runsc\ninstall runsc /usr/bin\n",  # curl -O
        "wget -O tool https://example.com/tool\nrun tool\n",  # wget -O FILE
        "curl --output=runsc https://x\ninstall runsc /usr/bin\n",  # =-joined target
        "wget https://example.com/tool\ninstall tool /usr/bin\n",  # bare wget (disk)
        "curl https://example.com/x.sh > x.sh\nbash x.sh\n",  # shell redirect
    ],
)
def test_script_rejects_unverified_download(tmp_path, script: str) -> None:
    """The real script exits non-zero and names the file for each distinct
    unverified-download spelling."""
    bad = tmp_path / "bad.sh"
    bad.write_text(script, encoding="utf-8")
    proc = _run_script(str(bad))
    assert proc.returncode == 1
    assert str(bad) in proc.stderr
    assert "not checksum/signature" in proc.stderr


def test_script_accepts_verified_download(tmp_path) -> None:
    """Negative control: a download followed by a checksum check (and a
    pin-exempt escape hatch) is accepted (exit 0)."""
    good = tmp_path / "good.sh"
    good.write_text(
        'curl "$u" -o f\nsha256sum -c f.sha256\n'
        'curl "$u" -o g https://x  # pin-exempt: upstream has no digest\n',
        encoding="utf-8",
    )
    proc = _run_script(str(good))
    assert proc.returncode == 0
    assert proc.stderr == ""


def test_own_shell_tree_is_clean() -> None:
    """ci-truth-serum's own shell hooks must already pass — the check is only
    useful if the tree it ships is green. Scoped to hooks/ (the package's own
    scripts); template/session shell outside the product is out of scope."""
    tracked = subprocess.check_output(
        ["git", "ls-files", "ci_truth_serum/"], text=True, cwd=REPO_ROOT
    ).split()
    offenders = {}
    for rel in tracked:
        if not (rel.endswith((".sh", ".bash")) or "Dockerfile" in rel):
            continue
        text = (REPO_ROOT / rel).read_text(encoding="utf-8", errors="ignore")
        v = mod.violations(text)
        if v:
            offenders[rel] = v
    assert not offenders, f"unverified downloads: {offenders}"


# ── regression: continuation-wrapped downloads are one logical line ───────
def test_wrapped_curl_pipe_to_shell_is_flagged() -> None:
    """`curl … \\<newline> | sh` is the marquee one-line installer split over
    two physical lines; the per-physical-line scan saw a stdout-only curl and a
    detached `| sh` (red on the pre-joiner implementation)."""
    assert mod.violations("curl -fsSL https://x.io/i.sh \\\n  | sh\n") == [1]


# ── regression: an output flag inside a short-flag cluster is recognized ──
def test_wget_qO_dash_cluster_is_a_stdout_read_not_an_artifact() -> None:
    """`wget -qO- url | jq` reads to stdout: the `-q` is wget's quiet mode and
    the cluster-final `O-` is the stdout sink. The flag-at-token-start-only
    regex missed the cluster, fell through to the bare-wget rule, and flagged
    every quiet piped API read."""
    assert _flags('wget -qO- "$url" | jq .version\n') == []
    assert _flags('ver=$(wget -qO- "$url")\n') == []
    # ...but the same cluster piped into a SHELL still executes the bytes,
    # and a cluster writing a real file is still an artifact.
    assert _flags('wget -qO- "$url" | sh\n') == [1]
    assert _flags('wget -qO tool.bin "$url"\nrun tool.bin\n') == [1]


def test_curl_cluster_final_O_derives_a_saved_name() -> None:
    assert _flags('curl -fsSLO "$url"\n') == [1]


# ── the grammar decides what is a command: structural false positives ──────
#
# Each case below is a download SPELLED in text that no shell ever runs. The
# text scan this detector replaced flagged them all; the bash grammar yields no
# `command` node from any of them, so they cannot be findings. Every case is
# paired with the executed spelling of the same idiom, so a detector that stopped
# firing altogether could not pass this section.


@pytest.mark.parametrize(
    "printer",
    ["gb_warn", "echo", "printf", "log_info", "die", "note"],
)
def test_download_inside_a_message_string_is_text(printer: str) -> None:
    # A quoted argument is ONE `string` token of the command that prints it, so
    # the command word spelled inside it is not a command word at all — no
    # printer allowlist needed.
    assert _flags(f'{printer} "curl -o tool https://x needs a checksum"\n') == []
    # Positive marker: the same fetch, unquoted, is still flagged.
    assert _flags("curl -o tool https://x\n") == [1]


def test_separator_inside_a_quoted_string_starts_no_command() -> None:
    # A `;` / `&&` / `|` inside a string separates nothing: the whole quoted run
    # stays one argument of the printing command.
    assert _flags('gb_warn "a; curl -o t https://x"\n') == []
    assert _flags('gb_warn "a && curl -o t https://x"\n') == []
    assert _flags('gb_warn "curl https://x | sh"\n') == []
    # Positive markers: the executed spellings of the same three lines.
    assert _flags("a; curl -o t https://x\n") == [1]
    assert _flags("a && curl -o t https://x\n") == [1]
    assert _flags("curl https://x | sh\n") == [1]


def test_heredoc_body_written_to_a_file_is_data() -> None:
    # `cat <<'EOF' > doc.txt` writes its body as bytes; nothing executes it, so a
    # download documented in that body is prose. (The `> doc.txt` redirect belongs
    # to `cat`, not to any downloader.)
    doc = "cat <<'EOF' > doc.txt\ncurl -o tool https://x needs a checksum\nEOF\n"
    assert _flags(doc) == []


def test_heredoc_body_fed_to_an_interpreter_is_a_script() -> None:
    # Positive counterpart: the same body handed to a shell IS executed, so the
    # download inside it is flagged — at its own physical line — and a checksum
    # inside the body verifies it.
    assert _flags("bash <<'EOF'\ncurl -o t https://x\nEOF\n") == [2]
    assert _flags("bash <<'EOF'\ncurl -o t https://x\nsha256sum -c t.sha\nEOF\n") == []


def test_redirection_token_is_never_read_as_an_output_target() -> None:
    # `>&2` and `2>…` are `file_redirect` siblings, not arguments, and neither
    # writes the fetched bytes to a file: one dups an FD, the other routes
    # diagnostics. A stdout-only curl carrying either is not an artifact.
    assert _flags("curl -sSf https://x >&2\n") == []
    assert _flags("curl -sSf https://x 2>err.log | jq .\n") == []
    # Positive marker: a real stdout redirect on the same shape IS an artifact.
    assert _flags("curl -sSf https://x > tool\nrun tool\n") == [1]


def test_group_redirect_covers_the_commands_inside_it() -> None:
    # `{ … } > f` / `( … ) > f` redirect the group's stdout, so a fetch inside
    # writes the file even though the redirect is not on its own command.
    assert _flags("{ curl https://x; } > tool\nrun tool\n") == [1]
    assert _flags("( curl https://x ) > tool\nrun tool\n") == [1]
    # ...but inside `{ curl … | jq .; } > f` the fetch writes to `jq`, not the
    # file, so the downloaded bytes never reach disk.
    assert _flags("{ curl -sL https://x | jq .; } > tags\n") == []
    assert _flags("{ curl https://x; } > /dev/null\n") == []


def test_download_inside_an_executed_string_is_a_command() -> None:
    # The one place quoted text holds commands: something runs it. A shell's `-c`
    # script, `eval`'s argument, and `ssh`'s remote command all execute.
    assert _flags('bash -c "curl -o tool https://x"\n') == [1]
    assert _flags('ssh host "curl -o tool https://x"\n') == [1]
    # ...and a pipe into `ssh` is not a pipe into a shell (`ssh` != `sh`).
    assert _flags("curl -sL https://x | ssh host cat\n") == []


def test_a_url_path_segment_is_not_a_command_name() -> None:
    # A `scheme://` token is a value the fetch reads, so its last path segment
    # names no command: `https://cdn/curl` neither downloads nor verifies.
    assert _flags("run https://cdn/curl > tool\n") == []
    assert _flags("curl -o t https://x\nrun https://cdn/sha256sum\n") == [1]
    # Positive marker: a real path to the same binary IS the command.
    assert _flags("/usr/bin/curl -o tool https://x\n") == [1]


def test_a_shell_named_inside_a_later_stages_argument_is_not_the_sink() -> None:
    # The pipe sink is the downstream STAGE's command, not any shell name
    # appearing somewhere after the `|`.
    assert _flags('curl -sL https://x | tee "$(bash -c name)"\n') == []
    # Positive marker: the same fetch piped into a real shell stage fires.
    assert _flags("curl -sL https://x | tee f | sh\n") == [1]


def test_verification_inside_a_message_string_does_not_verify() -> None:
    # A `sha256sum` printed in a message checks nothing — it is one `string`
    # token, not a command. Same rule as the download side, applied to the gate.
    assert _flags('curl -o t https://x\necho "sha256sum -c t"\n') == [1]
    # Positive marker: the executed checksum on the same two lines does verify.
    assert _flags("curl -o t https://x\nsha256sum -c t\n") == []


def test_pin_exempt_on_a_continued_downloads_second_line_excuses_it() -> None:
    # A `\`-continued download is ONE command node reported at its first line; the
    # annotation is accepted on any physical line the command occupies, which is
    # where a reader naturally puts it.
    text = 'curl -fsSL "$url" \\\n  -o tool  # pin-exempt: upstream has no digest\n'
    assert _flags(text) == []
    # Same command without the annotation is flagged at its first line.
    assert _flags('curl -fsSL "$url" \\\n  -o tool\n') == [1]


def test_pathological_input_fails_loudly(
    tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    # tree-sitter-bash allocates quadratically on chained pipelines, so
    # `_bash_ast.parse` refuses such input. main() must report it and FAIL rather
    # than skip the file — a silent skip would false-green exactly the input an
    # adversary controls.
    hostile = tmp_path / "hostile.sh"
    hostile.write_text("cmd |" * 3000 + " cmd\n", encoding="utf-8")
    assert mod.main([str(hostile)]) == 1
    assert "pipe bytes" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        # A later branch's redirect belongs to that branch alone. Without this,
        # `curl` sitting in the package list makes the install look like a fetch
        # that saved a file, and the real download two branches later is then
        # squeezed out of its own verification window.
        ("apt-get install -y ca-certificates curl && echo hi > /s\n", []),
        # The same shape where the redirect IS on the download: still reported.
        ("true && curl -fsSL https://x/k > /k\n", [1]),
        ("true && curl -fsSL https://x/k > /k && sha256sum -c /s\n", []),
        # A group redirects every command inside it, so the climb must not treat
        # these like a `list` — both stay reported.
        ("{ curl -fsSL https://x/k; } > /k\n", [1]),
        ("( curl -fsSL https://x/k ) > /k\n", [1]),
    ],
)
def test_a_list_redirect_belongs_to_its_last_branch_only(
    script: str, expected: list[int]
) -> None:
    assert mod.violations(script) == expected


def test_an_install_list_naming_curl_is_not_a_download() -> None:
    """The shape that reached a consumer: one `RUN` whose first branch installs a
    package called `curl`, whose fourth fetches a key, and whose sixth checksums
    it. Every branch sits under one redirected `list`, so crediting the redirect
    to all of them reported the install line and hid the verification from the
    fetch that needed it."""
    script = (
        "RUN apt-get install -y --no-install-recommends ca-certificates curl && \\\n"
        "    curl -fsSL https://cli.github.com/packages/keyring.gpg \\\n"
        "      -o /etc/apt/keyrings/keyring.gpg && \\\n"
        '    echo "abc  /etc/apt/keyrings/keyring.gpg" \\\n'
        "      > /tmp/k.sha256 && \\\n"
        "    sha256sum -c /tmp/k.sha256\n"
    )
    assert mod.violations(script) == []
