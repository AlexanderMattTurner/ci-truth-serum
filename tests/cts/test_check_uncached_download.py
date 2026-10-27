"""Tests for ci_truth_serum/check_uncached_download.py — the (opinionated) lint that
requires a cache in any job that downloads a version-pinned tool.

Drives check_file(path) directly so each rule is asserted in isolation. The
composite-action cases point the module's REPO_ROOT at the tmp tree, because that
is what `uses: ./…` resolves against."""

from pathlib import Path

import pytest

from tests._helpers import load_hook

uc = load_hook("check_uncached_download.py", "check_uncached_download")

CACHE_STEP = "      - uses: actions/cache@0057852bfaa89a56745cba8c7296529d2fc39830\n"


def _write(tmp_path: Path, body: str, name: str = "wf.yaml") -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def _job(steps: str, name: str = "build") -> str:
    return f"name: x\non:\n  push:\njobs:\n  {name}:\n    runs-on: ubuntu-latest\n    steps:\n{steps}"


def _run(script: str) -> str:
    """A `run:` step whose body is SCRIPT, indented into a job's step list."""
    body = "\n".join(f"          {line}" for line in script.splitlines())
    return f"      - name: install\n        run: |\n{body}\n"


def _messages(path: Path) -> list[str]:
    return [message for _, message in uc.check_file(path)]


# ── flagged ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("script", "named"),
    [
        ("pip install ruff==0.14.0", "ruff==0.14.0"),
        ('pip install "ruff==${RUFF_VERSION}"', "ruff==${RUFF_VERSION}"),
        ("uv tool install pre-commit==4.6.1", "pre-commit==4.6.1"),
        ("npm install -g @anthropic-ai/claude-code@2.1.201", "claude-code@2.1.201"),
        ("npm install --prefix /tmp/j '@jazzer.js/core@4.0.0'", "core@4.0.0"),
        ("yarn global add pre-commit@4.6.1", "pre-commit@4.6.1"),
        (
            "curl -fsSL -o t https://github.com/o/r/releases/download/v2.12.0/t",
            "releases/download/v2.12.0/t",
        ),
        (
            'curl -O "https://example.com/dl/${TOOL_VERSION}/t.tgz"',
            "${TOOL_VERSION}",
        ),
        ("sudo curl -O https://example.com/x/v1.2.3/t.tgz", "v1.2.3"),
    ],
    ids=[
        "pip",
        "pip-var",
        "uv-tool",
        "npm-global",
        "npm-prefix",
        "yarn-global",
        "curl",
        "curl-var",
        "sudo-curl",
    ],
)
def test_a_pinned_download_in_an_uncached_job_is_flagged(tmp_path, script, named):
    """Each install family this lint knows, alone in a job with no cache."""
    messages = _messages(_write(tmp_path, _job(_run(script))))
    assert len(messages) == 1, messages
    assert named in messages[0]
    assert "build" in messages[0]


def test_the_finding_points_at_the_step_line(tmp_path):
    path = _write(tmp_path, _job(_run("pip install ruff==0.14.0")))
    line, _ = uc.check_file(path)[0]
    assert (
        path.read_text(encoding="utf-8")
        .splitlines()[line - 1]
        .strip()
        .startswith("- name: install")
    )


def test_each_uncached_job_is_flagged_separately(tmp_path):
    path = _write(
        tmp_path,
        _job(_run("pip install ruff==0.14.0"))
        + f"  other:\n    runs-on: ubuntu-latest\n    steps:\n{_run('pip install black==24.1.0')}",
    )
    assert len(uc.check_file(path)) == 2


# ── clean ────────────────────────────────────────────────────────────────────


def test_a_cache_step_anywhere_in_the_job_exempts_it(tmp_path):
    """The lenient rule: one cache in the job is enough. A better-targeted second
    cache is a judgement this lint does not make."""
    path = _write(tmp_path, _job(CACHE_STEP + _run("pip install ruff==0.14.0")))
    assert uc.check_file(path) == []


def test_a_cache_below_the_install_still_exempts_the_job(tmp_path):
    """Step order does not matter: the question is whether the job caches at all."""
    path = _write(tmp_path, _job(_run("pip install ruff==0.14.0") + CACHE_STEP))
    assert uc.check_file(path) == []


@pytest.mark.parametrize(
    "step",
    [
        "      - uses: actions/setup-node@v4\n        with:\n          cache: npm\n",
        "      - uses: astral-sh/setup-uv@v5\n        with:\n          enable-cache: true\n",
        "      - uses: Swatinem/rust-cache@v2\n",
        "      - uses: actions/cache/restore@v4\n",
    ],
    ids=["setup-node", "setup-uv", "rust-cache", "cache-restore"],
)
def test_each_caching_action_exempts_the_job(tmp_path, step):
    assert (
        uc.check_file(_write(tmp_path, _job(step + _run("pip install ruff==0.14.0"))))
        == []
    )


def test_a_setup_action_with_caching_off_does_not_exempt_the_job(tmp_path):
    """The input is what turns the cache on, so its absence must not read as a
    cache. Without this the lint would go silent on every job that sets up a
    language runtime, which is most of them."""
    step = "      - uses: actions/setup-node@v4\n        with:\n          node-version: 20\n"
    assert (
        len(
            uc.check_file(
                _write(tmp_path, _job(step + _run("pip install ruff==0.14.0")))
            )
        )
        == 1
    )


@pytest.mark.parametrize(
    "script",
    [
        "pip install ruff",
        "apt-get install -y curl",
        "npm install -g prettier",
        "npm install -g prettier@latest",
        "docker pull ubuntu:24.04",
        "curl -fsSL -o t https://example.com/latest/t",
        'curl -O "https://host/${GITHUB_SHA}/tool"',
        'curl -O "https://host/${GITHUB_REF_NAME}/tool"',
        'curl -O "https://host/${RUNNER_ARCH_REF}/tool"',
        "yarn add pre-commit@4.6.1",
        "npm install",
        "pip install -r requirements.txt",
        "echo 'pip install ruff==0.14.0'",
        "# pip install ruff==0.14.0",
    ],
    ids=[
        "unpinned-pip",
        "unpinned-apt",
        "unpinned-npm",
        "npm-latest",
        "docker-pull",
        "unpinned-url",
        "github-sha-url",
        "github-ref-url",
        "runner-var-url",
        "yarn-local-add",
        "local-npm-install",
        "requirements-file",
        "inside-a-message",
        "inside-a-comment",
    ],
)
def test_what_this_lint_leaves_alone(tmp_path, script):
    """An unpinned download has no stable cache key, so flagging it would be an
    unfixable finding — check-versionless-install owns that class. A local `npm`
    or `yarn` install records its pins in package.json. A URL built from the
    runner's own $GITHUB_SHA / $GITHUB_REF_NAME changes on every run, so no cache
    key could track it either. A string a command prints, and a comment, are not
    commands at all."""
    assert uc.check_file(_write(tmp_path, _job(_run(script)))) == []


def test_a_pinned_install_beside_an_unpinned_one_is_not_flagged(tmp_path):
    """One command, two specs: the unpinned spec makes the whole command
    un-cacheable, so this lint stays out of it."""
    path = _write(tmp_path, _job(_run("pip install ruff==0.14.0 black")))
    assert uc.check_file(path) == []


def test_a_uses_job_is_skipped(tmp_path):
    """A reusable-workflow call runs another file's steps, which are judged there."""
    path = _write(
        tmp_path,
        "name: x\non:\n  push:\njobs:\n  gate:\n    uses: ./.github/workflows/o.yaml\n",
    )
    assert uc.check_file(path) == []


# ── opt-out ──────────────────────────────────────────────────────────────────


def test_an_annotation_in_the_run_body_suppresses(tmp_path):
    path = _write(
        tmp_path,
        _job(_run("# cache-exempt: runs once a month\npip install ruff==0.14.0")),
    )
    assert uc.check_file(path) == []


def test_an_annotation_anywhere_in_the_job_block_suppresses(tmp_path):
    path = _write(
        tmp_path,
        "name: x\non:\n  push:\njobs:\n  build: # cache-exempt: the download is the test\n"
        f"    runs-on: ubuntu-latest\n    steps:\n{_run('pip install ruff==0.14.0')}",
    )
    assert uc.check_file(path) == []


def test_a_hash_inside_a_string_value_does_not_suppress(tmp_path):
    """The job-scoped read takes the block's COMMENTS. A `#` inside a quoted
    scalar is a value the job's author writes, so it marks nothing. The run-body
    read above stays raw, where a `#` is a real shell comment."""
    path = _write(
        tmp_path,
        "name: x\non:\n  push:\njobs:\n  build:\n    runs-on: ubuntu-latest\n"
        '    env:\n      NOTE: "# cache-exempt: fake reason in a value"\n'
        f"    steps:\n{_run('pip install ruff==0.14.0')}",
    )
    assert len(uc.check_file(path)) == 1


def test_an_annotation_without_a_reason_does_not_suppress(tmp_path):
    """A bare token is a claim with no argument behind it, so it does not count."""
    path = _write(tmp_path, _job(_run("# cache-exempt\npip install ruff==0.14.0")))
    assert len(uc.check_file(path)) == 1


# ── composite actions ────────────────────────────────────────────────────────


def _action(tmp_path: Path, name: str, steps: str) -> None:
    directory = tmp_path / ".github" / "actions" / name
    directory.mkdir(parents=True)
    (directory / "action.yaml").write_text(
        f"name: {name}\nruns:\n  using: composite\n  steps:\n{steps}", encoding="utf-8"
    )


@pytest.fixture(name="rooted")
def _rooted(tmp_path, monkeypatch):
    """Resolve `uses: ./…` against the tmp tree, as a real run resolves it against
    the repository root."""
    monkeypatch.setattr(uc, "REPO_ROOT", tmp_path)
    return tmp_path


def test_a_cache_inside_a_used_composite_action_exempts_the_job(rooted):
    """The measured false-positive source. A job whose caching lives one level down
    in a shared setup action IS cached; a lint that did not follow `uses: ./…`
    would report almost every job in a real tree."""
    _action(rooted, "setup", CACHE_STEP)
    path = _write(
        rooted,
        _job(
            "      - uses: ./.github/actions/setup\n" + _run("pip install ruff==0.14.0")
        ),
    )
    assert uc.check_file(path) == []


def test_an_install_inside_a_used_composite_action_is_reported_at_the_uses_step(rooted):
    """The cache a finding asks for belongs to the JOB, so the annotation points at
    the workflow step that pulled the action in. The message still names the action,
    because that is where the install itself is written."""
    _action(rooted, "install-tool", _run("pip install ruff==0.14.0"))
    path = _write(rooted, _job("      - uses: ./.github/actions/install-tool\n"))
    findings = uc.check_file(path)
    assert len(findings) == 1
    line, message = findings[0]
    assert path.read_text(encoding="utf-8").splitlines()[line - 1].strip() == (
        "- uses: ./.github/actions/install-tool"
    )
    assert "./.github/actions/install-tool" in message


def test_two_actions_that_use_each_other_terminate(rooted):
    """A cycle must not recurse forever. Neither action caches, so the install in
    one of them is still reported."""
    _action(rooted, "a", "      - uses: ./.github/actions/b\n")
    _action(
        rooted,
        "b",
        "      - uses: ./.github/actions/a\n" + _run("pip install ruff==0.14.0"),
    )
    path = _write(rooted, _job("      - uses: ./.github/actions/a\n"))
    assert len(uc.check_file(path)) == 1


def test_a_missing_composite_action_is_not_a_crash(rooted):
    """A `uses: ./…` pointing at nothing on disk is actionlint's finding, not this
    lint's — it must not stop the rest of the job being judged."""
    path = _write(
        rooted,
        _job(
            "      - uses: ./.github/actions/gone\n" + _run("pip install ruff==0.14.0")
        ),
    )
    assert len(uc.check_file(path)) == 1


# ── unparseable input ────────────────────────────────────────────────────────


def test_unparseable_yaml_is_reported_not_passed(tmp_path):
    """A file this lint cannot read must never read as 'no uncached downloads
    here' — the silent pass is the failure mode the report exists to prevent."""
    path = _write(tmp_path, "name: x\njobs:\n  a: [unclosed\n")
    findings = uc.check_file(path)
    assert len(findings) == 1
    assert "could not parse" in findings[0][1]


def test_an_unparseable_composite_action_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(uc, "REPO_ROOT", tmp_path)
    _action(tmp_path, "broken", "      - uses: [unclosed\n")
    path = _write(tmp_path, _job("      - uses: ./.github/actions/broken\n"))
    findings = uc.check_file(path)
    assert len(findings) == 1
    assert "could not parse" in findings[0][1]


# ── followed scripts ─────────────────────────────────────────────────────────

PINNED = "https://example.com/v2.7.0/tool"
DIGEST = "160c6253e2922e9e0a6a9771c5c4c40a4b3c6090b34a3cdd5e80c96cc2e4218b"


def _tree(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _step_line(path: Path, line: int) -> str:
    return path.read_text(encoding="utf-8").splitlines()[line - 1].strip()


LIB = f'fetch_pinned() {{ curl -fsSLo "$1" "$2"; }}\nfetch_pinned tool {PINNED}\n'


@pytest.mark.parametrize(
    "source",
    [
        'source "$(dirname "$0")/lib.bash"',
        'source "${BASH_SOURCE[0]%/*}/lib.bash"',
        'HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"\n. "$HERE/lib.bash"',
        '# shellcheck source=lib.bash\nsource "$LIBDIR/lib.bash"',
        "source scripts/lib.bash",
        'source "$(git rev-parse --show-toplevel)/scripts/lib.bash"',
    ],
    ids=["dirname-0", "bash-source", "cd-pwd", "directive", "literal", "toplevel"],
)
def test_a_fetch_in_a_sourced_library_function_fires_at_the_step(rooted, source):
    """The step runs a script, the script sources a library, and the
    library calls a function that wraps curl with a pinned URL."""
    _tree(rooted, {"scripts/fetch.sh": source + "\n", "scripts/lib.bash": LIB})
    path = _write(rooted, _job(_run("bash scripts/fetch.sh")))
    findings = uc.check_file(path)
    assert len(findings) == 1, findings
    line, message = findings[0]
    assert _step_line(path, line) == "- name: install"
    assert f"{PINNED} in scripts/lib.bash" in message


def test_a_function_called_from_the_entry_script_resolves_into_the_library(rooted):
    """The call sits in the entry script, the definition in the library: the
    URL is an argument at the call site, and the curl is in the body."""
    _tree(
        rooted,
        {
            "scripts/fetch.sh": f'source scripts/lib.bash\nfetch_pinned tool "{PINNED}"\n',
            "scripts/lib.bash": 'fetch_pinned() { curl -fsSLo "$1" "$2"; }\n',
        },
    )
    path = _write(rooted, _job(_run("bash scripts/fetch.sh")))
    assert [PINNED in m for m in _messages(path)] == [True]


@pytest.mark.parametrize(
    ("files", "run"),
    [
        (
            {"scripts/lib.bash": f'fetch_pinned() {{ curl -fsSLo t "{PINNED}"; }}\n'},
            "source scripts/lib.bash",
        ),
        (
            {
                "scripts/fetch.sh": (
                    f"cat > doc.txt <<'EOF'\ncurl -fsSLo tool {PINNED}\nEOF\n"
                )
            },
            "bash scripts/fetch.sh",
        ),
        ({}, "bash scripts/missing.sh"),
        (
            {"scripts/fetch.sh": f"curl -fsSLo t {PINNED}\n"},
            'bash "$UNRESOLVABLE/fetch.sh"',
        ),
    ],
    ids=["defined-never-called", "heredoc-body", "missing-script", "unresolvable"],
)
def test_what_the_walk_does_not_run_is_not_judged(rooted, files, run):
    """A function nobody calls, a heredoc body written to a file, a script that
    is not on disk, and a path only the run can compute: none is a download."""
    _tree(rooted, files)
    assert uc.check_file(_write(rooted, _job(_run(run)))) == []


def test_two_scripts_that_source_each_other_terminate(rooted):
    _tree(
        rooted,
        {
            "scripts/a.bash": f"source scripts/b.bash\ncurl -fsSLo t {PINNED}\n",
            "scripts/b.bash": "source scripts/a.bash\n",
        },
    )
    path = _write(rooted, _job(_run("bash scripts/a.bash")))
    assert len(uc.check_file(path)) == 1


def test_a_composite_action_script_resolves_through_the_action_path(rooted):
    _tree(rooted, {".github/actions/tool/install.sh": f"curl -fsSLo t {PINNED}\n"})
    _action(
        rooted,
        "tool-run",
        '      - shell: bash\n        run: bash "$GITHUB_ACTION_PATH/../tool/install.sh"\n',
    )
    path = _write(rooted, _job("      - uses: ./.github/actions/tool-run\n"))
    messages = _messages(path)
    assert len(messages) == 1
    assert "in .github/actions/tool/install.sh" in messages[0]


# ── which cache covers which download ───────────────────────────────────────


def _cache(key: str, path: str = "/tmp/elsewhere") -> str:
    return (
        "      - uses: actions/cache@0057852bfaa89a56745cba8c7296529d2fc39830\n"
        f"        with:\n          path: {path}\n          key: {key}\n"
    )


@pytest.mark.parametrize(
    ("cache", "fires"),
    [
        (_cache("deps-${{ runner.os }}"), True),
        (_cache("tool-2.7.0-${{ runner.os }}"), False),
        (_cache("tool-v2.7.0-${{ runner.os }}"), False),
        (_cache("tool-${{ env.TOOL_VERSION }}"), False),
        (_cache("tool-${{ hashFiles('scripts/pin.bash') }}"), False),
        (_cache("tool-${{ hashFiles('scripts/other.bash') }}"), True),
        (_cache("deps-${{ runner.os }}", "/tmp/tool-cache"), False),
        (_cache("deps-${{ runner.os }}", "~/.cache/tool"), True),
        (_cache("${{ steps.key.outputs.key }}"), False),
    ],
    ids=[
        "unrelated-key",
        "version-in-key",
        "v-prefixed-version-in-key",
        "version-variable-in-key",
        "hash-of-the-pin-file",
        "hash-of-another-file",
        "path-holds-the-output",
        "path-holds-something-else",
        "unreadable-key",
    ],
)
def test_a_cache_covers_a_download_only_when_it_can_hold_it(rooted, cache, fires):
    """A cache covers a fetch only when its key names the pin or its path holds
    the output. A key the lint cannot read covers every fetch in the job."""
    _tree(
        rooted,
        {
            "scripts/pin.bash": "TOOL_VERSION=2.7.0\n",
            "scripts/fetch.sh": (
                "source scripts/pin.bash\n"
                "curl -fsSLo /tmp/tool-cache/tool "
                '"https://example.com/v${TOOL_VERSION}/tool"\n'
            ),
        },
    )
    path = _write(rooted, _job(cache + _run("bash scripts/fetch.sh")))
    messages = _messages(path)
    assert len(messages) == (1 if fires else 0), messages
    if fires:
        assert "no cache in the job keyed on its pin" in messages[0]


def test_a_key_naming_the_variable_a_pin_is_derived_from_covers_it(rooted):
    """`version="${TOOL_VERSION#v}"` is the pin the URL reads, and a key that
    names TOOL_VERSION names it too."""
    script = (
        'TOOL_VERSION="${TOOL_VERSION:-v2.7.0}"\nversion="${TOOL_VERSION#v}"\n'
        'curl -fsSLo t "https://example.com/v${version}/tool"'
    )
    path = _write(rooted, _job(_cache("tool-${{ env.TOOL_VERSION }}") + _run(script)))
    assert uc.check_file(path) == []


@pytest.mark.parametrize(
    ("key", "fires"),
    [("t-${{ hashFiles('pins.sh') }}", 0), ("t-${{ hashFiles('lib.bash') }}", 1)],
    ids=["hashes-the-pin-file", "hashes-another-file"],
)
def test_a_key_hashing_the_file_that_assigns_the_pin_covers_it(rooted, key, fires):
    """The walk cannot follow `source "$1"` inside a function, but the key
    hashes the file that assigns the pin, so the cache still covers it."""
    _tree(
        rooted,
        {
            "pins.sh": "TOOL_VERSION=2.7.0\n",
            "lib.bash": 'load_pins() { source "$1"; }\n',
            "fetch.sh": (
                "source lib.bash\nload_pins pins.sh\n"
                'curl -fsSLo t "https://example.com/v${TOOL_VERSION}/tool"\n'
            ),
        },
    )
    path = _write(rooted, _job(_cache(key) + _run("bash fetch.sh")))
    assert len(_messages(path)) == fires


@pytest.mark.parametrize(
    ("key", "fires"),
    [
        ("tool-${{ env.ARCH }}", 1),
        ("tool-version-1", 1),
        ("tool-${{ env.TOOL_VERSION }}", 0),
    ],
    ids=["a-variable-that-is-no-pin", "a-bare-word", "the-pin-variable"],
)
def test_only_a_pin_variable_named_in_a_key_covers_a_fetch(rooted, key, fires):
    """`$ARCH` shapes the URL but holds no version, so a key that names it does
    not track the pin. A bare `version` in a key is text, not the variable."""
    script = (
        "ARCH=amd64\nTOOL_VERSION=2.7.0\nversion=$TOOL_VERSION\n"
        'curl -fsSLo t "https://example.com/v${version}/tool-$ARCH"'
    )
    path = _write(rooted, _job(_cache(key) + _run(script)))
    assert len(_messages(path)) == fires


@pytest.mark.parametrize(
    ("pattern", "fires"),
    [("**/fetch.sh", 0), ("*/fetch.sh", 1), ("fetch.sh", 0)],
    ids=["globstar-matches-the-root", "star-needs-a-directory", "literal"],
)
def test_a_hashfiles_glob_matches_as_github_matches_it(rooted, pattern, fires):
    """`**/` matches zero or more directories, and `*` never crosses a `/`."""
    _tree(rooted, {"fetch.sh": 'curl -fsSLo t "https://example.com/v2.7.0/tool"\n'})
    key = f"t-${{{{ hashFiles('{pattern}') }}}}"
    path = _write(rooted, _job(_cache(key) + _run("bash fetch.sh")))
    assert len(_messages(path)) == fires


WORKDIR_JOB = """\
name: x
on:
  push:
jobs:
  build:
    runs-on: ubuntu-latest
    defaults:
      run:
        working-directory: {directory}
    steps:
      - run: bash install.sh
"""
PINNED_FETCH = 'curl -fsSLo t "https://example.com/v2.7.0/tool"\n'


@pytest.mark.parametrize(
    ("directory", "where", "fires"),
    [("tools", "tools", 1), ("tools", "", 0), ("${{ inputs.dir }}", "", 0)],
    ids=["script-in-the-directory", "same-name-at-the-root", "unreadable-directory"],
)
def test_a_relative_path_resolves_in_the_working_directory(
    rooted, directory, where, fires
):
    """`bash install.sh` runs the file in the step's working directory. A
    directory the text does not fix resolves no relative path at all."""
    _tree(rooted, {f"{where}/install.sh".lstrip("/"): PINNED_FETCH, "tools/x": ""})
    path = _write(rooted, WORKDIR_JOB.format(directory=directory))
    assert len(_messages(path)) == fires


def test_a_pin_named_like_a_runner_context_value_is_not_in_its_key(rooted):
    """`$os` is a pin of the URL, but `runner.os` in the key is the runner's
    value, not the script's variable, so the cache does not cover the fetch."""
    script = (
        "os=linux\nTOOL_VERSION=2.7.0\n"
        'curl -fsSLo t "https://example.com/v${TOOL_VERSION}/tool-$os"'
    )
    path = _write(rooted, _job(_cache("pip-${{ runner.os }}") + _run(script)))
    assert len(_messages(path)) == 1


# ── a digest pins the bytes ─────────────────────────────────────────────────


DIGEST_SCRIPT = (
    f"SHA={DIGEST}\n"
    "URL=https://example.com/tool/latest\n"
    'curl -fsSLo t "$URL"\n'
    "{check}\n"
)


@pytest.mark.parametrize(
    "check",
    [
        'echo "$SHA  t" | sha256sum -c -',
        'echo "$SHA  t" | shasum -a 256 -c -',
        'sha256sum --check --status <<<"$SHA  t"',
    ],
    ids=["sha256sum", "shasum", "here-string"],
)
def test_a_download_checked_against_a_digest_is_pinned(tmp_path, check):
    """No version in the URL, but the job refuses any other bytes."""
    path = _write(tmp_path, _job(_run(DIGEST_SCRIPT.format(check=check))))
    messages = _messages(path)
    assert len(messages) == 1, messages
    assert "https://example.com/tool/latest" not in messages[0]
    assert "$URL (digest-checked)" in messages[0]


def test_an_unversioned_download_with_no_digest_check_is_left_alone(tmp_path):
    path = _write(tmp_path, _job(_run(DIGEST_SCRIPT.format(check="chmod +x t"))))
    assert uc.check_file(path) == []


def test_a_digest_check_of_another_file_does_not_pin_the_download(tmp_path):
    """The check must name the file the download writes. Here it names a
    different file, so the unversioned download stays unpinned."""
    script = DIGEST_SCRIPT.format(check='echo "$SHA  other.tgz" | sha256sum -c -')
    assert uc.check_file(_write(tmp_path, _job(_run(script)))) == []


@pytest.mark.parametrize(
    ("download", "fires"),
    [
        ('curl -fsSLO "$URL/t"', True),
        ('curl -fsSL "$URL/t" > t', True),
        ('curl -fsSL "$URL/t" > other', False),
        ('curl -fsSL "$URL/api" | jq -r .tag', False),
    ],
    ids=["remote-name", "redirect", "redirect-elsewhere", "no-output"],
)
def test_a_digest_pins_only_the_file_a_download_writes(tmp_path, download, fires):
    """A direct download pins through a check only when the check names the
    file it writes. A lookup whose output goes to a pipe writes no file."""
    script = (
        f"SHA={DIGEST}\nURL=https://example.com/tool\n{download}\n"
        'echo "$SHA  t" | sha256sum -c -\n'
    )
    assert len(_messages(_write(tmp_path, _job(_run(script))))) == (1 if fires else 0)


def test_a_checksum_file_names_the_file_it_vouches_for(tmp_path):
    script = DIGEST_SCRIPT.format(check="sha256sum -c t.sha256").replace(
        "curl -fsSLo t ", 'curl -fsSLo t.sha256 "$URL.sha256"\ncurl -fsSLo t '
    )
    messages = _messages(_write(tmp_path, _job(_run(script))))
    assert ["$URL (digest-checked)" in m for m in messages] == [True]


def test_a_download_to_stdout_is_not_pinned_by_a_check_reading_stdin(tmp_path):
    """`-o -` and `sha256sum -c -` both spell stdin or stdout, not a file."""
    script = (
        "curl -fsSL https://example.com/install.sh -o - | bash\n"
        f'echo "{DIGEST}  tool.tgz" | sha256sum -c -\n'
    )
    assert uc.check_file(_write(tmp_path, _job(_run(script)))) == []


def test_a_downloader_function_with_a_hidden_output_is_not_digest_pinned(tmp_path):
    """The function writes where its body decides, so no check can be said to
    name the file this call fetches."""
    script = (
        'get() { curl -fsSL "$1"; }\n'
        "get https://api.github.com/repos/o/r/releases/latest > rel.json\n"
        "sha256sum -c sums.txt\n"
    )
    assert uc.check_file(_write(tmp_path, _job(_run(script)))) == []


def test_a_cache_keyed_on_the_digest_covers_a_digest_pinned_download(tmp_path):
    script = DIGEST_SCRIPT.format(check='echo "$SHA  t" | sha256sum -c -')
    path = _write(tmp_path, _job(_cache(f"tool-{DIGEST}") + _run(script)))
    assert uc.check_file(path) == []


# ── Python the shell runs ───────────────────────────────────────────────────


FETCH_PY = """\
import hashlib
import urllib.request

URL = "https://example.com/tool/latest"
WANT = "{digest}"


def download(dest):
    data = urllib.request.urlopen(URL).read()
    {check}
    with open(dest, "wb") as handle:
        handle.write(data)


if __name__ == "__main__":
    download("tool")
"""

HASH_CHECK = "if hashlib.sha256(data).hexdigest() != WANT:\n        raise SystemExit(1)"
INSTALL_SH = 'DIR="${BASH_SOURCE[0]%/*}"\nCOMMAND "$DIR/fetch.py"\n'


@pytest.mark.parametrize("command", ["python3", "uv run", "python"])
def test_a_python_fetch_that_checks_a_digest_fires(rooted, command):
    """The step runs a shell script that hands a Python file to an
    interpreter, and that file hashes what it fetched and compares the hash."""
    _tree(
        rooted,
        {
            "scripts/install.sh": INSTALL_SH.replace("COMMAND", command),
            "scripts/fetch.py": FETCH_PY.format(digest=DIGEST, check=HASH_CHECK),
        },
    )
    path = _write(rooted, _job(_run("bash scripts/install.sh")))
    messages = _messages(path)
    assert len(messages) == 1, messages
    assert "fetched by scripts/fetch.py:download, digest-checked" in messages[0]


def test_a_python_fetch_in_an_imported_helper_fires(rooted):
    """The entry script imports a repo module, and the fetch lives there."""
    _tree(
        rooted,
        {
            "scripts/main.py": "import helper\nhelper.download('t')\n",
            "scripts/helper.py": FETCH_PY.format(digest=DIGEST, check=HASH_CHECK),
        },
    )
    path = _write(rooted, _job(_run("python3 scripts/main.py")))
    assert ["scripts/helper.py:download" in m for m in _messages(path)] == [True]


def test_a_fetcher_imported_beside_another_module_is_read(rooted):
    _tree(
        rooted,
        {
            "fetch.py": (
                "import os, requests\nrequests.get('https://example.com/v2.7.0/tool')\n"
            )
        },
    )
    assert len(_messages(_write(rooted, _job(_run("python3 fetch.py"))))) == 1


def test_a_python_fetch_at_a_pinned_url_fires_with_no_digest(rooted):
    _tree(
        rooted,
        {
            "fetch.py": (
                "import requests\nVERSION = '2.7.0'\n"
                "requests.get(f'https://example.com/v{VERSION}/tool')\n"
            )
        },
    )
    path = _write(rooted, _job(_run("python3 fetch.py")))
    assert [
        "v${VERSION}/tool (fetched by fetch.py:<module>)" in m for m in _messages(path)
    ] == [True]


@pytest.mark.parametrize(
    ("install", "check"),
    [
        (INSTALL_SH.replace("COMMAND", "python3"), "pass"),
        ('python3 "$UNRESOLVABLE/fetch.py"\n', HASH_CHECK),
    ],
    ids=["no-digest-no-version", "unresolvable-path"],
)
def test_what_the_python_walk_leaves_alone(rooted, install, check):
    _tree(
        rooted,
        {
            "scripts/install.sh": install,
            "scripts/fetch.py": FETCH_PY.format(digest=DIGEST, check=check),
        },
    )
    path = _write(rooted, _job(_run("bash scripts/install.sh")))
    assert uc.check_file(path) == []


def test_a_hash_compare_in_another_function_does_not_pin_a_method_fetch(rooted):
    """The fetch sits in a class method, and the only compare sits in a
    different function, so nothing checks the bytes this fetch reads."""
    _tree(
        rooted,
        {
            "fetch.py": (
                "import hashlib\nimport urllib.request\n\n"
                "class Client:\n"
                "    def latest(self):\n"
                "        return urllib.request.urlopen('https://e/api/latest').read()\n"
                "\n\ndef verify(data, want):\n"
                "    return hashlib.sha256(data).hexdigest() == want\n"
            )
        },
    )
    assert uc.check_file(_write(rooted, _job(_run("python3 fetch.py")))) == []


@pytest.mark.parametrize(
    "run", ["uv run pytest tests/fetch.py", "uv run ruff check tests/fetch.py"]
)
def test_a_python_file_another_program_reads_is_not_the_step_script(rooted, run):
    _tree(rooted, {"tests/fetch.py": FETCH_PY.format(digest=DIGEST, check=HASH_CHECK)})
    assert uc.check_file(_write(rooted, _job(_run(run)))) == []


# ── the measured case ───────────────────────────────────────────────────────

# The osv-scanner fetch path of agent-glovebox's `js-scan-run` job, through its
# `scan.sh` and `osv-scanner.bash`. The fetch sits two script hops below the
# `run:` body, so only a walk that follows scripts sees it.
GLOVEBOX_SCAN = """\
set -euo pipefail

HERE="$(dirname "${BASH_SOURCE[0]}")"
# shellcheck source=../lib/osv-scanner.bash disable=SC1091
source "$HERE/../lib/osv-scanner.bash"

download_osv_scanner osv-scanner
./osv-scanner scan source --lockfile pnpm-lock.yaml
"""
GLOVEBOX_OSV = """\
# shellcheck source=../lib-pinned-install.sh disable=SC1091
source "$(dirname "${BASH_SOURCE[0]}")/../lib-pinned-install.sh"

OSV_VERSION="2.7.0"
OSV_SHA256="160c6253e2922e9e0a6a9771c5c4c40a4b3c6090b34a3cdd5e80c96cc2e4218b"

download_osv_scanner() {
  local dest="$1"
  PINNED_TOOL=osv-scanner pinned_curl \\
    "https://github.com/google/osv-scanner/releases/download/v${OSV_VERSION}/osv-scanner_linux_amd64" \\
    "$dest"
  echo "${OSV_SHA256}  ${dest}" | sha256sum -c -
  chmod +x "$dest"
}
"""
GLOVEBOX_PINNED = """\
pinned_curl() {
  local url="$1" dest="${2:-$PINNED_STAGED}" retry_widen=--retry-connrefused
  curl --proto '=https' -fsSL --retry 6 "$retry_widen" -o "$dest" "$url" || return
}
"""
GLOVEBOX_JOB = """\
name: deps
on:
  pull_request:
jobs:
  js-scan-run:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false
{cache}      - name: Scan pnpm-lock.yaml for known vulnerabilities
        run: bash .github/scripts/deps-vuln/scan.sh js
"""
# glovebox PR #9135: a cache keyed on the file that pins the version and digest.
GLOVEBOX_CACHE = """\
      - name: Cache the pinned osv-scanner
        uses: actions/cache@55cc8345863c7cc4c66a329aec7e433d2d1c52a9 # v6.1.0
        with:
          path: /tmp/osv-scanner-cache
          key: osv-scanner-${{ runner.os }}-${{ hashFiles('.github/scripts/lib/osv-scanner.bash') }}
"""


@pytest.mark.parametrize(("cache", "fires"), [("", True), (GLOVEBOX_CACHE, False)])
def test_the_glovebox_osv_scanner_job(rooted, cache, fires):
    _tree(
        rooted,
        {
            ".github/scripts/deps-vuln/scan.sh": GLOVEBOX_SCAN,
            ".github/scripts/lib/osv-scanner.bash": GLOVEBOX_OSV,
            ".github/scripts/lib-pinned-install.sh": GLOVEBOX_PINNED,
        },
    )
    path = _write(rooted, GLOVEBOX_JOB.format(cache=cache))
    findings = uc.check_file(path)
    assert len(findings) == (1 if fires else 0), findings
    if fires:
        line, message = findings[0]
        assert _step_line(path, line).startswith("- name: Scan pnpm-lock.yaml")
        assert (
            "osv-scanner_linux_amd64 in .github/scripts/lib/osv-scanner.bash" in message
        )
        assert "no cache step anywhere in the job" in message
