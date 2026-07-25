- `check-trusted-base` now also reports a privileged `pull_request(_target)` job
  that checks out an author-chosen **BASE** ref (`github.event.pull_request.base.ref`
  / `.sha`, `github.base_ref`), not only the PR head. A PR's base is a branch the
  author picks: push branch `A` carrying a rewritten build/release script, open PR
  `B` *based on* `A`, and a job that stages "the base branch" stages and executes
  `A` with the org credentials live — while PR `B`'s own diff stays clean, so a
  "does this PR edit the machinery?" refusal never sees it. Narrowing the base to
  `main || master` is not a fix: on a repo whose default branch is `main`,
  `master` is an ordinary pushable branch name. Re-derive the default branch from
  `$GITHUB_EVENT_PATH` and refuse on every negative path instead. Scoped to a base
  ref passed to `actions/checkout` — a base context used as *data* (a diff range,
  a version baseline) never materializes a tree and is not reported. Head-ref
  findings are unchanged, message included.
