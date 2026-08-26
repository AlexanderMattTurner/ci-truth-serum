"""Tests for .github/scripts/phone-home-extract.js."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None and not os.environ.get("CI"),
    reason="node not available (CI runners must have it: skipping there would silently drop this suite)",
)

REPO_ROOT = Path(
    subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
)
SCRIPT = REPO_ROOT / ".github" / "scripts" / "phone-home-extract.js"
SUBMIT_SCRIPT = REPO_ROOT / ".github" / "scripts" / "phone-home-submit.js"


def phone_home_dir(tmp_path: Path) -> Path:
    """Per-test output dir handed to the script via $PHONE_HOME_DIR.

    Every test gets its own, so parallel xdist workers cannot collide on the
    script's shared /tmp/phone-home default."""
    return tmp_path / "phone-home"


def run_extract(
    tmp_path: Path,
    pr_body: str,
    repo: str = "owner/repo",
    template_repo: str = "tmpl/repo",
) -> tuple[dict, subprocess.CompletedProcess]:
    """Invoke phone-home-extract.js with a mock github-script environment.

    The captured core.setOutput() values are written to a dedicated JSON file
    (not stdout) so the script's own console.log lines can't corrupt them."""
    wrapper = tmp_path / "run.js"
    out_file = tmp_path / "outputs.json"
    wrapper.write_text(
        f"""
const fs = require("fs");
const extract = require({json.dumps(str(SCRIPT))});
const outputs = {{}};
const core = {{ setOutput: (k, v) => {{ outputs[k] = v; }} }};
const [repoOwner, repoName] = (process.env.REPO || "owner/repo").split("/");
const context = {{
  payload: {{
    pull_request: {{
      body: process.env.PR_BODY || "",
      title: "Test PR",
      html_url: `https://github.com/${{process.env.REPO}}/pull/1`,
    }},
  }},
  repo: {{ owner: repoOwner, repo: repoName }},
}};
extract({{ context, core }}).then(() => {{
  fs.writeFileSync(process.env.OUT_FILE, JSON.stringify(outputs));
}}).catch((err) => {{
  process.stderr.write(err.message + "\\n");
  process.exit(1);
}});
""",
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "PR_BODY": pr_body,
        "REPO": repo,
        "TEMPLATE_REPO": template_repo,
        "OUT_FILE": str(out_file),
        "PHONE_HOME_DIR": str(phone_home_dir(tmp_path)),
    }
    result = subprocess.run(
        ["node", str(wrapper)], env=env, capture_output=True, text=True
    )
    outputs: dict = {}
    if result.returncode == 0 and out_file.exists():
        try:
            outputs = json.loads(out_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            pytest.fail(
                f"wrapper wrote unparseable JSON {out_file.read_text(encoding='utf-8')!r}: {exc}"
            )
    return outputs, result


def test_extracts_lessons_with_double_hash(tmp_path: Path) -> None:
    pr_body = (
        "## Summary\n\nSome changes.\n\n"
        "## Lessons Learned\n\n"
        "- Use jq instead of node for JSON parsing.\n\n"
        "## Other\n\nNothing.\n"
    )
    outputs, result = run_extract(tmp_path, pr_body)
    assert result.returncode == 0, result.stderr
    assert outputs.get("has_lessons") == "true"
    content = (phone_home_dir(tmp_path) / "lessons.txt").read_text(encoding="utf-8")
    assert "Use jq instead of node for JSON parsing." in content
    assert "Nothing." not in content  # the following ## section must terminate


def test_extracts_lessons_with_triple_hash(tmp_path: Path) -> None:
    """### Lessons Learned (3 hashes) must be recognised and terminated by the
    next heading, regardless of that heading's level."""
    pr_body = (
        "## Summary\n\nSome changes.\n\n"
        "### Lessons Learned\n\n"
        "- Always validate input before processing.\n\n"
        "### Notes\n\nnoise-after-section.\n"
    )
    outputs, result = run_extract(tmp_path, pr_body)
    assert result.returncode == 0, result.stderr
    assert outputs.get("has_lessons") == "true"
    content = (phone_home_dir(tmp_path) / "lessons.txt").read_text(encoding="utf-8")
    assert "Always validate input before processing." in content
    assert "noise-after-section." not in content


def test_lessons_not_cut_short_by_internal_blank_line(tmp_path: Path) -> None:
    """Multi-paragraph lessons must not be truncated at the first blank line."""
    pr_body = (
        "## Lessons Learned\n\n- First bullet.\n\n- Second bullet after blank line.\n"
    )
    outputs, result = run_extract(tmp_path, pr_body)
    assert result.returncode == 0, result.stderr
    assert outputs.get("has_lessons") == "true"
    content = (phone_home_dir(tmp_path) / "lessons.txt").read_text(encoding="utf-8")
    assert "First bullet." in content
    assert "Second bullet after blank line." in content


def test_skips_when_no_lessons_section(tmp_path: Path) -> None:
    pr_body = "## Summary\n\nSome changes.\n\n## Notes\n\nNothing here.\n"
    outputs, result = run_extract(tmp_path, pr_body)
    assert result.returncode == 0, result.stderr
    assert "has_lessons" not in outputs


def test_skips_empty_lessons_section(tmp_path: Path) -> None:
    pr_body = "## Lessons Learned\n\n\n\n## Other Section\n\nContent.\n"
    outputs, result = run_extract(tmp_path, pr_body)
    assert result.returncode == 0, result.stderr
    assert "has_lessons" not in outputs


def test_skips_unfilled_skeleton_section(tmp_path: Path) -> None:
    """A Lessons section that is ONLY the unfilled What/Where/Why skeleton is long
    enough to clear the first two length gates, so it reaches the skeleton-strip
    check (phone-home-extract.js:73-77): once `**What**:`/`**Where**:`/`**Why**:`
    are stripped, nothing substantive remains and has_lessons must not be set."""
    pr_body = (
        "## Lessons Learned\n\n- **What**: \n- **Where**: \n- **Why**: \n"
        "## Other\n\nContent.\n"
    )
    outputs, result = run_extract(tmp_path, pr_body)
    assert result.returncode == 0, result.stderr
    assert "has_lessons" not in outputs
    assert not (phone_home_dir(tmp_path) / "lessons.txt").exists()


def test_skips_template_repo(tmp_path: Path) -> None:
    pr_body = "## Lessons Learned\n\n- Important lesson.\n"
    outputs, result = run_extract(
        tmp_path, pr_body, repo="tmpl/repo", template_repo="tmpl/repo"
    )
    assert result.returncode == 0, result.stderr
    assert "has_lessons" not in outputs


def test_filters_session_links(tmp_path: Path) -> None:
    pr_body = (
        "## Lessons Learned\n\n"
        "- Real lesson here.\n"
        "https://claude.ai/code/session_abc123\n"
    )
    outputs, result = run_extract(tmp_path, pr_body)
    assert result.returncode == 0, result.stderr
    assert outputs.get("has_lessons") == "true"
    content = (phone_home_dir(tmp_path) / "lessons.txt").read_text(encoding="utf-8")
    assert "claude.ai" not in content


def test_submit_reads_the_dir_extract_wrote(tmp_path: Path) -> None:
    """The two scripts must agree on $PHONE_HOME_DIR: extract writes lessons.txt
    there and submit reads it back. Drive both against one non-default dir so a
    divergence between them fails here instead of at runtime on a merged PR."""
    pr_body = "## Lessons Learned\n\n- Round-trip lesson body.\n"
    outputs, result = run_extract(tmp_path, pr_body)
    assert result.returncode == 0, result.stderr
    assert outputs.get("has_lessons") == "true"

    wrapper = tmp_path / "run-submit.js"
    issue_file = tmp_path / "issue.json"
    wrapper.write_text(
        f"""
const fs = require("fs");
const submit = require({json.dumps(str(SUBMIT_SCRIPT))});
const github = {{
  rest: {{
    issues: {{
      create: async (p) => {{
        fs.writeFileSync(process.env.ISSUE_FILE, JSON.stringify(p));
        return {{ data: {{ html_url: "https://example.invalid/1", number: 1 }} }};
      }},
      addLabels: async () => {{}},
    }},
  }},
}};
submit({{ github }}).catch((err) => {{
  process.stderr.write(err.message + "\\n");
  process.exit(1);
}});
""",
        encoding="utf-8",
    )
    submit_result = subprocess.run(
        ["node", str(wrapper)],
        env={
            **os.environ,
            "PHONE_HOME_DIR": str(phone_home_dir(tmp_path)),
            "ISSUE_FILE": str(issue_file),
            "PR_TITLE": "Test PR",
            "PR_URL": "https://github.com/owner/repo/pull/1",
            "SOURCE_REPO": "owner/repo",
            "TEMPLATE_REPO": "tmpl/repo",
        },
        capture_output=True,
        text=True,
    )
    assert submit_result.returncode == 0, submit_result.stderr
    issue = json.loads(issue_file.read_text(encoding="utf-8"))
    assert "Round-trip lesson body." in issue["body"]


def test_workflow_carries_the_path_in_exactly_one_place() -> None:
    """The workflow declares PHONE_HOME_DIR once and every step reads it from
    there. A second hardcoded copy (the gitleaks `-s` argument was one) makes
    the scan silently miss the lessons file the moment the path moves."""
    workflow = yaml.safe_load(
        (REPO_ROOT / ".github" / "workflows" / "phone-home.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert workflow["env"].get("PHONE_HOME_DIR"), "workflow must declare the dir"
    steps = workflow["jobs"]["phone-home"]["steps"]
    hardcoded = [s.get("name") for s in steps if "/tmp/" in json.dumps(s)]
    assert hardcoded == [], (
        f"steps hardcode a /tmp path instead of $PHONE_HOME_DIR: {hardcoded}"
    )
