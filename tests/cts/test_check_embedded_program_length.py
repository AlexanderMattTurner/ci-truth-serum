"""Tests for ci_truth_serum/check_embedded_program_length.py.

Each case drives the real detector over real shell or workflow source, so the
assertion is on which lines it reports, never on the text of the check.
"""

from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests._helpers import load_hook

mod = load_hook("check_embedded_program_length.py", "check_embedded_program_length")

LONG_BODY = "\n".join(f"value_{n} = {n}" for n in range(9))
SHORT_BODY = "import ssl\nprint(ssl.OPENSSL_VERSION)"
# Long by CHARACTERS on one line: the minified shape the char limit exists for.
MINIFIED = ";".join(f"value_{n}={n}" for n in range(30))
SHORT_ONE_LINE = "import ssl; print(ssl.OPENSSL_VERSION)"


# --- shell: the three shapes ---


def test_a_short_probe_passes() -> None:
    assert mod.violations(f"#!/bin/bash\npython3 -c '{SHORT_BODY}'\n") == []


def test_a_long_c_argument_reds() -> None:
    assert mod.violations(f"#!/bin/bash\npython3 -c '{LONG_BODY}'\n") == [2]


def test_a_long_heredoc_program_reds() -> None:
    source = f"#!/bin/bash\npython3 - <<'PY'\n{LONG_BODY}\nPY\n"
    assert mod.violations(source) == [2]


def test_a_heredoc_program_piped_onward_still_reds() -> None:
    source = f"#!/bin/bash\npython3 - <<'PY' | jq .\n{LONG_BODY}\nPY\n"
    assert mod.violations(source) == [2]


def test_a_program_reaching_the_interpreter_through_read_reds() -> None:
    source = (
        f"#!/bin/bash\nread -r -d '' PROG <<'PY' || true\n{LONG_BODY}\nPY\n"
        'python3 -c "$PROG"\n'
    )
    # Shebang, the read, nine body lines, PY: the use sits on line 13.
    assert mod.violations(source) == [13]


def test_a_variable_assigned_a_long_program_carries_to_its_use() -> None:
    source = f"#!/bin/bash\nPROG='{LONG_BODY}'\npython3 -c \"$PROG\"\n"
    assert mod.violations(source) == [11]


def test_a_short_assignment_is_not_a_bound_program() -> None:
    source = "#!/bin/bash\nPROG=hello\npython3 -c \"print('$PROG')\"\n"
    assert mod.violations(source) == []


def test_a_captured_output_is_not_a_bound_program() -> None:
    # OUT holds what the program printed, so a later call naming it runs no
    # long program. The capture itself is still reported, once, at line 2.
    source = (
        f"#!/bin/bash\nOUT=\"$(python3 - <<'PY'\n{LONG_BODY}\nPY\n)\"\n"
        "python3 -c \"print('$OUT')\"\n"
    )
    assert mod.violations(source) == [2]


def test_a_dollar_name_inside_a_single_quoted_program_is_not_a_reference() -> None:
    # Single quotes expand nothing, so `$PROG` here is program text.
    source = f"#!/bin/bash\nPROG='{LONG_BODY}'\nperl -e 'print $PROG'\n"
    assert mod.violations(source) == []


@pytest.mark.parametrize(
    "binding",
    [
        pytest.param("PROG='{body}'", id="assignment"),
        pytest.param(
            "read -r -d '' PROG <<'PY' || true\n{body}\nPY", id="read-heredoc"
        ),
    ],
)
def test_a_minified_program_behind_a_variable_reds(binding: str) -> None:
    source = f'#!/bin/bash\n{binding.format(body=MINIFIED)}\nnode -e "$PROG"\n'
    assert mod.violations(source) == [len(source.splitlines())]


# --- shell: who counts as the interpreter ---


def test_an_env_scrubbed_invocation_reds() -> None:
    source = f"#!/bin/bash\nenv -i PATH=/usr/bin python3 -c '{LONG_BODY}'\n"
    assert mod.violations(source) == [2]


def test_an_interpreter_named_by_absolute_path_is_still_checked() -> None:
    assert mod.violations(f"#!/bin/bash\n/usr/bin/python3 -c '{LONG_BODY}'\n") == [2]


@pytest.mark.parametrize("interpreter", sorted(mod.INTERPRETERS))
def test_every_declared_interpreter_is_detected(interpreter: str) -> None:
    option = "-e" if interpreter in ("node", "deno", "bun") else "-c"
    source = f"#!/bin/bash\n{interpreter} {option} '{LONG_BODY}'\n"
    assert mod.violations(source) == [2]


def test_a_wrapper_with_no_command_behind_it_is_not_an_interpreter() -> None:
    assert mod.violations("#!/bin/bash\nenv -i\n") == []


def test_an_unknown_wrapper_hides_the_call_until_it_is_registered() -> None:
    source = f"#!/bin/bash\nas_root python3 -c '{LONG_BODY}'\n"
    assert mod.violations(source) == []
    limits = mod.Limits(wrappers=mod.TRANSPARENT_PREFIXES | {"as_root"})
    assert mod.violations(source, limits) == [2]


# --- shell: what is data, not a program ---


def test_a_long_string_that_is_not_a_program_passes() -> None:
    assert mod.violations(f"#!/bin/bash\nprintf '%s' '{LONG_BODY}'\n") == []


def test_an_interpreter_with_no_program_argument_passes() -> None:
    assert mod.violations("#!/bin/bash\npython3 ./tool.py --flag\n") == []


def test_a_heredoc_feeding_a_module_is_data_not_a_program() -> None:
    body = "\n".join(f'  "key{n}": {n},' for n in range(9))
    source = (
        f"#!/bin/bash\npython3 lib/splice.py \"$TEMPLATE\" <<'JSON'\n{{\n{body}\n}}\n"
        "JSON\n"
    )
    assert mod.violations(source) == []


def test_a_heredoc_beside_dash_m_is_data_not_a_program() -> None:
    body = "\n".join(f"line {n}" for n in range(9))
    source = f"#!/bin/bash\npython3 -m json.tool <<'JSON'\n{body}\nJSON\n"
    assert mod.violations(source) == []


def test_a_stdin_program_given_argv_after_the_dash_reds() -> None:
    # `-` names stdin, so `"$f" x` are the program's argv, not a file to run.
    source = f"#!/bin/bash\npython3 - \"$f\" x <<'PY'\n{LONG_BODY}\nPY\n"
    assert mod.violations(source) == [2]


def test_a_bare_interpreter_reading_a_heredoc_reds() -> None:
    assert mod.violations(f"#!/bin/bash\npython3 <<'PY'\n{LONG_BODY}\nPY\n") == [2]


def test_a_preload_option_consumes_its_value_before_the_stdin_operand() -> None:
    source = f"#!/bin/bash\nnode -r ./preload.js - <<'JS'\n{LONG_BODY}\nJS\n"
    assert mod.violations(source) == [2]


def test_a_valueless_option_before_a_heredoc_still_reads_as_stdin() -> None:
    source = f"#!/bin/bash\npython3 -u <<'PY'\n{LONG_BODY}\nPY\n"
    assert mod.violations(source) == [2]


# --- the two probes: text no shell executes ---


def test_probe_the_idiom_inside_a_logger_message_does_not_fire() -> None:
    source = f"#!/bin/bash\nlog_warn \"run python3 -c '{MINIFIED}' by hand\"\n"
    assert mod.violations(source) == []


def test_probe_the_idiom_inside_a_heredoc_written_to_a_file_does_not_fire() -> None:
    source = f"#!/bin/bash\ncat <<'EOF' > doc.txt\npython3 -c '{MINIFIED}'\nEOF\n"
    assert mod.violations(source) == []


# --- shell: a generated script, one level deep ---


def test_a_program_inside_a_generated_script_reds() -> None:
    inner = f"#!/usr/bin/env bash\npython3 -c '{LONG_BODY}'\n"
    source = f"#!/bin/bash\ntee \"$HOOK_DIR/x.sh\" >/dev/null <<'HOOK'\n{inner}HOOK\n"
    assert mod.violations(source) == [2]


def test_a_generated_file_that_is_not_shell_is_not_re_parsed() -> None:
    body = "\n".join(f"key{n} = {n}" for n in range(9))
    source = f"#!/bin/bash\ntee /etc/x.conf >/dev/null <<'CONF'\n{body}\nCONF\n"
    assert mod.violations(source) == []


def test_a_generated_script_honours_its_own_opt_out() -> None:
    inner = (
        "#!/usr/bin/env bash\n# allow-inline-program: the guest has no file system\n"
        f"python3 -c '{MINIFIED}'\n"
    )
    source = f"#!/bin/bash\ncat >hook.sh <<'EOF'\n{inner}EOF\n"
    assert mod.violations(source) == []


# --- shell: one entry per program ---


def test_two_programs_on_one_line_are_both_reported() -> None:
    source = f"#!/bin/bash\npython3 -c '{MINIFIED}' | node -e '{MINIFIED}'\n"
    assert mod.violations(source) == [2, 2]


def test_two_programs_in_one_generated_hook_are_both_reported() -> None:
    body = f"#!/bin/bash\npython3 -c '{MINIFIED}'\nnode -e '{MINIFIED}'\n"
    source = f"#!/bin/bash\ncat >hook.sh <<'EOF'\n{body}EOF\n"
    assert mod.violations(source) == [2, 2]


# --- shell: the opt-out ---


def test_the_allow_annotation_exempts_a_site() -> None:
    source = (
        "#!/bin/bash\n# allow-inline-program: the interpreter is not on PATH yet\n"
        f"python3 -c '{LONG_BODY}'\n"
    )
    assert mod.violations(source) == []


def test_a_bare_annotation_without_a_reason_does_not_exempt() -> None:
    source = f"#!/bin/bash\n# allow-inline-program\npython3 -c '{LONG_BODY}'\n"
    assert mod.violations(source) == [3]


def test_a_program_quoting_the_marker_cannot_exempt_itself() -> None:
    body = "\n".join(
        ["# allow-inline-program: quoted by the program, not by the author"]
        + [f"value_{n} = {n}" for n in range(9)]
    )
    assert mod.violations(f"#!/bin/bash\npython3 -c '{body}'\n") == [2]


def test_a_heredoc_program_quoting_the_marker_cannot_exempt_itself() -> None:
    source = (
        "#!/bin/bash\npython3 - <<'PY'\n"
        f"# allow-inline-program: a python comment\n{LONG_BODY}\nPY\n"
    )
    assert mod.violations(source) == [2]


# --- the thresholds, and that each one contributes ---


@pytest.mark.parametrize(
    ("program", "limits", "expected"),
    [
        pytest.param("a\nb\nc", mod.Limits(lines=2), [2], id="lines-rule-fires"),
        pytest.param("a\nb\nc", mod.Limits(lines=3), [], id="lines-at-limit"),
        pytest.param("x" * 50, mod.Limits(chars=49), [2], id="chars-rule-fires"),
        pytest.param("x" * 50, mod.Limits(chars=50), [], id="chars-at-limit"),
        pytest.param("a\n\n# c\n// d\nb", mod.Limits(lines=2), [], id="skips-comments"),
    ],
)
def test_each_threshold_contributes(
    program: str, limits: object, expected: list[int]
) -> None:
    assert mod.violations(f"#!/bin/bash\npython3 -c '{program}'\n", limits) == expected


def test_each_shape_contributes_on_its_own() -> None:
    # Every shape alone reds, so no shape rides on another's finding.
    shapes = {
        "argument": f"python3 -c '{MINIFIED}'",
        "heredoc": f"python3 - <<'PY'\n{MINIFIED}\nPY",
        "binding": f"P='{MINIFIED}'\npython3 -c \"$P\"",
        "generated": f"cat >x.sh <<'EOF'\n#!/bin/sh\npython3 -c '{MINIFIED}'\nEOF",
    }
    for name, shape in shapes.items():
        assert mod.violations(f"#!/bin/bash\n{shape}\n"), name


def test_the_default_thresholds_are_the_documented_ones() -> None:
    five = "\n".join(f"v{n} = {n}" for n in range(5))
    six = "\n".join(f"v{n} = {n}" for n in range(6))
    assert mod.violations(f"#!/bin/bash\npython3 -c '{five}'\n") == []
    assert mod.violations(f"#!/bin/bash\npython3 -c '{six}'\n") == [2]
    assert mod.violations(f"#!/bin/bash\nnode -e '{'x' * 200}'\n") == []
    assert mod.violations(f"#!/bin/bash\nnode -e '{'x' * 201}'\n") == [2]


# --- workflows: each `run:` value is shell ---

_INDENTED_LONG = "\n".join(f"          {line}" for line in LONG_BODY.splitlines())


def test_a_long_program_in_a_block_scalar_run_reds_at_its_own_line() -> None:
    source = (
        "jobs:\n  a:\n    steps:\n      - name: x\n        run: |\n"
        f"          set -e\n          python3 -c '{MINIFIED}'\n"
    )
    assert mod.workflow_violations(source) == [7]


def test_a_long_program_in_a_plain_scalar_run_reds_on_that_line() -> None:
    source = f"jobs:\n  a:\n    steps:\n      - run: python3 -c '{MINIFIED}'\n"
    assert mod.workflow_violations(source) == [4]


def test_a_heredoc_program_inside_a_run_block_reds() -> None:
    source = (
        "jobs:\n  a:\n    steps:\n      - run: |\n          python3 - <<'PY'\n"
        f"{_INDENTED_LONG}\n          PY\n"
    )
    assert mod.workflow_violations(source) == [5]


def test_a_short_run_block_passes() -> None:
    source = f"jobs:\n  a:\n    steps:\n      - run: python3 -c '{SHORT_ONE_LINE}'\n"
    assert mod.workflow_violations(source) == []


def test_a_workflow_with_no_run_blocks_passes() -> None:
    assert mod.workflow_violations("name: x\non:\n  push: {}\njobs: {}\n") == []


def test_every_run_block_is_reached_not_just_the_first() -> None:
    source = (
        "runs:\n  using: composite\n  steps:\n"
        f"    - run: python3 -c '{MINIFIED}'\n      shell: bash\n"
        f"    - run: node -e '{MINIFIED}'\n      shell: bash\n"
    )
    assert mod.workflow_violations(source) == [4, 6]


def test_two_programs_on_one_workflow_line_are_both_reported() -> None:
    source = (
        "on: push\njobs:\n  build:\n    steps:\n      - run: |\n"
        f"          python3 -c '{MINIFIED}' | node -e '{MINIFIED}'\n"
    )
    assert mod.workflow_violations(source) == [6, 6]


def test_a_run_block_the_grammar_cannot_read_does_not_hide_its_neighbour() -> None:
    collapsing = "\n".join(f'          x="${{line{n}%]*}}"' for n in range(30))
    source = (
        "jobs:\n  a:\n    steps:\n      - run: |\n"
        f"{collapsing}\n      - run: python3 -c '{MINIFIED}'\n"
    )
    assert mod.workflow_violations(source) == [35]


def test_a_yaml_comment_above_the_run_key_opts_out() -> None:
    source = (
        "jobs:\n  a:\n    steps:\n"
        "      # allow-inline-program: the runner has no checkout yet\n"
        f"      - run: python3 -c '{MINIFIED}'\n"
    )
    assert mod.workflow_violations(source) == []


def test_a_marker_inside_a_yaml_value_does_not_opt_out() -> None:
    source = (
        "jobs:\n  a:\n    steps:\n"
        '      - name: "# allow-inline-program: a step name"\n'
        f"        run: python3 -c '{MINIFIED}'\n"
    )
    assert mod.workflow_violations(source) == [5]


def test_a_marker_quoted_by_the_program_in_a_run_value_does_not_opt_out() -> None:
    source = (
        "jobs:\n  a:\n    steps:\n"
        "      - run: |\n"
        f"          python3 -c '# allow-inline-program: x; {MINIFIED}'\n"
    )
    assert mod.workflow_violations(source) == [5]


def test_the_probes_do_not_fire_inside_a_run_block() -> None:
    source = (
        "jobs:\n  a:\n    steps:\n      - run: |\n"
        f"          echo \"python3 -c '{MINIFIED}'\"\n"
        "          cat <<'EOF' > doc.txt\n"
        f"          python3 -c '{MINIFIED}'\n          EOF\n"
    )
    assert mod.workflow_violations(source) == []


# --- workflows: one span, one finding ---

_SIXTEEN = "\n".join(f"          echo step{n}" for n in range(16))


def test_a_run_block_check_inline_run_length_reports_is_left_to_it() -> None:
    source = (
        "jobs:\n  a:\n    steps:\n      - run: |\n"
        f"{_SIXTEEN}\n          python3 -c '{MINIFIED}'\n"
    )
    assert mod.workflow_violations(source) == []


def test_a_run_block_opted_out_of_inline_run_length_is_judged_here() -> None:
    source = (
        "jobs:\n  a:\n    steps:\n      - run: |\n"
        "          # allow-long-run: a bootstrap step that runs before checkout\n"
        f"{_SIXTEEN}\n          python3 -c '{MINIFIED}'\n"
    )
    assert mod.workflow_violations(source) == [22]


def test_an_unscannable_workflow_refuses_rather_than_passing() -> None:
    with pytest.raises(mod.UnscannableWorkflowError):
        mod.workflow_violations('jobs:\n  a: "unterminated\n')


# --- main ---


def _write(path: Path, text: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_main_passes_a_clean_file(tmp_path: Path) -> None:
    path = _write(tmp_path / "clean.sh", f"#!/bin/bash\npython3 -c '{SHORT_BODY}'\n")
    assert mod.main([path]) == 0


def test_main_reds_and_names_the_line_and_the_opt_out(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write(tmp_path / "dirty.sh", f"#!/bin/bash\npython3 -c '{LONG_BODY}'\n")
    assert mod.main([path]) == 1
    err = capsys.readouterr().err
    assert f"{path}:2: a program of more than 5 significant lines" in err
    assert "# allow-inline-program: <reason>" in err


def test_main_takes_the_thresholds_from_the_command_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write(tmp_path / "s.sh", f"#!/bin/bash\npython3 -c '{LONG_BODY}'\n")
    assert mod.main(["--max-lines", "20", path]) == 0
    assert mod.main(["--max-lines", "3", "--max-chars", "1000", path]) == 1
    assert "more than 3 significant lines or 1000 characters" in capsys.readouterr().err


def test_main_takes_a_wrapper_from_the_command_line(tmp_path: Path) -> None:
    path = _write(tmp_path / "w.sh", f"#!/bin/bash\nas_root python3 -c '{LONG_BODY}'\n")
    assert mod.main([path]) == 0
    assert mod.main(["--wrapper", "as_root", path]) == 1


def test_main_routes_a_workflow_path_to_its_run_values(tmp_path: Path) -> None:
    text = f"jobs:\n  a:\n    steps:\n      - run: python3 -c '{MINIFIED}'\n"
    workflow = _write(tmp_path / ".github" / "workflows" / "ci.yaml", text)
    other = _write(tmp_path / "config" / "settings.yaml", text)
    assert mod.main([workflow]) == 1
    assert mod.main([other]) == 0


def test_main_refuses_an_unscannable_workflow_loudly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workflow = _write(
        tmp_path / ".github" / "workflows" / "x.yml", 'a: "unterminated\n'
    )
    assert mod.main([workflow]) == 1
    assert "the YAML scanner cannot read this workflow" in capsys.readouterr().err


def test_main_refuses_a_shell_file_the_grammar_cannot_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = "#!/bin/bash\n" + 'x="${line%]*}"\n' * 30 + f"python3 -c '{LONG_BODY}'\n"
    path = _write(tmp_path / "collapsed.sh", text)
    assert mod.main([path]) == 1
    assert "could not parse" in capsys.readouterr().err


# --- fuzz: any text, never a crash, only real lines, the same answer twice ---

_TOKENS = [
    "python3 -c '",
    'node -e "$P"',
    "python3 - <<'PY'",
    'python3 - "$f" <<PY',
    "PY",
    "read -r -d '' P <<'PY'",
    "P='",
    "cat >x.sh <<'EOF'",
    "#!/bin/sh",
    "EOF",
    "# allow-inline-program: r",
    "env -i A=1 python3 -c 'x'",
    "jobs:",
    "  - run: |",
    "      run: python3 -c 'x'",
    "x" * 60,
    "'",
    '"',
]
_text = st.lists(
    st.one_of(st.sampled_from(_TOKENS), st.text(max_size=12)), max_size=30
).map("\n".join)
_SMALL = mod.Limits(lines=1, chars=20)


@given(_text)
def test_violations_never_crash_and_report_real_lines(text: str) -> None:
    result = mod.violations(text, _SMALL)
    assert mod.violations(text, _SMALL) == result
    assert all(1 <= line <= max(len(text.split("\n")), 1) for line in result)


@given(_text)
def test_workflow_violations_report_real_lines_or_refuse(text: str) -> None:
    try:
        result = mod.workflow_violations(text, _SMALL)
    except mod.UnscannableWorkflowError:
        return
    assert all(1 <= line <= max(len(text.split("\n")), 1) for line in result)
