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

| Hook                              | Failure it prevents                                                                                                                                                                                                                                                                                                                                                                                                                         |
| --------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `check-workflow-pipefail`         | CI went green while `pytest` was crashing, because `pytest \| tee log` exits with `tee`’s status—under a `runCmd:` / `shell: sh` / custom `bash` that lacks `pipefail`.                                                                                                                                                                                                                                                                     |
| `check-exit-suppression`          | A teardown that left a volume pinned reported success, because `cleanup \|\| true` discarded its non-zero exit while keeping its output.                                                                                                                                                                                                                                                                                                    |
| `check-stderr-suppression`        | A container launch failed with a bare non-zero and no clue why, because `docker compose up 2>/dev/null` threw away the only diagnostic.                                                                                                                                                                                                                                                                                                     |
| `check-substitution-exit-swallow` | An allowlist-building loop reported success while adding nothing, because `done < <(jq …)` (or `jq … \| while read`) discards `jq`/`yq`'s exit status—a renamed key or malformed input makes the producer exit non-zero, the loop iterates zero times, and the fail-open goes unnoticed. Curated to `jq`/`yq` (structured-data extractors whose non-zero exit is a fail-closed signal); opt out with `# allow-substitution-exit: <reason>`. |
| `check-pr-paths`                  | A required check hung at “Expected—Waiting” forever and the PR could never merge, because `paths:`/`paths-ignore:`/`branches:` on `pull_request` skipped the workflow without reporting (a stacked PR on a non-main base is the branch-filter trap).                                                                                                                                                                                        |
| `check-pipefail-grep-pipe`        | A teardown check reported a still-present secret as removed, because `secret_store ls \| grep -q "$name"` under `pipefail` let grep’s early exit SIGPIPE the producer, surfacing 141 as no-match once the listing outgrew the pipe buffer.                                                                                                                                                                                                  |

### Identity (Tier 1, default-on)

| Hook                        | Failure it prevents                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| --------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `check-pinned-base-images`  | The base image you reviewed and the one CI built diverged, because `FROM node:22` is a mutable tag the registry can re-point. **Demands a `@sha256:` digest.**                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| `check-pinned-downloads`    | A tampered release or compromised mirror swapped the binary you `curl`ed and then ran, because the download carried no checksum/signature check.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| `check-provenance-repo-url` | A fork's very first release died at `npm publish --provenance` with `E422 … Failed to validate repository information`, because `package.json`'s `repository.url` still named the upstream it was forked from (it killed the first releases of three sibling repos). Compares `package.json` `repository`/`repository.url` and `pyproject.toml` `[project.urls]` repository-ish keys (never `Homepage`) against the local `origin` remote, normalized; also fails a workflow that runs `npm/pnpm publish` with no `repository.url` at all. No opt-out for a mismatch—forks must repoint their self-referential URLs. A repo with no origin remote is skipped. |

### Opinionated (Tier 2, opt-in)

| Hook                              | Failure it prevents                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| --------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `check-always-reporter`           | A gated workflow stranded a required check at “Expected—Waiting” when the decide gate skipped every work job. Assumes a **decide-job + `always()` reporter** pattern.                                                                                                                                                                                                                                                                                                                                                                |
| `check-required-reporter`         | A new `always()` reporter shipped as a green-but-never-required check because nothing tied a workflow’s reporters to the branch-protection required-set. Assumes the required-set is mirrored from these annotations.                                                                                                                                                                                                                                                                                                                |
| `check-inline-run-length`         | A long inline `run:` block shipped unchecked (unquoted expansions, missing `pipefail`) because shellcheck/shfmt/shellharden only see standalone `.sh` files.                                                                                                                                                                                                                                                                                                                                                                         |
| `check-concurrency`               | New pushes queued behind stale runs instead of cancelling, because a `concurrency:` block omitted `cancel-in-progress` and it silently defaulted to `false`.                                                                                                                                                                                                                                                                                                                                                                         |
| `check-static-concurrency`        | A required check hung at “Expected—Waiting” forever, because a static workflow-level `concurrency.group` (no `github.ref`/`head_ref` key) let a sibling ref’s run cancel this one’s pending run wholesale before any job—and its `always()` reporter—ever started.                                                                                                                                                                                                                                                                   |
| `check-requires-concurrency`      | A `pull_request(_target)` workflow shipped with **no** `concurrency:` block at all, so every push to a PR started a second full run instead of cancelling the superseded one—stacking runs on a capped, shared runner pool. (`check-concurrency` only validates a block that exists; this one requires it to exist.) Satisfied by a block at the workflow level **or** on any job. Opt out with `# concurrency-not-required`.                                                                                                        |
| `check-externalized-markers`      | A workflow guard that scans inline `run:` for a policy marker (e.g. a history-rewrite command that demands `fetch-depth: 0`) went blind and passed vacuously the moment that command moved into `.github/scripts/*.sh` or a composite action. Flags any job where the marker is reachable only through that indirection.                                                                                                                                                                                                             |
| `check-path-gate-deps`            | A gated job silently skipped—and its `always()` reporter went green—on the exact PR that changed a file the job depends on, because the decide job's path filters omitted a composite action or `.github/scripts/` helper. Verifies every gated job's static dependencies (composites, run scripts one `source` hop deep, and `# gate-deps:`-declared paths) are covered by the decide filters; suppress one dep with `# path-gate-ok: <dep> <reason>`.                                                                              |
| `check-failure-notifier-coverage` | A new push/schedule workflow failed silently forever because `ci-failure-notify.yaml`'s `on.workflow_run.workflows` list (necessarily a hand-copied list—`workflow_run` has no wildcard) was never updated. Round-trip freshness check: the list must equal the tree's push/schedule workflow names; prints the corrected block on mismatch. Pass `--require-notifier` to fail when the notifier workflow itself is missing.                                                                                                         |
| `check-token-fallback`            | A tag push started 403'ing and a retrying version-bump loop walked an npm package from 1.x to 5.x, because `token: ${{ secrets.PAT \|\| secrets.GITHUB_TOKEN }}` silently switched push identity the day someone set the first secret. Flags any `secrets.A \|\| secrets.B` fallback in a token position (a `token:`/`github-token:` input, or a `GITHUB_TOKEN`/`GH_TOKEN` env var); opt out with `# token-fallback-ok: <reason>` when the switch is designed.                                                                       |
| `check-workflow-secret-names`     | A workflow read `secrets.ANTHROPIC_API_KEY` while the configured secret was `GH_ACTION_ANTHROPIC_API_KEY`, and changelog drafting silently degraded for a week (a misspelled secret just evaluates empty); three renames of a release token each surfaced only at runtime. Round-trip contract: every `secrets.*`/`vars.*` name referenced under `.github/` must equal the checked-in `.github/workflow-secrets.txt` allowlist exactly (both directions; `GITHUB_TOKEN` is implicit). Prints the corrected file content on mismatch. |
| `check-pin-comment-truth`         | The same `actions/checkout@<sha>` carried `# v6` on one line and `# v7.0.0` on twelve others—at most one comment could be true, and the comment is the only part of a SHA pin a reviewer can read. Offline rules: every SHA-pinned `uses:` needs a wellformed `# v<digits>[.<digits>[.<digits>]]` comment (trailing text allowed), and one SHA must carry ONE comment string repo-wide. No network resolution; opt out with `# pin-comment-ok`.                                                                                      |
| `check-stderr-merge-parse`        | An npm stderr warning merged via `2>&1` became "the version" and every release aborted on the nonsense comparison. Flags a substitution that merges and pipes into a parser (`v=$(cmd 2>&1 \| tail -1)`), and a merged capture later piped into head/tail/grep/awk/cut/sed/jq/sort/wc or used in a `[[ … ]]`/`(( … ))` comparison. `out=$(cmd 2>&1)` followed only by echo/printf is never flagged—capture-for-diagnostics is the dominant legit use. Opt out with `# stderr-merge-ok: <reason>`.                                    |
| `check-echo-fallback`             | A release-version decision was fed the literal strings `error` and `Unable to get diff`, because `$(cmd \|\| echo "…")` converted each failure into a benign parseable value. Flags `\|\| echo`/`\|\| printf` inside command substitutions and as unaborted bare statements; a fallback that redirects to stderr or aborts (`\|\| { echo … >&2; exit 1; }`) is a real recovery and passes. Opt out with `# echo-fallback-ok: <reason>`.                                                                                              |
| `check-lockstep-pins`             | A repo's `.pre-commit-config.yaml` ci-truth-serum `rev:` and a workflow's `pip install git+…@<sha>` pin were hand-bumped three times in one week, each time with a "keep in lockstep" note that enforced nothing. Config-driven: each repeatable `--pair FILE1 REGEX1 FILE2 REGEX2` (one capture group each) must match exactly once per file and the captures must be equal—zero or multiple matches is a hard error. Not in the `check-tier2` aggregate (it needs per-repo args); enable it standalone.                            |

### Unrelated bonus checks (Extras)

| Hook                         | Failure it prevents                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| ---------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `check-symlinks`             | A tracked symlink with an absolute target (`/Users/you/...`) broke on every machine but the author’s.                                                                                                                                                                                                                                                                                                                                                                                                    |
| `check-unnamed-regex-groups` | A regex’s match handling went positional and brittle because a `re.*` literal used an unnamed `( )` group.                                                                                                                                                                                                                                                                                                                                                                                               |
| `check-global-stdio-swap`    | Concurrent calls clobbered each other’s output because code reassigned the process-global `sys.stdout` to capture I/O.                                                                                                                                                                                                                                                                                                                                                                                   |
| `check-claude-model`         | A `claude-code-action` step billed Opus silently because it omitted `--model` and rode the action’s expensive default tier.                                                                                                                                                                                                                                                                                                                                                                              |
| `check-drift-guards`         | A copies-agree ("drift guard") test shipped with no stated reason why a single source of truth is infeasible—the duplication it polices kept drifting anyway. Requires `@pytest.mark.drift_guard("<why no SSOT is feasible>")` on any test whose name/docstring reads as a drift guard, so the judgement is reviewed, not implied.                                                                                                                                                                       |
| `check-graceful-handwave`    | A doc or comment claimed the code "fails gracefully"—a guarantee that specifies nothing (which input? which exit code?)—and nobody could tell whether the behaviour was real or wished-for. Scans prose (Markdown/RST) line-by-line and code comment-only; opt out by stating the behaviour: `allow-graceful: <what actually happens>`. Pass `--prose` to scan a free-standing text file (e.g. a PR body) line-by-line.                                                                                  |
| `check-historical-comments`  | A comment narrating the past ("renamed from X", "now uses Y") rotted into a lie the moment the code moved—the reader can't see the old code, so the note was unverifiable from day one. Bans only tokens with no present-tense reading; opt out (e.g. a reader of a legacy on-disk format) with `# allow-history: <reason>`.                                                                                                                                                                             |
| `check-doc-line-refs`        | A doc cited source by exact line number and pointed at whatever now happens to live there after the next refactor. Bans `<file>.<ext>:<N>` and `(L<N>)`-style cites in Markdown (fenced code blocks and any `CHANGELOG.md` are skipped); cite a function/section/anchor instead, or suppress with `<!-- allow-line-ref: <reason> -->`.                                                                                                                                                                   |
| `check-flag-arity`           | A CLI parser died with a raw `$2: unbound variable` instead of a clean "--branch needs a value", because a `--branch) X="$2"; shift 2` arm trusted the loop's outer `$# -gt 0` (which proves only `$1`) and the flag was passed last. Flags any `case` arm whose label is a `-x`/`--xxx`/`--xxx=*` option that reads `$2`/`shift 2` without its own guard; satisfied by `[[ $# -ge 2 ]] \|\| die`, a self-guarding `${2:?…}`, or a `need_val`/`need_arg` helper. Suppress with `# flag-arity-ok: <why>`. |
| `check-case-default`         | An unexpected bump-type value matched no `case` arm, left `NEW_VERSION` unset, and the release script continued on garbage. Requires a bare `*)` (or `(*)` / `* )` / a `x\|*)` alternative) default arm on every `case … esac`—globs like `*.txt)` or `--*)` don't count. One-liners and quoted `case` text are parsed conservatively (fail open). Opt out with `# case-default-ok: <reason>` on the `case` line.                                                                                        |
| `check-cron-comment`         | In two repos a schedule's header comment said "daily" while the cron was weekly, and the job silently ran 1/7th as often as everyone believed. Pairs a cadence claim (`hourly`/`daily`/`weekly`/`monthly`/`every N minutes\|hours\|days`) in a comment on or within 3 lines above a `cron:` line with the expression's shape, and fails only on a clean contradiction—lists, ranges, and exotic crons are unclassifiable and always pass. Opt out with `# cron-comment-ok`.                              |
| `check-toolchain-skips`      | `skipif(shutil.which("node") is None)` silently zeroed the coverage of every guarded script on a runner missing the tool, and the suite stayed green. Flags `pytest.mark.skipif`/`pytest.importorskip` conditions that do binary discovery (`shutil.which`, `which(`, `find_executable`) with no CI env guard—the skip must FAIL in CI: `shutil.which("node") is None and not os.environ.get("CI")`. Only test files are scanned. Opt out with `# toolchain-skip-ok: <reason>`.                          |

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
  - repo: https://github.com/AlexanderMattTurner/ci-truth-serum
    rev: v0.1.0 # pin to a tag
    hooks:
      # ── Tier 1 · Honesty (default-on) ──
      - id: check-workflow-pipefail
      - id: check-exit-suppression
      - id: check-stderr-suppression
      - id: check-substitution-exit-swallow
      - id: check-pr-paths
      - id: check-pipefail-grep-pipe
      # ── Tier 1 · Identity (default-on) ──
      - id: check-pinned-base-images
      - id: check-pinned-downloads
      - id: check-provenance-repo-url
      # ── Tier 2 · Opinionated (opt-in: uncomment to enable) ──
      # - id: check-always-reporter      # assumes a decide-job + always() reporter
      # - id: check-required-reporter    # classify each always() reporter required-check: true|false
      # - id: check-inline-run-length
      # - id: check-concurrency
      # - id: check-static-concurrency   # static workflow-level concurrency.group on a required check
      # - id: check-requires-concurrency  # every pull_request workflow must declare a concurrency block
      # - id: check-externalized-markers  # marker reachable only via script/composite indirection
      # - id: check-path-gate-deps       # decide filters must cover every gated-job dependency
      # - id: check-failure-notifier-coverage  # keep ci-failure-notify's workflow_run list fresh
      # - id: check-token-fallback       # no secrets.A || secrets.B fallbacks in token positions
      # - id: check-workflow-secret-names  # referenced secrets/vars == .github/workflow-secrets.txt
      # - id: check-pin-comment-truth    # `# vX.Y` comments on SHA pins: present + consistent
      # - id: check-stderr-merge-parse   # never parse a 2>&1-merged stream
      # - id: check-echo-fallback        # no `|| echo` fallbacks that fake a value
      # - id: check-lockstep-pins        # config-driven twin-pin equality (needs --pair args)
      # ── Extras · Unrelated bonus checks (opt-in) ──
      # - id: check-symlinks
      # - id: check-unnamed-regex-groups
      # - id: check-global-stdio-swap
      # - id: check-claude-model         # require an explicit --model on claude-code-action steps
      # - id: check-drift-guards         # copies-agree tests must justify why no SSOT is feasible
      # - id: check-graceful-handwave    # "graceful" hand-waves must state the concrete behaviour
      # - id: check-historical-comments  # comments describe the present code, not its past
      # - id: check-doc-line-refs        # docs cite symbols/sections, not line numbers
      # - id: check-flag-arity           # value-taking CLI flag arms must guard $2 before reading it
      # - id: check-case-default         # every shell case block needs a bare *) default arm
      # - id: check-cron-comment         # schedule comments must not contradict their cron
      # - id: check-toolchain-skips      # which()-gated pytest skips must fail (not skip) in CI
```

`pre-commit run --all-files` sweeps the whole repo (handy on first adoption).

### Enable a whole tier with one id

Instead of adding a new `- id:` every time a check ships, enable a tier
aggregate: one id runs every Python check in that tier, and checks added later
are picked up with **no change to your config**:

```yaml
repos:
  - repo: https://github.com/AlexanderMattTurner/ci-truth-serum
    rev: v0.1.0 # pin to a tag
    hooks:
      - id: check-tier1 # all honesty + identity checks (the safe default-on set)
      # - id: check-tier2   # all opinionated checks: assumes the decide-gate + reporter architecture
      # - id: check-extras  # the Python extras (vendor-/style-specific)
```

Two checks are not in any aggregate—add their `- id:` separately if you want
them: `check-symlinks` is a shell (`language: script`) hook, not a Python
module, and `check-lockstep-pins` is config-driven (it hard-errors without the
per-repo `--pair` args an aggregate cannot supply). Mixing an aggregate with individual ids is fine (a check just runs
twice).

### Scope one check to specific paths

When one check in a tier needs tighter file scoping than the rest (e.g.,
`check-exit-suppression` is too strict for your `tests/` directory), use
`--skip <module_name>` to drop it from the aggregate, then re-add it as a
standalone hook with normal pre-commit `files:`/`exclude:` filters:

```yaml
- repo: https://github.com/AlexanderMattTurner/ci-truth-serum
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

### Config: enforce twin pins with check-lockstep-pins

`check-lockstep-pins` replaces "keep these in lockstep" comments with a gate.
The motivating pair—a `.pre-commit-config.yaml` `rev:` and a workflow's
`pip install git+…@` pin of the same release:

```yaml
- id: check-lockstep-pins
  args:
    - --pair
    - .pre-commit-config.yaml
    - 'ci-truth-serum\s+rev:\s*(\S+)'
    - .github/workflows/lint.yaml
    - 'ci-truth-serum\.git@(\S+)'
```

Each regex needs exactly one capture group and must match exactly once in its
file—zero (the pattern rotted) or several (ambiguous) is a hard error, and the
two captures must be equal. Repeat `--pair` for more pins.

### Apply: verify a release with release-canary

`release-canary` asserts the three places a release leaves its version agree:
the npm registry (semver-max of `npm view <pkg> versions --json`—deliberately
NOT `npm view <pkg> version`, which returns the `latest` dist-tag and silently
misreports when a publish set the tag wrong), the semver-max `v*` git tag, and
the changelog's top dated `## [x.y.z]` heading (`## Unreleased` is skipped).
On mismatch it prints all three labeled values and exits non-zero; the
`npm view` call is its only network touch.

```bash
pip install ci-truth-serum

release-canary                    # package name read from ./package.json
release-canary --package my-pkg --changelog CHANGELOG.md --repo-dir .
```

Run it as a post-release workflow step so a publish that died after tagging
(or a tag push that 403'd after publishing) is caught the day it happens, not
at the next release.

## Complements, doesn’t replace

ci-truth-serum enforces policy gaps; keep running the tools it doesn’t
duplicate: [`zizmor`](https://github.com/woodruffw/zizmor) to SHA-pin `uses:`
references, [`hadolint`](https://github.com/hadolint/hadolint) for Dockerfiles
(`check-pinned-base-images` is stronger: it demands a `@sha256:` digest, not just
an explicit tag), [`actionlint`](https://github.com/rhysd/actionlint) for
workflow syntax/types, and `shellcheck` for shell.
