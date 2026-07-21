#!/usr/bin/env python3
"""Demand that every downloaded artifact is checksum/signature-verified.

A ``curl``/``wget`` that saves a file to disk and is then run, installed, or
extracted is a supply-chain entry point: without verifying the bytes against a
pinned digest, a compromised mirror or a tampered release silently swaps what
you execute. The same is true of a Dockerfile ``ADD <url> <dest>``, which writes
the remote bytes straight into the image. This check fires on any ``curl``/``wget``
invocation that writes an artifact — an explicit output flag (``-o FILE`` / ``-O`` /
``--output`` / ``--remote-name``), a shell redirect into a file (``> FILE`` /
``>> FILE``), a bare ``wget <url>`` (which saves to disk by default), or a pipe
straight into a shell (``curl … | sh`` / ``curl -fsSL … | sudo bash``, the marquee
one-line installer, which never touches disk but *executes* the unverified bytes) —
and on any ``ADD`` from an ``http(s)://`` URL, unless a verification token appears
close after it:

  * ``sha256sum`` / ``sha512sum`` / ``shasum`` / ``md5sum`` (a ``… -c`` check)
  * ``cosign verify`` or ``gpg --verify`` (signature check)
  * ``_sha256_verify`` (a common verify-helper naming)
  * ``ADD --checksum=sha256:<digest>`` (Docker's own built-in pin)

Downloads to ``/dev/null``/``/dev/stdout``/``-`` (reachability probes, piped
API reads to a data reader like ``| jq``) are not artifacts and are ignored — but a
stdout sink piped into a *shell* (``curl -O- … | sh``) still executes, so it fires.
The same goes for commands inside message
strings (``echo``/``printf``/``warn``/``status``/``die``/``log`` lines). A
download that genuinely cannot be pinned opts out with a same-line or
preceding-line ``# pin-exempt: <reason>``.

Invoked by pre-commit with the staged shell + Dockerfile paths as arguments.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _linecheck import MESSAGE_PREFIX, run_line_checks  # noqa: E402,I001  # pylint: disable=wrong-import-position

# How many lines after a download to scan for its verification before giving up.
# The scan also stops early at the next download, so one check can't cover two.
_WINDOW = 25

_DOWNLOADER = re.compile(r"\b(?:curl|wget)\b")

# A Dockerfile `ADD <url> <dest>` that fetches a remote artifact (optionally with
# build flags like `--chown=`/`--chmod=`). This is the idiomatic Dockerfile
# download and writes the bytes straight into the image, so it owes a verification
# exactly like curl/wget. Docker's own `--checksum=sha256:<digest>` IS that
# verification (matched by _VERIFY below), so a pinned ADD passes.
_ADD_URL = re.compile(r"^\s*ADD\s+(?:--\S+\s+)*\S*https?://", re.IGNORECASE)

# An output flag that makes the fetch write a file. `-o`/`--output` and wget's
# `-O` take a target (captured so a /dev/null/stdout/- sink can be excused);
# curl's `-O` and `--remote-name` derive the name from the URL and take none. The
# target may be space-separated (`-o f`) or `=`-joined (`--output=f`); the `-O-`
# shorthand (write to stdout, no space) is captured by the `stdout` alternative.
_OUTPUT_FLAG = re.compile(
    r"(?:^|\s)(?:-o|-O|--output|--remote-name(?:-all)?)\b"
    r"(?:[=\s]+(?P<target>\S+)|(?P<stdout>-)(?=\s|$))?"
)

_NULL_TARGETS = {"/dev/null", "/dev/stdout", "/dev/stderr", "-"}

# A shell redirect that writes the fetched bytes into a file: `> f` / `>> f` (with
# any inter-token spacing). The `>` must be at a word boundary (start or after
# whitespace) so an FD-qualified redirect like `2>` / `1>` (stderr/stdout, glued to
# a digit) is NOT mistaken for an artifact write; a `&`/`|`-led target (`2>&1`, a
# pipe) is excluded from the captured path.
_REDIRECT = re.compile(r"(?:^|\s)>>?\s*(?P<rt>[^\s&|<>]+)")

# wget (unlike curl, which defaults to stdout) writes to disk by default, so a bare
# `wget <url>` with no output flag or redirect is still an artifact download.
_WGET = re.compile(r"\bwget\b")

# `curl … | sh` / `curl -fsSL … | sudo bash`: the streamed bytes never hit disk, but
# the shell EXECUTES them — the same supply-chain exposure as save-then-run, and the
# marquee one-line installer. A pipe to a data reader (`| jq`, `| tar`, `| grep`) is
# NOT an execution, so only a shell interpreter counts. `ssh`/`bashful` are rejected
# by the `\b…\b` word boundaries. An optional `sudo` (with its flags) and an absolute
# path (`/bin/sh`) are tolerated between the pipe and the interpreter name.
_PIPE_TO_SHELL = re.compile(
    r"\|\s*"
    r"(?:sudo\b[^|]*?\s)?"
    r"(?:\S*/)?"
    r"\b(?:sh|bash|dash|zsh|ksh|ash)\b"
)

_VERIFY = re.compile(
    r"\b(?:sha256sum|sha512sum|sha384sum|sha1sum|shasum|md5sum|_sha256_verify)\b"
    r"|\bcosign\s+verify\b"
    r"|\bgpg\b[^\n]*--verify\b"
    r"|--checksum=sha256:"  # Docker `ADD --checksum=sha256:<digest> <url>`
)


def _is_artifact_download(line: str) -> bool:
    """True if LINE runs curl/wget to save a real file (not /dev/null/stdout/-).

    Recognizes three ways bytes reach disk: an explicit output flag
    (``-o FILE``/``-O``/``--output``/``--remote-name``), a shell redirect into a
    file (``curl url > f``), and a bare ``wget url`` (wget saves by default; curl,
    which defaults to stdout, does not — so a flag-less, redirect-less curl is not
    an artifact) — plus a fourth: a pipe straight into a shell (``curl … | sh``),
    which executes the bytes without ever saving them."""
    if not _DOWNLOADER.search(line):
        return False
    # A pipe into a shell executes the download regardless of any stdout sink, so it
    # is checked before the `-O-`/`-o -` early-returns below (which would otherwise
    # excuse `curl -O- … | sh` as a mere stdout write).
    if _PIPE_TO_SHELL.search(line):
        return True
    m = _OUTPUT_FLAG.search(line)
    if m:
        if m.group("stdout"):  # `-O-` writes to stdout, not an artifact
            return False
        # A captured target may be a null sink; `-O`/`--remote-name` capture none
        # (they derive the name from the URL) and so are always a real artifact.
        return m.group("target") not in _NULL_TARGETS
    rm = _REDIRECT.search(line)
    if rm and rm.group("rt") not in _NULL_TARGETS:
        return True
    return bool(_WGET.search(line))


def _is_download(line: str) -> bool:
    """True if LINE fetches a remote artifact to disk — a curl/wget save or a
    Dockerfile `ADD <url>` — so it must carry a nearby verification."""
    return _is_artifact_download(line) or bool(_ADD_URL.search(line))


def violations(text: str) -> list[int]:
    """1-based line numbers of artifact downloads with no nearby verification."""
    lines = text.splitlines()
    hits = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or MESSAGE_PREFIX.match(stripped):
            continue
        if not _is_download(line):
            continue
        if "pin-exempt" in line or (i > 0 and "pin-exempt" in lines[i - 1]):
            continue
        if not _verified_within_window(lines, i):
            hits.append(i + 1)
    return hits


def _verified_within_window(lines: list[str], start: int) -> bool:
    """Scan [start, start+_WINDOW] for a verification token, stopping at the next
    download so each fetch must carry its own check."""
    for j in range(start, min(len(lines), start + _WINDOW + 1)):
        if j > start and _is_download(lines[j]):
            return False
        if _VERIFY.search(lines[j]):
            return True
    return False


def main(argv: list[str]) -> int:
    return run_line_checks(
        argv,
        violations,
        "downloaded artifact is not checksum/signature verified — add a "
        "sha256sum/cosign/gpg check after it, or annotate `# pin-exempt: <reason>`",
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
