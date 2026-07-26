- New `check-unscoped-tool-grant` (Tier 1, security): flags an unscoped or inert
  file-tool rule in a Claude Code tool grant inside a workflow — `--allowedTools`
  / `--allowed-tools`, an `*ALLOWED_TOOLS` env var, or a legacy `allowed_tools:`
  input. Two classes, both established by probing the real CLI (2.1.220) headless
  against a stub API, every case against a matched denying control:
  - A file tool named with **no path** (`Read`, `Grep`, `Glob`, `Write`, `Edit`,
    `NotebookEdit`, `MultiEdit`) is a whole-tool grant applied _after_ the
    per-path working-directory check, overriding its verdict — so an `--add-dir`
    beside it is prompt suppression, not a jail. A bare grant is strictly per
    tool name: bare `Read` does not cover `Grep`, bare `Edit` does not cover
    `Write`. Opt out per half with `# allow-unscoped-read-grant: <reason>` /
    `# allow-unscoped-write-grant: <reason>` — legitimate when the same job also
    grants bare `Bash`, since a redirect writes anything regardless.
  - A path rule spelled under any name but `Read` or `Edit` is a rule **nothing
    consults**: `Write(//out.json)` _denies_ the write it appears to grant.
    `Edit(...)` is the spelling that confines Edit, Write, MultiEdit and
    NotebookEdit, and an absolute path needs two leading slashes or the rule
    anchors at the CLI's cwd. This class has **no** opt-out.
    `DISALLOWED_TOOLS` is deliberately excluded, so the lint can never push an
    author to narrow a deny list. The grant scan runs over comment-stripped source,
    so a grant merely quoted in a comment is not linted.
