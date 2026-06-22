- `check-pinned-base-images` gained an opt-in `--fix` mode: it resolves each
  unpinned `FROM`'s current registry digest and rewrites the line in place
  (`FROM node:22` → `FROM node:22@sha256:…`), preserving `--platform` flags and
  `AS <stage>` suffixes. `--fix` is the lint's only network path (a Docker
  Registry v2 manifest lookup); detection stays offline. Images whose digest
  can't be resolved are left untouched and still reported.
