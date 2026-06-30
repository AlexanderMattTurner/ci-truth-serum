"""Tests for hooks/check_pinned_base_images.py — the pre-commit lint that demands
Docker base images be pinned to an immutable @sha256 digest.
"""

import json
import subprocess
import sys
import urllib.error

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests._helpers import HOOKS_DIR, REPO_ROOT, load_hook

_SRC = HOOKS_DIR / "check_pinned_base_images.py"
mod = load_hook("check_pinned_base_images.py", "check_pinned_base_images")


def _flags(text: str) -> list[int]:
    return mod.violations(text)


def test_unpinned_tags_flagged() -> None:
    assert _flags("FROM node:22\n") == [1]
    assert _flags("FROM python:3.12-slim\n") == [1]
    assert _flags("FROM ubuntu:latest\n") == [1]
    assert _flags("FROM node:22 AS build\n") == [1]


def test_digest_pinned_passes() -> None:
    assert _flags("FROM node:22@sha256:" + "a" * 64 + "\n") == []
    assert _flags("FROM python:3.12@sha256:" + "b" * 64 + " AS base\n") == []
    assert _flags("FROM --platform=linux/amd64 node:22@sha256:" + "c" * 64 + "\n") == []


def test_malformed_digest_is_flagged() -> None:
    # `@sha256:` present but not a real 64-hex digest must NOT pass as pinned.
    assert _flags("FROM node@sha256:\n") == [1]
    assert _flags("FROM node@sha256:abc123\n") == [1]
    assert _flags("FROM node@sha256:" + "a" * 63 + "\n") == [1]  # one short
    assert _flags("FROM node@sha256:" + "z" * 64 + "\n") == [1]  # non-hex


def test_scratch_and_stage_refs_allowed() -> None:
    assert _flags("FROM scratch\n") == []
    text = "FROM node:22@sha256:" + "a" * 64 + " AS builder\nFROM builder\n"
    assert _flags(text) == []


def test_from_with_only_flags_is_skipped_not_crashed() -> None:
    # Malformed FROM (only a flag, no image ref) must not IndexError.
    assert _flags("FROM --platform=linux/amd64\n") == []


def test_main_wires_violations_and_message(
    tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    """main() runs this script's detector through the shared loop with its own
    message. The generic loop behaviour is covered once in test_linecheck.py;
    here we only pin that main() emits THIS message."""
    bad = tmp_path / "Dockerfile"
    bad.write_text("FROM node:22\n")
    assert mod.main([str(bad)]) == 1
    assert "not pinned to @sha256" in capsys.readouterr().err


def _run_script(*paths: str) -> subprocess.CompletedProcess[str]:
    """Invoke the real script as pre-commit does (paths on argv), capturing both
    streams so a behavioral test asserts on the actual exit code + emitted path."""
    return subprocess.run(
        [sys.executable, str(_SRC), *paths],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    "dockerfile",
    [
        "FROM node:22\n",  # mutable tag
        "FROM ubuntu:latest\n",  # :latest
        "FROM node@sha256:abc123\n",  # short/malformed digest
        "FROM node:22 AS build\n",  # tagged build stage
    ],
)
def test_script_rejects_unpinned_dockerfile(tmp_path, dockerfile: str) -> None:
    """The real script exits non-zero and names the offending file for each
    distinct unpinned spelling — not just the in-process detector."""
    bad = tmp_path / "Dockerfile"
    bad.write_text(dockerfile, encoding="utf-8")
    proc = _run_script(str(bad))
    assert proc.returncode == 1
    assert str(bad) in proc.stderr
    assert "not pinned to @sha256" in proc.stderr


def test_script_accepts_pinned_dockerfile(tmp_path) -> None:
    """Negative control: a correctly digest-pinned base is accepted (exit 0), so
    the rejections above prove discrimination, not blanket failure."""
    good = tmp_path / "Dockerfile"
    good.write_text(
        "FROM node:22@sha256:" + "a" * 64 + " AS base\nFROM base\nFROM scratch\n",
        encoding="utf-8",
    )
    proc = _run_script(str(good))
    assert proc.returncode == 0
    assert proc.stderr == ""


def test_repo_dockerfiles_are_pinned() -> None:
    tracked = subprocess.check_output(
        ["git", "ls-files", "*Dockerfile*"], text=True, cwd=REPO_ROOT
    ).split()
    offenders = {}
    for rel in tracked:
        text = (REPO_ROOT / rel).read_text(encoding="utf-8", errors="ignore")
        v = mod.violations(text)
        if v:
            offenders[rel] = v
    assert not offenders, f"unpinned base images: {offenders}"


# ── --fix: digest pinning ────────────────────────────────────────────────────

_DIGEST = "sha256:" + "a" * 64


def _const(digest: str = _DIGEST):
    """A resolver that returns a fixed digest and records what it was asked to pin."""
    seen: list[str] = []

    def resolve(image: str) -> str:
        seen.append(image)
        return digest

    return resolve, seen


@pytest.mark.parametrize(
    "before, after",
    [
        ("FROM node:22\n", f"FROM node:22@{_DIGEST}\n"),
        ("FROM ubuntu:latest\n", f"FROM ubuntu:latest@{_DIGEST}\n"),
        # AS and --platform are preserved; only the image ref grows a digest.
        (
            "FROM --platform=linux/amd64 node:22 AS build\n",
            f"FROM --platform=linux/amd64 node:22@{_DIGEST} AS build\n",
        ),
        # A malformed digest is replaced wholesale by the resolved one.
        ("FROM node:22@sha256:abc123\n", f"FROM node:22@{_DIGEST}\n"),
        # No trailing newline is not invented.
        ("FROM node:22", f"FROM node:22@{_DIGEST}"),
        # A CRLF terminator is preserved exactly, not normalized to LF.
        ("FROM node:22\r\n", f"FROM node:22@{_DIGEST}\r\n"),
    ],
)
def test_fix_text_pins_unpinned(before: str, after: str) -> None:
    resolve, _ = _const()
    new_text, fixed, unfixed = mod.fix_text(before, resolve=resolve)
    assert new_text == after
    assert fixed == [1]
    assert unfixed == []


def test_fix_text_leaves_scratch_stage_and_pinned_untouched() -> None:
    def fail(image: str) -> str:  # must never be called for a non-violation line
        raise AssertionError(f"resolver hit for {image!r}")

    text = (
        "FROM scratch\n"
        f"FROM node:22@{_DIGEST} AS base\n"
        "FROM base\n"
        "RUN echo not-a-from\n"
    )
    new_text, fixed, unfixed = mod.fix_text(text, resolve=fail)
    assert new_text == text
    assert fixed == [] and unfixed == []


def test_fix_text_leaves_unresolvable_flagged_not_guessed() -> None:
    def boom(image: str) -> str:
        raise mod.DigestResolutionError("registry down")

    new_text, fixed, unfixed = mod.fix_text("FROM node:22\n", resolve=boom)
    assert new_text == "FROM node:22\n"  # untouched — never guesses a digest
    assert fixed == []
    assert unfixed == [(1, "registry down")]


def test_fix_text_pins_only_resolvable_in_mixed_file() -> None:
    def selective(image: str) -> str:
        if image.startswith("node"):
            return _DIGEST
        raise mod.DigestResolutionError("no such image")

    text = "FROM node:22\nFROM ghost:9\n"
    new_text, fixed, unfixed = mod.fix_text(text, resolve=selective)
    assert new_text == f"FROM node:22@{_DIGEST}\nFROM ghost:9\n"
    assert fixed == [1]
    assert unfixed == [(2, "no such image")]


def test_main_fix_writes_file_and_signals_modification(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(mod, "resolve_digest", lambda image: _DIGEST)
    df = tmp_path / "Dockerfile"
    df.write_text("FROM node:22\n")
    assert mod.main(["--fix", str(df)]) == 1  # modified → non-zero for re-staging
    assert df.read_text() == f"FROM node:22@{_DIGEST}\n"
    # Re-running over the now-pinned file is a clean no-op.
    assert mod.main(["--fix", str(df)]) == 0


def test_main_fix_reports_unresolvable(tmp_path, monkeypatch, capsys) -> None:
    def boom(image: str) -> str:
        raise mod.DigestResolutionError("offline")

    monkeypatch.setattr(mod, "resolve_digest", boom)
    df = tmp_path / "Dockerfile"
    df.write_text("FROM node:22\n")
    assert mod.main(["--fix", str(df)]) == 1
    assert df.read_text() == "FROM node:22\n"  # unchanged
    assert "could not pin" in capsys.readouterr().err


def test_main_fix_skips_unreadable_path_without_aborting(tmp_path, monkeypatch) -> None:
    """An unreadable path is skipped, not fatal: a readable Dockerfile in the same
    run is still fixed."""
    monkeypatch.setattr(mod, "resolve_digest", lambda image: _DIGEST)
    missing = tmp_path / "nope" / "Dockerfile"  # parent absent → OSError on open
    good = tmp_path / "Dockerfile"
    good.write_text("FROM node:22\n")
    assert mod.main(["--fix", str(missing), str(good)]) == 1
    assert good.read_text() == f"FROM node:22@{_DIGEST}\n"


@pytest.mark.parametrize(
    "image, name, tag",
    [
        ("node:22", "node", "22"),
        ("node", "node", "latest"),
        ("ghcr.io/owner/app:1.2", "ghcr.io/owner/app", "1.2"),
        ("localhost:5000/img", "localhost:5000/img", "latest"),
        ("node:22@sha256:dead", "node", "22"),  # malformed digest dropped
    ],
)
def test_split_ref(image: str, name: str, tag: str) -> None:
    assert mod._split_ref(image) == (name, tag)


@pytest.mark.parametrize(
    "name, registry, repo",
    [
        ("node", "registry-1.docker.io", "library/node"),
        ("owner/app", "registry-1.docker.io", "owner/app"),
        ("ghcr.io/owner/app", "ghcr.io", "owner/app"),
        ("localhost:5000/img", "localhost:5000", "img"),
    ],
)
def test_registry_and_repo(name: str, registry: str, repo: str) -> None:
    assert mod._registry_and_repo(name) == (registry, repo)


# ── resolve_digest control flow (offline, via a fake urlopen) ─────────────────

_HEADER_DIGEST = "sha256:" + "e" * 64
_BEARER = (
    'Bearer realm="https://auth.example/token",'
    'service="reg",scope="repository:library/node:pull"'
)


class _FakeResp:
    """Minimal urlopen stand-in: a context manager exposing ``headers`` and a
    ``read()`` body (for the token endpoint's JSON)."""

    def __init__(self, headers: dict | None = None, body: bytes = b"") -> None:
        self.headers = headers or {}
        self._body = body

    def read(self, *_a) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *_a) -> bool:
        return False


def _http_error(code: int, headers: dict | None = None) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://reg/v2/library/node/manifests/22", code, "err", headers or {}, None
    )


def _fake_urlopen(manifest_seq, token: dict | None = None):
    """A urlopen replacement: each manifest request consumes the next item of
    MANIFEST_SEQ (raised if an exception, else returned); the token endpoint returns
    TOKEN as JSON."""
    seq = iter(manifest_seq)

    def fake(req, timeout=None):  # noqa: ARG001
        # `resolve_digest` passes a Request (manifest); `_bearer_token` a str URL.
        url = req.full_url if hasattr(req, "full_url") else req
        if "/manifests/" in url:
            item = next(seq)
            if isinstance(item, Exception):
                raise item
            return item
        return _FakeResp(body=json.dumps(token or {"token": "T"}).encode())

    return fake


def test_resolve_digest_direct_200(monkeypatch) -> None:
    monkeypatch.setattr(
        mod.urllib.request,
        "urlopen",
        _fake_urlopen([_FakeResp(headers={"Docker-Content-Digest": _HEADER_DIGEST})]),
    )
    assert mod.resolve_digest("node:22") == _HEADER_DIGEST


def test_resolve_digest_completes_bearer_challenge(monkeypatch) -> None:
    monkeypatch.setattr(
        mod.urllib.request,
        "urlopen",
        _fake_urlopen(
            [
                _http_error(401, {"WWW-Authenticate": _BEARER}),
                _FakeResp(headers={"Docker-Content-Digest": _HEADER_DIGEST}),
            ]
        ),
    )
    assert mod.resolve_digest("node:22") == _HEADER_DIGEST


@pytest.mark.parametrize(
    "manifest_seq, expected",
    [
        # A non-401 HTTP error is a hard failure, not an auth challenge.
        ([_http_error(404)], "node:22"),
        # 401 without a Bearer challenge → no token to retry with.
        ([_http_error(401, {"WWW-Authenticate": "Basic realm=x"})], "demanded auth"),
        # 200 but the registry served no digest header.
        ([_FakeResp(headers={})], "no digest"),
        # Transport failure surfaces as the typed error, not a raw URLError.
        ([urllib.error.URLError("boom")], "node:22"),
    ],
)
def test_resolve_digest_raises_typed_error(monkeypatch, manifest_seq, expected) -> None:
    monkeypatch.setattr(mod.urllib.request, "urlopen", _fake_urlopen(manifest_seq))
    with pytest.raises(mod.DigestResolutionError, match=expected):
        mod.resolve_digest("node:22")


# ── _bearer_token branches (only exercised indirectly above) ─────────────────


def _no_network(monkeypatch) -> None:
    def boom(*_a, **_k):
        raise AssertionError("urlopen must not be called")

    monkeypatch.setattr(mod.urllib.request, "urlopen", boom)


def test_bearer_token_uses_access_token_fallback(monkeypatch) -> None:
    # A token endpoint that returns `access_token` instead of `token` (ghcr.io does
    # this) must still resolve — the `or body.get("access_token")` fallback branch,
    # which the manifest-challenge test never reaches.
    monkeypatch.setattr(
        mod.urllib.request,
        "urlopen",
        lambda req, timeout=None: _FakeResp(  # noqa: ARG005
            body=json.dumps({"access_token": "AT"}).encode()
        ),
    )
    assert mod._bearer_token(_BEARER) == "AT"


def test_bearer_token_rejects_non_bearer_scheme(monkeypatch) -> None:
    # A `Basic` challenge is not a bearer flow → None, before any network call.
    _no_network(monkeypatch)
    assert mod._bearer_token("Basic realm=x") is None


def test_bearer_token_without_realm_returns_none(monkeypatch) -> None:
    # A bearer challenge missing realm= cannot be completed → None, no network call.
    _no_network(monkeypatch)
    assert mod._bearer_token('Bearer service="reg"') is None


def test_bearer_token_queryless_realm_opens_bare_realm(monkeypatch) -> None:
    # realm present but no service/scope → the request URL is the bare realm, with no
    # trailing `?` (the else-branch of `f"{realm}?{query}" if query else realm`).
    seen: dict[str, str] = {}

    def fake(url, timeout=None):  # noqa: ARG001
        seen["url"] = url
        return _FakeResp(body=json.dumps({"token": "T"}).encode())

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake)
    assert mod._bearer_token('Bearer realm="https://auth.example/token"') == "T"
    assert seen["url"] == "https://auth.example/token"


# ── property: the offline ref parsers never crash on arbitrary refs ──────────


@given(st.text(max_size=40))
def test_ref_parsers_never_crash(ref: str) -> None:
    # _split_ref → _registry_and_repo is the chain resolve_digest runs on an image
    # ref read straight from a Dockerfile (untrusted). It must always return string
    # pairs, never raise, whatever the ref looks like.
    name, tag = mod._split_ref(ref)
    assert isinstance(name, str) and isinstance(tag, str)
    registry, repo = mod._registry_and_repo(name)
    assert isinstance(registry, str) and isinstance(repo, str)
