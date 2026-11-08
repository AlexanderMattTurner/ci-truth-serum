"""The one YAML loader every check in this pack parses through.

PROBLEM CLASS — `yaml.safe_load` picks PyYAML's pure-Python parser, and the same
install already ships a libyaml-backed `CSafeLoader` about 12x faster. The cost
is invisible per call and compounds per file, because `run_tier` runs each
selector member in its own `python -m ci_truth_serum.check_*` subprocess: a repo
with 25 workflow members parses its whole workflow tree 25 times and shares
nothing. On a consumer with 87 workflow files the pure loader reads them in
0.809 s and the C loader in 0.068 s, and `cProfile` puts 8.98 s of one member's
9.22 s inside `yaml/composer.py`, `yaml/scanner.py` and `yaml/parser.py`.

The helpers take the same input and return the same objects as `yaml.safe_load`,
`yaml.compose` and `yaml.scan`, so a call site converts by changing the name.
They fall back to the pure-Python loader when the wheel was built without
libyaml, which is a speed difference and never a behavior one: both loaders
implement the same safe subset and raise the same `yaml.YAMLError` subclasses.

The name is `_cts_fastyaml`, not `_yaml`, because `_yaml` is PyYAML's own libyaml
binding. Every check prepends this directory to `sys.path`, so a module of that
name here would shadow the extension this one depends on.
"""

from collections.abc import Iterator
from typing import Any, TextIO

import yaml

# `CSafeLoader` exists only on a wheel built against libyaml. Resolved once at
# import so no call site has to ask.
SafeLoader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)

# The three loads below name a loader ruff's S506 cannot resolve, so it assumes
# the unsafe one. `SafeLoader` above resolves to `CSafeLoader` or `SafeLoader`
# and to nothing else — the two safe loaders, which construct no arbitrary
# object. That refusal to widen the name is what keeps `yaml.Loader` out of this
# module.


def safe_load(stream: str | bytes | TextIO) -> Any:
    """Parse one YAML document, as `yaml.safe_load` does."""
    return yaml.load(stream, Loader=SafeLoader)  # noqa: S506


def _marked_loader(stream: str | bytes | TextIO) -> type:
    """The loader whose marks index STREAM as the caller holds it.

    A check slices its own text with `mark.index`. libyaml counts that index
    after a leading byte-order mark and the Python scanner counts it before,
    so on a text that opens with one, every libyaml slice starts a character
    early. The opt-out views then blank the wrong bytes. That text takes the
    Python scanner, which is slower and agrees with the caller.
    """
    if isinstance(stream, str) and stream.startswith("\ufeff"):
        return yaml.SafeLoader
    return SafeLoader


def compose(stream: str | bytes | TextIO) -> Any:
    """Parse one YAML document into its NODE tree, as `yaml.compose` does.

    The nodes carry `start_mark.line`, `.column` and `.index`, which is what a
    check that reports `<path>:<line>:` off the source reads.
    """
    return yaml.compose(stream, Loader=_marked_loader(stream))  # noqa: S506


def scan(stream: str | bytes | TextIO) -> Iterator[yaml.Token]:
    """Tokenize one YAML document, as `yaml.scan` does.

    `strip_yaml_comments` reads the scalar tokens' `start_mark.index` and
    `end_mark.index` to find the spans where a `#` cannot open a comment.
    """
    return yaml.scan(stream, Loader=_marked_loader(stream))  # noqa: S506
