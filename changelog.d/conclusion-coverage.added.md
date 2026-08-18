- `check-conclusion-coverage`: one repository, one set of run conclusions that
  mean red. A completed workflow run also ends `timed_out`, `startup_failure` or
  `action_required`, and a consumer that recognizes only `failure` lets those
  runs reach nobody while nothing goes red. The check reads three surfaces
  through their own parsers — a GitHub Actions expression, a shell `[[ ]]` or
  `case` test, and a Python comparison or membership test — and names the
  conclusions each site left out, so the workflow YAML, the shell and the Python
  cannot narrow the set one at a time. An `if`/`elif` chain is one decision, and
  a set of conclusions bound to a module constant is resolved, so a complete
  classifier is not reported as a partial one. A site is judged only when it
  recognizes `failure`, the conclusion that means red with no further cause; a
  test for one specific cause, for `success`, or for `cancelled` alone asks a
  different question and passes. `cancelled` is never required, because a run
  that a newer push supersedes is not a failure. A repository that treats more
  conclusions as red declares them once in `.github/conclusion-coverage.yml`
  (`extra: [stale]`), and every consumer is then held to the widened set. Opt
  out with `# allow-conclusion-subset: <reason>`.
