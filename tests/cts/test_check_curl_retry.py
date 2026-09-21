"""Tests for ci_truth_serum/check_curl_retry.py — the lint that requires curl's
own widened retry on a file-writing ``curl`` download.

Drives ``violations()`` and ``findings()`` directly for the parsing rules, and
``main()`` for the argv/exit-code contract.
"""

import subprocess
from pathlib import Path

import pytest

from tests._helpers import HOOKS_DIR, REPO_ROOT, load_hook

_SRC = HOOKS_DIR / "check_curl_retry.py"
mod = load_hook("check_curl_retry.py", "check_curl_retry")

# The flag pair that the consumer config in the field passed to the retired
# `--retry-wrapper`. Kept here to pin that a wrapper no longer exempts anything.
_WRAPPERS = ("gb_retry", "retry_cmd")

_WIDENED = "--retry 3 --retry-all-errors"


@pytest.mark.parametrize(
    "line",
    [
        'curl -fsSL --connect-timeout 10 --max-time 600 "$url" -o "$file"',
        'curl "$url" --output "$file"',
        '  if ! curl -fsSL "$u" -o "$f"; then warn x; fi',
        # bundled short-flag tail: `-fsSLo` == `-f -s -S -L -o`, a real download
        'curl -fsSLo "$f" "$u"',
        # A backslash-continued download is ONE command, so the `-o` on a later
        # line still belongs to the `curl` on the first line.
        'curl -fsSL \\\n  --output "$f" \\\n  "$u"',
        # A bound is not a retry: `timeout` wraps the download but nothing retries it.
        'timeout 30 curl -fsSL "$u" -o "$f"',
        # `--output=<file>` writes to disk exactly as `--output <file>` does.
        'curl -fsSL "$u" --output=/tmp/f',
        # `--retry-delay` alone starts no retry: it only shapes a ladder
        # `--retry` would begin, so the download is still single-shot.
        'curl -fsSL --retry-delay 2 --retry-max-time 60 "$u" -o "$f"',
    ],
)
def test_fires_on_single_shot_output_curl(line: str) -> None:
    assert mod.violations(line) == [1]
    assert mod.violations(line, mod.ARM_MISSING) == [1]
    assert mod.violations(line, mod.ARM_NARROW) == []


@pytest.mark.parametrize(
    "text",
    [
        # a widened retry flag makes it resilient
        f'curl -fsSL {_WIDENED} --retry-delay 2 "$url" -o "$file"',
        # the narrower widener is accepted too
        'curl -fsSL --retry 3 --retry-connrefused "$url" -o "$file"',
        # `--retry=N` is the same flag, spelled with `=`
        'curl -fsSL --retry=3 --retry-all-errors "$u" -o "$f"',
        # no -o: a var-capturing fetch is out of scope
        'json="$(curl -fsSL --connect-timeout 10 "$api")"',
        # a comment
        "# curl -o downloads must carry --retry",
        # a trailing comment quoting the banned form runs nothing
        'true  # curl -fsSL "$u" -o "$f"',
        # an UNQUOTED word list under a message command is a sentence, not a download
        "echo curl -fsSLo /tmp/f url",
        "warn curl -o /tmp/f url",
        # `-o -` / `--output=-` name stdout — a capture, not a download
        'raw="$(curl -s --max-time 6 -o - -w \'%{http_code}\' "$u")"',
        'raw="$(curl -s --output=- "$u")"',
        # the inert body of a quoted-delimiter heredoc is text the script PRINTS
        "cat <<'EOF' >/tmp/help\ncurl -fsSLo /tmp/x https://e/x\nEOF",
        # same-line annotation
        'curl -fsSL "$url" -o "$file"  # curl-retry-ok: one-shot by design',
    ],
)
def test_clean_lines_do_not_fire(text: str) -> None:
    assert mod.violations(text) == []


# ── arm B: a `--retry` too narrow to cover a refused or aborted connection ───
@pytest.mark.parametrize(
    "line",
    [
        'curl -fsSL --retry 3 --retry-delay 2 "$url" -o "$file"',
        'curl -fsSL --retry=6 --retry-max-time 60 "$u" --output "$f"',
        'timeout 30 curl -fsSLo "$f" --retry 3 "$u"',
    ],
)
def test_fires_on_a_narrow_retry(line: str) -> None:
    assert mod.violations(line, mod.ARM_NARROW) == [1]
    assert mod.violations(line, mod.ARM_MISSING) == []


@pytest.mark.parametrize("widener", sorted(mod._WIDENING_FLAGS))  # noqa: SLF001  # pylint: disable=protected-access
def test_each_widening_flag_clears_arm_b(widener: str) -> None:
    # Driven from the module's own set, so a new widening flag fails here until
    # this test knows it. The refusing direction sits directly below.
    assert mod.violations(f'curl -fsSL --retry 3 {widener} "$u" -o "$f"') == []
    assert mod.violations('curl -fsSL --retry 3 "$u" -o "$f"', mod.ARM_NARROW) == [1]


def test_one_line_reports_under_one_arm_only() -> None:
    assert mod.findings('curl -fsSL --retry 3 "$u" -o "$f"') == [(1, mod.ARM_NARROW)]
    assert mod.findings('curl -fsSL "$u" -o "$f"') == [(1, mod.ARM_MISSING)]


# ── a flag the script computes into a variable still counts ─────────────────
def test_a_widening_flag_carried_by_a_variable_clears_arm_b() -> None:
    text = 'widen="--retry-connrefused"\ncurl -fsSL --retry 6 "$widen" -o "$f" "$u"\n'
    assert mod.violations(text) == []


def test_a_retry_flag_carried_by_an_array_clears_arm_a() -> None:
    text = 'OPTS=(--retry 3 --retry-all-errors)\ncurl "${OPTS[@]}" -o "$f" "$u"\n'
    assert mod.violations(text) == []


def test_an_array_carrying_only_a_narrow_retry_still_fires_arm_b() -> None:
    # The refusing counterpart: resolving the variable must not bless the call
    # outright, only credit the flags the variable actually holds.
    text = 'OPTS=(--retry 3)\ncurl "${OPTS[@]}" -o "$f" "$u"\n'
    assert mod.violations(text, mod.ARM_NARROW) == [2]


def test_a_variable_holding_no_retry_flag_leaves_arm_a_firing() -> None:
    text = 'OPTS=(-fsSL --connect-timeout 10)\ncurl "${OPTS[@]}" -o "$f" "$u"\n'
    assert mod.violations(text, mod.ARM_MISSING) == [2]


@pytest.mark.parametrize("destination", sorted(mod._NO_FILE_DESTINATIONS))  # noqa: SLF001  # pylint: disable=protected-access
def test_a_destination_that_holds_no_bytes_is_not_a_download(destination: str) -> None:
    # A throughput probe writing to /dev/null, or a capture into a variable, owes
    # no retry. Driven from the module's set, so a new destination fails here.
    assert mod.violations(f"curl -sS -o {destination} -w '%{{http_code}}' \"$u\"") == []
    assert mod.violations(f'curl -sS --output={destination} "$u"') == []


def test_a_bare_curl_to_a_real_file_still_fires() -> None:
    # The counterpart to the destination exemption: assert the refusing
    # direction over the same command shape, so the exemption above isn't vacuous.
    assert mod.violations("curl -sS -o /tmp/payload -w '%{http_code}' \"$u\"") == [1]


# ── the retired `--retry-wrapper` exempts nothing ───────────────────────────
@pytest.mark.parametrize("wrapper", _WRAPPERS)
def test_a_retry_wrapper_no_longer_exempts_a_download(wrapper: str) -> None:
    # The field defect: `retry_cmd 3 2 curl …` gave up about six seconds after
    # the first failure, because a proxy aborted every CONNECT at once.
    line = (
        f'{wrapper} 3 2 curl -fsSL --connect-timeout 10 --max-time 120 "$url" -o "$tmp"'
    )
    assert mod.violations(line, mod.ARM_MISSING) == [1]


@pytest.mark.parametrize("wrapper", _WRAPPERS)
def test_a_wrapper_around_a_widened_curl_is_clean(wrapper: str) -> None:
    # The positive marker for the rule above: the wrapper is not itself banned,
    # so the only thing arm A asks for is curl's own widened retry.
    assert mod.violations(f'{wrapper} 3 2 curl -fsSL {_WIDENED} "$url" -o "$tmp"') == []


def test_two_downloads_on_one_line_report_once() -> None:
    assert mod.violations('curl -o a "$u"; curl -o b "$u"\n') == [1]


def test_opt_out_needs_no_reason() -> None:
    # curl-retry's marker does not require a stated reason, unlike retry-loop's.
    assert mod.violations('curl -fsSL "$u" -o "$f"  # curl-retry-ok\n') == []


def test_opt_out_covers_both_arms() -> None:
    assert mod.violations('curl --retry 3 -o "$f" "$u"  # curl-retry-ok: POST\n') == []


def test_opt_out_on_line_above() -> None:
    text = '# curl-retry-ok: justified\ncurl -fsSL "$u" -o "$f"\n'
    assert mod.violations(text) == []


def test_opt_out_about_a_different_line_does_not_reach_this_one() -> None:
    text = '# curl-retry-ok: something else\ndo_a\ncurl -fsSL "$u" -o "$f"\n'
    assert mod.violations(text) == [3]


# ── the two structural probes shell-lint-parsing.md requires ────────────────
def test_probe_message_string_does_not_fire() -> None:
    assert mod.violations('gb_warn "curl -fsSL \\"$u\\" -o \\"$f\\""\n') == []


def test_probe_message_string_holding_a_narrow_retry_does_not_fire() -> None:
    # Arm B's own probe: the banned idiom inside a logger's message string.
    assert mod.violations('gb_warn "curl --retry 3 -o f https://x"\n') == []


def test_probe_heredoc_body_does_not_fire() -> None:
    text = 'cat <<\'EOF\' >/tmp/x\ncurl -fsSL "$u" -o "$f"\nEOF\n'
    assert mod.violations(text) == []


def test_probe_heredoc_body_holding_a_narrow_retry_does_not_fire() -> None:
    text = "cat <<'EOF' > doc.txt\ncurl --retry 3 -o f https://x\nEOF\n"
    assert mod.violations(text) == []


def test_the_probes_are_not_vacuous() -> None:
    # The positive marker both probes need: the SAME idiom, executed rather than
    # printed, fires under each arm.
    assert mod.findings('curl -fsSL "$u" -o "$f"\n') == [(1, mod.ARM_MISSING)]
    assert mod.findings("curl --retry 3 -o f https://x\n") == [(1, mod.ARM_NARROW)]


# ── pathological input fails loudly ─────────────────────────────────────────
def test_pathological_input_fails_loudly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An input the grammar refuses to parse is reported and exits 1 — never
    skipped as a silent no-findings pass — and the paths beside it are still
    checked."""
    pathological = tmp_path / "huge.sh"
    pathological.write_text("cmd " + "| cmd " * 3000 + "\n", encoding="utf-8")
    with pytest.raises(mod.PathologicalInputError):
        mod.violations(pathological.read_text(encoding="utf-8"))
    bad = tmp_path / "bad.sh"
    bad.write_text('curl -fsSL "$u" -o "$f"\n', encoding="utf-8")
    assert mod.main([str(pathological), str(bad)]) == 1
    err = capsys.readouterr().err
    assert "pipe bytes" in err
    assert f"{bad}:1: single-shot" in err


# ── main() argv/exit-code contract ───────────────────────────────────────────
def test_main_with_no_files_exits_2(capsys) -> None:
    assert mod.main([]) == 2
    assert "no files to scan" in capsys.readouterr().err


def test_main_with_only_flags_and_no_files_exits_2() -> None:
    assert mod.main(["--retry-wrapper", "gb_retry"]) == 2


def test_main_reports_a_hit_and_exits_1(tmp_path, capsys) -> None:
    path = tmp_path / "s.sh"
    path.write_text('curl -fsSL "$url" -o "$file"\n', encoding="utf-8")
    assert mod.main([str(path)]) == 1
    assert f"{path}:1:" in capsys.readouterr().err


def test_main_gives_each_arm_its_own_message(tmp_path, capsys) -> None:
    path = tmp_path / "s.sh"
    path.write_text(
        'curl -fsSL "$u" -o "$a"\ncurl -fsSL --retry 3 "$u" -o "$b"\n',
        encoding="utf-8",
    )
    assert mod.main([str(path)]) == 1
    err = capsys.readouterr().err
    assert f"{path}:1: {mod.MESSAGES[mod.ARM_MISSING]}" in err
    assert f"{path}:2: {mod.MESSAGES[mod.ARM_NARROW]}" in err
    assert mod.MESSAGES[mod.ARM_MISSING] != mod.MESSAGES[mod.ARM_NARROW]


def test_main_still_accepts_the_retired_wrapper_flag(tmp_path, capsys) -> None:
    # Backward compatibility: a consumer config that still passes the flag keeps
    # running. The download is reported all the same.
    path = tmp_path / "s.sh"
    path.write_text('retry_cmd 3 2 curl -fsSL "$url" -o "$file"\n', encoding="utf-8")
    assert mod.main(["--retry-wrapper=retry_cmd", str(path)]) == 1
    err = capsys.readouterr().err
    assert "--retry-wrapper is retired and ignored (retry_cmd)" in err
    assert f"{path}:1:" in err


def test_main_says_nothing_about_the_flag_when_it_is_absent(tmp_path, capsys) -> None:
    path = tmp_path / "s.sh"
    path.write_text(f'curl {_WIDENED} "$url" -o "$file"\n', encoding="utf-8")
    assert mod.main([str(path)]) == 0
    assert "retired" not in capsys.readouterr().err


def test_main_clean_file_exits_0(tmp_path) -> None:
    path = tmp_path / "s.sh"
    path.write_text(f'curl -fsSL {_WIDENED} "$url" -o "$file"\n', encoding="utf-8")
    assert mod.main([str(path)]) == 0


# ── the shipped tree is clean under this lint ────────────────────────────────
def test_repo_shell_tree_is_clean() -> None:
    """Dogfood: this repo's own tracked shell files must not violate. A finding
    here is either a real single-shot download to fix or a false positive to
    answer, and both must block rather than sit undetected."""
    tracked = subprocess.run(
        ["git", "ls-files", "-z", "*.sh", "*.bash"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\0")
    paths = [path for path in tracked if path]
    assert paths, "no tracked shell files found — the dogfood check would be vacuous"
    result = subprocess.run(
        ["python", "-m", "ci_truth_serum.check_curl_retry", *paths],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_the_module_parses_the_grammar_rather_than_the_text() -> None:
    """Meta-contract (.claude/rules/shell-lint-parsing.md): every structural
    question here is answered by `_cts_bash_ast`, so the module must import the
    parser and must not carry a quote-state scanner or `shlex`."""
    source = _SRC.read_text(encoding="utf-8")
    assert "from _cts_bash_ast import" in source
    assert "shlex" not in source
