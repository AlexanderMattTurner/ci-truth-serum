# ci-truth-serum

**Make your CI confess what it’s hiding.** A pack of fast, offline pre-commit
lints that catch two kinds of lie a green check can hide:

- **Honesty lies:** the pipeline reports success while the real work failed
  (exit codes masked by pipes), or a required check silently never reports and
  the PR hangs forever.
- **Identity lies:** a base image or downloaded artifact is pinned to a
  _mutable_ name (a tag, a bare URL), so the bytes you run aren’t provably the
  bytes you reviewed.

## What it checks

### Honesty (Tier 1, default-on)

| Hook                       | Failure it prevents                                                                                                                                                          |
| -------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `check-workflow-pipefail`  | CI went green while `pytest` was crashing, because `pytest \| tee log` exits with `tee`’s status—under a `runCmd:` / `shell: sh` / custom `bash` that lacks `pipefail`.      |
| `check-exit-suppression`   | A teardown that left a volume pinned reported success, because `cleanup \|\| true` discarded its non-zero exit while keeping its output.                                     |
| `check-stderr-suppression` | A container launch failed with a bare non-zero and no clue why, because `docker compose up 2>/dev/null` threw away the only diagnostic.                                      |
| `check-pr-paths`           | A required check hung at “Expected—Waiting” forever and the PR could never merge, because `paths:`/`paths-ignore:` on `pull_request` skipped the workflow without reporting. |

### Identity (Tier 1, default-on)

| Hook                       | Failure it prevents                                                                                                                                            |
| -------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `check-pinned-base-images` | The base image you reviewed and the one CI built diverged, because `FROM node:22` is a mutable tag the registry can re-point. **Demands a `@sha256:` digest.** |
| `check-pinned-downloads`   | A tampered release or compromised mirror swapped the binary you `curl`ed and then ran, because the download carried no checksum/signature check.               |

### Opinionated (Tier 2, opt-in)

| Hook                      | Failure it prevents                                                                                                                                                                                                   |
| ------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `check-always-reporter`   | A gated workflow stranded a required check at “Expected—Waiting” when the decide gate skipped every work job. Assumes a **decide-job + `always()` reporter** pattern.                                                 |
| `check-required-reporter` | A new `always()` reporter shipped as a green-but-never-required check because nothing tied a workflow’s reporters to the branch-protection required-set. Assumes the required-set is mirrored from these annotations. |
| `check-inline-run-length` | A long inline `run:` block shipped unchecked (unquoted expansions, missing `pipefail`) because shellcheck/shfmt/shellharden only see standalone `.sh` files.                                                          |
| `check-concurrency`       | New pushes queued behind stale runs instead of cancelling, because a `concurrency:` block omitted `cancel-in-progress` and it silently defaulted to `false`.                                                          |

### Unrelated bonus checks (Extras)

| Hook                         | Failure it prevents                                                                                                         |
| ---------------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| `check-symlinks`             | A tracked symlink with an absolute target (`/Users/you/...`) broke on every machine but the author’s.                       |
| `check-unnamed-regex-groups` | A regex’s match handling went positional and brittle because a `re.*` literal used an unnamed `( )` group.                  |
| `check-global-stdio-swap`    | Concurrent calls clobbered each other’s output because code reassigned the process-global `sys.stdout` to capture I/O.      |
| `check-claude-model`         | A `claude-code-action` step billed Opus silently because it omitted `--model` and rode the action’s expensive default tier. |

## Usage

These are [pre-commit](https://pre-commit.com) hooks. Install pre-commit and
enable its git hook:

```bash
pipx install pre-commit # or: pip install pre-commit / brew install pre-commit
pre-commit install
```

Then add ci-truth-serum to your `.pre-commit-config.yaml`. Tier 1 (honesty +
identity) is enabled below; Tier 2 and Extras are commented out: uncomment what
you want. pre-commit builds each hook’s isolated Python environment, so it is
the only prerequisite.

```yaml
repos:
  - repo: https://github.com/alexander-turner/ci-truth-serum
    rev: v0.1.0 # pin to a tag
    hooks:
      # ── Tier 1 · Honesty (default-on) ──
      - id: check-workflow-pipefail
      - id: check-exit-suppression
      - id: check-stderr-suppression
      - id: check-pr-paths
      # ── Tier 1 · Identity (default-on) ──
      - id: check-pinned-base-images
      - id: check-pinned-downloads
      # ── Tier 2 · Opinionated (opt-in: uncomment to enable) ──
      # - id: check-always-reporter      # assumes a decide-job + always() reporter
      # - id: check-required-reporter    # classify each always() reporter required-check: true|false
      # - id: check-inline-run-length
      # - id: check-concurrency
      # ── Extras · Unrelated bonus checks (opt-in) ──
      # - id: check-symlinks
      # - id: check-unnamed-regex-groups
      # - id: check-global-stdio-swap
      # - id: check-claude-model         # require an explicit --model on claude-code-action steps
```

`pre-commit run --all-files` sweeps the whole repo (handy on first adoption).

### Enable a whole tier with one id

Instead of adding a new `- id:` every time a check ships, enable a tier
aggregate: one id runs every Python check in that tier, and checks added later
are picked up with **no change to your config**:

```yaml
repos:
  - repo: https://github.com/alexander-turner/ci-truth-serum
    rev: v0.1.0 # pin to a tag
    hooks:
      - id: check-tier1 # all honesty + identity checks (the safe default-on set)
      # - id: check-tier2   # all opinionated checks: assumes the decide-gate + reporter architecture
      # - id: check-extras  # the Python extras (vendor-/style-specific)
```

`check-symlinks` is the only check not in an aggregate: it is a shell
(`language: script`) hook, not a Python module, so add its `- id:` separately if
you want it. Mixing an aggregate with individual ids is fine (a check just runs
twice).

### Scope one check to specific paths

When one check in a tier needs tighter file scoping than the rest (e.g.,
`check-exit-suppression` is too strict for your `tests/` directory), use
`--skip <module_name>` to drop it from the aggregate, then re-add it as a
standalone hook with normal pre-commit `files:`/`exclude:` filters:

```yaml
- repo: https://github.com/alexander-turner/ci-truth-serum
  rev: v0.1.0
  hooks:
    - id: check-tier1
      args: [--skip, check_exit_suppression] # drop from aggregate...
    - id: check-exit-suppression # ...then re-add with scoped filters
      files: '^(bin/|setup\.bash$|\.devcontainer/|\.claude/hooks/)'
      exclude: "^bin/(bench-|check-)"
```

`--skip` is repeatable: pass one `--skip <name>` pair per check to drop.
**An unknown name is a hard error** (to catch typos that would silently
re-include the check). Module names use underscores and match the TIERS
registry in `hooks/run_tier.py` (e.g., `check_exit_suppression`, not
`check-exit-suppression`).

The key property is preserved: any new check added to the tier upstream still
flows in automatically via the aggregate: you only opt out of the ones you
deliberately scope.

### Autofix (opt-in): digest-pin base images

`check-pinned-base-images` can rewrite what it finds: pass `--fix` and it
resolves each unpinned `FROM`’s current registry digest and appends it
(`FROM node:22` → `FROM node:22@sha256:…`), preserving `--platform` flags and
`AS <stage>` suffixes. It is opt-in because `--fix` is the pack’s only network
call (a Docker Registry v2 manifest lookup); detection stays offline, and an
image whose digest can’t be resolved is left untouched: never guessed.

```yaml
- id: check-pinned-base-images
  args: [--fix]
```

### Apply: mirror branch protection from the annotations

`check-required-reporter` lints locally; `sync-required-checks` applies. It reads
every job marked `# required-check: true` (any job, not just `always()`
reporters), expands each `name:` across its `strategy.matrix` into concrete check
contexts, and rewrites the repo’s branch-protection ruleset so
`required_status_checks` matches that set exactly. The annotations become the
single source of truth, so the required-set stops drifting in the GitHub UI.

```bash
pip install ci-truth-serum

# Report drift and exit non-zero WITHOUT mutating (PR-safe gate):
sync-required-checks --repo owner/name --check

# Rewrite the ruleset to match the annotations:
GH_TOKEN=<token-with-administration:write> sync-required-checks --repo owner/name
```

The mutation path needs a token (`GH_TOKEN` / `GITHUB_TOKEN`) with
`administration: write`; it reads the marker from the same scoped lines the lint
classifies, so the gate and the apply step can never disagree. Pass
`--ruleset-id` if the repo has more than one branch ruleset.

## Complements, doesn’t replace

ci-truth-serum enforces policy gaps; keep running the tools it doesn’t
duplicate: [`zizmor`](https://github.com/woodruffw/zizmor) to SHA-pin `uses:`
references, [`hadolint`](https://github.com/hadolint/hadolint) for Dockerfiles
(`check-pinned-base-images` is stronger: it demands a `@sha256:` digest, not just
an explicit tag), [`actionlint`](https://github.com/rhysd/actionlint) for
workflow syntax/types, and `shellcheck` for shell.
