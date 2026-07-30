- This repo's own CI now pins the version it installs: the pre-commit workflow
  installs `pre-commit==4.6.1`, and `setup.sh` bootstraps pnpm at the version
  `package.json`'s `packageManager` field already declares.
