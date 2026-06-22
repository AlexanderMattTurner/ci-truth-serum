"""ci-truth-serum: fast, offline pre-commit lints that make a CI pipeline confess
what it's hiding. The individual lints are importable/runnable as
``hooks.check_*`` (``python -m hooks.check_foo`` or ``python3 hooks/check_foo.py
<files...>``); ``hooks._linecheck`` holds the machinery they share."""
