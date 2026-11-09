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
    ("step", "covered", "other"),
    [
        (
            "      - uses: actions/setup-node@v4\n        with:\n          cache: npm\n",
            "npm install -g pre-commit@4.6.1",
            "pip install ruff==0.14.0",
        ),
        (
            "      - uses: actions/setup-node@v4\n        with:\n          cache: pnpm\n",
            "pnpm add --global pre-commit@4.6.1",
            "npm install -g pre-commit@4.6.1",
        ),
        (
            "      - uses: actions/setup-python@v5\n        with:\n          cache: pip\n",
            "python -m pip install ruff==0.14.0",
            "uv pip install ruff==0.14.0",
        ),
        (
            "      - uses: astral-sh/setup-uv@v5\n        with:\n          enable-cache: true\n"
            '          prune-cache: "false"\n',
            "uv tool install ruff==0.14.0",
            "pip install ruff==0.14.0",
        ),
        (
            "      - uses: astral-sh/setup-uv@v6\n        with:\n"
            "          prune-cache: false\n",
            "uv tool install ruff==0.14.0",
            "uv tool install --no-cache ruff==0.14.0",
        ),
        (
            "      - uses: astral-sh/setup-uv@v6\n        with:\n"
            "          enable-cache: true\n",
            None,
            "uv tool install ruff==0.14.0",
        ),
        (
            "      - uses: actions/setup-python@v5\n        with:\n          cache: pip\n",
            None,
            "pip install --no-cache-dir ruff==0.14.0",
        ),
        (
            "      - uses: actions/setup-python@v5\n        with:\n          cache:\n",
            None,
            "pip install ruff==0.14.0",
        ),
        (
            "      - uses: actions/setup-node@v4\n        with:\n"
            "          cache: ${{ inputs.manager }}\n",
            "yarn global add pre-commit@4.6.1",
            "curl -fsSLo t https://example.com/v2.7.0/tool",
        ),
        (
            "      - uses: Swatinem/rust-cache@v2\n",
            None,
            "pip install ruff==0.14.0",
        ),
    ],
    ids=[
        "setup-node",
        "setup-node-pnpm",
        "setup-python",
        "setup-uv",
        "setup-uv-on-by-default",
        "setup-uv-prunes-its-downloads",
        "an-install-that-skips-the-store",
        "an-empty-cache-input",
        "computed",
        "rust-cache",
    ],
)
def test_a_setup_action_caches_only_its_own_installer(tmp_path, step, covered, other):
    """A setup action caches one package manager's store. That store holds the
    installs of that manager, and nothing a different manager or a download
    fetches. A cache input set by an expression may name any manager. setup-uv
    caches by default, but it prunes the wheels uv downloaded unless
    `prune-cache` is false. An install told to skip the store leaves nothing
    in it, and an empty cache input turns the cache off."""
    if covered:
        path = _write(tmp_path, _job(step + _run(covered)))
        assert uc.check_file(path) == []
    path = _write(tmp_path, _job(step + _run(other)))
    assert other.split()[-1] in _finding(path)


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


OUTPUT_JOB = """\
name: x
on:
  push:
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/cache@0057852bfaa89a56745cba8c7296529d2fc39830
        with:
          path: {path}
          key: deps-${{{{ runner.os }}}}
      - working-directory: {directory}
        run: |
          {script}
"""


@pytest.mark.parametrize(
    ("directory", "script", "path", "fires"),
    [
        ("tools", f"curl -fsSLo t.tgz {PINNED}", "tools/t.tgz", 0),
        ("tools", f"curl -fsSLo t.tgz {PINNED}", "t.tgz", 1),
        (".", f"cd sub && curl -fsSLo t.tgz {PINNED}", "sub/t.tgz", 0),
        (".", f"cd sub && curl -fsSLo t.tgz {PINNED}", "other/x.tgz", 1),
        ("${{ inputs.dir }}", f"curl -fsSLo t.tgz {PINNED}", "a/t.tgz", 0),
    ],
    ids=[
        "in-the-working-directory",
        "same-name-at-the-root",
        "after-a-cd",
        "after-a-cd-elsewhere",
        "unreadable-directory",
    ],
)
def test_a_relative_output_lands_in_the_directory_the_step_runs_in(
    rooted, directory, script, path, fires
):
    """`-o t.tgz` writes into the step's working directory. After a `cd`, or in
    a directory the text does not fix, only the output's own name is known, so
    a cache path that ends in that name holds it."""
    (rooted / "tools").mkdir()
    workflow = OUTPUT_JOB.format(path=path, directory=directory, script=script)
    assert len(_messages(_write(rooted, workflow))) == fires


@pytest.mark.parametrize(
    ("script", "fires"),
    [
        (f"wget -q -P /tmp/tools {PINNED}.tgz", 0),
        (f"wget -q --directory-prefix=/tmp/tools {PINNED}.tgz", 0),
        (f"curl -fsSL --output-dir /tmp/tools -O {PINNED}.tgz", 0),
        (f"curl -fsSL --output-dir /tmp/tools -o t {PINNED}.tgz", 0),
        (f"wget -q -P /tmp/elsewhere {PINNED}.tgz", 1),
    ],
    ids=[
        "wget-P",
        "wget-directory-prefix",
        "curl-output-dir-O",
        "curl-output-dir-o",
        "elsewhere",
    ],
)
def test_a_download_directory_flag_places_the_output(rooted, script, fires):
    """`wget -P DIR` and `curl --output-dir DIR` put the file in DIR."""
    workflow = OUTPUT_JOB.format(path="/tmp/tools", directory=".", script=script)
    assert len(_messages(_write(rooted, workflow))) == fires


@pytest.mark.parametrize(
    ("directory", "script", "path", "fires"),
    [
        (".", f"curl -fsSLo bin/tool {PINNED}\ncd bin && chmod +x tool", "bin", 0),
        (".", f"( cd docs && make )\ncurl -fsSLo bin/tool {PINNED}", "bin", 0),
        (".", f'OUT=bin/tool\ncurl -fsSL --output="$OUT" {PINNED}', "bin", 0),
        (
            ".",
            f'INSTALL_DIR="$HOME/.local/bin"\ncurl -fsSLo "$INSTALL_DIR/tool" {PINNED}',
            "~/.local/bin",
            0,
        ),
        ("/tmp/w", f"curl -fsSLo tool {PINNED}", "/tmp/w/tool", 0),
        (".", f"wget -q -P /tmp/a -P /tmp/b {PINNED}", "/tmp/b", 0),
        (".", f"wget -q -P /tmp/a -P /tmp/b {PINNED}", "/tmp/a", 1),
        (
            ".",
            f"curl -fsSL -Hcontent-type:text -o /tmp/a/t {PINNED}",
            "ntent-type:text",
            1,
        ),
        (".", f"f() {{ curl -fsSLo tool {PINNED}; }}\ncd sub\nf", "sub/tool", 0),
        (".", f"f() {{ curl -fsSLo tool {PINNED}; }}\ncd sub\nf", "tool", 1),
        (
            ".",
            f"diff <(cd a && ls) <(cd b && ls)\ncurl -fsSLo bin/tool {PINNED}",
            "bin",
            0,
        ),
        (".", f"cd a | cat\ncurl -fsSLo bin/tool {PINNED}", "bin", 0),
        (".", f"cd sub && curl -fsSLo bin/tool {PINNED}", "sub/bin", 0),
        (".", f"command cd sub\ncurl -fsSLo tool {PINNED}", "tool", 1),
    ],
    ids=[
        "cd-after-the-download",
        "cd-in-a-subshell",
        "output-flag-with-equals",
        "variable-built-on-home",
        "absolute-working-directory",
        "last-directory-flag",
        "earlier-directory-flag",
        "an-o-inside-another-flag-value",
        "function-called-after-a-cd",
        "function-called-after-a-cd-root-path",
        "cd-in-a-process-substitution",
        "cd-in-a-pipeline",
        "directory-of-the-output-after-a-cd",
        "command-cd",
    ],
)
def test_an_output_resolves_as_the_shell_resolves_it(
    rooted, directory, script, path, fires
):
    """A `cd` moves only the commands after it in the same shell. A variable
    with one assignment spells that value, and the last directory flag wins."""
    script = script.replace("\n", "\n          ")
    workflow = OUTPUT_JOB.format(path=path, directory=directory, script=script)
    assert len(_messages(_write(rooted, workflow))) == fires


@pytest.mark.parametrize(
    ("script", "path", "fires"),
    [
        ('cd sub\ncd "$GITHUB_WORKSPACE"\n./scripts/install.sh', "nothing", 1),
        ("cd sub\ncd ..\nbash scripts/install.sh", "nothing", 1),
        (f"cd sub && curl -fsSLo t.tgz {PINNED}", "sub", 0),
        (f"cd sub && curl -fsSLo t.tgz {PINNED}", "t.tgz", 1),
        (f'cd "$UNKNOWN" && curl -fsSLo bin/tool {PINNED}', "sub/bin", 0),
        (f"cd sub | curl -fsSLo t.tgz {PINNED}", "t.tgz", 0),
        (f"cd sub | curl -fsSLo t.tgz {PINNED}", "sub", 1),
        (f"cd sub &\ncurl -fsSLo t.tgz {PINNED}", "t.tgz", 0),
        (f"cd sub &\ncurl -fsSLo t.tgz {PINNED}", "sub", 1),
        (f"f() {{ curl -fsSLo t.tgz {PINNED}; }}\n( cd sub; f )", "sub", 0),
        (f"f() {{ curl -fsSLo t.tgz {PINNED}; }}\n( cd sub; f )", "t.tgz", 1),
    ],
    ids=[
        "cd-back-to-the-workspace",
        "cd-back-up",
        "directory-after-a-fixed-cd",
        "root-after-a-fixed-cd",
        "unknown-cd-keeps-the-name",
        "a-cd-in-another-pipeline-member",
        "a-cd-in-another-pipeline-member-moved-path",
        "a-cd-in-the-background",
        "a-cd-in-the-background-moved-path",
        "a-function-runs-where-it-is-called",
        "a-function-runs-where-it-is-called-root-path",
    ],
)
def test_a_cd_the_text_fixes_sets_the_directory(rooted, script, path, fires):
    """A `cd` whose target the text fixes tells where the next command runs,
    so a script it starts is still followed and an output is still placed."""
    _tree(rooted, {"scripts/install.sh": f"curl -fsSLo t {PINNED}\n", "sub/x": ""})
    script = script.replace("\n", "\n          ")
    workflow = OUTPUT_JOB.format(path=path, directory=".", script=script)
    assert len(_messages(_write(rooted, workflow))) == fires


LIB_FETCH = f"curl -fsSLo bin/tool {PINNED}\n"
TWICE = 'source scripts/lib.sh\ncd sub\nsource "$GITHUB_WORKSPACE/scripts/lib.sh"'


@pytest.mark.parametrize(("path", "fires"), [("bin", 0), ("sub/bin", 1)])
def test_a_long_call_chain_runs_where_its_first_call_runs(rooted, path, fires):
    """300 functions, each calling the one before, run where the outer call
    runs: the step's directory."""
    script = "\n".join(
        [f"f0() {{ {LIB_FETCH.strip()}; }}"]
        + [f"f{i}() {{ f{i - 1}; }}" for i in range(1, 300)]
        + ["f299"]
    )
    script = script.replace("\n", "\n          ")
    workflow = OUTPUT_JOB.format(path=path, directory=".", script=script)
    assert len(_messages(_write(rooted, workflow))) == fires


@pytest.mark.parametrize("path", ["other/bin", "sub/bin"])
@pytest.mark.parametrize(
    "script",
    [
        f"if false; then cd sub; fi\n{LIB_FETCH}",
        f"for d in sub; do cd sub; done\n{LIB_FETCH}",
        f"for d in a b; do {LIB_FETCH.strip()}; cd sub; done",
        f"true && cd sub\n{LIB_FETCH}",
        f"f() {{ {LIB_FETCH.strip()}; }}\nf\ncd sub\nf",
        f"if false; then cd sub; else {LIB_FETCH.strip()}; fi",
        f"if false; then cd sub; elif true; then {LIB_FETCH.strip()}; fi",
        f"case x in y) cd sub ;; x) {LIB_FETCH.strip()} ;; esac",
        f"cd sub || {LIB_FETCH.strip()}",
        f"if cd sub; then :; else {LIB_FETCH.strip()}; fi",
        f"if ! cd sub; then {LIB_FETCH.strip()}; fi",
        f"f() {{ {LIB_FETCH.strip()}; }}\ntrap f EXIT\ncd sub",
        f"f() {{ {LIB_FETCH.strip()}; }}\ncd sub\ngrep -q f x\ncd ..\nfn=f\n$fn",
    ],
    ids=[
        "if",
        "loop",
        "loop-holding-the-fetch",
        "and-list",
        "calls-that-disagree",
        "if-else-arms",
        "if-elif-arms",
        "case-arms",
        "or-list",
        "cd-in-a-condition",
        "negated-cd",
        "trap",
        "a-word-that-is-not-a-call",
    ],
)
def test_a_cd_the_text_does_not_order_keeps_the_name(rooted, script, path):
    """A `cd` in a branch or a loop may or may not run before the fetch, and
    two calls of one function can run in two directories. The directory is
    unknown, so the output keeps its name, and a cache path that ends in its
    directory covers it."""
    _tree(rooted, {"sub/x": ""})
    script = script.replace("\n", "\n          ")
    workflow = OUTPUT_JOB.format(path=path, directory=".", script=script)
    assert len(_messages(_write(rooted, workflow))) == 0


@pytest.mark.parametrize(
    ("lib", "script", "path", "fires"),
    [
        (LIB_FETCH, "source scripts/lib.sh\ncd sub", "bin", 0),
        (LIB_FETCH, "source scripts/lib.sh\ncd sub", "sub/bin", 1),
        (LIB_FETCH, 'cd sub\nsource "$GITHUB_WORKSPACE/scripts/lib.sh"', "sub/bin", 0),
        (LIB_FETCH, 'cd sub\nsource "$GITHUB_WORKSPACE/scripts/lib.sh"', "bin", 1),
        ("cd sub\n", f"source scripts/lib.sh\n{LIB_FETCH}", "sub/bin", 0),
        ("cd sub\n", f"source scripts/lib.sh\n{LIB_FETCH}", "bin", 1),
        ("cd sub\n", f"{LIB_FETCH}source scripts/lib.sh", "bin", 0),
        ("cd sub\n", f"( source scripts/lib.sh )\n{LIB_FETCH}", "bin", 0),
        ("cd sub\n", f"( source scripts/lib.sh )\n{LIB_FETCH}", "sub/bin", 1),
        ("cd sub\n", f"source scripts/lib.sh | cat\n{LIB_FETCH}", "bin", 0),
        (
            LIB_FETCH,
            '( cd sub; source "$GITHUB_WORKSPACE/scripts/lib.sh" )',
            "sub/bin",
            0,
        ),
        (LIB_FETCH, '( cd sub; source "$GITHUB_WORKSPACE/scripts/lib.sh" )', "bin", 1),
        (LIB_FETCH, TWICE, "sub/bin", 0),
        (LIB_FETCH, TWICE, "other/bin", 0),
    ],
    ids=[
        "fetch-in-a-sourced-file-then-cd",
        "fetch-in-a-sourced-file-then-cd-wrong-path",
        "cd-then-fetch-in-a-sourced-file",
        "cd-then-fetch-in-a-sourced-file-root-path",
        "cd-in-a-sourced-file-then-fetch",
        "cd-in-a-sourced-file-then-fetch-root-path",
        "fetch-then-cd-in-a-sourced-file",
        "cd-in-a-file-sourced-in-a-subshell",
        "cd-in-a-file-sourced-in-a-subshell-moved-path",
        "cd-in-a-file-sourced-in-a-pipeline",
        "fetch-in-a-file-sourced-after-a-subshell-cd",
        "fetch-in-a-file-sourced-after-a-subshell-cd-root-path",
        "a-file-sourced-at-two-lines-keeps-the-name",
        "a-file-sourced-at-two-lines-keeps-the-name-root-path",
    ],
)
def test_a_sourced_file_runs_at_its_source_line(rooted, lib, script, path, fires):
    """The commands of a sourced file run where the `source` command runs, so a
    `cd` before or after it orders against them like any other command."""
    _tree(rooted, {"scripts/lib.sh": lib, "sub/x": ""})
    script = script.replace("\n", "\n          ")
    workflow = OUTPUT_JOB.format(path=path, directory=".", script=script)
    assert len(_messages(_write(rooted, workflow))) == fires


def test_two_files_with_one_text_keep_their_own_source_line(rooted):
    """Two files with the same text share one parse tree. Each one still has
    its own `source` line, so the walk ends and the last `cd` places the file."""
    hop = 'cd /tmp\nsource "${BASH_SOURCE[0]%/*}/../run.sh"\n'
    _tree(rooted, {"sub/sub/run.sh": hop, "sub/run.sh": hop, "run.sh": "true\n"})
    script = f'source "$GITHUB_WORKSPACE/sub/sub/run.sh"\n{LIB_FETCH}'
    script = script.replace("\n", "\n          ")
    workflow = OUTPUT_JOB.format(path="/tmp/bin", directory=".", script=script)
    assert len(_messages(_write(rooted, workflow))) == 0


@pytest.mark.parametrize(
    ("script", "path", "fires"),
    [
        (f"wget -q -P/opt/tools {PINNED}", "/opt/tools", 0),
        (f"wget -qP/opt/tools {PINNED}", "/opt/tools", 0),
        (f"wget -qO/opt/tools/t {PINNED}", "/opt/tools", 0),
        (f"curl -fsSL -o/opt/tools/t {PINNED}", "/opt/tools", 0),
        (f"curl -fsSLo/opt/tools/t {PINNED}", "/opt/tools", 0),
        (f"curl -fsSL -o/opt/tools/t {PINNED}", "/opt/else", 1),
        (f"curl -fsSL -Ttool.tgz -o /opt/tools/t {PINNED}", "l.tgz", 1),
        (f"wget -BxyzO/opt/t {PINNED}", "/opt", 1),
        (f"curl -fsSLOJ {PINNED}", "tool", 0),
        (f"curl -fsSLJo tool {PINNED}", "tool", 0),
        (f"curl -OHx-y:z {PINNED}", "tool", 0),
    ],
    ids=[
        "wget-P-attached",
        "wget-qP-attached",
        "wget-qO-attached",
        "curl-o-attached",
        "curl-cluster-o-attached",
        "curl-o-attached-elsewhere",
        "a-value-flag-before-o",
        "a-value-flag-before-wget-O",
        "curl-OJ-cluster",
        "curl-J-before-o",
        "curl-value-flag-after-O",
    ],
)
def test_an_attached_flag_value_is_read(rooted, script, path, fires):
    """`-P/opt/tools` and `-o/opt/tools/t` carry their value in the same word.
    Only letters that take no value may come before the flag: in `-Ttool.tgz`
    the letters after `-T` are its value, so no `-o` is there."""
    workflow = OUTPUT_JOB.format(path=path, directory=".", script=script)
    assert len(_messages(_write(rooted, workflow))) == fires


@pytest.mark.parametrize(
    ("path", "fires"), [("bin", 1), ("tools/bin/tool", 0)], ids=["root-bin", "tail"]
)
def test_a_script_run_after_a_cd_writes_where_the_cd_went(rooted, path, fires):
    """The parent shell runs `cd tools`, so `-o bin/tool` in the child lands in
    `tools/bin/tool`, not in `bin/tool`."""
    _tree(
        rooted, {"scripts/fetch.sh": f"curl -fsSLo bin/tool {PINNED}\n", "tools/x": ""}
    )
    script = 'cd tools && bash "$GITHUB_WORKSPACE/scripts/fetch.sh"'
    workflow = OUTPUT_JOB.format(path=path, directory=".", script=script)
    assert len(_messages(_write(rooted, workflow))) == fires


@pytest.mark.parametrize(
    ("key", "files", "fires"),
    [
        ("t-${{ hashFiles('wf.yaml') }}", {}, 0),
        ("t-${{ hashFiles('other.yaml') }}", {}, 1),
        ("t-${{ hashFiles('wf.yaml') }}", {"fetch.sh": f"curl -fsSLo t {PINNED}\n"}, 1),
    ],
    ids=["hashes-the-workflow", "hashes-another-file", "fetch-in-a-script"],
)
def test_a_key_hashing_the_workflow_covers_its_inline_fetch(rooted, key, files, fires):
    """The `run:` body is text of the workflow file, so a hash of that file
    names the fetch. A fetch in a script the step runs is not in it."""
    _tree(rooted, files)
    run = "bash fetch.sh" if files else f"curl -fsSLo t {PINNED}"
    path = _write(rooted, _job(_cache(key) + _run(run)))
    assert len(_messages(path)) == fires


def test_a_key_hashing_the_action_file_covers_its_inline_fetch(rooted):
    _action(
        rooted, "tool", f"      - shell: bash\n        run: curl -fsSLo t {PINNED}\n"
    )
    key = "t-${{ hashFiles('.github/actions/tool/action.yaml') }}"
    path = _write(rooted, _job(_cache(key) + "      - uses: ./.github/actions/tool\n"))
    assert uc.check_file(path) == []


@pytest.mark.parametrize(
    ("pattern", "fires"),
    [("scripts/**.sh", 0), ("scripts/*.sh", 0), ("scripts/*.bash", 1)],
    ids=["double-star-inside-a-segment", "star", "another-suffix"],
)
def test_a_hashed_pin_file_is_found_by_any_glob(rooted, pattern, fires):
    """GitHub reads `**.sh` inside a segment as `*.sh`. A matched file that is
    not UTF-8 and that the step never runs is not read."""
    _tree(
        rooted,
        {
            "scripts/pins.sh": "TOOL_VERSION=2.7.0\n",
            "lib.bash": 'load_pins() { source "$1"; }\n',
            "fetch.sh": (
                "source lib.bash\nload_pins scripts/pins.sh\n"
                'curl -fsSLo t "https://example.com/v${TOOL_VERSION}/tool"\n'
            ),
        },
    )
    (rooted / "scripts" / "blob.sh").write_bytes(b"\xff\xfe\n")
    key = f"t-${{{{ hashFiles('{pattern}') }}}}"
    path = _write(rooted, _job(_cache(key) + _run("bash fetch.sh")))
    assert len(_messages(path)) == fires


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
        'ARGV=(shasum -a 256)\necho "$SHA  t" | "${ARGV[@]}" -c -',
    ],
    ids=["sha256sum", "shasum", "here-string", "tool-array"],
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
    """`sha256sum -c t.sha256` checks `t` against the committed `t.sha256`."""
    script = DIGEST_SCRIPT.format(check="sha256sum -c t.sha256")
    assert "$URL (digest-checked)" in _finding(_write(tmp_path, _job(_run(script))))


@pytest.mark.parametrize(
    "check",
    [
        'curl -fsSLo t.sha256 "$URL.sha256"\nsha256sum -c t.sha256',
        'curl -fsSLo sums "$URL.sums"\ngrep " t$" sums | sha256sum -c -',
        'sums="$(mktemp)"\ncurl -fsSLo "$sums" "$URL.sums"\n'
        'line="$(grep " t$" "$sums")"\nsha="${line%% *}"\n'
        "printf '%s  %s\\n' \"$sha\" t | sha256sum -c -",
        'sha256sum -c <<< "$(curl -fsSL "$URL.sha256")  t"',
        'sha="$(gh api repos/o/r/releases/latest --jq .body)"\n'
        'echo "$sha  t" | sha256sum -c -',
        'sha="$(git ls-remote https://example.com/r HEAD)"\n'
        '[[ "$(sha256sum <t | cut -d\' \' -f1)" == "$sha" ]] || exit 1',
        'curl -fsSLo t.sha256 "$URL.sha256"\nsha="$(<t.sha256)"\n'
        'echo "$sha  t" | sha256sum -c -',
    ],
    ids=[
        "downloaded-checksum-file",
        "piped-from-a-download",
        "through-two-assignments",
        "here-string-from-curl",
        "from-gh",
        "from-git-ls-remote",
        "read-by-redirect",
    ],
)
def test_a_check_against_a_digest_the_run_fetches_pins_nothing(tmp_path, check):
    """A digest the run downloads, or asks GitHub or a git remote for, changes
    with the bytes it vouches for. Each `-c` input is followed: the checksum
    file, the commands that pipe into the check, and each assignment a printed
    value takes its value from. The `$SHA` control in the suite above pins the
    same download."""
    script = DIGEST_SCRIPT.format(check=check)
    assert _messages(_write(tmp_path, _job(_run(script)))) == []


@pytest.mark.parametrize(
    "lookup",
    [
        'name="$(curl -fsSL "$URL/name")"\nsha="$(grep "$name" pins.sums | cut -d" " -f1)"',
        'sha="$(grep -F -- "$URL" pins.sums | cut -d" " -f1)"',
        'sha="$(grep -F -- "$SRC" pins.sums | cut -d" " -f1)"',
    ],
    ids=["an-argument-from-a-download", "keyed-by-the-url", "keyed-by-a-url-variable"],
)
def test_a_digest_looked_up_in_a_committed_file_pins(tmp_path, lookup):
    """A digest read from a committed file pins the download, whatever words
    pick its line. A variable that is only an argument of the lookup is no
    part of the digest. A URL names no file, though its last segment matches
    the file the download writes. A variable that holds the URL another
    download reads names no file either."""
    script = (
        'URL=https://example.com/tool/latest\ncurl -fsSLo t "$URL"\n'
        'SRC="https://example.com/notes"\n'
        '[[ -z "${MIRROR:-}" ]] || SRC="$MIRROR/notes"\n'
        'curl -fsSLo notes "$SRC"\n'
        f'{lookup}\necho "$sha  t" | sha256sum -c -\n'
    )
    assert "(digest-checked)" in _finding(_write(tmp_path, _job(_run(script))))


@pytest.mark.parametrize(
    "script",
    [
        'EXPECTED="$(cat sums)"\nOUT=t\ncurl -fsSLo "$OUT" "$URL"\n'
        'sha256sum -c <<< "${EXPECTED}  ${OUT}"',
        'OUT="$1"\ncurl -fsSLo "$OUT" "$URL"\nsha256sum -c <<< "$(cat sums)  ${OUT}"',
    ],
    ids=["known-name", "name-only-the-run-knows"],
)
def test_a_partly_unknown_here_string_still_names_the_file(tmp_path, script):
    """The digest is computed at run time, but the file name is the output's:
    a fixed value, or the same variable the download writes to."""
    script = "URL=https://example.com/tool/latest\n" + script
    messages = _messages(_write(tmp_path, _job(_run(script))))
    assert ["(digest-checked)" in m for m in messages] == [True]


def test_wget_to_stdout_in_a_flag_cluster_writes_no_file(tmp_path):
    """`-qO-` sends the bytes to stdout, so `t` is a file nobody wrote."""
    script = (
        f"SHA={DIGEST}\nURL=https://example.com/tool\n"
        'wget -qO- "$URL/t" | tar -x\necho "$SHA  t" | sha256sum -c -\n'
    )
    assert uc.check_file(_write(tmp_path, _job(_run(script)))) == []


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


# ── a comparison against a digest pins the bytes ───────────────────────────


def _finding(path: Path) -> str:
    """The one finding the workflow at PATH gets."""
    messages = _messages(path)
    assert len(messages) == 1, messages
    return messages[0]


@pytest.mark.parametrize(
    "check",
    [
        '[[ "$(sha256sum <t | cut -d\' \' -f1)" == "$SHA" ]] || exit 1',
        '[ "$(sha256sum t | cut -d\' \' -f1)" = "$SHA" ] || exit 1',
        'got="$(sha256sum t)"\nif [[ "${got%% *}" != "$SHA" ]]; then exit 1; fi',
        'ARGV=(sha256sum)\n[[ "$("${ARGV[@]}" <t | cut -d\' \' -f1)" == "$SHA" ]]',
        'H=(shasum)\nH+=(-a 256)\n[[ "$("${H[@]}" <t | cut -d\' \' -f1)" == "$SHA" ]]',
        'h() { sha256sum | cut -d\' \' -f1; }\n[[ "$(h <t)" == "$SHA" ]]',
        'pin() { echo "$SHA"; }\nwant="$(pin kata)"\n'
        '[[ "$(sha256sum <t | cut -d\' \' -f1)" == "$want" ]]',
        '[[ "$(sha256sum <t | cut -d\' \' -f1)" == "$(cat pins/t.sha256)" ]]',
    ],
    ids=[
        "double-bracket",
        "single-bracket",
        "assigned-first",
        "tool-array",
        "tool-array-that-appends-options",
        "function",
        "digest-from-a-function",
        "digest-from-a-committed-file",
    ],
)
def test_a_download_compared_against_a_digest_is_pinned(tmp_path, check):
    """`sha256sum -c` is one spelling of a digest check. Comparing the hash a
    tool prints for the downloaded file is another. A `+=` only appends
    options, so the array still names its tool. A digest that the run reads
    from the tree, and does not download, is still a pin."""
    path = _write(tmp_path, _job(_run(DIGEST_SCRIPT.format(check=check))))
    assert "$URL (digest-checked)" in _finding(path)


@pytest.mark.parametrize(
    "check",
    [
        '[[ "$(sha256sum <other | cut -d\' \' -f1)" == "$SHA" ]]',
        '[[ "$(sha256sum t)" =~ $SHA ]]',
        '[[ "$(sha256sum t)" =~ $SHA && "$MODE" == strict ]]',
        '[[ "$(cat t)" == "$SHA" ]]',
        'got="$(sha256sum t)"\necho "$got"',
        'ARGV=(cat)\n[[ "$("${ARGV[@]}" <t)" == "$SHA" ]]',
        '[[ "$(sha256sum <t)" != "" ]] || exit 1',
        'got="$(sha256sum t)"\n[ "$got" != "" ] || exit 1',
        'got="$(sha256sum t)"\nverify() { [[ "$got" == "$SHA" ]]; }',
        'want="$(curl -fsSL https://example.com/latest/t.sha256)"\n'
        '[[ "$(sha256sum <t | cut -d\' \' -f1)" == "$want" ]] || exit 1',
        'curl -fsSLo t.sha256 "$URL.sha256"\n'
        '[[ "$(sha256sum <t | cut -d\' \' -f1)" == "$(cat t.sha256)" ]] || exit 1',
    ],
    ids=[
        "another-file",
        "regex-match",
        "another-comparison-in-the-test",
        "no-hash",
        "never-compared",
        "array-of-another-tool",
        "against-an-empty-string",
        "assigned-against-an-empty-string",
        "compared-in-a-function-that-never-runs",
        "against-a-downloaded-digest",
        "against-a-downloaded-checksum-file",
    ],
)
def test_a_comparison_that_is_no_digest_check_pins_nothing(tmp_path, check):
    """The last two compare against a digest the run downloads. That digest
    changes with the bytes, so no cache key can name them."""
    path = _write(tmp_path, _job(_run(DIGEST_SCRIPT.format(check=check))))
    assert uc.check_file(path) == []


# ── a function that downloads a file and checks its digest ─────────────────

# The shape agent-glovebox downloads through: the URL arrives as a variable a
# helper binds at run time, and the digest check sits in a function the call
# reaches two levels down.
CHECKED_LIB = """\
if command -v sha256sum >/dev/null; then HASH_ARGV=(sha256sum); else HASH_ARGV=(shasum -a 256); fi
hash_into() {
  local out hex='([0-9a-f]{64})'
  out="$("${HASH_ARGV[@]}")" || return 1
  [[ "$out" =~ $hex ]] || return 1
  printf -v "$1" '%s' "${BASH_REMATCH[1]}"
}
hash_of() { local sum; hash_into sum || return 1; printf '%s\\n' "$sum"; }
get() { curl -fsSL -o "$2" "$1"; }
download_pinned() {
  local url="$1" want="$2" file="$3"
  retry get "$url" "$file.part" || return 1
  [[ "$(hash_of <"$file.part")" == "$want" ]] || return 1
  mv -f -- "$file.part" "$file"
}
"""
KATA_CALL = 'fetch_verified kata "$kata_version" "$kata_url" kata.tar.zst\n'
CHECKED_PROVISION = f"""\
source "$(dirname "$0")/lib.bash"
fetch_verified() {{
  local tool="$1" version="$2" url="$3" dest="$4"
  download_pinned "$url" "$(pin_of "$tool")" "$dest"
}}
eval "$(python3 pins.py)"
{KATA_CALL}"""
WRAPPED_PROVISION = CHECKED_PROVISION.replace(
    KATA_CALL,
    f'provision() {{\n  {KATA_CALL}  fetch_verified fc "$fc_version" "$fc_url" fc.tar\n}}\n'
    "provision\n",
)
# A downloader that checks the file it wrote against a digest in its body.
VERIFIED_FETCH = (
    f"SHA={DIGEST}\n"
    'fetch() { curl -fsSL -o "$2" "$1"; echo "$SHA  $2" | sha256sum -c -; }\n'
)


def _provision(
    rooted: Path,
    provision: str = CHECKED_PROVISION,
    lib: str = CHECKED_LIB,
    key: str | None = None,
) -> Path:
    """A workflow whose step runs PROVISION beside LIB, under a cache on KEY."""
    _tree(rooted, {"lib.bash": lib, "provision.bash": provision})
    return _write(
        rooted, _job((_cache(key) if key else "") + _run("bash provision.bash"))
    )


def test_a_call_to_a_downloader_that_checks_a_digest_is_pinned_where_it_is_called(
    rooted,
):
    """The call names the tool and its version, so that call is the site. The
    calls inside the two functions are their own plumbing, not more fetches."""
    finding = _finding(_provision(rooted))
    assert (
        "fetch_verified kata $kata_version $kata_url kata.tar.zst (digest-checked)"
        " in provision.bash" in finding
    )
    assert "download_pinned" not in finding
    assert "retry get" not in finding


@pytest.mark.parametrize(
    ("key", "fires"),
    [
        ("k-${{ env.kata_version }}", 0),
        ("k-${{ hashFiles('lib.bash') }}", 0),
        ("k-${{ hashFiles('provision.bash') }}", 0),
        ("k-${{ hashFiles('other.bash') }}", 1),
    ],
    ids=["version", "the-checking-file", "the-calling-file", "an-unrelated-file"],
)
def test_a_cache_keyed_on_the_call_or_the_checking_function_covers_it(
    rooted, key, fires
):
    assert len(_messages(_provision(rooted, key=key))) == fires


@pytest.mark.parametrize(
    ("provision", "lib"),
    [
        (
            CHECKED_PROVISION,
            CHECKED_LIB.replace(
                '[[ "$(hash_of <"$file.part")" == "$want" ]] || return 1\n', ""
            ),
        ),
        (CHECKED_PROVISION, CHECKED_LIB.replace("retry get", "retry cp")),
        (
            CHECKED_PROVISION.replace(
                KATA_CALL, "command -v fetch_verified >/dev/null || exit 1\n"
            ),
            CHECKED_LIB,
        ),
        (CHECKED_PROVISION.replace(KATA_CALL, "type -t fetch_verified\n"), CHECKED_LIB),
    ],
    ids=["no-digest-check", "no-download", "lookup-command-v", "lookup-type"],
)
def test_a_function_that_does_not_both_download_and_check_pins_nothing(
    rooted, provision, lib
):
    """A lookup of the checked downloader runs nothing, so it downloads
    nothing either."""
    assert uc.check_file(_provision(rooted, provision, lib)) == []


@pytest.mark.parametrize(
    "script",
    [
        "get_latest() { curl -fsSL -o tool https://example.com/latest/tool; }\n"
        'verify_config() { [[ "$(sha256sum <cfg)" == "$CFG_SHA" ]]; }\n'
        "install() { get_latest; verify_config; }\n"
        "install\n",
        "get_tool() {\n"
        "  curl -fsSL https://example.com/latest/t -o t\n"
        '  [[ "$(sha256sum <other)" == "$SHA" ]]\n'
        "}\n"
        "get_tool\n",
        "get_tool() {\n"
        "  curl -fsSL https://example.com/latest/t -o t\n"
        '  [[ "$(sha256sum <package.json)" == "$STAMP" ]]\n'
        "}\n"
        "get_tool\n",
        f"SHA={DIGEST}\n"
        'fetch() { curl -fsSL "$1" 2>"$2"; echo "$SHA  $2" | sha256sum -c -; }\n'
        'fetch "$URL" tool\n',
    ],
    ids=[
        "check-in-a-sibling-function",
        "check-of-another-file-in-the-body",
        "check-of-a-cache-stamp-in-the-body",
        "checked-file-gets-only-stderr",
    ],
)
def test_a_check_of_a_file_no_download_writes_pins_nothing(tmp_path, script):
    """Each check hashes a file other than the one the download writes: a file
    in a sibling function, another file in the same body, or a file that
    gets only curl's messages (`2>`) while the bytes go to stdout."""
    assert uc.check_file(_write(tmp_path, _job(_run(script)))) == []


@pytest.mark.parametrize(
    ("key", "fires"),
    [
        ("k-${{ env.kata_version }}-${{ env.fc_version }}", 0),
        ("k-${{ env.kata_version }}", 1),
    ],
    ids=["both-pins", "one-pin"],
)
def test_a_wrapper_with_no_arguments_does_not_hide_the_pinned_calls(rooted, key, fires):
    """`provision` is the outermost call, but it carries no pin. The calls
    inside it name each tool and its version, so they are the fetches."""
    finding = _finding(_provision(rooted, WRAPPED_PROVISION))
    assert "fetch_verified kata $kata_version" in finding
    assert "fetch_verified fc $fc_version" in finding
    assert "provision (digest-checked)" not in finding
    assert len(_messages(_provision(rooted, WRAPPED_PROVISION, key=key))) == fires


@pytest.mark.parametrize(
    ("provision", "key"),
    [
        (
            WRAPPED_PROVISION.replace('"$fc_version"', '"$rel"'),
            "k-${{ env.kata_version }}",
        ),
        (
            WRAPPED_PROVISION.replace('"$kata_version"', '"$a"').replace(
                '"$fc_version"', '"$b"'
            ),
            None,
        ),
    ],
    ids=["one-call-names-no-pin", "no-call-names-a-pin"],
)
def test_the_outermost_call_reports_a_download_no_call_pins(rooted, provision, key):
    """When `fc` names no pin, a key on the kata pin alone leaves `fc`
    downloading on every run, so the wrapper reports it."""
    finding = _finding(_provision(rooted, provision, key=key))
    assert "provision (digest-checked) in provision.bash" in finding
    assert "fetch_verified" not in finding


@pytest.mark.parametrize(
    ("calls", "key", "label"),
    [
        (
            'provision() { pip install ruff==0.14.0; fetch "$URL" t; }\nprovision\n',
            "k-ruff-0.14.0",
            "provision (digest-checked)",
        ),
        ('a() { fetch "$URL" t; b; }\nb() { a; }\na\n', None, "a (digest-checked)"),
        (
            'prov() { fetch "$URL" "t-$V"; }\nprov "$TOOL_VERSION"\n',
            None,
            "prov $TOOL_VERSION (digest-checked)",
        ),
    ],
    ids=["wrapper-that-also-installs", "call-cycle", "pin-on-each-call"],
)
def test_one_call_reports_a_chain_of_checked_calls(tmp_path, calls, key, label):
    """The key covers the ruff install and leaves the checked download the
    same wrapper calls. A cycle is one download. With a pin on each call,
    the outermost one is the fetch."""
    script = VERIFIED_FETCH + calls
    path = _write(tmp_path, _job((_cache(key) if key else "") + _run(script)))
    assert label in _finding(path)


@pytest.mark.parametrize(
    ("key", "fires"),
    [(f"k-{DIGEST}", 0), ("k-${{ env.SHA }}", 0), ("k-other", 1)],
    ids=["digest", "digest-variable", "unrelated"],
)
def test_the_digest_inside_a_checking_function_is_a_pin_of_its_call(
    tmp_path, key, fires
):
    """The call passes only a URL the run decides, but the function checks
    the bytes against `$SHA`, so a key that names that digest holds them."""
    script = VERIFIED_FETCH + 'fetch "$URL" tool\n'
    path = _write(tmp_path, _job(_cache(key) + _run(script)))
    assert len(_messages(path)) == fires


def test_a_checked_downloader_with_a_pinned_url_inside_is_reported_once(tmp_path):
    """The URL inside the function already names the pin, so the call to the
    function adds no second fetch."""
    script = (
        f"SHA={DIGEST}\n"
        "download() {\n"
        '  curl -fsSLo "$1" https://example.com/v1.2.3/t\n'
        '  echo "$SHA  $1" | sha256sum -c -\n'
        "}\n"
        "download t\n"
    )
    finding = _finding(_write(tmp_path, _job(_run(script))))
    assert "https://example.com/v1.2.3/t" in finding
    assert "(digest-checked)" not in finding


def test_a_setup_script_behind_a_timeout_is_followed(rooted):
    """agent-glovebox's fresh-setup leg: `timeout … bash setup.bash` is the
    only edge from the step to the downloads, so a walk that stops at the
    wrapper reports a job that fetches every pin on every run as clean."""
    _tree(
        rooted,
        {
            "lib.bash": CHECKED_LIB,
            "provision.bash": CHECKED_PROVISION,
            "setup.bash": "bash provision.bash\n",
            "fresh-setup.sh": "timeout --kill-after=30 900 bash setup.bash </dev/null\n",
        },
    )
    assert "fetch_verified kata" in _finding(
        _write(rooted, _job(_run("bash fresh-setup.sh")))
    )


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


# A Python module that downloads through a `bash -c` script, the way
# agent-glovebox's pkg_fetch.py calls fetch.bash. Its directory reaches
# `sys.path` only through another module, from a base the fold cannot read.
SUBPROCESS_FETCHER = """\
import subprocess
from pathlib import Path

_LIB = Path(__file__).resolve().parent / "fetch.bash"


def get(url, sha256, dest):
    owner = ["download_pinned", url, sha256, dest] if sha256 else ["true"]
    subprocess.run(["bash", "-c", 'source "$1"; shift; "$@"', "_", str(_LIB), *owner])


def wrap():
    get(URL, SHA, "f")


def other():
    return 1
"""
PATHS_PY = (
    "import sys\nfrom pathlib import Path\n"
    "sys.path.insert(0, str(find_root(Path(__file__)) / 'lib'))\n"
)


@pytest.mark.parametrize(
    ("main", "fires"),
    [
        ("import paths\nimport fetcher\nfetcher.get(URL, SHA, 'f')\n", 1),
        ("import paths\nfrom fetcher import get\nget(URL, SHA, 'f')\n", 1),
        ("import paths\nimport fetcher\nfetcher.wrap()\n", 1),
        ("import paths\nfrom fetcher import *\nget(URL, SHA, 'f')\n", 1),
        ("import paths\nimport fetchpkg\nfetchpkg.get(URL, SHA, 'f')\n", 1),
        ("import paths\nimport fetcher\nfetcher.other()\n", 0),
        ("import fetcher\nfetcher.get(URL, SHA, 'f')\n", 0),
    ],
    ids=[
        "calls-the-fetch",
        "imports-the-fetch",
        "calls-a-caller-of-the-fetch",
        "star-import",
        "package-re-export",
        "calls-another-function",
        "no-path-insert",
    ],
)
def test_a_python_fetch_through_a_shell_subprocess_fires(rooted, main, fires):
    _tree(
        rooted,
        {
            "lib/fetch.bash": CHECKED_LIB,
            "lib/fetcher.py": SUBPROCESS_FETCHER,
            "lib/fetchpkg/__init__.py": "from .fetcher import get\n",
            "lib/fetchpkg/fetch.bash": CHECKED_LIB,
            "lib/fetchpkg/fetcher.py": SUBPROCESS_FETCHER,
            "scripts/paths.py": PATHS_PY,
            "scripts/main.py": main,
        },
    )
    messages = _messages(_write(rooted, _job(_run("python3 scripts/main.py"))))
    assert ["fetcher.py:get)" in m for m in messages] == [True] * fires


CURL_PY = (
    "import subprocess\nVERSION = '2.7.0'\nsubprocess.check_call(\n"
    "    ['curl', '-fsSLo', '/tmp/tool-cache/tool',\n"
    "     f'https://example.com/releases/download/v{VERSION}/tool'])\n"
)


@pytest.mark.parametrize(
    ("cache", "fires"),
    [
        ("", 1),
        (_cache("tool-${{ hashFiles('fetch.py') }}", "/tmp/tool-cache"), 0),
        (_cache("deps-1", "~/.cache/some-other-tool"), 1),
    ],
    ids=["no-cache", "cached", "cache-holds-something-else"],
)
def test_a_python_curl_subprocess_is_a_fetch(rooted, cache, fires):
    _tree(rooted, {"fetch.py": CURL_PY})
    path = _write(rooted, _job(cache + _run("python3 fetch.py")))
    assert len(_messages(path)) == fires


IMPORT_TIME_FETCHER = """\
import urllib.request


def download():
    urllib.request.urlretrieve("https://example.com/v2.7.0/tool", "tool")


download()
"""


@pytest.mark.parametrize(
    ("call", "fires"),
    [("download()\n", 1), ('if __name__ == "__main__":\n    download()\n', 0)],
    ids=["at-import", "under-the-main-guard"],
)
def test_a_download_an_imported_module_runs_at_import_fires(rooted, call, fires):
    """The module calls its downloader at module level, so importing it is
    enough: the importer names nothing from it. A main guard runs nothing on
    import."""
    fetcher = IMPORT_TIME_FETCHER.replace("download()\n", call)
    _tree(rooted, {"main.py": "import fetcher\n", "fetcher.py": fetcher})
    assert len(_messages(_write(rooted, _job(_run("python3 main.py"))))) == fires


@pytest.mark.parametrize(
    ("local", "fires"),
    [
        ("    URL = 'https://example.com/latest/tool'\n", 0),
        ("", 1),
    ],
    ids=["local-floating-url-shadows-the-pin", "module-pin"],
)
def test_a_local_binding_shadows_a_module_constant_in_argv(rooted, local, fires):
    fetch = (
        "import subprocess\nURL = 'https://example.com/v2.7.0/tool'\n\n\n"
        f"def get():\n{local}    subprocess.run(['curl', '-o', '/tmp/t', URL])\n\n\nget()\n"
    )
    _tree(rooted, {"fetch.py": fetch})
    assert len(_messages(_write(rooted, _job(_run("python3 fetch.py"))))) == fires


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


# ── a cache key or path that holds an expression ───────────────────────────

PIN_LIB = {"scripts/pin.bash": "TOOL_VERSION=2.7.0\n"}
PIN_FETCH = (
    "source scripts/pin.bash\n"
    'curl -fsSLo /tmp/tool-cache/tool "https://example.com/v${TOOL_VERSION}/tool"'
)


def _step(body: str, **fields: str) -> str:
    """A step with FIELDS (an `id`, a `uses`) and an indented BODY of YAML."""
    head = "".join(f"        {name}: {value}\n" for name, value in fields.items())
    return "      - " + head.lstrip() + body


def _pin_step(script: str, step_id: str = "pin") -> str:
    lines = "".join(f"          {line}\n" for line in script.splitlines())
    return _step(f"        run: |\n{lines}", id=step_id)


@pytest.mark.parametrize(
    ("writer", "fires"),
    [
        ('echo "v=$TOOL_VERSION" >> "$GITHUB_OUTPUT"', 0),
        ('gha_output "v=${TOOL_VERSION}"', 0),
        ('{ echo "v=$TOOL_VERSION"; } >> "$GITHUB_OUTPUT"', 0),
        ('echo "v=$OTHER" >> "$GITHUB_OUTPUT"', 1),
        ('echo "v=2.7.0" >> "$GITHUB_OUTPUT"', 0),
        ('echo "v=1.0.0" >> "$GITHUB_OUTPUT"', 1),
        ('echo "v=$(cat .tool-version)" >> "$GITHUB_OUTPUT"', 0),
        ('echo "v=$A" >> "$GITHUB_OUTPUT"\necho "v=$B" >> "$GITHUB_OUTPUT"', 0),
        ('echo "other=$TOOL_VERSION" >> "$GITHUB_OUTPUT"', 0),
        ('printf \'v=%s\\n\' "$TOOL_VERSION" >> "$GITHUB_OUTPUT"', 0),
    ],
    ids=[
        "echo-names-the-pin",
        "helper-names-the-pin",
        "grouped-echo-names-the-pin",
        "names-another-variable",
        "the-pinned-version",
        "another-version",
        "computed",
        "two-values",
        "no-such-output",
        "printf-format",
    ],
)
def test_a_step_output_in_a_key_reads_what_the_step_writes(rooted, writer, fires):
    """`steps.pin.outputs.v` is the value the step's script gives `v`. A key that
    holds it names the pin when the script writes `v=$TOOL_VERSION`. A value the
    lint cannot fix (a command substitution, two writes, no write) could be the
    pin, so that cache covers every download in the job."""
    _tree(rooted, PIN_LIB)
    path = _write(
        rooted,
        _job(
            _pin_step(writer)
            + _cache("tool-${{ steps.pin.outputs.v }}")
            + _run(PIN_FETCH)
        ),
    )
    assert len(_messages(path)) == fires


def test_a_step_output_only_a_later_step_writes_is_not_read(rooted):
    """A step after the cache cannot set its key, so the key reads no pin from
    it and covers like any key the lint cannot read."""
    _tree(rooted, PIN_LIB)
    cache = _cache("tool-${{ steps.pin.outputs.v }}")
    late = _pin_step('echo "v=$OTHER" >> "$GITHUB_OUTPUT"')
    path = _write(rooted, _job(cache + late + _run(PIN_FETCH)))
    assert _messages(path) == []


@pytest.mark.parametrize(
    ("restore_key", "save_key", "fires"),
    [
        (
            "tool-${{ env.TOOL_VERSION }}",
            "${{ steps.restore.outputs.cache-primary-key }}",
            0,
        ),
        ("deps-${{ runner.os }}", "${{ steps.restore.outputs.cache-primary-key }}", 1),
        ("deps-${{ runner.os }}", "deps-${{ steps.restore.outputs.cache-hit }}", 1),
    ],
    ids=["primary-key-names-the-pin", "primary-key-names-nothing", "cache-hit-flag"],
)
def test_a_cache_steps_outputs_read_as_its_own_key(
    rooted, restore_key, save_key, fires
):
    """A save step keyed on the restore step's primary key has the restore
    step's key. `cache-hit` reports how the restore went and names no pin."""
    _tree(rooted, PIN_LIB)
    restore = _step(
        f"        with:\n          path: /tmp/elsewhere\n          key: {restore_key}\n",
        id="restore",
        uses="actions/cache/restore@v4",
    )
    save = _step(
        f"        with:\n          path: /tmp/elsewhere\n          key: {save_key}\n",
        uses="actions/cache/save@v4",
    )
    path = _write(
        rooted,
        _job(_step("", uses="actions/checkout@v4") + _run(PIN_FETCH) + save).replace(
            "      - uses: actions/checkout@v4\n", restore
        ),
    )
    assert len(_messages(path)) == fires


ACTION_CACHE = (
    "    - uses: actions/cache@v4\n"
    "      with:\n"
    "        path: /tmp/elsewhere\n"
    "        key: ${{ inputs.tool }}-${{ hashFiles(format('scripts/{0}.bash', inputs.tool)) }}\n"
)


@pytest.mark.parametrize(
    ("with_block", "default", "fires"),
    [
        ("        with:\n          tool: pin\n", "", 0),
        ("        with:\n          tool: other\n", "", 1),
        ("", "pin", 0),
        ("", "other", 1),
        ("        with:\n          tool: ${{ matrix.tool }}\n", "", 0),
    ],
    ids=[
        "passed-the-pin-file",
        "passed-another",
        "default",
        "other-default",
        "computed",
    ],
)
def test_a_composite_actions_key_reads_the_inputs_its_caller_passes(
    rooted, with_block, default, fires
):
    """`inputs.tool` in a composite action is what the step that uses it passes,
    or the input's default. `format()` and `hashFiles()` then read the file
    that sets the pin. An input the caller computes covers every download."""
    _tree(rooted, PIN_LIB)
    directory = rooted / ".github" / "actions" / "cache-tool"
    directory.mkdir(parents=True)
    (directory / "action.yaml").write_text(
        "name: cache-tool\ninputs:\n  tool:\n    description: x\n"
        f"    default: '{default}'\n"
        f"runs:\n  using: composite\n  steps:\n{ACTION_CACHE}",
        encoding="utf-8",
    )
    path = _write(
        rooted,
        _job(
            "      - uses: ./.github/actions/cache-tool\n"
            + with_block
            + _run(PIN_FETCH)
        ),
    )
    assert len(_messages(path)) == fires


@pytest.mark.parametrize(
    ("cache_path", "env", "fires"),
    [
        ("${{ env.TOOL_CACHE }}", "env:\n  TOOL_CACHE: /tmp/tool-cache\n", 0),
        ("${{ env.TOOL_CACHE }}", "env:\n  TOOL_CACHE: /tmp/other\n", 1),
        ("${{ runner.temp }}/tool-cache", "", 0),
        ("${{ runner.temp }}/other-cache", "", 1),
        ("/tmp/${{ runner.os }}", "", 0),
        ("${{ env.STORE_PATH }}", "", 1),
        ("${{ steps.none.outputs.dir }}/tool-cache", "", 0),
        ("${{ fromJSON('[]') }}/other-cache", "", 1),
        (
            "${{ env.TOOL_CACHE }}",
            "env:\n  TOOL_CACHE: ${{ runner.temp }}/tool-cache\n",
            0,
        ),
        ("${{ env.TOOL_CACHE }}", "env:\n  TOOL_CACHE: ${{ runner.temp }}/other\n", 1),
        ("${{ env.TOOL_CACHE }}", "env:\n  TOOL_CACHE: ${{ env.TOOL_CACHE }}/x\n", 1),
    ],
    ids=[
        "env-holds-it",
        "env-holds-another",
        "computed-root",
        "computed-root-elsewhere",
        "computed-leaf",
        "computed-whole",
        "unreadable-root",
        "unreadable-root-elsewhere",
        "env-computed-root",
        "env-computed-root-elsewhere",
        "env-names-itself",
    ],
)
def test_a_cache_path_reads_the_env_and_globs_the_rest(rooted, cache_path, env, fires):
    """`env.*` in a path reads the workflow's `env:`. A part the text does not
    fix is a `**`, so `/tmp/${{ runner.os }}` and `${{ runner.temp }}/tool-cache`
    can hold `/tmp/tool-cache/tool`, and `${{ runner.temp }}/other-cache`
    cannot. An expression the lint cannot read is a `**` too, and so is the
    computed part of an `env:` value. A path the text fixes no part of holds
    nothing the lint can tie to a download."""
    _tree(rooted, PIN_LIB)
    body = _job(_cache("deps-${{ runner.os }}", cache_path) + _run(PIN_FETCH))
    path = _write(rooted, body.replace("jobs:\n", env + "jobs:\n"))
    assert len(_messages(path)) == fires


def test_an_expression_with_an_operator_covers_every_download(rooted):
    """GitHub's operators coerce by rules the lint does not re-implement, so the
    key's value is unknown and could name the pin."""
    _tree(rooted, PIN_LIB)
    cache = _cache("tool-${{ inputs.a || 'x' }}")
    path = _write(rooted, _job(cache + _run(PIN_FETCH)))
    assert _messages(path) == []


@pytest.mark.parametrize(
    ("read", "hashed", "fires"),
    [
        ('TOOL_VERSION="$(jq -r .version pins/tool.json)"', "pins/tool.json", 0),
        ('TOOL_VERSION="$(jq -r .version pins/tool.json)"', "pins/other.json", 1),
        ('TOOL_VERSION="$(jq -r .version "pins/$name.json")"', "pins/tool.json", 0),
        ('TOOL_VERSION="$(jq -r .version "$file")"', "pins/tool.json", 1),
        ('TOOL_VERSION="$(cat <pins/tool.txt)"', "pins/tool.txt", 0),
        ('TOOL_VERSION="$(<pins/tool.txt)"', "pins/tool.txt", 0),
        ('TOOL_VERSION="$(<pins/tool.txt)"', "pins/other.txt", 1),
        ('cd pins\nTOOL_VERSION="$(cat tool.txt)"', "pins/tool.txt", 0),
        ('cd pins\nTOOL_VERSION="$(cat tool.txt)"', "tool.txt", 1),
        ('raw="$(cat pins/tool.txt)"\nTOOL_VERSION="${raw#v}"', "pins/tool.txt", 0),
        ('TOOL_VERSION="$(jq -r .version -- -)"', "-", 1),
    ],
    ids=[
        "the-file",
        "another-file",
        "a-glob",
        "any-file",
        "stdin",
        "redirect-only",
        "redirect-only-another-file",
        "after-cd",
        "after-cd-another-file",
        "through-a-variable",
        "a-flag",
    ],
)
def test_a_pin_read_from_a_file_is_named_by_a_hash_of_that_file(
    rooted, read, hashed, fires
):
    """`TOOL_VERSION="$(jq -r .version pins/tool.json)"` takes the pin from
    that file, so a key that hashes it names the pin. A part of the path the
    text does not fix reads as `*`, and a word the text fixes no part of could
    be any file. A relative path lands in the directory the `cd` left, and a
    variable the pin is computed from counts as the pin's source. A flag
    names no file."""
    script = read + '\ncurl -fsSLo /tmp/t "https://example.com/v${TOOL_VERSION}/tool"'
    path = _write(
        rooted, _job(_cache(f"t-${{{{ hashFiles('{hashed}') }}}}") + _run(script))
    )
    assert len(_messages(path)) == fires


@pytest.mark.parametrize(
    ("script", "fires"),
    [
        (
            f'w="$(mktemp -d)"\ncurl -fsSLo "$w/t" {PINNED}\ninstall -m 0755 "$w/t" /tmp/tool-cache/t',
            0,
        ),
        (
            f'w="$(mktemp -d)"\ncurl -fsSLo "$w/t" {PINNED}\nsudo cp "$w/t" /tmp/tool-cache/',
            0,
        ),
        (
            f'w="$(mktemp -d)"\ncurl -fsSLo "$w/t" {PINNED}\ninstall -m 0755 "$w/t" /tmp/other/t',
            1,
        ),
        (f'f() {{ curl -fsSLo "$1" "$2"; }}\nf /tmp/tool-cache/t {PINNED}', 0),
        (f'f() {{ curl -fsSLo "$1" "$2"; }}\nf /tmp/other/t {PINNED}', 1),
        (f'd="${{TOOL_CACHE:-$HOME/.cache/tool}}"\ncurl -fsSLo "$d/t" {PINNED}', 0),
    ],
    ids=[
        "installed-into-it",
        "copied-into-it",
        "installed-elsewhere",
        "handed-to-a-downloader",
        "handed-elsewhere",
        "unset-default",
    ],
)
def test_a_download_lands_where_its_copies_and_calls_put_it(rooted, script, fires):
    """A download copied into the cache path, or handed to a downloader function
    as an absolute path under it, is held by that cache. `${X:-$HOME/…}` with no
    file that assigns X spells its default."""
    cache = _cache("deps-${{ runner.os }}", "/tmp/tool-cache")
    if "HOME" in script:
        cache = _cache("deps-${{ runner.os }}", "~/.cache/tool")
    path = _write(rooted, _job(cache + _run(script)))
    assert len(_messages(path)) == fires


@pytest.mark.parametrize(
    ("install", "cache_path", "fires"),
    [
        ("pip install ruff==0.14.0", "~/.cache/pip", 0),
        ("pip install ruff==0.14.0", "~/.npm", 1),
        ("uv tool install ruff==0.14.0", "~/.cache/uv", 0),
        ("npm install -g pre-commit@4.6.1", "~/.npm", 0),
        ("pnpm add --global pre-commit@4.6.1", "/github/home/.local/share/pnpm", 0),
        ("sudo apt-get install -y jq=1.6-2", "/var/cache/apt/archives", 0),
        ("pip install ruff==0.14.0", "web/node_modules/.cache", 1),
        ("pip install ruff==0.14.0", "/home/runner/.cache", 0),
    ],
    ids=[
        "pip",
        "pip-not-npm",
        "uv",
        "npm",
        "pnpm-in-a-container",
        "apt",
        "pip-not-a-project-cache",
        "pip-under-the-runner-home",
    ],
)
def test_a_cache_of_an_installers_store_holds_its_installs(
    tmp_path, install, cache_path, fires
):
    """An installer keeps each download in its own store, so a cache whose path
    holds that store holds the install, whatever its key."""
    path = _write(
        tmp_path, _job(_cache("deps-${{ runner.os }}", cache_path) + _run(install))
    )
    assert len(_messages(path)) == fires


@pytest.mark.parametrize(
    ("passed", "fires"), [("", 0), ('          cache: "false"\n', 1)]
)
def test_a_setup_actions_cache_input_reads_the_composite_input(rooted, passed, fires):
    """`enable-cache: ${{ inputs.cache }}` is on or off as the caller says."""
    _action(
        rooted,
        "uv",
        "    - uses: astral-sh/setup-uv@v5\n      with:\n        enable-cache: ${{ inputs.cache }}\n"
        "        prune-cache: false\n",
    )
    action = rooted / ".github" / "actions" / "uv" / "action.yaml"
    action.write_text(
        action.read_text(encoding="utf-8").replace(
            "runs:", "inputs:\n  cache:\n    default: 'true'\nruns:"
        ),
        encoding="utf-8",
    )
    with_block = "        with:\n" + passed if passed else ""
    uses = "      - uses: ./.github/actions/uv\n" + with_block
    path = _write(rooted, _job(uses + _run("uv tool install ruff==0.14.0")))
    assert len(_messages(path)) == fires


@pytest.mark.parametrize(
    ("script", "named"),
    [
        ('specs=("ruff==0.14.0")\npip install "${specs[@]}"', "ruff==0.14.0"),
        (
            'specs=()\nspecs+=("ruff==${RUFF_VERSION}")\npip install "${specs[@]}"',
            "ruff==${RUFF_VERSION}",
        ),
        ('specs=(ruff==0.14.0 black)\npip install "${specs[@]}"', None),
        (
            'specs=()\nif [[ -n "$X" ]]; then specs+=(ruff==0.14.0); fi\npip install "${specs[@]}"',
            None,
        ),
        (
            'specs=()\nfor t in "$@"; do\n  case "$t" in\n  ruff) specs+=(ruff==0.14.0) ;;\n  esac\n'
            'done\npip install "${specs[@]}"',
            None,
        ),
        ('specs="ruff==0.14.0"\npip install "${specs[@]}"', None),
        (
            'specs=(ruff==0.14.0)\nif [[ -n "$X" ]]; then specs+=(black); fi\n'
            'pip install "${specs[@]}"',
            None,
        ),
        (
            'specs=(ruff==0.14.0)\nif [[ -n "$X" ]]; then specs+=(black==24.1.0); fi\n'
            'pip install "${specs[@]}"',
            "ruff==0.14.0",
        ),
        (
            'specs=()\n[[ -n "$X" ]] && specs+=(ruff==0.14.0)\npip install "${specs[@]}"',
            None,
        ),
        (
            'specs=()\nspecs+=(ruff==0.14.0) || exit 1\npip install "${specs[@]}"',
            "ruff==0.14.0",
        ),
    ],
    ids=[
        "literal",
        "appended",
        "one-unpinned",
        "under-an-if",
        "under-a-case",
        "not-a-list",
        "unpinned-under-an-if",
        "pinned-under-an-if",
        "after-and",
        "before-or",
    ],
)
def test_an_install_reads_the_specs_an_array_holds(tmp_path, script, named):
    """`pip install "${specs[@]}"` installs what every assignment of `specs`
    gives it. An unpinned element makes the install the versionless lint's
    concern, even when a condition adds it. A pinned element added under a
    condition is not fixed by the text, so no finding names it."""
    messages = _messages(_write(tmp_path, _job(_run(script))))
    assert [named in m for m in messages] == ([True] if named else [])
    assert not any("black" in m for m in messages)
