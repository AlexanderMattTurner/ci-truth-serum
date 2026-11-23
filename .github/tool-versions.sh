# shellcheck shell=bash disable=SC2034  # these are consumed by files that source this one
# Single source of truth for the versions of the CI-only tools this repo pins.
#
# mergiraf backs the structural pre-pass in auto-resolve/prepare.sh: a
# syntax-aware merge that resolves the structural subset of a PR's source
# conflicts so only genuinely semantic conflicts reach the paid LLM pass.
# install-mergiraf.sh sources this file, then downloads the pinned release
# tarball from Codeberg and sha256-verifies it before extracting (fail-closed).
# Bump MERGIRAF_VERSION together with the checksum.
MERGIRAF_VERSION=v0.18.0
MERGIRAF_SHA256_linux_amd64=4de0986ff9155411dd105958b94362056d0055025db75369eddd3ecd25334cd2

# PyYAML for the workflow Python that reads YAML (resolver-ref.py, template-sync.sh's SYNC_PATHS
# reader). run-with-pyyaml.sh installs it through uv at this pin, so no step relies on the runner
# image shipping it.
PYYAML_VERSION=6.0.3
