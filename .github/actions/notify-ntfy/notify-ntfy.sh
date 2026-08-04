#!/usr/bin/env bash
# Push a phone notification through ntfy (https://ntfy.sh or a self-hosted
# instance) when a build or publish workflow fails. Invoked by the notify-ntfy
# composite action.
#
# An unset topic does not fail the run. A repo that did not configure the
# GH_NTFY_SUBJECT secret must not have the notifier redden its runs. Delivery is
# best-effort for the same reason: the caller runs this only for a workflow that
# already failed, and a dead ntfy server must not add a second red.
#
# Both paths notify nobody, so each one writes a `::warning::` annotation. The
# run then shows on its own summary that the alert did not arrive. A line on
# stderr leaves that fact in a log nobody opens.
set -euo pipefail

topic="${NTFY_TOPIC:-}"
if [[ -z "$topic" ]]; then
  # A `::warning::` is what makes this visible. The exit status stays 0, so a repo
  # that never opted in still does not go red — but the run now carries an
  # annotation saying the failure reached nobody, instead of leaving that fact on
  # a stderr line in a log nobody opens.
  echo "::warning title=ntfy is not configured::GH_NTFY_SUBJECT is unset, so this failure notified nobody."
  exit 0
fi

base_url="${NTFY_BASE_URL:-}"
[[ -z "$base_url" ]] && base_url="https://ntfy.sh"
base_url="${base_url%/}"

message="${NTFY_MESSAGE:-A build or publish workflow failed.}"

# ntfy carries metadata in HTTP headers, whose values must be single-line;
# collapse newlines so a multi-line title/tag can neither break the request nor
# smuggle an extra header.
sanitize_header() {
  printf '%s' "$1" | tr '\n\r' '  '
}

curl_args=(
  --silent --show-error --fail
  --max-time 20
  --retry 3 --retry-delay 2 --retry-connrefused
  -H "Title: $(sanitize_header "${NTFY_TITLE:-Workflow failed}")"
  -H "Priority: ${NTFY_PRIORITY:-5}"
  -H "Tags: $(sanitize_header "${NTFY_TAGS:-rotating_light}")"
)
click="${NTFY_CLICK:-}"
[[ -n "$click" ]] && curl_args+=(-H "Click: $(sanitize_header "$click")")
curl_args+=(-d "$message" "${base_url}/${topic}")

# The topic is secret; keep it out of the logs even though Actions masks it.
if curl "${curl_args[@]}"; then
  echo "notify-ntfy: notification sent to ${base_url}/<topic>."
else
  rc=$?
  # Same reasoning as the unset topic above, and the same outcome for the reader:
  # nobody was notified. A dead ntfy server still must not add a second red, so
  # the exit status stays 0 and the annotation carries the news.
  echo "::warning title=ntfy delivery failed::curl exited ${rc} for ${base_url}/<topic>, so this failure notified nobody."
fi
exit 0
