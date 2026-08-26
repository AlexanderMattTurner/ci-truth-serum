"""Tests for ci_truth_serum/check_curl_retry.py — the lint that requires a
retry on a file-writing ``curl`` download.

Drives ``violations()`` directly for the parsing rules and ``main()`` for the
argv/exit-code contract.
"""

import pytest

from tests._helpers import load_hook

mod = load_hook("check_curl_retry.py", "check_curl_retry")

_WRAPPERS = frozenset({"gb_retry", "retry_cmd"})


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
    ],
)
def test_fires_on_single_shot_output_curl(line: str) -> None:
    assert mod.violations(line) == [1]


@pytest.mark.parametrize(
    "text",
    [
        # a retry flag makes it resilient
        'curl -fsSL --retry 3 --retry-delay 2 "$url" -o "$file"',
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


@pytest.mark.parametrize("wrapper", sorted(_WRAPPERS))
def test_a_configured_retry_wrapper_satisfies_the_rule(wrapper: str) -> None:
    assert (
        mod.violations(f'{wrapper} 3 2 curl -fsSL "$url" -o "$file"', _WRAPPERS) == []
    )


def test_an_unconfigured_wrapper_name_does_not_satisfy_the_rule() -> None:
    # With no --retry-wrapper given, only curl's own --retry flag is a retry.
    assert mod.violations('gb_retry 3 2 curl -fsSL "$url" -o "$file"') == [1]


def test_two_downloads_on_one_line_report_once() -> None:
    assert mod.violations('curl -o a "$u"; curl -o b "$u"\n') == [1]


def test_opt_out_needs_no_reason() -> None:
    # curl-retry's marker does not require a stated reason, unlike retry-loop's.
    assert mod.violations('curl -fsSL "$u" -o "$f"  # curl-retry-ok\n') == []


def test_opt_out_on_line_above() -> None:
    text = '# curl-retry-ok: justified\ncurl -fsSL "$u" -o "$f"\n'
    assert mod.violations(text) == []


def test_opt_out_about_a_different_line_does_not_reach_this_one() -> None:
    text = '# curl-retry-ok: something else\ndo_a\ncurl -fsSL "$u" -o "$f"\n'
    assert mod.violations(text) == [3]


# ── the two structural probes shell-lint-parsing.md requires ────────────────
def test_probe_message_string_does_not_fire() -> None:
    assert mod.violations('gb_warn "curl -fsSL \\"$u\\" -o \\"$f\\""\n') == []


def test_probe_heredoc_body_does_not_fire() -> None:
    text = 'cat <<\'EOF\' >/tmp/x\ncurl -fsSL "$u" -o "$f"\nEOF\n'
    assert mod.violations(text) == []


# ── non-vacuity ──────────────────────────────────────────────────────────────
def test_non_vacuous_default_flag_config() -> None:
    """A default run (no --retry-wrapper) still flags a real single-shot download —
    guards against the wrapper flag silently swallowing every case."""
    assert mod.violations('curl -fsSL "$url" -o "$file"\n', frozenset()) == [1]


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


def test_main_names_the_configured_wrappers_in_the_remedy(tmp_path, capsys) -> None:
    path = tmp_path / "s.sh"
    path.write_text('curl -fsSL "$url" -o "$file"\n', encoding="utf-8")
    assert mod.main(["--retry-wrapper", "gb_retry", str(path)]) == 1
    assert "gb_retry" in capsys.readouterr().err


def test_main_falls_back_to_generic_wording_with_no_wrappers_configured(
    tmp_path, capsys
) -> None:
    path = tmp_path / "s.sh"
    path.write_text('curl -fsSL "$url" -o "$file"\n', encoding="utf-8")
    assert mod.main([str(path)]) == 1
    assert "wrap it in your retry helper" in capsys.readouterr().err


def test_main_clean_file_exits_0(tmp_path) -> None:
    path = tmp_path / "s.sh"
    path.write_text('curl -fsSL --retry 3 "$url" -o "$file"\n', encoding="utf-8")
    assert mod.main([str(path)]) == 0
