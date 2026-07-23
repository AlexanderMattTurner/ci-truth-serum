"""ci-truth-serum: fast, offline pre-commit lints that make a CI pipeline confess
what it's hiding. The individual lints are importable/runnable as
``ci_truth_serum.check_*`` (``python -m ci_truth_serum.check_foo`` or ``python3
ci_truth_serum/check_foo.py <files...>``); ``ci_truth_serum._linecheck`` holds the
machinery they share."""
