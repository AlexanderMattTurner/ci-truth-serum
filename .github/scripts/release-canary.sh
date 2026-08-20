#!/usr/bin/env bash
# Post-release verification: assert this repo's version markers agree.
#
# Runs `release-canary` (this package's own console script) on the default
# branch after tag-release.sh has tagged and published, which is the first
# moment the comparison is meaningful: the `v*` tag and the changelog's top
# dated heading only line up once the release is cut, so a pre-merge gate could
# only ever check the state the release is about to leave behind. Verifying here
# catches the half-finished release — a tag push that 403'd, a changelog
# promotion that never ran — on the push that caused it.
#
# The canary reads the `v*` tags from the local repo, and the release job
# checks out shallow (fetch-depth 2, no tags), so fetch them first. A fetch
# that fails is a hard error: without the tag set the canary would read the git
# marker as absent and report a `missing marker` failure that blames the
# release rather than the fetch.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../../bin/lib/retry.bash disable=SC1091
source "$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)/bin/lib/retry.bash"

<<<<<<< local
if ! retry_cmd 4 2 git fetch --tags --quiet origin; then
  echo "Error: failed to fetch tags after 4 attempts; the release canary cannot read the git-tag marker" >&2
||||||| base
log() { echo "$@" >&2; }

# Self-check guard: a repo that does not publish to npm (package.json
# "private": true — e.g. the template itself) has no release pipeline to canary,
# so skip. Fails CLOSED on an unreadable package.json, matching version-bump.sh.
# "error" is a deliberate sentinel — the case below has an explicit `*)` arm
# that fails loud on it, so the fallback is caught, never silently treated as
# "false". echo-fallback-ok: sentinel is explicitly checked downstream.
IS_PRIVATE=$(node -p "require('./package.json').private === true" 2>/dev/null || echo "error")
case "$IS_PRIVATE" in
true)
  log "package.json has \"private\": true; this repo does not publish to npm. Nothing to canary."
  exit 0
  ;;
false) ;;
*)
  log "Error: could not read package.json \"private\" field (got: '$IS_PRIVATE'). Refusing to run the canary."
  exit 1
  ;;
esac

PACKAGE_NAME=$(node -p "require('./package.json').name")

# Positive publish evidence: does this repo actually publish to npm? `private:
# true` already exited above; but a repo can be non-private yet simply not be an
# npm package (a Python project, a website, a monorepo of scripts). Declaring
# `publishConfig` in package.json is the explicit "I publish" signal. Fails
# CLOSED to "declares publishing" only on a clean true; any read error is caught
# by the case's `*)` arm, never silently treated as false.
# echo-fallback-ok: sentinel is explicitly checked in the case below.
HAS_PUBLISHCONFIG=$(node -p "require('./package.json').publishConfig != null" 2>/dev/null || echo "error")
case "$HAS_PUBLISHCONFIG" in
true | false) ;;
*)
  log "Error: could not read package.json \"publishConfig\" field (got: '$HAS_PUBLISHCONFIG'). Refusing to run the canary."
  exit 1
  ;;
esac

# npm side: max stable X.Y.Z over the full published-versions list. `--json`
# yields an array normally, but a bare string for a single-release package, and
# an `{ "error": { "code": "E404" } }` object (still on stdout) when the package
# was never published. The max is computed by npm-max-stable.mjs, which orders
# versions with the `semver` package (exit 3 when nothing stable is published).
npm_rc=0
VERSIONS_JSON=$(npm view "$PACKAGE_NAME" versions --json 2>/dev/null) || npm_rc=$?

# A never-published package (E404) is only a failure if the repo DECLARED intent
# to publish (publishConfig). Otherwise this repo is simply not an npm publisher
# and the canary does not apply — skip with a clear "disable me" pointer rather
# than firing a red alert every single day, the false-alarm this guard removes.
if [[ "$npm_rc" -ne 0 ]] &&
  NPM_OUT="$VERSIONS_JSON" node -e 'const o=JSON.parse(process.env.NPM_OUT||"{}");process.exit(o?.error?.code==="E404"?0:1)' 2>/dev/null; then
  if [[ "$HAS_PUBLISHCONFIG" == "true" ]]; then
    log "Error: package.json declares \"publishConfig\" but '$PACKAGE_NAME' has never been published to npm. The release pipeline never published — investigate."
    exit 1
  fi
  log "'$PACKAGE_NAME' is not published to npm and package.json declares no \"publishConfig\": this repo is not an npm publisher, so the release canary does not apply."
  log "Disable this check: exclude .github/workflows/release-canary.yaml (and release-canary.sh, npm-max-stable.mjs) via EXCLUDE_PATHS in template-sync.yaml, or set \"private\": true if the package should never publish."
  exit 0
fi

# echo-fallback-ok reasoning no longer applies: any other npm failure (network,
# auth, a non-E404 error) is a real problem this canary must report loudly.
if [[ "$npm_rc" -ne 0 ]] || [[ -z "$VERSIONS_JSON" ]]; then
  log "Error: could not read published versions for '$PACKAGE_NAME' from npm (exit $npm_rc)."
  exit 1
fi
if ! NPM_MAX=$(NPM_VERSIONS="$VERSIONS_JSON" node "$SCRIPT_DIR/npm-max-stable.mjs"); then
  log "Error: no stable X.Y.Z version published for '$PACKAGE_NAME'."
=======
log() { echo "$@" >&2; }

# No package.json at all: not a package, so there is nothing to canary. Kept
# separate from the fail-closed arm below, which must keep treating a
# package.json that EXISTS but does not parse as an error. Without this arm the
# daily canary alerts forever in a synced consumer that has no Node project.
if [[ ! -f package.json ]]; then
  log "No package.json; this repo does not publish to npm. Nothing to canary."
  exit 0
fi

# Self-check guard: a repo that does not publish to npm (package.json
# "private": true — e.g. the template itself) has no release pipeline to canary,
# so skip. Fails CLOSED on an unreadable package.json, matching version-bump.sh.
# "error" is a deliberate sentinel — the case below has an explicit `*)` arm
# that fails loud on it, so the fallback is caught, never silently treated as
# "false". echo-fallback-ok: sentinel is explicitly checked downstream.
IS_PRIVATE=$(node -p "require('./package.json').private === true" 2>/dev/null || echo "error")
case "$IS_PRIVATE" in
true)
  log "package.json has \"private\": true; this repo does not publish to npm. Nothing to canary."
  exit 0
  ;;
false) ;;
*)
  log "Error: could not read package.json \"private\" field (got: '$IS_PRIVATE'). Refusing to run the canary."
  exit 1
  ;;
esac

PACKAGE_NAME=$(node -p "require('./package.json').name")

# Positive publish evidence: does this repo actually publish to npm? `private:
# true` already exited above; but a repo can be non-private yet simply not be an
# npm package (a Python project, a website, a monorepo of scripts). Declaring
# `publishConfig` in package.json is the explicit "I publish" signal. Fails
# CLOSED to "declares publishing" only on a clean true; any read error is caught
# by the case's `*)` arm, never silently treated as false.
# echo-fallback-ok: sentinel is explicitly checked in the case below.
HAS_PUBLISHCONFIG=$(node -p "require('./package.json').publishConfig != null" 2>/dev/null || echo "error")
case "$HAS_PUBLISHCONFIG" in
true | false) ;;
*)
  log "Error: could not read package.json \"publishConfig\" field (got: '$HAS_PUBLISHCONFIG'). Refusing to run the canary."
  exit 1
  ;;
esac

# npm side: max stable X.Y.Z over the full published-versions list. `--json`
# yields an array normally, but a bare string for a single-release package, and
# an `{ "error": { "code": "E404" } }` object (still on stdout) when the package
# was never published. The max is computed by npm-max-stable.mjs, which orders
# versions with the `semver` package (exit 3 when nothing stable is published).
npm_rc=0
VERSIONS_JSON=$(npm view "$PACKAGE_NAME" versions --json 2>/dev/null) || npm_rc=$?

# A never-published package (E404) is only a failure if the repo DECLARED intent
# to publish (publishConfig). Otherwise this repo is simply not an npm publisher
# and the canary does not apply — skip with a clear "disable me" pointer rather
# than firing a red alert every single day, the false-alarm this guard removes.
if [[ "$npm_rc" -ne 0 ]] &&
  NPM_OUT="$VERSIONS_JSON" node -e 'const o=JSON.parse(process.env.NPM_OUT||"{}");process.exit(o?.error?.code==="E404"?0:1)' 2>/dev/null; then
  if [[ "$HAS_PUBLISHCONFIG" == "true" ]]; then
    log "Error: package.json declares \"publishConfig\" but '$PACKAGE_NAME' has never been published to npm. The release pipeline never published — investigate."
    exit 1
  fi
  log "'$PACKAGE_NAME' is not published to npm and package.json declares no \"publishConfig\": this repo is not an npm publisher, so the release canary does not apply."
  log "Disable this check: exclude .github/workflows/release-canary.yaml (and release-canary.sh, npm-max-stable.mjs) via EXCLUDE_PATHS in template-sync.yaml, or set \"private\": true if the package should never publish."
  exit 0
fi

# echo-fallback-ok reasoning no longer applies: any other npm failure (network,
# auth, a non-E404 error) is a real problem this canary must report loudly.
if [[ "$npm_rc" -ne 0 ]] || [[ -z "$VERSIONS_JSON" ]]; then
  log "Error: could not read published versions for '$PACKAGE_NAME' from npm (exit $npm_rc)."
  exit 1
fi
if ! NPM_MAX=$(NPM_VERSIONS="$VERSIONS_JSON" node "$SCRIPT_DIR/npm-max-stable.mjs"); then
  log "Error: no stable X.Y.Z version published for '$PACKAGE_NAME'."
>>>>>>> template
  exit 1
fi

# `uv run` resolves the console script from pyproject.toml's [project.scripts],
# so this exercises the exact entry point the README tells consumers to install.
# Its exit status is this script's: a mismatch or a missing marker fails the
# job, and the job's failure is what reaches a human.
uv run --frozen release-canary "$@"
