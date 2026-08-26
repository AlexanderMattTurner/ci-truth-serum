#!/usr/bin/env bash
# Install the pinned uv release from its published artifact, verified against a
# committed SHA-256, and leave `uv`/`uvx` in DEST.
#
# Why not astral-sh/setup-uv: on a hosted runner that action resolves the
# download URL through https://raw.githubusercontent.com/astral-sh/versions/…
# on every job — an exact `version:` skips version RESOLUTION but not the
# artifact lookup, which is unconditional (v8.3.2 src/download/download-version.ts
# calls getArtifact before anything else, and getArtifact fetches the manifest).
# That single GET carries a hard 5-second AbortSignal.timeout and no retry
# (v8.3.2 src/utils/fetch.ts), and a hosted runner's tool cache is empty, so the
# read happens once per job with no second chance. One slow CDN edge then reds a
# job before any work runs, and across a ~90-leg matrix a one-in-a-hundred blip
# becomes a near-certain red PR. Deriving the URL from the pinned version removes
# that read entirely. The artifact fetch that remains is irreducible — the bytes
# have to come from somewhere — so it is retried with backoff, attempted against
# both published hosts, and checked against a committed digest.
#
# This script is the single source of truth for the pinned uv version and its
# matching digests — they are coupled, so both live here rather than being
# restated in each workflow.
#
# Env:
#   UV_VERSION  optional — override the pinned release (no leading v).
#   UV_DEST     optional — dir to install `uv`/`uvx` into (default: cwd).
set -euo pipefail

UV_VERSION="${UV_VERSION:-0.11.31}"
DEST="${UV_DEST:-.}"

# Pinned SHA-256 of uv-<target>.tar.gz, keyed "<version>|<target>". Taken from
# the release's published `<artifact>.sha256`. Bumping UV_VERSION REQUIRES
# refreshing every target's digest in the same change:
#   for t in x86_64-unknown-linux-gnu aarch64-unknown-linux-gnu; do
#     curl -fsSL "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-${t}.tar.gz.sha256"
#   done
declare -A UV_SHA256=(
  ["0.11.31|x86_64-unknown-linux-gnu"]="8cc1cd82d434ec565376f98bd938d4b715b5791a80ff2d3aa78821cf85091b4b"
  ["0.11.31|aarch64-unknown-linux-gnu"]="d74f23949fd07be4970f293d06ca99d87cd2a78a341c3d7b7fc0df7bc2d8a145"
)

machine="$(uname -m)"
case "$machine" in
x86_64 | amd64) target="x86_64-unknown-linux-gnu" ;;
aarch64 | arm64) target="aarch64-unknown-linux-gnu" ;;
*)
  echo "Error: no pinned uv artifact for machine '${machine}'." >&2
  echo "Add its target to UV_SHA256 in $0, or run this job on x86_64/aarch64 Linux." >&2
  exit 1
  ;;
esac

sha256="${UV_SHA256["${UV_VERSION}|${target}"]:-}"
if [[ -z "$sha256" ]]; then
  echo "Error: no pinned SHA-256 for uv ${UV_VERSION} (${target})." >&2
  echo "Add it to UV_SHA256 in $0 from the release's .sha256 file." >&2
  exit 1
fi

# Stage the archive outside the checkout so a failed verification never leaves a
# stray artifact in the working tree for a later step to trip over.
staging="$(mktemp -d)"
trap 'rm -rf "${staging}"' EXIT

tarball="${staging}/uv-${target}.tar.gz"
# Astral's mirror and GitHub Releases publish the identical artifact, so the
# digest below covers either. The second host is a second network path to the
# same bytes, not a second party to trust.
hosts=(
  "https://releases.astral.sh/github/uv/releases/download/${UV_VERSION}"
  "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}"
)

source_host=""
for host in "${hosts[@]}"; do
  if curl --proto '=https' -fsSL --retry 6 --retry-all-errors --retry-delay 5 \
    --connect-timeout 30 -o "$tarball" "${host}/uv-${target}.tar.gz"; then
    source_host="$host"
    break
  fi
  echo "Warning: uv ${UV_VERSION} download from ${host} failed; trying the next host." >&2
done

if [[ -z "$source_host" ]]; then
  echo "Error: uv ${UV_VERSION} (${target}) could not be downloaded from any published host." >&2
  exit 1
fi

echo "${sha256}  ${tarball}" | sha256sum -c -

mkdir -p "$DEST"
[[ -d "$DEST" ]] || {
  echo "Error: could not create ${DEST}." >&2
  exit 1
}
# The archive nests both binaries under a uv-<target>/ directory; strip it so
# DEST holds `uv` and `uvx` directly and can be prepended to PATH as-is.
tar xzf "$tarball" -C "$DEST" --strip-components=1 \
  "uv-${target}/uv" "uv-${target}/uvx"

# A download that exits 0 but leaves no executable would otherwise surface much
# later as a confusing "uv: command not found" in an unrelated step.
for binary in uv uvx; do
  if [[ ! -x "${DEST}/${binary}" ]]; then
    echo "Error: ${DEST}/${binary} is missing or not executable after extracting the uv ${UV_VERSION} archive." >&2
    exit 1
  fi
done

echo "Installed uv ${UV_VERSION} (${target}) from ${source_host} into ${DEST}"
