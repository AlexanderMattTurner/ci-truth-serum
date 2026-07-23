"""Tests for ci_truth_serum/check_trusted_base.py — the (security) pre-commit lint that
flags the pwn-request shape: a pull_request(_target) job that checks out the PR
head ref AND runs privileged (write permissions or a secret in env).

Drives check_file(path) directly so each rule is asserted in isolation."""

from pathlib import Path

from tests._helpers import REPO_ROOT, load_hook

tb = load_hook("check_trusted_base.py", "check_trusted_base")

_HEAD_CHECKOUT = (
    "    steps:\n"
    "      - uses: actions/checkout@v4\n"
    "        with:\n"
    "          ref: ${{ github.event.pull_request.head.sha }}\n"
)


def _write(tmp_path: Path, body: str, name: str = "wf.yaml") -> Path:
    path = tmp_path / name
    path.write_text(body)
    return path


# ── the flagged shape ────────────────────────────────────────────────────────


def test_head_checkout_with_write_permissions_is_flagged(tmp_path):
    path = _write(
        tmp_path,
        "on:\n  pull_request_target:\n"
        "permissions:\n  contents: write\n"
        "jobs:\n  build:\n    runs-on: ubuntu-latest\n" + _HEAD_CHECKOUT,
    )
    result = tb.check_file(path)
    assert len(result) == 1
    line, message = result[0]
    assert line == 6  # the `build:` job key
    assert "pwn-request" in message
    assert "build" in message


def test_head_checkout_with_secret_in_step_env_is_flagged(tmp_path):
    path = _write(
        tmp_path,
        "on:\n  pull_request:\n"
        "jobs:\n  build:\n    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "        with:\n          ref: ${{ github.head_ref }}\n"
        "      - run: ./build.sh\n"
        "        env:\n          TOKEN: ${{ secrets.NPM_TOKEN }}\n",
    )
    result = tb.check_file(path)
    assert len(result) == 1
    assert "pwn-request" in result[0][1]


def test_head_ref_dot_ref_variant_is_flagged(tmp_path):
    path = _write(
        tmp_path,
        "on:\n  pull_request:\n"
        "permissions:\n  pull-requests: write\n"
        "jobs:\n  build:\n    runs-on: ubuntu-latest\n"
        "    steps:\n      - uses: actions/checkout@v4\n"
        "        with:\n          ref: ${{ github.event.pull_request.head.ref }}\n",
    )
    assert len(tb.check_file(path)) == 1


# ── safe shapes (not flagged) ────────────────────────────────────────────────


def test_head_checkout_read_only_no_secret_is_clean(tmp_path):
    """Checking out untrusted code with only read perms and no secret is the
    CORRECT way to lint/build a PR — it must not be flagged."""
    path = _write(
        tmp_path,
        "on:\n  pull_request:\n"
        "permissions:\n  contents: read\n"
        "jobs:\n  build:\n    runs-on: ubuntu-latest\n" + _HEAD_CHECKOUT,
    )
    assert tb.check_file(path) == []


def test_privileged_but_no_head_checkout_is_clean(tmp_path):
    """A privileged job that checks out the trusted base merge ref (the default)
    is safe."""
    path = _write(
        tmp_path,
        "on:\n  pull_request_target:\n"
        "permissions:\n  contents: write\n"
        "jobs:\n  build:\n    runs-on: ubuntu-latest\n"
        "    steps:\n      - uses: actions/checkout@v4\n",
    )
    assert tb.check_file(path) == []


def test_non_pr_trigger_is_clean(tmp_path):
    """The vulnerability only exists on pull_request(_target) — a push workflow
    that checks out a ref with write perms is not this class."""
    path = _write(
        tmp_path,
        "on:\n  push:\n"
        "permissions:\n  contents: write\n"
        "jobs:\n  build:\n    runs-on: ubuntu-latest\n" + _HEAD_CHECKOUT,
    )
    assert tb.check_file(path) == []


def test_empty_permissions_drops_all_scopes_is_clean(tmp_path):
    path = _write(
        tmp_path,
        "on:\n  pull_request:\n"
        "permissions: {}\n"
        "jobs:\n  build:\n    runs-on: ubuntu-latest\n" + _HEAD_CHECKOUT,
    )
    assert tb.check_file(path) == []


def test_write_all_string_permissions_is_flagged(tmp_path):
    path = _write(
        tmp_path,
        "on:\n  pull_request:\n"
        "permissions: write-all\n"
        "jobs:\n  build:\n    runs-on: ubuntu-latest\n" + _HEAD_CHECKOUT,
    )
    assert len(tb.check_file(path)) == 1


# ── opt-out (reason required) ────────────────────────────────────────────────


def test_optout_with_reason_suppresses(tmp_path):
    path = _write(
        tmp_path,
        "# trusted-base-ok: runs only the base branch's trusted script copy\n"
        "on:\n  pull_request:\n"
        "permissions:\n  contents: write\n"
        "jobs:\n  build:\n    runs-on: ubuntu-latest\n" + _HEAD_CHECKOUT,
    )
    assert tb.check_file(path) == []


def test_reasonless_optout_does_not_suppress(tmp_path):
    path = _write(
        tmp_path,
        "# trusted-base-ok:\n"
        "on:\n  pull_request:\n"
        "permissions:\n  contents: write\n"
        "jobs:\n  build:\n    runs-on: ubuntu-latest\n" + _HEAD_CHECKOUT,
    )
    assert len(tb.check_file(path)) == 1


def test_optout_token_in_string_value_does_not_suppress(tmp_path):
    path = _write(
        tmp_path,
        "on:\n  pull_request:\n"
        "permissions:\n  contents: write\n"
        'env:\n  NOTE: "trusted-base-ok: fake reason in a value"\n'
        "jobs:\n  build:\n    runs-on: ubuntu-latest\n" + _HEAD_CHECKOUT,
    )
    assert len(tb.check_file(path)) == 1


# ── malformed / non-dict ─────────────────────────────────────────────────────


def test_malformed_yaml_is_reported_not_raised(tmp_path):
    path = _write(tmp_path, "on: [pull_request\njobs: {\n")
    result = tb.check_file(path)
    assert len(result) == 1
    assert result[0][0] is None
    assert "could not parse as YAML" in result[0][1]


def test_non_dict_yaml_is_ignored(tmp_path):
    path = _write(tmp_path, "- a\n- b\n", name="list.yaml")
    assert tb.check_file(path) == []


# ── main wiring ──────────────────────────────────────────────────────────────


def test_main_reports_and_returns_nonzero(tmp_path, monkeypatch, capsys):
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "bad.yaml").write_text(
        "on:\n  pull_request_target:\n"
        "permissions:\n  contents: write\n"
        "jobs:\n  build:\n    runs-on: ubuntu-latest\n" + _HEAD_CHECKOUT
    )
    monkeypatch.setattr(tb, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(tb, "WORKFLOWS_DIR", wf)
    monkeypatch.setattr(tb, "ACTIONS_DIR", tmp_path / ".github" / "actions")
    assert tb.main() == 1
    out = capsys.readouterr().out
    assert "pwn-request" in out
    assert "violation" in out


# ── dogfood: the repo's own workflows are clean (release-prep opts out) ───────


def test_own_workflows_are_clean(monkeypatch):
    monkeypatch.setattr(tb, "REPO_ROOT", REPO_ROOT)
    monkeypatch.setattr(tb, "WORKFLOWS_DIR", REPO_ROOT / ".github" / "workflows")
    monkeypatch.setattr(tb, "ACTIONS_DIR", REPO_ROOT / ".github" / "actions")
    offenders = []
    for path in tb.workflow_files():
        offenders += [f"{path.name}: {msg}" for _, msg in tb.check_file(path)]
    assert offenders == [], offenders
