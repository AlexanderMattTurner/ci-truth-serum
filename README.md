# ci-truth-serum

**Make your CI confess what it’s hiding.** A pack of fast, offline pre-commit
lints that catch two kinds of lie a green check can hide:

- **Honesty lies** — the pipeline reports success while the real work failed
  (exit codes masked by pipes / `|| true` / `2>/dev/null`), or a required check
  silently never reports and the PR hangs forever.
- **Identity lies** — a base image or downloaded artifact is pinned to a
  _mutable_ name (a tag, a bare URL), so the bytes you run aren’t provably the
  bytes you reviewed.

## What it checks

### Honesty (Tier 1, default-on)

| Hook                       | Failure it prevents                                                                                                                                                          | Opt-out marker                          |
| -------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------- |
| `check-workflow-pipefail`  | CI went green while `pytest` was crashing, because `pytest \| tee log` exits with `tee`’s status—under a `runCmd:` / `shell: sh` / custom `bash` that lacks `pipefail`.      | `# allow-no-pipefail: <reason>`         |
| `check-exit-suppression`   | A teardown that left a volume pinned reported success, because `cleanup \|\| true` discarded its non-zero exit while keeping its output.                                     | `# allow-exit-suppress: <reason>`       |
| `check-stderr-suppression` | A container launch failed with a bare non-zero and no clue why, because `docker compose up 2>/dev/null` threw away the only diagnostic.                                      | `# allow-stderr-suppress: <reason>`     |
| `check-pr-paths`           | A required check hung at “Expected—Waiting” forever and the PR could never merge, because `paths:`/`paths-ignore:` on `pull_request` skipped the workflow without reporting. | `# not-required-check` (on the trigger) |

### Identity (Tier 1, default-on)

| Hook                       | Failure it prevents                                                                                                                                            | Opt-out marker           |
| -------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------ |
| `check-pinned-base-images` | The base image you reviewed and the one CI built diverged, because `FROM node:22` is a mutable tag the registry can re-point. **Demands a `@sha256:` digest.** | _(none—pin or don’t)_    |
| `check-pinned-downloads`   | A tampered release or compromised mirror swapped the binary you `curl`ed and then ran, because the download carried no checksum/signature check.               | `# pin-exempt: <reason>` |

### Opinionated (Tier 2, opt-in)

| Hook                      | Failure it prevents                                                                                                                                                   | Opt-out marker                      |
| ------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------- |
| `check-always-reporter`   | A gated workflow stranded a required check at “Expected—Waiting” when the decide gate skipped every work job. Assumes a **decide-job + `always()` reporter** pattern. | `# not-required-check` (on trigger) |
| `check-inline-run-length` | A long inline `run:` block shipped unchecked (unquoted expansions, missing `pipefail`) because shellcheck/shfmt/shellharden only see standalone `.sh` files.          | `# allow-long-run: <reason>`        |
| `check-concurrency`       | New pushes queued behind stale runs instead of cancelling, because a `concurrency:` block omitted `cancel-in-progress` and it silently defaulted to `false`.          | `# cancel-in-progress-not-required` |

### Unrelated bonus checks (Extras)

| Hook                         | Failure it prevents                                                                                                    | Opt-out marker                 |
| ---------------------------- | ---------------------------------------------------------------------------------------------------------------------- | ------------------------------ |
| `check-symlinks`             | A tracked symlink with an absolute target (`/Users/you/...`) broke on every machine but the author’s.                  | _(none)_                       |
| `check-unnamed-regex-groups` | A regex’s match handling went positional and brittle because a `re.*` literal used an unnamed `( )` group.             | _(use `(?P<name>...)`)_        |
| `check-global-stdio-swap`    | Concurrent calls clobbered each other’s output because code reassigned the process-global `sys.stdout` to capture I/O. | `# allow-stdio-swap: <reason>` |

## Usage

These are [pre-commit](https://pre-commit.com) hooks. Install pre-commit and
enable its git hook:

```bash
pipx install pre-commit # or: pip install pre-commit / brew install pre-commit
pre-commit install
```

Then add ci-truth-serum to your `.pre-commit-config.yaml`. Tier 1 (honesty +
identity) is shown enabled; Tier 2 and Extras are commented in—uncomment what
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
      # - id: check-inline-run-length
      # - id: check-concurrency
      # ── Extras · Unrelated bonus checks (opt-in) ──
      # - id: check-symlinks
      # - id: check-unnamed-regex-groups
      # - id: check-global-stdio-swap
```

`pre-commit run --all-files` sweeps the whole repo (handy on first adoption).

### Autofix (opt-in): digest-pin base images

`check-pinned-base-images` can rewrite what it finds: pass `--fix` and it
resolves each unpinned `FROM`’s current registry digest and appends it
(`FROM node:22` → `FROM node:22@sha256:…`), preserving `--platform` flags and
`AS <stage>` suffixes. It is opt-in because `--fix` is the pack’s only network
call (a Docker Registry v2 manifest lookup); detection stays offline, and an
image whose digest can’t be resolved is left untouched—never guessed.

```yaml
- id: check-pinned-base-images
  args: [--fix]
```

## Complements, doesn’t replace

ci-truth-serum enforces policy gaps; keep running the tools it doesn’t
duplicate: [`zizmor`](https://github.com/woodruffw/zizmor) to SHA-pin `uses:`
references, [`hadolint`](https://github.com/hadolint/hadolint) for Dockerfiles
(`check-pinned-base-images` is stronger—it demands a `@sha256:` digest, not just
an explicit tag), [`actionlint`](https://github.com/rhysd/actionlint) for
workflow syntax/types, and `shellcheck` for shell.
