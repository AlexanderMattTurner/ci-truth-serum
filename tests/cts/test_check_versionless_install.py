"""Tests for ci_truth_serum/check_versionless_install.py — the lint that demands a
version on every install command, so CI cannot silently install different bytes
than the ones that were reviewed.

Drives ``violations()`` for the per-family parsing rules and ``main()`` for the
argv/exit-code contract (including the workflow `run:`-block routing).
"""

import pytest

from tests._helpers import load_hook

mod = load_hook("check_versionless_install.py", "check_versionless_install")


# ── flagged: an install whose spec names no version ──────────────────────
@pytest.mark.parametrize(
    "line",
    [
        "pip install ruff",
        "pip3 install ruff",
        "python3 -m pip install ruff",
        "uv pip install ruff",
        "pip install --upgrade pip",
        "pipx install pre-commit",
        "uv tool install ruff",
        "apt-get install -y shellcheck",
        "sudo apt-get install -y --no-install-recommends docker-sbx",
        "apt install shellcheck",
        "aptitude install shellcheck",
        "npm install -g pnpm",
        "npm i --global prettier",
        "pnpm add -g @scope/tool",
        "yarn global add typescript",
    ],
)
def test_versionless_install_is_flagged(line: str) -> None:
    assert mod.violations(f"#!/usr/bin/env bash\n{line}\n") == [2]


@pytest.mark.parametrize(
    "line",
    [
        "pip install 'ruff>=0.14'",  # a floor is not a pin
        "pip install 'ruff~=0.14.0'",
        "pip install ruff==0.14.5 pytest",  # one pinned, one not
        "apt-get install -y curl=8.5.0-2 shellcheck",
    ],
)
def test_partial_or_loose_pins_are_flagged(line: str) -> None:
    assert mod.violations(f"{line}\n") == [1]


def test_each_unpinned_command_on_its_own_line_is_reported() -> None:
    src = "pip install ruff\npip install ruff==0.14.5\napt-get install -y jq\n"
    assert mod.violations(src) == [1, 3]


def test_install_after_a_separator_is_found() -> None:
    # The `&&` continuation joins both commands into one logical line; the install
    # is not at its start.
    src = "apt-get update -qq &&\n  apt-get install -y shellcheck\n"
    assert mod.violations(src) == [1]


def test_continued_command_is_reported_at_its_first_line() -> None:
    src = "apt-get install -y \\\n  --no-install-recommends \\\n  shellcheck\n"
    assert mod.violations(src) == [1]


def test_install_inside_a_substitution_is_flagged() -> None:
    assert mod.violations("out=$(pip install ruff)\n") == [1]


def test_a_redirect_does_not_hide_the_spec() -> None:
    assert mod.violations("pip install ruff > /dev/null\n") == [1]


# ── not flagged: the version is pinned, or lives where this lint can't judge ──
@pytest.mark.parametrize(
    "line",
    [
        "pip install ruff==0.14.5",
        'pip install "ruff==${RUFF_VERSION}"',
        "pip install ruff==0.14.5 pytest==8.4.2",
        "pipx install pre-commit==4.6.1",
        "uv tool install ruff@0.14.5",
        "uv pip install ruff==0.14.5",
        "apt-get install -y docker-sbx=0.35.0-1",
        "npm install -g pnpm@11.8.0",
        "pnpm add -g @scope/tool@1.0.0",
        "yarn global add typescript@5.7.2",
    ],
)
def test_pinned_installs_pass(line: str) -> None:
    assert mod.violations(f"{line}\n") == []


@pytest.mark.parametrize(
    "line",
    [
        "pip install -r requirements.txt",
        "pip install --requirement requirements.txt",
        "pip install -c constraints.txt ruff",  # the constraints file fixes it
        "pip install --constraint=constraints.txt ruff",
        "pip install -e .",
        "pip install .[dev]",
        "pip install ./dist/pkg-1.0-py3-none-any.whl",
        "python3 -m pip install --user .",
        "pip install https://example.com/pkg-1.0.tar.gz",
        "pip install git+https://github.com/psf/black@24.1.0",
        'apt-get install -y "$PKG"',
        "apt-get install -y ./local.deb",
        "npm install prettier",  # local: the range lands in package.json
        "pnpm add prettier",
        "npm ci",
        "pip install --upgrade",  # no positional spec at all
    ],
)
def test_out_of_scope_or_pinned_elsewhere_passes(line: str) -> None:
    assert mod.violations(f"{line}\n") == []


def test_apt_option_value_is_not_read_as_a_pinned_package() -> None:
    # `-o Dpkg::Options::=--force-confnew` carries an `=`; reading it as the
    # package spec would make an unpinned apt install look pinned.
    src = "apt-get install -o Dpkg::Options::=--force-confnew -y shellcheck\n"
    assert mod.violations(src) == [1]


def test_pip_target_flag_value_is_consumed() -> None:
    assert mod.violations("pip install -t vendor ruff==1.0\n") == []


def test_pipx_spec_flag_pins_the_named_tool() -> None:
    assert mod.violations("pipx install --spec 'ruff==0.14.5' ruff\n") == []


def test_an_unpinned_uv_tool_with_package_is_flagged() -> None:
    # `--with pkg` installs another package, so it is judged like a positional.
    assert mod.violations("uv tool install ruff==0.14.5 --with pytest\n") == [1]


def test_install_in_a_message_string_is_not_a_command() -> None:
    assert mod.violations('echo "next: pip install ruff"\n') == []


def test_install_joined_onto_a_message_is_still_a_command() -> None:
    # The leading `echo` scopes to its own command, not to the whole joined line.
    assert mod.violations('echo "installing" &&\n  pip install ruff\n') == [1]


def test_install_run_through_an_interpreter_string_is_flagged() -> None:
    assert mod.violations('bash -c "pip install ruff"\n') == [1]


def test_install_quoted_directly_after_a_message_command_is_not_run() -> None:
    assert mod.violations('echo "pip install ruff"\n') == []


def test_install_in_a_comment_is_not_a_command() -> None:
    assert mod.violations("#!/bin/bash\n# run pip install ruff first\n") == []


def test_a_command_named_like_an_installer_is_not_one() -> None:
    assert mod.violations("nopip install ruff\nmyapt-get install x\n") == []


# ── the pin-exempt opt-out ───────────────────────────────────────────────
def test_same_line_annotation_opts_out() -> None:
    src = "apt-get install -y curl # pin-exempt: distro index rolls\n"
    assert mod.violations(src) == []


def test_preceding_line_annotation_opts_out() -> None:
    src = "# pin-exempt: distro index rolls\napt-get install -y curl\n"
    assert mod.violations(src) == []


def test_annotation_on_a_later_physical_line_of_the_command_opts_out() -> None:
    src = "apt-get install -y \\\n  curl # pin-exempt: distro index rolls\n"
    assert mod.violations(src) == []


def test_annotation_without_a_reason_does_not_opt_out() -> None:
    assert mod.violations("apt-get install -y curl # pin-exempt\n") == [1]


def test_annotation_two_lines_above_does_not_reach_the_command() -> None:
    src = "# pin-exempt: distro index rolls\n\napt-get install -y curl\n"
    assert mod.violations(src) == [3]


# ── main(): argv, exit codes, workflow routing ───────────────────────────
def test_main_reports_and_exits_nonzero(tmp_path, capsys) -> None:
    script = tmp_path / "install.sh"
    script.write_text("#!/bin/bash\npip install ruff\n", encoding="utf-8")
    assert mod.main([str(script)]) == 1
    err = capsys.readouterr().err
    assert f"{script}:2:" in err
    assert "pin-exempt" in err


def test_main_is_silent_and_zero_on_a_pinned_script(tmp_path, capsys) -> None:
    script = tmp_path / "install.sh"
    script.write_text("#!/bin/bash\npip install ruff==0.14.5\n", encoding="utf-8")
    assert mod.main([str(script)]) == 0
    assert capsys.readouterr().err == ""


def test_main_scans_workflow_run_blocks_and_reports_the_step_line(
    tmp_path, capsys
) -> None:
    workflow = tmp_path / ".github" / "workflows" / "ci.yaml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        "jobs:\n"
        "  build:\n"
        "    steps:\n"
        "      - name: pinned\n"
        "        run: pip install ruff==0.14.5\n"
        "      - name: floating\n"
        "        run: |\n"
        "          pip install pre-commit\n",
        encoding="utf-8",
    )
    assert mod.main([str(workflow)]) == 1
    err = capsys.readouterr().err
    # Reported at the flagged step's first key (line 6), not the pinned step's.
    assert f"{workflow}:6:" in err
    assert f"{workflow}:4:" not in err


def test_main_scans_composite_action_run_blocks(tmp_path, capsys) -> None:
    action = tmp_path / ".github" / "actions" / "setup" / "action.yaml"
    action.parent.mkdir(parents=True)
    action.write_text(
        "runs:\n"
        "  using: composite\n"
        "  steps:\n"
        "    - run: pipx install pre-commit\n"
        "      shell: bash\n",
        encoding="utf-8",
    )
    assert mod.main([str(action)]) == 1
    assert f"{action}:4:" in capsys.readouterr().err


def test_main_ignores_non_workflow_yaml(tmp_path, capsys) -> None:
    data = tmp_path / "data.yaml"
    data.write_text("cmd: pip install ruff\n", encoding="utf-8")
    assert mod.main([str(data)]) == 0
    assert capsys.readouterr().err == ""


def test_main_skips_an_unparseable_workflow(tmp_path, capsys) -> None:
    workflow = tmp_path / ".github" / "workflows" / "broken.yaml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("jobs: [unclosed\n", encoding="utf-8")
    assert mod.main([str(workflow)]) == 0
    assert capsys.readouterr().err == ""


def test_main_skips_a_vanished_path(tmp_path, capsys) -> None:
    assert mod.main([str(tmp_path / "gone.sh")]) == 0
    assert capsys.readouterr().err == ""


# ── a logger's message is a hint for a human, not a command ──────────────
@pytest.mark.parametrize(
    "line",
    [
        'gb_error "install it (macOS: brew install coreutils; Debian: apt install coreutils)"',
        'gb_warn "could not upgrade; run apt-get install docker-sbx by hand"',
        'note "  auto-run: setup.py   [runs on pip install]"',
        'log_info "try pip install ruff"',
        '_die "run apt-get install jq"',
        'warning "pip install x"',
        'ct_debug "npm install -g pnpm"',
    ],
)
def test_project_logger_messages_are_not_commands(line: str) -> None:
    assert mod.violations(f"{line}\n") == []


def test_a_separator_inside_a_message_string_does_not_start_a_command() -> None:
    # The `;` lives inside the quotes, so the text after it is still the logger's
    # message — not a second command whose name happens to be `apt-get`.
    src = 'gb_error "first: brew install x; then: apt install y"\n'
    assert mod.violations(src) == []


def test_an_install_after_a_real_separator_is_still_flagged() -> None:
    # The mirror image: an unquoted `;`/`&&` DOES start a new command, so a
    # leading logger call cannot shield it.
    assert mod.violations('gb_warn "installing"; pip install ruff\n') == [1]
    assert mod.violations('note "x" && apt-get install -y curl\n') == [1]


def test_a_command_whose_name_merely_contains_install_is_not_a_logger() -> None:
    # Non-vacuity for the logger family: `install_deps` is not a print command,
    # so the install after it still fires.
    assert mod.violations("install_deps && pip install ruff\n") == [1]


def test_a_quoted_install_run_by_an_interpreter_is_not_excused() -> None:
    # Quote-awareness must not turn every quoted install into a message: the
    # command in front of it is `bash -c`, not a logger.
    assert mod.violations('bash -c "pip install ruff"\n') == [1]


# ── a quoted install runs only when something executes the string ────────
@pytest.mark.parametrize(
    "line",
    [
        'require_command jq "e.g. apt-get install jq / brew install jq"',
        'gb_error "install it (Debian: apt install coreutils)"',
        'usage "run: pip install ruff"',  # not a name this lint could enumerate
        'fail_with_hint "pipx install pre-commit"',
    ],
)
def test_a_quoted_install_with_no_executor_is_help_text(line: str) -> None:
    assert mod.violations(f"{line}\n") == []


@pytest.mark.parametrize(
    "line",
    [
        'bash -c "pip install ruff"',
        'sh -c "apt-get install -y curl"',
        'eval "pipx install pre-commit"',
        'ssh host "apt-get install -y curl"',
        "xargs -I{} sh -c 'pip install ruff'",
    ],
)
def test_a_quoted_install_an_executor_runs_is_flagged(line: str) -> None:
    assert mod.violations(f"{line}\n") == [1]


def test_an_unquoted_install_after_a_quoted_argument_is_flagged() -> None:
    # The quoted part is a progress message; the install itself is bare.
    assert mod.violations('run_quiet "Installing uv..." pipx install uv\n') == [1]


# ── shell plumbing in the argument list is not a package ─────────────────
@pytest.mark.parametrize(
    "line",
    [
        'apt-get install --only-upgrade -y "$pin_spec" >&2',
        "apt-get install -y docker-sbx=0.35.0-1 >&2",
        "pip install ruff==1.0 >log 2>&1",
        "apt-get install -y curl=8.5.0-2 &",
    ],
)
def test_redirections_are_not_read_as_unpinned_packages(line: str) -> None:
    assert mod.violations(f"{line}\n") == []


def test_a_redirect_does_not_mask_a_real_unpinned_package() -> None:
    # The mirror image: dropping the plumbing must not drop the spec beside it.
    assert mod.violations("apt-get install --only-upgrade -y docker-sbx >&2\n") == [1]
