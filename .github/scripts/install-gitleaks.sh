#!/usr/bin/env bash
# Download the pinned gitleaks release and verify it against a committed SHA-256
# before extracting the binary. A curl'd tarball that is extracted and executed
# with no digest check is a supply-chain entry point: a compromised mirror or a
# tampered release silently swaps what runs. Verifying the bytes against a pinned
# digest (dogfooding this repo's own check_pinned_downloads rule) closes that.
#
# This script is the single source of truth for the pinned gitleaks version and
# its matching digest — they are coupled, so both live here rather than being
# restated in each workflow. Callers just run `bash install-gitleaks.sh`.
#
# Env:
#   GITLEAKS_VERSION  optional — override the pinned release version (no leading v).
#   GITLEAKS_DEST     optional — dir to extract `gitleaks` into (default: cwd).
set -euo pipefail

GITLEAKS_VERSION="${GITLEAKS_VERSION:-8.30.1}"
DEST="${GITLEAKS_DEST:-.}"

# Pinned SHA-256 of gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz. Taken from the
# release's published `gitleaks_${GITLEAKS_VERSION}_checksums.txt`. Bumping
# GITLEAKS_VERSION REQUIRES refreshing this digest in the same change:
#   curl -fsSL "https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_checksums.txt" \
#     | grep "linux_x64.tar.gz"
declare -A GITLEAKS_SHA256=(
  ["8.30.1"]="551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb"
)

sha256="${GITLEAKS_SHA256[$GITLEAKS_VERSION]:-}"
if [[ -z "$sha256" ]]; then
  echo "Error: no pinned SHA-256 for gitleaks $GITLEAKS_VERSION." >&2
  echo "Add it to GITLEAKS_SHA256 in $0 from the release checksums file." >&2
  exit 1
fi

tarball="gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz"
url="https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/${tarball}"

curl --proto '=https' -fsSL --retry 6 --retry-all-errors --retry-delay 15 \
  --connect-timeout 30 -o "$tarball" "$url"

echo "${sha256}  ${tarball}" | sha256sum -c -

tar xzf "$tarball" -C "$DEST" gitleaks
