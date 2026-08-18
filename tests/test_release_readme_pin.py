"""The release path must move the README's `rev:` pins with the version bump.

The README shows a consumer a `.pre-commit-config.yaml` to copy, and each
example pins `rev: vX.Y.Z`. That pin resolves only when the tag exists, and the
tag comes from `package.json`, so the pins have to move in the release commit.
They did not: `tests/cts/test_readme_rev.py` failed every release PR, and
v1.1.0 sat open and red for three days while the daily readiness run stood down
because a release PR was already open.

Two layers: the pinner is driven for real against a temporary README, and the
two release scripts are pinned to call it before they commit.
"""

import re
import subprocess
from pathlib import Path

from tests._helpers import REPO_ROOT

PINNER = REPO_ROOT / "scripts/pin-readme-rev.mjs"
PREP = REPO_ROOT / ".github/scripts/release-prep.sh"
READINESS = REPO_ROOT / ".github/scripts/release-readiness.sh"
BOOTSTRAP = REPO_ROOT / ".github/scripts/release-prep-bump-version.sh"

README_SAMPLE = """# pack

```yaml
repos:
  - repo: https://github.com/AlexanderMattTurner/ci-truth-serum
    rev: v1.0.0 # the release tag
    hooks:
      - id: check-tier1
```

```yaml
- repo: https://github.com/AlexanderMattTurner/ci-truth-serum
  rev: v1.0.0
  hooks:
    - id: check-select
```
"""


def _run(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["node", str(PINNER), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_pinner_moves_every_rev(tmp_path):
    readme = tmp_path / "README.md"
    readme.write_text(README_SAMPLE, encoding="utf-8")
    done = _run(["1.1.0", str(readme)], tmp_path)
    assert done.returncode == 0, done.stderr
    text = readme.read_text(encoding="utf-8")
    assert text.count("rev: v1.1.0") == 2
    assert "v1.0.0" not in text


def test_a_readme_with_no_pin_is_a_no_op(tmp_path):
    """A downstream repo that ships no consumer config has nothing to pin, and
    its release must still run. This repo's own missed pin is caught by
    tests/cts/test_readme_rev.py instead."""
    readme = tmp_path / "README.md"
    readme.write_text("# pack\n\nno config here\n", encoding="utf-8")
    done = _run(["1.1.0", str(readme)], tmp_path)
    assert done.returncode == 0, done.stderr
    assert "nothing to move" in done.stdout
    assert readme.read_text(encoding="utf-8") == "# pack\n\nno config here\n"


def test_a_missing_readme_is_a_no_op(tmp_path):
    """A repo without the file still releases; the pin step is not its gate."""
    done = _run(["1.1.0", str(tmp_path / "README.md")], tmp_path)
    assert done.returncode == 0, done.stderr
    assert "does not exist" in done.stdout


def test_a_version_that_is_not_semver_fails_loudly(tmp_path):
    readme = tmp_path / "README.md"
    readme.write_text(README_SAMPLE, encoding="utf-8")
    done = _run(["v1.1.0", str(readme)], tmp_path)
    assert done.returncode != 0
    assert "strict X.Y.Z" in done.stderr


def test_the_pinned_readme_satisfies_the_rev_guard(tmp_path):
    """End to end against the real guard: pin this repo's own README to a new
    version, and the check that blocked v1.1.0 accepts it."""
    readme = tmp_path / "README.md"
    readme.write_text(
        (REPO_ROOT / "README.md").read_text(encoding="utf-8"), encoding="utf-8"
    )
    assert _run(["9.9.9", str(readme)], tmp_path).returncode == 0
    matches = re.finditer(
        r"^\s*rev:\s*(?P<rev>\S+)", readme.read_text(encoding="utf-8"), re.M
    )
    revs = {m.group("rev") for m in matches}
    # Non-vacuity: the real README genuinely documents rev-pinned examples.
    assert len(revs) == 1 and revs == {"v9.9.9"}


def test_release_prep_pins_the_readme_before_it_commits():
    source = PREP.read_text(encoding="utf-8")
    pin = source.index('node "$PIN_README_REV"')
    commit = source.index("git commit -m")
    assert pin < commit
    # The pinned README must reach the release commit, not the runner's floor,
    # and it is staged under an existence test: `git add` exits 128 on a
    # pathspec that matches nothing, which would abort a release in a repo that
    # ships no README.
    assert "git add -A -- package.json CHANGELOG.md changelog.d" in source
    assert "if [[ -f README.md ]]; then\n  git add -A -- README.md" in source


def test_release_readiness_pins_the_readme_before_it_commits():
    source = READINESS.read_text(encoding="utf-8")
    pin = source.index("pin-readme-rev.mjs")
    commit = source.index('commit -aqm "chore(release)')
    assert pin < commit


def test_the_privileged_job_runs_the_trusted_pinner():
    """The bump job holds the release credentials, so it must run the base
    branch's copy of every script — the pinner included."""
    source = BOOTSTRAP.read_text(encoding="utf-8")
    assert "stage_trusted scripts/pin-readme-rev.mjs PIN_README_REV" in source
