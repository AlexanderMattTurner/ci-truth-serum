# shellcheck shell=bash
# release-model-call.bash — shared Anthropic-call plumbing for the two release
# scripts (release-readiness.sh and release-prep.sh), plus the deterministic
# floor release-readiness.sh falls back to.
# Contract: sourced into strict-mode (set -euo pipefail) callers; do not re-set
# shell options.
#
# Both scripts ask a model one small question — "does this merit a release" and
# "is this bump minor or patch". A single credential answering that question is a
# single point of failure for the whole release pipeline: when it hits its usage
# cap the daily cron fails, no release PR is opened, and changelog fragments pile
# up against one tag. Two layers remove that:
#
#   1. anthropic_call walks a LADDER of credentials, so one exhausted key does
#      not end the attempt.
#   2. bump_from_fragments derives the bump from data already on disk, so a
#      total credential outage degrades to a mechanical answer rather than to
#      nothing at all.
#
# Layer 2 is safe precisely because the release still lands through a human: the
# readiness path only OPENS a PR, so a mechanically-derived bump is reviewed
# before it ships. release-prep.sh pushes a commit instead, so it does NOT take
# the floor — it fails loudly with the per-rung reasons.
#
# This file is sourced, not executed: it defines functions and sets no state.

# Per-rung transient-failure retry budget. A rung gets ANTHROPIC_MAX_ATTEMPTS
# tries for a retryable status (transport error, 408/429/5xx) with a fixed
# ANTHROPIC_RETRY_DELAY between them; a terminal status skips straight to the
# next rung. Overridable so a test can drive the loop without sleeping.
ANTHROPIC_MAX_ATTEMPTS="${ANTHROPIC_MAX_ATTEMPTS:-3}"
ANTHROPIC_RETRY_DELAY="${ANTHROPIC_RETRY_DELAY:-2}"
ANTHROPIC_API_URL="${ANTHROPIC_API_URL:-https://api.anthropic.com/v1/messages}"

# The credential env vars to try, in order. Populated from the environment so the
# workflow decides which secrets exist; an unset or empty rung is skipped rather
# than attempted and failed.
RELEASE_CREDENTIAL_ORDER=(
  ANTHROPIC_API_KEY
  CLAUDE_CODE_OAUTH_TOKEN
  CLAUDE_CODE_OAUTH_TOKEN_FALLBACK
  CLAUDE_CODE_OAUTH_TOKEN_FALLBACK_3
  CLAUDE_CODE_OAUTH_TOKEN_FALLBACK_5
)

# release_credential_ladder — print the NAME of every non-empty credential env
# var, one per line, in ladder order. Names only: a value never leaves this file.
release_credential_ladder() {
  local name
  for name in "${RELEASE_CREDENTIAL_ORDER[@]}"; do
    [[ -n "${!name:-}" ]] && printf '%s\n' "$name"
  done
  return 0
}

# auth_headers_for CREDENTIAL — set the AUTH_HEADERS array to the curl arguments
# that credential authenticates with, to be splatted as "${AUTH_HEADERS[@]}".
#
# Anthropic API keys (sk-ant-api…) use x-api-key; Claude subscription OAuth
# tokens (sk-ant-oat…) use Bearer plus the oauth beta header.
#
# The headers go into an array rather than through a printf/read round-trip
# because a credential is arbitrary bytes: any line- or space-delimited
# serialization would split a credential containing that delimiter across two
# curl arguments and send a truncated secret.
auth_headers_for() {
  local credential="$1"
  if [[ "$credential" == sk-ant-oat* ]]; then
    AUTH_HEADERS=(
      -H "authorization: Bearer $credential"
      -H "anthropic-beta: oauth-2025-04-20"
      -H "anthropic-version: 2023-06-01"
    )
    return 0
  fi
  AUTH_HEADERS=(
    -H "x-api-key: $credential"
    -H "anthropic-version: 2023-06-01"
  )
}

# _anthropic_status REQUEST_BODY RESPONSE_FILE — one POST; prints the HTTP status.
# A curl transport error maps to the sentinel 000, which the caller treats as a
# retryable status like any 5xx.
_anthropic_status() {
  # Truncate first: on a transport failure curl writes nothing, so an unemptied
  # file would leave the PREVIOUS rung's error body for _report_rung_failure to
  # quote — attaching one credential's rejection reason to another's name, in the
  # very log the degraded PR body sends a human to read.
  : >"$2"
  # pin-exempt: Anthropic API JSON response, parsed by jq — never executed/extracted
  curl -s -o "$2" -w "%{http_code}" \
    --max-time 30 "$ANTHROPIC_API_URL" \
    -H "Content-Type: application/json" \
    "${AUTH_HEADERS[@]}" \
    -d "$1" || echo "000" # echo-fallback-ok: a curl transport error maps to the sentinel 000, which is reported and retried/laddered — never a value fed to a decision
}

# _is_terminal_status CODE — true when retrying this credential is pointless.
#
# A 400/401/403 fails identically on every retry — a malformed request, a
# bad/revoked key, or an account over its usage cap — so burning the backoff
# budget on it only delays the next rung, which may well be healthy. Every other
# non-200 (000 transport, 408, 429, 5xx) can succeed on a retry.
_is_terminal_status() {
  [[ "$1" == "400" || "$1" == "401" || "$1" == "403" ]]
}

# anthropic_call REQUEST_BODY RESPONSE_FILE — POST to the Messages API, walking
# the credential ladder. Returns 0 with the 200 body in RESPONSE_FILE, or 1 when
# every rung is exhausted (the caller then decides whether to take a
# deterministic floor or fail).
anthropic_call() {
  local request_body="$1" response_file="$2"
  local name credential code attempt rungs=0
  while IFS= read -r name; do
    rungs=$((rungs + 1))
    credential="${!name:-}"
    auth_headers_for "$credential"
    for ((attempt = 1; attempt <= ANTHROPIC_MAX_ATTEMPTS; attempt++)); do
      code=$(_anthropic_status "$request_body" "$response_file")
      if [[ "$code" == "200" ]]; then
        echo "Model call succeeded on credential $name." >&2
        return 0
      fi
      _report_rung_failure "$name" "$code" "$response_file"
      if _is_terminal_status "$code"; then
        echo "Credential $name is terminally rejected; moving to the next rung." >&2
        break
      fi
      if ((attempt < ANTHROPIC_MAX_ATTEMPTS)); then
        echo "Retrying credential $name (attempt $((attempt + 1)) of $ANTHROPIC_MAX_ATTEMPTS)." >&2
        sleep "$ANTHROPIC_RETRY_DELAY"
      fi
    done
  done < <(release_credential_ladder)
  if ((rungs == 0)); then
    echo "No release credential is set; every ladder rung (${RELEASE_CREDENTIAL_ORDER[*]}) is empty." >&2
  else
    echo "Every one of the $rungs configured credential rungs was rejected; see the per-rung reasons above." >&2
  fi
  return 1
}

# _report_rung_failure NAME CODE RESPONSE_FILE — log why one rung failed.
#
# The credential NAME is safe to log; its VALUE never is, so nothing derived from
# the credential is interpolated here — only the variable name, the status code,
# and the API's own error message, which is quoted so an exhausted cap reads as
# an exhausted cap in the log rather than as a generic "unreachable".
_report_rung_failure() {
  local name="$1" code="$2" response_file="$3" msg
  msg=$(jq -r '.error.message // empty' "$response_file" 2>/dev/null || true) # allow-double-swallow: best-effort parse of an API error body; a non-JSON body falls through to the generic line below
  if [[ -n "$msg" ]]; then
    echo "Credential $name rejected (HTTP $code): $msg" >&2
  else
    echo "Credential $name failed (HTTP $code); response body was not Anthropic-shaped." >&2
  fi
}

# bump_from_fragments CHANGELOG_DIR — print "minor" or "patch" derived from the
# pending fragments alone, with no model call.
#
# The fragment discipline already encodes the judgment the model is asked to
# re-derive: a fragment exists only for a user-facing change (internal churn gets
# none), and its CATEGORY is the semver signal. added/changed/removed/deprecated
# describe a changed surface, so they take the minor; fixed/security leave the
# surface alone and take the patch. The pipeline never cuts a major, so minor is
# the ceiling and an empty or missing directory floors at patch.
bump_from_fragments() {
  local dir="$1" path base category
  for path in "$dir"/*.md; do
    # An unmatched glob stays literal, so the -e test is what makes an empty or
    # missing directory fall through to the patch floor.
    [[ -e "$path" ]] || continue
    base="${path##*/}"
    [[ "$base" == "README.md" ]] && continue
    # <id>.<category>.md — the category is the field before the extension.
    category="${base%.md}"
    category="${category##*.}"
    case "$category" in
    added | changed | removed | deprecated)
      printf 'minor\n'
      return 0
      ;;
    # fixed, security, and any malformed name leave the surface alone, so they
    # add nothing here and fall through to the patch floor after the loop.
    *) ;;
    esac
  done
  printf 'patch\n'
}
