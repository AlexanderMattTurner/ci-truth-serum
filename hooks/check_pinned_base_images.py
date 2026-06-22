#!/usr/bin/env python3
"""Demand that every Docker base image is pinned to an immutable digest.

A ``FROM`` on a mutable tag (``node:22``, ``python:3.12-slim``, ``:latest``)
lets the registry serve different bytes under the same name over time, so the
image you build today and the one CI signed for a commit can silently diverge.
Requiring ``@sha256:<digest>`` makes the base content-addressed and reproducible
(Dependabot's docker ecosystem keeps the digests fresh).

This is STRICTLY STRONGER than hadolint's DL3006/DL3007, which are satisfied by
*any* explicit tag — ``node:22.3.0`` passes hadolint yet is still mutable. Only a
digest pin is immutable, so this lint demands one.

A ``FROM`` is allowed without a digest only when it references ``scratch`` or an
earlier build stage declared with ``AS <name>`` in the same file.

Invoked by pre-commit with the staged Dockerfile paths as arguments.

Detection is offline and the default. Pass ``--fix`` to additionally REWRITE each
unpinned ``FROM`` in place, appending the ``@sha256:<digest>`` the registry serves
for that tag right now. ``--fix`` is the one place this lint reaches the network
(a Docker Registry v2 manifest lookup), so it is opt-in only — wire it with
``args: [--fix]`` in ``.pre-commit-config.yaml``. Any image whose digest cannot be
resolved is left untouched and still reported, so the fix never guesses.
"""

import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _linecheck import run_line_checks  # noqa: E402,I001  # pylint: disable=wrong-import-position

_FROM = re.compile(r"^\s*FROM\s+(?P<rest>.+?)\s*$", re.IGNORECASE)
_AS = re.compile(r"\bAS\s+(?P<name>\S+)\s*$", re.IGNORECASE)
# A digest: `sha256:` + exactly 64 lowercase hex chars. One source of truth, anchored
# two ways below — at the end of an in-ref pin, and as a whole registry header value.
_SHA256_HEX = r"sha256:[0-9a-f]{64}"
# A real digest pin: `@sha256:<64-hex>` at the end of the ref. Checking shape (not
# mere `@sha256:` presence) rejects a truncated/empty digest (`node@sha256:`) that
# would otherwise pass unpinned.
_DIGEST_PINNED = re.compile(rf"@{_SHA256_HEX}$")


def _stage_names(lines: list[str]) -> set[str]:
    """Names introduced by `FROM … AS <name>`, referenceable by later stages."""
    names = set()
    for line in lines:
        m = _FROM.match(line)
        if not m:
            continue
        a = _AS.search(m.group("rest"))
        if a:
            names.add(a.group("name").lower())
    return names


def violations(text: str) -> list[int]:
    """1-based line numbers of FROM lines whose base image isn't digest-pinned."""
    lines = text.splitlines()
    stages = _stage_names(lines)
    hits = []
    for i, line in enumerate(lines):
        m = _FROM.match(line)
        if not m:
            continue
        # The image is the first token; drop a trailing `AS <name>` and platform
        # flags (`--platform=…`) so only the ref itself is judged.
        tokens = [t for t in m.group("rest").split() if not t.startswith("--")]
        if not tokens:
            continue
        image = tokens[0]
        if image.lower() == "scratch" or image.lower() in stages:
            continue
        if not _DIGEST_PINNED.search(image):
            hits.append(i + 1)
    return hits


_MESSAGE = (
    "base image is not pinned to @sha256:<digest> — pin it so the build is reproducible"
)

# Manifest media types we accept, newest-first: an OCI/Docker image *index*
# (multi-arch) resolves to the index digest — exactly what a `FROM` should pin —
# and a single-arch manifest to its own. The registry returns the matching
# `Docker-Content-Digest` header either way.
_ACCEPT = ", ".join(
    [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
    ]
)
_DIGEST = re.compile(rf"^{_SHA256_HEX}$")
_DOCKER_HUB = "registry-1.docker.io"


class DigestResolutionError(Exception):
    """A base image's current digest could not be fetched from its registry."""


def _split_ref(image: str) -> tuple[str, str]:
    """(name, tag) for an image ref, dropping any (possibly malformed) ``@digest``.

    A bare name resolves to ``latest`` (Docker's own default). The tag separator is
    the last ``:`` in the FINAL path component, so a ``registry:port`` host is not
    mistaken for a tag.
    """
    ref = image.split("@", 1)[0]
    if ":" in ref.rsplit("/", 1)[-1]:
        name, tag = ref.rsplit(":", 1)
        return name, tag
    return ref, "latest"


def _registry_and_repo(name: str) -> tuple[str, str]:
    """(registry_host, repository) for an image name. A first path component that
    looks like a host (has a dot or colon, or is ``localhost``) is the registry;
    otherwise the image lives on Docker Hub, where bare names get the ``library/``
    namespace."""
    first = name.split("/", 1)[0]
    if "/" in name and ("." in first or ":" in first or first == "localhost"):
        return first, name.split("/", 1)[1]
    return _DOCKER_HUB, name if "/" in name else f"library/{name}"


def _bearer_token(challenge: str) -> str | None:
    """Fetch a pull token for a ``WWW-Authenticate: Bearer realm=…`` challenge."""
    if not challenge.strip().lower().startswith("bearer "):
        return None
    # Assumes quoted param values, which Docker Hub / ghcr.io / quay all emit; an
    # exotic registry using bare-token (unquoted) params would drop service/scope
    # and surface as a DigestResolutionError (image left flagged, never mispinned).
    params = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
    realm = params.get("realm")
    if not realm:
        return None
    query = urllib.parse.urlencode(
        {k: params[k] for k in ("service", "scope") if params.get(k)}
    )
    with urllib.request.urlopen(
        f"{realm}?{query}" if query else realm, timeout=15
    ) as r:
        body = json.load(r)
    return body.get("token") or body.get("access_token")


def resolve_digest(image: str) -> str:
    """The ``sha256:…`` digest the registry currently serves for IMAGE's tag.

    Performs an anonymous Docker Registry v2 manifest request, completing a single
    Bearer-token challenge if the registry demands one (Docker Hub, ghcr.io, …).
    Raises DigestResolutionError on any HTTP/network failure or a missing/malformed
    digest header, so callers can leave the unresolved FROM untouched.
    """
    name, tag = _split_ref(image)
    registry, repo = _registry_and_repo(name)
    url = f"https://{registry}/v2/{repo}/manifests/{tag}"

    def _open(token: str | None):
        req = urllib.request.Request(url)  # noqa: S310 — https URL built from an image ref
        req.add_header("Accept", _ACCEPT)
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        return urllib.request.urlopen(req, timeout=15)

    try:
        try:
            resp = _open(None)
        except urllib.error.HTTPError as unauth:
            if unauth.code != 401:
                raise
            token = _bearer_token(unauth.headers.get("WWW-Authenticate", ""))
            if not token:
                raise DigestResolutionError(
                    f"{image}: registry demanded auth"
                ) from unauth
            resp = _open(token)
    except (urllib.error.URLError, OSError) as exc:
        raise DigestResolutionError(f"{image}: {exc}") from exc
    with resp:
        digest = resp.headers.get("Docker-Content-Digest", "")
    if not _DIGEST.match(digest):
        raise DigestResolutionError(f"{image}: registry returned no digest")
    return digest


def _pin_from_line(line: str, resolve: Callable[[str], str]) -> str | None:
    """LINE with its base image digest-pinned, or None if LINE pins nothing fixable
    (not a FROM, or a scratch/flags-only ref). Raises DigestResolutionError when the
    image is real but its digest can't be fetched."""
    m = _FROM.match(line)
    if not m:
        return None
    rest = m.group("rest")
    tokens = [t for t in rest.split() if not t.startswith("--")]
    if not tokens or tokens[0].lower() == "scratch":
        return None
    image = tokens[0]
    pinned = f"{image.split('@', 1)[0]}@{resolve(image)}"
    new_rest = re.sub(rf"(?<!\S){re.escape(image)}(?!\S)", pinned, rest, count=1)
    return line[: m.start("rest")] + new_rest


def fix_text(
    text: str, resolve: Callable[[str], str] | None = None
) -> tuple[str, list[int], list[tuple[int, str]]]:
    """Digest-pin every unpinned FROM in TEXT.

    Returns (new_text, fixed_linenos, unfixed) where ``unfixed`` is the
    ``(lineno, reason)`` of violations whose digest could not be resolved (left
    verbatim). Only lines ``violations()`` already flags are touched, so stage refs,
    ``scratch``, and already-pinned bases are never rewritten. RESOLVE defaults to
    the live ``resolve_digest`` (read at call time so tests can monkeypatch it).
    """
    resolve = resolve or resolve_digest
    lines = text.splitlines(keepends=True)
    fixed: list[int] = []
    unfixed: list[tuple[int, str]] = []
    for lineno in violations(text):
        raw = lines[lineno - 1]
        # Split the line's content from its exact terminator. splitlines() recognises
        # every separator keepends preserved (LF, CRLF, CR, U+2028, …), so the tail
        # after the content IS that terminator — reattached verbatim after rewriting.
        parts = raw.splitlines()
        content = parts[0] if parts else raw
        nl = raw[len(content) :]
        try:
            pinned = _pin_from_line(content, resolve)
        except DigestResolutionError as exc:
            unfixed.append((lineno, str(exc)))
            continue
        if pinned is None:
            continue
        lines[lineno - 1] = pinned + nl
        fixed.append(lineno)
    return "".join(lines), fixed, unfixed


def _run_fix(paths: list[str]) -> int:
    """Rewrite each path's unpinned bases in place. Exits non-zero when a file was
    modified (so pre-commit blocks the commit for re-staging) or a digest could not
    be resolved."""
    status = 0
    for path in paths:
        try:
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
        except (OSError, UnicodeDecodeError):
            continue
        new_text, fixed, unfixed = fix_text(text)
        if new_text != text:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(new_text)
            for lineno in fixed:
                print(f"{path}:{lineno}: pinned base image to digest", file=sys.stderr)
            status = 1
        for lineno, reason in unfixed:
            print(f"{path}:{lineno}: could not pin — {reason}", file=sys.stderr)
            status = 1
    return status


def main(argv: list[str]) -> int:
    if "--fix" in argv:
        return _run_fix([a for a in argv if a != "--fix"])
    return run_line_checks(argv, violations, _MESSAGE)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
