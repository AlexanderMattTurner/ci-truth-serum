- **BREAKING**: the installed Python package is now `ci_truth_serum` (was the
  generic top-level name `hooks`, which any consumer repo with its own `hooks/`
  directory on `sys.path` could shadow, breaking every hook). Hook ids are
  unchanged — consumers only bump `rev:`; anything importing the package or
  running `python -m hooks.check_*` directly must switch to
  `python -m ci_truth_serum.check_*`.
