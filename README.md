**Make your CI confess what it’s hiding.** A green check should mean the work actually passed, and a pinned dependency
should be the exact bytes you reviewed. Two kinds of lie break that:

- **Honesty lies**—the pipeline reports success while the real work failed
  (exit codes masked by pipes / `|| true` / `2>/dev/null`), or a required status
  check silently never reports at all and the PR hangs forever.
- **Identity lies**—a base image or downloaded artifact is pinned to a
  _mutable_ name (a tag, a bare URL), so the bytes you run are not provably the
  bytes you reviewed; the reference stays the same while the content drifts.

`ci-truth-serum` offers a pack of fast, offline pre-commit lints that catch both.

## Why this didn’t exist before

The GitHub Actions tooling ecosystem stays in correctness/security lanes on
purpose. [`actionlint`](https://github.com/rhysd/actionlint) checks workflow
syntax and expression types; [`zizmor`](https://github.com/woodruffw/zizmor)
audits for security smells; [`hadolint`](https://github.com/hadolint/hadolint)
lints Dockerfiles; `shellcheck` lints shell. None of them enforce the _policy_
gaps these tools leave open—and those gaps span YAML **and** bash
**and** Dockerfile at once, fitting no single tool’s file-type scope.

For example, a path filter on a `pull_request` trigger
only strands a check when a repo combines branch protection **AND** path filters
**AND** required checks. That intersection is common enough to keep biting, but
narrow enough that demand never crossed the threshold for anyone to package the fix.
So here it is.

## What `ci-truth-serum` checks

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

## Complements, doesn’t replace

You should also run:

- **Action pinning** → use [`zizmor`](https://github.com/woodruffw/zizmor)’s
  `unpinned-uses` audit to SHA-pin `uses:` references. (This pack deliberately
  ships no action-pinning lint—that would just be noise on top of zizmor.)
- **Dockerfile lint** → use [`hadolint`](https://github.com/hadolint/hadolint).
  Our `check-pinned-base-images` is **strictly stronger** than hadolint’s
  `DL3006`/`DL3007`: those are satisfied by _any_ explicit tag, so `node:22.3.0`
  passes hadolint yet is still mutable. Only a `@sha256:` digest is immutable, and
  that is what this lint demands.
- **Workflow syntax/types** → use [`actionlint`](https://github.com/rhysd/actionlint).
- **Shell** → use `shellcheck` (the `check-inline-run-length` lint exists to make
  inline shell _reachable_ by `shellcheck` in the first place).

## Usage

Add to your `.pre-commit-config.yaml`. Tier 1 (honesty + identity) is shown
enabled; Tier 2 and Extras are commented in—uncomment only what you want.

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

You can also run standalone:

```bash
python3 hooks/check_pinned_base_images.py path/to/Dockerfile
python -m hooks.check_pr_paths            # globs ./.github/{workflows,actions}
```

### Autofix (opt-in): digest-pin base images

`check-pinned-base-images` can rewrite the violations it finds. Pass `--fix` and
it resolves each unpinned `FROM`’s current registry digest and appends it
(`FROM node:22` → `FROM node:22@sha256:…`), preserving any `--platform` flag and
`AS <stage>` suffix. It is **opt-in** because `--fix` is the one place this pack
touches the network (a Docker Registry v2 manifest lookup against Docker Hub,
ghcr.io, and the like); detection stays fully offline. An image whose digest
can’t be resolved is left untouched and still reported—the fix never guesses.

```yaml
- id: check-pinned-base-images
  args: [--fix] # online: pin to the digest the registry serves now
```

Like other autofixing hooks, it exits non-zero when it rewrites a file so
pre-commit stops for you to review and re-stage the change.
