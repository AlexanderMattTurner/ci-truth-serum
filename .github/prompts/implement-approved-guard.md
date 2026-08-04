# Implement an approved guard

You implement ONE guard that a human already approved. The approval is the trigger. Without it, stop and say so.

A guard proposal is written as a `## Proposed guards` entry in a pull request body: the defect class, the check's shape, its expected false-positive rate, and what it costs every future pull request. A human reads that entry and asks for the guard. This prompt starts there.

**Input**: the approved proposal, plus the defect that produced it (the pull request, the commit, or the incident).

## 1. Confirm the guard belongs in this pack

`ci-truth-serum` ships to every consumer repository. A hook here runs on trees you never see.

Keep the guard here when all three hold:

- The defect class comes from a tool every consumer uses: GitHub Actions, shell, Python, pre-commit, a package manager.
- A consumer with no knowledge of the originating repository would still want the check.
- The detector needs no per-repository configuration. A check that needs a project prefix or a file pair is config-driven, and those stay out of the tier aggregates (see `ci_truth_serum/run_tier.py`).

Otherwise the guard belongs in the repository that found the defect. Say which repository you chose and why, in one sentence, then stop if it is not this one.

## 2. Name the class, not the instance

Write one sentence: **the property that any correct file has, which the defective file broke.**

Then answer both:

- **Does a hook here already own this class?** Read `.pre-commit-hooks.yaml` and the module docstrings. If one exists and did not fire, the bug is that hook's recall gap. Widen it. Do NOT add a sibling hook — two hooks for one class drift apart, and each one's opt-out then disarms the other.
- **Can the check iterate a single source of truth instead of matching text?** A check that reads the manifest, the workflow graph, or the tier registry and asks a question of each entry covers members added later. A check that greps for one spelling covers only that spelling.

## 3. Pick the weakest mechanism that answers the question

In order. Take the first one that works:

1. **A type or a data shape** that makes the defect impossible to write.
2. **A grammar rule.** `_bash_ast` for shell, `_py_ast` for Python, `_js_ast` for JavaScript and TypeScript, `yaml` for workflows.
3. **A line or regex scan** over text.

The order tracks the false-positive rate. **A question about shell STRUCTURE always uses `_bash_ast`, never text** — is this a command or a string a command prints, one command or two, an argument or a redirect. A regex, a quote-state scanner, or `shlex` answering one of those is a partial re-implementation of bash. `.claude/rules/shell-lint-parsing.md` holds the full rule.

Read narration with `comment_body`, not the raw line: a pattern inside a string literal is a value the program builds, not a claim about the tree.

## 4. Write the check

New module: `ci_truth_serum/check_<slug>.py`.

- Module docstring states **what the check prevents** and **the measured defect it comes from**, with real numbers where you have them.
- Export `violations(text: str) -> list[int]` — the 1-based line numbers that violate.
- Drive it through `run_line_checks(argv, violations, MESSAGE)` from `_linecheck`. A check that picks a parser by path uses `run_source_checks`.
- `MESSAGE` names the remedy. A reader must learn what to change from the failure text alone.
- Opt out with `annotated_near(lines, line, OPT_OUT)` and `OPT_OUT = "allow-<slug>"`. **The reason is required.** Never write a bare `token in line` test — `annotation_re` owns token boundaries, and the meta-test in `tests/cts/test_annotation_predicates.py` bans the substring form.

**Fail closed on the artifact under test.** A workflow lint whose one argument IS the workflow reports an unparseable file as a violation. A content lint that receives pre-commit's file list skips an unreadable path, because that path was already classified as text and a read failure means it vanished. Copy the arm that matches your input, and say which in the docstring.

## 5. Register it in all four places

A missing one is silent:

| Place                                | What breaks without it                                      |
| ------------------------------------ | ----------------------------------------------------------- |
| `.pre-commit-hooks.yaml`             | No consumer can enable the hook.                            |
| `ci_truth_serum/run_tier.py` `TIERS` | The hook escapes its tier aggregate. A contract test fails. |
| The `README.md` table for that tier  | `tests/cts/test_readme_hook_coverage.py` fails.             |
| `changelog.d/<id>.added.md`          | Consumers never learn the hook exists.                      |

Scope the manifest entry with `files:` and `types:` so the hook reads only where the failure bites. A workflow lint sets `pass_filenames: false` and self-discovers.

## 6. Test it

New file `tests/cts/test_check_<slug>.py`.

- One case per accepted idiom AND one per rejected idiom. Assert the line numbers, not a boolean.
- Assert the match set is **non-empty** on the positive cases. A guard test that only asserts absence keeps passing after the code it watches is refactored away.
- Include the exact text of the original defect as a case.

## 7. Dogfood before you commit

Run the check over every file this tree holds that the hook declares it reads, and over `tests/cts/fixtures/consumer`. Build the globs from the `files:` and `types:` you wrote in step 5. A shell-only glob under a hook that also reads YAML scans the wrong half of the tree, and then reports a clean count for files it never opened.

```bash
uv run python -m ci_truth_serum.check_<slug> $(git ls-files '<globs from your files: and types:>') tests/cts/fixtures/consumer/*
uv run pre-commit run check-<slug> --all-files
```

Every hit is either a real defect you fix now, or a false positive you narrow the detector for. Report the count both before and after.

**A guard that lands with a grandfathered baseline owes a second pull request that shrinks it.** Open that one too, with the count before and after. A baseline you cannot reduce at all means the detector flags an idiom the tree uses on purpose: narrow it or drop the guard.

## 8. State the watched surface

Answer in the pull request body, one line each:

- **When this hook goes red, who sees it?** Name the surface.
- **How long does it add to a commit?** Measure it.
- **What makes this hook removable later?** A guard that catches no defect for eight weeks is demoted, then deleted.

## Scope

Implement the approved guard and nothing else. Do not add a second guard you thought of while writing this one. Write it up as a proposal instead, and let a human approve it.

## Report

**At most 250 words**, in the pull request body. Say: the class in one sentence, the mechanism you picked and the one you rejected, the dogfood counts before and after, the baseline size if any, and the watched surface.

Do not recount the steps you ran. Do not report a clean result from a check that is usually clean. Do not repeat a finding that already has its own review thread.
