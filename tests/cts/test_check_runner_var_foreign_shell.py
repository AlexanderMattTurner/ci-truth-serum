"""Tests for ci_truth_serum/check_runner_var_foreign_shell.py — the lint that bans a
runner variable in a step whose shell GitHub does not set up.

The load-bearing tests are the two the module's own docstring claims. The shell
lookup follows YAML precedence, so a `shell:` written inside a `#` comment is not
a shell and a job's `defaults.run.shell` reaches a step that names none. And the
finding fires on the runner variable's name in ANY language, because a foreign
shell's script has no grammar this pack can pick.
"""

from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st

from tests._helpers import load_hook

rv = load_hook("check_runner_var_foreign_shell.py", "check_runner_var_foreign_shell")

HEADER = (
    "name: x\non:\n  push:\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n"
)


def _write(tmp_path: Path, body: str, name: str = "wf.yaml") -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


# ── the rule: a foreign shell may not read a runner variable ─────────────────


def test_a_container_shell_writing_an_output_is_flagged():
    body = HEADER + (
        "      - shell: docker run --rm -v ${{ github.workspace }}:/w img bash {0}\n"
        '        run: echo "result=ok" >> "$GITHUB_OUTPUT"\n'
    )
    found = rv.violations(body)
    assert len(found) == 1
    line, message = found[0]
    assert line == 8
    assert "`$GITHUB_OUTPUT`" in message
    assert "docker run" in message


def test_each_of_the_four_runner_variables_is_named():
    for name in rv.RUNNER_VARS:
        body = HEADER + (
            f'      - shell: perl {{0}}\n        run: open(F, ">>", $ENV{{{name}}});\n'
        )
        found = rv.violations(body)
        assert len(found) == 1, name
        assert f"`${name}`" in found[0][1]


def test_a_step_naming_two_variables_reports_both_once():
    body = HEADER + (
        "      - shell: perl {0}\n"
        "        run: |\n"
        "          print $ENV{GITHUB_ENV};\n"
        "          print $ENV{GITHUB_OUTPUT};\n"
        "          print $ENV{GITHUB_ENV};\n"
    )
    found = rv.violations(body)
    assert len(found) == 1
    assert found[0][1].count("`$GITHUB_ENV`") == 1
    assert "`$GITHUB_OUTPUT`" in found[0][1]


def test_a_composite_action_step_is_flagged():
    body = (
        "name: c\n"
        "description: d\n"
        "runs:\n"
        "  using: composite\n"
        "  steps:\n"
        "    - shell: ruby {0}\n"
        "      run: File.write(ENV['GITHUB_STEP_SUMMARY'], 'hi')\n"
    )
    found = rv.violations(body)
    assert len(found) == 1
    assert "composite action" in found[0][1]


def test_two_offending_steps_are_reported_separately():
    body = HEADER + (
        "      - shell: perl {0}\n"
        "        run: print $ENV{GITHUB_ENV};\n"
        "      - shell: ruby {0}\n"
        "        run: puts ENV['GITHUB_PATH']\n"
    )
    assert len(rv.violations(body)) == 2


# ── the shell lookup is YAML, not text ───────────────────────────────────────


def test_a_shell_written_in_a_comment_is_not_a_shell():
    """A grep for `shell:` finds this line; a YAML parser does not."""
    body = HEADER + (
        "      # shell: docker run --rm img bash {0}\n"
        '      - run: echo "result=ok" >> "$GITHUB_OUTPUT"\n'
    )
    assert rv.violations(body) == []


def test_a_jobs_default_shell_reaches_a_step_that_names_none():
    body = (
        "name: x\n"
        "on:\n"
        "  push:\n"
        "jobs:\n"
        "  build:\n"
        "    runs-on: ubuntu-latest\n"
        "    defaults:\n"
        "      run:\n"
        "        shell: docker run --rm img bash {0}\n"
        "    steps:\n"
        '      - run: echo "k=v" >> "$GITHUB_ENV"\n'
    )
    assert len(rv.violations(body)) == 1


def test_a_workflow_default_shell_reaches_a_step_that_names_none():
    body = (
        "name: x\n"
        "on:\n"
        "  push:\n"
        "defaults:\n"
        "  run:\n"
        "    shell: perl {0}\n"
        "jobs:\n"
        "  build:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: print $ENV{GITHUB_OUTPUT};\n"
    )
    assert len(rv.violations(body)) == 1


def test_a_steps_own_shell_beats_the_jobs_default():
    body = (
        "name: x\n"
        "on:\n"
        "  push:\n"
        "jobs:\n"
        "  build:\n"
        "    runs-on: ubuntu-latest\n"
        "    defaults:\n"
        "      run:\n"
        "        shell: perl {0}\n"
        "    steps:\n"
        "      - shell: bash\n"
        '        run: echo "k=v" >> "$GITHUB_ENV"\n'
    )
    assert rv.violations(body) == []


# ── what must NOT fire ───────────────────────────────────────────────────────


def test_every_shell_github_sets_up_is_clean():
    for shell in ("bash", "sh", "pwsh", "powershell", "python", "cmd"):
        body = HEADER + (
            f"      - shell: {shell}\n"
            '        run: echo "result=ok" >> "$GITHUB_OUTPUT"\n'
        )
        assert rv.violations(body) == [], shell


def test_a_custom_template_re_invoking_a_native_shell_is_clean():
    """`bash -e {0}` still starts bash on the runner, so the four paths resolve."""
    for shell in ("bash -e {0}", "bash --noprofile --norc {0}", "/bin/sh -x {0}"):
        body = HEADER + (
            f"      - shell: {shell}\n"
            '        run: echo "result=ok" >> "$GITHUB_OUTPUT"\n'
        )
        assert rv.violations(body) == [], shell


def test_an_unset_shell_is_the_github_default():
    body = HEADER + '      - run: echo "result=ok" >> "$GITHUB_OUTPUT"\n'
    assert rv.violations(body) == []


def test_a_foreign_shell_that_reads_no_runner_variable_is_clean():
    body = HEADER + ("      - shell: perl {0}\n        run: print 'hello';\n")
    assert rv.violations(body) == []


def test_a_longer_name_containing_a_runner_variable_is_not_one():
    body = HEADER + (
        "      - shell: perl {0}\n"
        "        run: print $ENV{MY_GITHUB_PATHS} . $ENV{GITHUB_OUTPUTS};\n"
    )
    assert rv.violations(body) == []


def test_a_computed_shell_is_skipped():
    """A `${{ }}` shell resolves at run time, so this cannot name it."""
    body = HEADER + (
        "      - shell: ${{ inputs.shell }}\n"
        '        run: echo "result=ok" >> "$GITHUB_OUTPUT"\n'
    )
    assert rv.violations(body) == []


def test_a_uses_step_has_no_script():
    body = HEADER + "      - uses: actions/checkout@v5\n"
    assert rv.violations(body) == []


# ── the opt-out ──────────────────────────────────────────────────────────────


def test_an_annotation_in_the_script_body_suppresses():
    body = HEADER + (
        "      - shell: node {0}\n"
        "        run: |\n"
        "          // allow-runner-var-foreign-shell: node runs on the runner itself\n"
        "          require('fs').appendFileSync(process.env.GITHUB_OUTPUT, 'a=b\\n');\n"
    )
    assert rv.violations(body) == []


def test_an_annotation_above_the_step_suppresses():
    body = HEADER + (
        "      # allow-runner-var-foreign-shell: node runs on the runner itself\n"
        "      - shell: node {0}\n"
        "        run: process.env.GITHUB_OUTPUT\n"
    )
    assert rv.violations(body) == []


def test_a_reasonless_annotation_does_not_suppress():
    body = HEADER + (
        "      # allow-runner-var-foreign-shell\n"
        "      - shell: node {0}\n"
        "        run: process.env.GITHUB_OUTPUT\n"
    )
    assert len(rv.violations(body)) == 1


def test_an_annotation_does_not_drift_onto_the_next_step():
    body = HEADER + (
        "      # allow-runner-var-foreign-shell: only the step below it\n"
        "      - shell: node {0}\n"
        "        run: process.env.GITHUB_OUTPUT\n"
        "      - shell: perl {0}\n"
        "        run: print $ENV{GITHUB_ENV};\n"
    )
    found = rv.violations(body)
    assert len(found) == 1
    assert "`$GITHUB_ENV`" in found[0][1]


# ── unparseable input is a finding, never a clean pass ───────────────────────


def test_unparseable_yaml_is_reported():
    found = rv.violations("jobs:\n  build:\n   steps:\n  - x: [\n")
    assert len(found) == 1
    assert "could not parse as YAML" in found[0][1]


def test_check_file_reads_the_file(tmp_path):
    body = HEADER + ("      - shell: perl {0}\n        run: print $ENV{GITHUB_ENV};\n")
    assert len(rv.check_file(_write(tmp_path, body))) == 1


# ── unit level ───────────────────────────────────────────────────────────────


def test_is_foreign_shell():
    assert rv.is_foreign_shell(None) is False
    assert rv.is_foreign_shell("") is False
    assert rv.is_foreign_shell("pwsh -File {0}") is False
    assert rv.is_foreign_shell("C:\\Windows\\System32\\cmd.exe /C {0}") is False
    assert rv.is_foreign_shell("${{ inputs.shell }} {0}") is False
    assert rv.is_foreign_shell("node {0}") is True
    assert rv.is_foreign_shell("docker run --rm img bash {0}") is True


def test_shell_program_reads_the_first_word_only():
    assert rv.shell_program("docker run --rm img bash {0}") == "docker"
    assert rv.shell_program("/usr/bin/perl -w {0}") == "perl"
    assert rv.shell_program("${{ inputs.shell }} {0}") is None


def test_runner_vars_keeps_first_appearance_order():
    script = "GITHUB_ENV GITHUB_OUTPUT GITHUB_ENV GITHUB_PATH"
    assert rv.runner_vars(script) == ["GITHUB_ENV", "GITHUB_OUTPUT", "GITHUB_PATH"]


# ── crash resistance ─────────────────────────────────────────────────────────

_TOKENS = [
    "jobs:",
    "  build:",
    "    steps:",
    "      - shell: node {0}",
    "      - shell: bash",
    "      - shell: ${{ inputs.s }}",
    '        run: echo "a=b" >> "$GITHUB_OUTPUT"',
    "        run: |",
    "          print $ENV{GITHUB_ENV};",
    "      # allow-runner-var-foreign-shell: reason",
    "runs:",
    "  using: composite",
    "defaults:",
    "  run:",
    "    shell: perl {0}",
    "GITHUB_STEP_SUMMARY",
    "\t",
    "- [",
]


@given(st.lists(st.sampled_from(_TOKENS), max_size=25))
def test_violations_never_raises_and_reports_real_lines(tokens):
    text = "\n".join(tokens)
    found = rv.violations(text)
    assert found == rv.violations(text)
    total = max(len(text.splitlines()), 1)
    for line, message in found:
        assert 1 <= line <= total
        assert isinstance(message, str) and message
