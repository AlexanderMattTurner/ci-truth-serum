- Every opt-out/annotation token is now matched by one shared comment-scoped
  matcher: the token must sit inside a real comment (`#`, `<!-- -->`, `//`)
  and — for every hook whose contract documents `<token>: <reason>` — carry a
  non-empty reason. A token smuggled into live data (a `group: "<token>"`
  string value, a printed message) no longer silently disables a lint, and a
  bare reason-less marker no longer suppresses hooks that demand a stated
  reason.
- `check-pinned-downloads` recognizes an output flag inside a short-flag
  cluster: `wget -qO- url | jq` is a stdout API read (previously flagged as an
  unverified artifact download), while `wget -qO file` / `curl -fsSLO` remain
  artifacts and `wget -qO- url | sh` still fires; a shell redirect to a file
  now wins over a stdout sink flag (`wget -qO- url > tool` is an artifact).
- `check-drift-guards`' non-Python phrase pass only scans TEST files
  (`tests/`-like directories, `test_*`, `*.test.*`/`*.spec.*`) — production
  scripts' own "keeps X in sync" comments no longer false-positive.
- `check-flag-arity` tracks the arity a guard actually PROVES as a number: a
  `[[ $# -ge 2 ]] || die` no longer clears an arm that reads `$3`/`shift 3`,
  a defaulting `${2:-x}` read no longer excuses a following `shift 2`, and a
  `shift N` lowers the proven bound. Abort-helper names are resolved against
  the file: a locally defined `fail() { echo oops; }` that does not exit no
  longer counts as a bailing guard (undefined conventional names are still
  trusted).
- `check-static-concurrency` / `check-cancellable-required-check`: a per-ref
  concurrency key counts only INSIDE a `${{ … }}` expression — a group string
  merely containing the literal text `github.ref` is static.
