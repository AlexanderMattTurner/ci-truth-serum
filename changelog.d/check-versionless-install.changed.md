- This repo's own CI now pins the version it installs: the pre-commit workflow
  installs from `.github/requirements-ci.txt` (a file Dependabot's `pip`
  ecosystem can bump, unlike a version written inline in a `run:` line), and
  `setup.sh` bootstraps pnpm at the version `package.json`'s `packageManager`
  field already declares.
