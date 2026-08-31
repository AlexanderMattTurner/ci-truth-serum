# Contributing

## What this pack is for

A green check does not always mean the work passed. Every check here finds a place where CI reports success over a real failure, or where you cannot prove what you ran. A check earns its place when it fires in a repository whose maintainers this project has never met.

## Does my check belong here?

Three tests decide. Run all three. A check that fails any one of them belongs in your own repository, not in this pack.

### 1. The grep test

Grep the finished check for the names of your repository: the project name, your helper scripts, your directory layout, your job names. Then look at **where** the hits land.

- No hits, or hits only in the docstring and the remedy string: the check travels.
- A repository name inside the predicate that decides a **violation**: keep the check local.

### 2. The remedy test

Read the fix the check asks for. A fix that says "call our helper" is local. A fix that says "call a language or operating-system primitive" travels.

The sharpest pair: a check that bans `flock <fixed-fd>` travels, because the defect is a property of `flock` itself. A check that demands you call your own `with_lock` wrapper does not travel. Same primitive, opposite verdicts.

### 3. The manifest test

Some checks read a manifest the repository writes, and assert that each entry has a consumer. Only the **engine** of such a check travels. Ship it parameterized and outside every aggregate, which is the shape `check-lockstep-pins --pair` and `check-env-symmetry --prefix` already use.

### Two counter-tests

Both of these look general and are not.

- **A check that can never fire in a stranger's repository does not belong here, however cleanly it greps.** Zero false positives plus zero findings is not a pass.
- **A portable regular expression can still carry a local policy.** Worked example: a check that requires every CI `git push` to pass `--no-verify`. That is a real defect only in a repository that points `core.hooksPath` at a developer-only hook directory. Almost everywhere else the rule is the opposite of correct. A portable pattern says nothing about a portable policy.

## Where a new check lands

The tier says how much of your CI architecture the check assumes. It decides whether the check can run at all.

| Tier       | Meaning                                                                  |
| ---------- | ------------------------------------------------------------------------ |
| **Tier 1** | Honesty and identity. It assumes nothing. Consumers run it by default.   |
| **Tier 2** | It prescribes an architecture, such as a decide gate and a reporter job. |
| **Extras** | Useful, and unrelated to CI truth.                                       |

The tag says what the check is **for**, such as `security` or `concurrency`. It decides whether a consumer wants the check. Tags come from a closed list in `ci_truth_serum/_cts_registry.py`. A new tag needs an entry there and a row in the README table.

## What a new check must ship

Add all of these in one commit. Several tests read them as one source of truth, so a partial change breaks CI.

1. The module, as `ci_truth_serum/check_<name>.py`.
2. A registry entry in `ci_truth_serum/_cts_registry.py`, with the tier, the file class and at least one tag.
3. A hook entry in `.pre-commit-hooks.yaml`. The hook id is the module name in kebab case.
4. A row in the README table for the tier you chose.
5. A test module, as `tests/cts/test_check_<name>.py`.
6. A changelog fragment in `changelog.d/`, when the change is user-facing.

## Rules a check must follow

- **Ask the grammar, never the text, when the question is about structure.** [`.claude/rules/shell-lint-parsing.md`](.claude/rules/shell-lint-parsing.md) is the full rule. It lists the questions that are structural, the tells that you are re-implementing a shell, and the one case where a text scan is still right.
- **Run both probes before you ship a shell check.** Feed the check its own banned idiom twice: once inside a message a command prints, and once inside a heredoc body. Neither is code a shell runs, so a finding on either is a false positive.
- **Give every check an opt-out annotation, and demand a reason.** The one exception is a rule that is never correct to break. Say so in the docstring when you take that exception.
- **Fail loudly.** A file the parser refuses must name the path and exit non-zero. A check that skips such a file silently reports a green it did not earn.
- **This pack ships no baseline file.** Every escape hatch is a per-line annotation. A check that a repository cannot adopt clean on its first day therefore costs that repository one annotation per existing site, and each annotation carries a reason somebody wrote. That cost is deliberate. A baseline that nobody pays down is the same shape of dishonest green this pack exists to catch, so a proposal for one needs the pay-down plan first.

## Reporting a check instead of writing one

An issue is welcome, and it is often the better first step. Say what the defect is, how it reached production, and why a stranger's repository would hit it too. State the false-positive rate you measured and the tree you measured it on. An honest "unmeasured elsewhere" is worth more than a confident guess.

## Working on the repository

```bash
pnpm install    # install dependencies and point git at .hooks
uv sync --frozen --all-extras --all-groups
uv run pytest tests/cts -n auto    # the checks' own suite
uv run pytest tests -n auto --ignore=tests/cts    # this repo's CI wiring
pnpm format
```

Commits use [Conventional Commits](https://www.conventionalcommits.org/). The `commit-msg` hook enforces the form.
