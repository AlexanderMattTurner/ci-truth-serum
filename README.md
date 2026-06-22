# Claude Automation Template

A GitHub template that makes [Claude Code](https://docs.anthropic.com/en/docs/claude-code) work reliably on your repositories. It wires up git hooks, CI workflows, and Claude session hooks so Claude can autonomously fix code, open PRs, and respond to `@claude` mentions—with guardrails that keep broken code from shipping.

## Why Use This

Using Claude Code on a repo otherwise means hand-rolling hooks, CI workflows, and guardrails against common failure modes (retry loops, pushing broken code, inconsistent formatting). This template ships all of that:

- **A solid `CLAUDE.md`**—high code-quality standards plus a self-critique loop that catches bugs before they leave the editor
- **Pre-push verification**—build, lint, type checks, and tests run before every `git push` / `gh pr create`
- **Deadlock-proof session hooks**—every hook is syntax-checked at session start and degrades to “ask” on parse failure
- **Skill-driven PR flow**—the `pr-creation` skill runs a compress-critique-fix loop on the diff, then watches CI and fixes failures
- **Enforced quality**—Conventional Commits (commitlint), Prettier, and lint-staged on every commit
- **`@claude` integration**—mention Claude in issues or PRs for a full-context response
- **Weekly security sweeps**—Dependabot, code-scanning, secret-scanning, and `pnpm audit` alerts rolled into a fix PR
- **Conflict-free releases**—changelog fragments plus a label-driven semver bump and auto-tagged GitHub Releases (see [Releases](#releases))
- **Automatic template sync**—downstream repos get improvements daily via a 3-way-merge PR that preserves customizations
- **Multi-language**—Node.js (pnpm), Python (uv/ruff/pytest), and shell (shfmt/shellcheck) out of the box

## Prerequisites

- [Node.js](https://nodejs.org/) (version in `.nvmrc`) and [pnpm](https://pnpm.io/) (`setup.sh` installs it if missing)
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code), installed and authenticated
- (Optional) [uv](https://docs.astral.sh/uv/) for Python projects

## Quick Start

1. Click **“Use this template”** on GitHub to create your repo.
2. Clone and set up—this installs dependencies and configures git hooks (output ends with `✓ Setup complete!`):

   ```bash
   git clone <your-repo-url>
   cd <your-repo>
   ./setup.sh
   ```

3. Install the [Claude GitHub App](https://github.com/apps/claude) for `@claude` mentions.
4. Customize: edit **`CLAUDE.md`** (project context/conventions) and wire up your `dev`, `build`, `test`, `lint`, `check` scripts in **`package.json`**. Unconfigured scripts are detected and skipped, so nothing breaks on first push.

## What’s Included

### Git Hooks (`.hooks/`)

| Hook          | What it does                                                                          |
| ------------- | ------------------------------------------------------------------------------------- |
| `pre-commit`  | Runs lint-staged—auto-formats with Prettier, shfmt, and ruff                          |
| `commit-msg`  | Validates [Conventional Commits](https://www.conventionalcommits.org/) via commitlint |
| `lint-skills` | Validates skill files have required frontmatter (`name`, `description`)               |

### Claude Session Hooks (`.claude/hooks/`)

Run inside Claude Code sessions (local or cloud), not in CI.

| Hook           | What it does                                                              |
| -------------- | ------------------------------------------------------------------------- |
| `SessionStart` | Installs tools (shfmt, shellcheck), configures git, installs dependencies |
| `PreToolUse`   | Runs build/lint/typecheck/tests before `git push` or `gh pr create`       |

### Claude Skills & Subagents (`.claude/`)

| Skill / Agent          | What it does                                                                   |
| ---------------------- | ------------------------------------------------------------------------------ |
| `pr-creation`          | Self-critique loop before PR submission, then watches CI and fixes failures    |
| `update-pr`            | Updates an existing PR with new changes and optionally revises the description |
| `conventional-commits` | Guides properly formatted commits with secret detection                        |
| `markdown-block`       | Outputs content in a fenced block so users can copy raw markdown               |
| `peer-review`          | Runs the read-only `code-reviewer` agent on the diff, then triages and fixes   |
| `explore-plan`         | Enforces the Explore → Plan → Review → Verify discipline for non-trivial work  |
| `code-reviewer`        | Read-only subagent (Read/Grep/Glob, `model: opus`)—unbiased second opinion     |

### GitHub Actions (`.github/workflows/`)

| Workflow                           | What it does                                                                           |
| ---------------------------------- | -------------------------------------------------------------------------------------- |
| `claude.yaml`                      | Responds to `@claude` mentions in issues and PR comments                               |
| `template-sync.yaml`               | Daily sync from the template repo with 3-way merge                                     |
| `phone-home.yaml`                  | Propagates “Lessons Learned” from merged PRs back to the template                      |
| `security-vulnerability-scan.yaml` | Weekly security sweep—collects alerts, opens a rollup fix PR                           |
| `release-prep.yaml`                | On the `release` label, bumps the version on the PR branch (see [Releases](#releases)) |
| `tag-release.yaml`                 | Post-merge: tags `vX.Y.Z` and publishes the GitHub Release                             |
| `node-tests.yaml` / `lint.yaml`    | Run `pnpm test` / `pnpm lint` + `pnpm check` (skip gracefully if unconfigured)         |
| `format-check.yaml`                | Checks Prettier formatting                                                             |
| `pre-commit.yaml`                  | Runs pre-commit hooks in CI                                                            |
| `validate-config.yaml`             | Validates `.claude/` and `.hooks/` config on every push                                |
| `dependabot-auto-merge.yaml`       | Auto-merges minor/patch Dependabot PRs after CI passes                                 |

**Required checks:** each PR-gating workflow ends with an `if: always()` summary job (`*-passed`) that passes only when its real jobs succeed or skip. Mark the `*-passed` jobs Required in branch protection, not the underlying jobs—a cancelled/skipped job never reports a status and would leave a PR stuck “pending.” For workflows that use `paths` filters (`lint`, `node-tests`, `validate-config`), drop the filter if you mark them Required, since a path-skipped workflow posts nothing at all.

### MCP Servers (`.mcp.json`)

Team-shared [MCP servers](https://modelcontextprotocol.io/) live in `.mcp.json`. Copy the starter and edit:

```bash
cp .mcp.json.example .mcp.json   # then set any env vars and run /mcp to verify
```

Resist tool bloat—each server adds reasoning overhead, so enable only what you use. Personal servers belong in `~/.claude.json`, not the committed file.

### Session Tuning (`.claude/settings.json`)

The `env` block sets defaults tuned for long-running web/automation sessions: `CLAUDE_CODE_AUTO_COMPACT_WINDOW` (compact earlier to curb context rot), `CLAUDE_CODE_AUTO_BACKGROUND_TASKS` (auto-background long commands), and `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` (disable autoupdater/telemetry). See the [env-vars reference](https://code.claude.com/docs/en/env-vars).

## Releases

Releases use **changelog fragments** so the changelog is never a merge-conflict hotspot. Instead of editing `CHANGELOG.md`, each user-facing change adds one small file under `changelog.d/` named `<id>.<category>.md` (categories follow [Keep a Changelog](https://keepachangelog.com/): `added`, `changed`, `deprecated`, `removed`, `fixed`, `security`). See `changelog.d/README.md` for authoring; `pre-commit` validates fragment names.

To cut a release, label the PR **`release`**:

1. `release-prep.yaml` asks Claude to classify the pending fragments as a **conservative** bump (patch or minor—never major) and commits the `package.json` bump plus the rolled `CHANGELOG.md` section onto the PR branch.
2. On merge, `tag-release.yaml` pushes the `vX.Y.Z` tag and publishes a GitHub Release with that version’s changelog section as the notes.

Both steps are idempotent. They need an `ANTHROPIC_API_KEY` secret (for the bump classification) and use the same `TEMPLATE_SYNC_TOKEN` as template sync when present, falling back to the built-in `GITHUB_TOKEN`. Preview the next changelog with `pnpm changelog:draft`.

## Automatic Updates

Template improvements sync daily at 9am UTC via `template-sync.yaml` (or trigger from **Actions › Sync from Template**). Changes arrive as a PR; a 3-way merge preserves local customizations and asks Claude to resolve conflicts when they arise.

**Token setup:** create a fine-grained PAT with **Read and write** access to `contents`, `workflows`, and `pull requests`, and add it as the repository secret **`TEMPLATE_SYNC_TOKEN`**.

## Project Structure

```
.
├── .claude/                # Session hooks, skills, agents, settings.json
├── .hooks/                 # Git hooks (pre-commit, commit-msg, lint-skills)
├── .github/
│   ├── workflows/          # CI workflows
│   └── scripts/            # Extracted workflow scripts (release, security, sync)
├── bin/lib/                # Shared shell helpers (e.g. retry.bash)
├── changelog.d/            # Changelog fragments (one file per change)
├── config/                 # Shared configuration (e.g. JavaScript linting)
├── scripts/                # assemble-changelog.mjs + tests
├── tests/                  # Python tests for hooks and config validation
├── CHANGELOG.md            # Assembled from changelog.d/ at release time
├── CLAUDE.md               # Instructions for Claude Code sessions
├── package.json            # Node.js deps + lint-staged config
├── pyproject.toml          # Python project config (ruff, pytest)
└── setup.sh                # One-command setup script
```
