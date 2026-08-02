# Changelog fragments

User-facing changes are recorded here as **one small file per change**, not by
editing `CHANGELOG.md` directly. Because every fragment is its own uniquely named
file, two PRs can never edit the same lines—the changelog stops being a
merge-conflict hotspot. At release time `scripts/assemble-changelog.mjs` rolls all
pending fragments into a new `## [version]` section in `CHANGELOG.md` and deletes
them.

## Adding an entry

Create a file named `<id>.<category>.md`:

- **`<id>`**—anything unique; use your PR number (`592`) so fragments sort in
  merge order and never collide. Multiple categories in one PR → multiple files
  (`592.added.md`, `592.fixed.md`).
- **`<category>`**—one of: `added`, `changed`, `deprecated`, `removed`,
  `fixed`, `security` (the [Keep a Changelog](https://keepachangelog.com/) groups).

The file’s contents are the Markdown that will appear under that `### Category`
heading—write it as one or more `-` bullets, exactly as it should read in the
changelog:

```markdown
- `--foo` flag: does the thing, gated on the other thing.
```

Only add a fragment for a **user-facing** change (new flag/command, changed
default, altered security boundary, fixed bug a user could hit). Internal churn
(test refactors, CI plumbing, comment edits) gets none—same rule as before.

A **version pin bump gets no fragment**. This covers an action SHA pin, a `rev:` in `.pre-commit-config.yaml`, and a CI tool pin in `.github/requirements-ci.txt`. A consumer of this pack reads the checks, not the versions this repo builds them on. The release-readiness run counts pending fragments to decide whether a release is due, so a fragment for each pin bump argues for a release that ships no new behavior. Add a fragment only when the bump changes what a check does.

`pre-commit` validates fragment names and rejects empties; preview the assembled
result with `node scripts/assemble-changelog.mjs --draft`.
