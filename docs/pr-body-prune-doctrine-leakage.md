# refactor!: prune doctrine-leakage checks, consolidate the concurrency-cancellation lints, state inclusion criteria, fix #81

Maintainer-approved scope (follow-up to the downstream CI-guard runaway):
prune the checks that exported one repo's doctrine into the shared pack,
consolidate the redundant concurrency-cancellation pair, write down the
inclusion criteria from #79 §1, and fix #81.

Closes #81. Implements the Scope section requested in #79 §1.

## Partitions

One branch, partitioned commits — one commit per concern.

### 1. `refactor(checks)!` — remove four doctrine-leakage checks

Removed entirely (module, hook id, tier-registry entry, tests, fuzz targets,
README rows/snippet lines):

- **`check-drift-guards`** — a meta-lint policing how consumers write their
  own tests; one repo's anti-lockstep doctrine exported as a phrase heuristic.
- **`check-historical-comments`** — comment-tense prose doctrine.
- **`check-graceful-handwave`** — bans one word; its only failure mode is
  prose style, with zero user-visible-bug reach.
- **`check-workflow-secret-names`** — requires a bespoke
  `.github/workflow-secrets.txt` registry no consumer other than the
  originating repo maintains (the repo's own registry file is deleted too).

All four fail the #79 criteria now written into the README (see partition 3).
Also removed: the now-orphaned `drift_guard` pytest marker registration and
its one in-repo use, and a dangling `drift-guard-ok:` annotation. Unreleased
changelog fragments announcing the removed checks are dropped or trimmed; a
`removed`-category fragment records the change for consumers (individual-id
consumers drop the entry when bumping their pin; tier-aggregate consumers
need no config change). Explicitly untouched, per the approved scope:
`check-gh-slurp-jq`, `check-test-predicate-shadow`, `check-doc-line-refs`.

### 2. `refactor(checks)` — fold `check-cancellable-required-check` into `check-pending-cancel-concurrency`

These were the 4th and 5th lints policing the same concurrency-cancellation
decision, one per polarity of the group key (ref-keyed groups cancelled by a
same-SHA type storm; static cancellable groups cancelled by a sibling ref).
One hook now detects both as two arms of one `check_file`.
`check-pending-cancel-concurrency` survives as the id — its `#
pending-cancel-ok` annotation is the one already present in consumer trees —
and **both** opt-out tokens stay recognized (`# pending-cancel-ok` clears the
type-storm arm, `# cancellable-required-check-ok` the static-cancellable arm)
because both annotations shipped and exist in the wild; the module docstring
records why. Detection semantics of each arm are unchanged; tests are
unioned, plus a new cross-arm test proving each opt-out clears only its own
arm.

### 3. `docs(readme)` — Scope / inclusion criteria (#79 §1)

New README section near the top: the **grep test** (where repo identifiers
land) and the **remedy test** (fix routes through a repo helper vs a
language/OS primitive), the issue's two counter-examples
(never-fires-elsewhere; portable-regex-with-locally-inverted-policy, the
`--no-verify` example), the rule that a check whose only failure mode is
prose style does not belong in the pack, and the parameterized-engine shape
for manifest-reading checks.

### 4. `fix(check-cron-staleness-optout)` — self-covering watchdog (#81, Option 1)

`--require-stale-marker` narrowed its marker demand to the declared watchdog
on the premise "nothing watches the watcher". A watchdog that also fires on a
non-`schedule:` trigger (push, `workflow_run`, …) breaks that premise —
GitHub's 60-day dormancy disable applies to `schedule:` only — and a consumer
whose runtime sweep reads the same marker as an opt-out was forced to drop
the watchdog from its own watched set (the agent-glovebox case in the issue).
Now a valid watchdog whose `on:` block carries any non-`schedule:` trigger is
treated as covered and owes no marker; a schedule-only watchdog still owes
its own. Regression tests reproduce the glovebox shape (schedule+push under
`--require-stale-marker`) plus `workflow_run` / `workflow_dispatch` /
list-form triggers — verified red on the pre-fix module (4 failed), green on
the new one.

Two trailing `style:` commits carry ruff-format/prettier alignment only.

## Verification

- `uv run --extra dev pytest tests/cts -n auto -q` — **1858 passed** (baseline
  on `origin/main`: 2069 passed; the delta is the removed checks' suites).
- `pre-commit run --all-files` — all hooks pass, including the dogfood tier
  aggregates and the roster/registry contract tests
  (`test_run_tier`, `test_fuzz_coverage`), except `zizmor`, which fails only
  on a sandbox network 403 fetching the GitHub advisory DB (environmental;
  no workflow content changed beyond a comment and a deleted text file).
- `pnpm test` (node scripts suite) — fail 0.
- `node scripts/assemble-changelog.mjs --check` — 71 fragments valid.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

<https://claude.ai/code/session_0186nRoRPUrSSUuz8Rxd34MX>
