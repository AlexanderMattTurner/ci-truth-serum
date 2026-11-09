# shellcheck shell=bash
# mute-failure-guard: bash reads this file through BASH_ENV before every non-interactive script.
#
# It records each failed command in a ledger, so a step that fails without a message still names
# the command, the file and the line. `set -E` passes the ERR trap into functions, subshells and
# command substitutions, so a failure that an outer command drops (`f "$(g)"`) is recorded too.
# It never uses `set -x`: that prints expanded values, and CI logs are public.
#
# Bash 3.2 safe: macOS runners run /bin/bash 3.2.
#
# The action copies this file into the job's ledger directory and writes an `active` marker
# beside it, so the file finds its ledger from its own path. That needs no second variable,
# which matters because a launcher that clears its environment usually keeps BASH_ENV. A copy
# with no marker beside it, such as the one in the action's source tree, does nothing.
_cts_mute_dir=${BASH_SOURCE[0]%/*}
[[ -f "$_cts_mute_dir/active" ]] || return 0

set -E

# _cts_mute_record STATUS SOURCE LINE — append one ledger row for the command that just failed.
# The row is tab-separated: step id, pid, file:line, status, open descriptors, command text.
# The handler must never fail: under errexit a failed write here would end the user's script.
_cts_mute_record() {
  local cmd fds=0 _
  if [[ -n "${BASH_EXECUTION_STRING:-}" ]]; then
    # A `bash -c "$x"` child holds expanded values, which can carry secrets.
    cmd='<bash -c>'
  else
    cmd=$BASH_COMMAND
  fi
  cmd=${cmd//$'\n'/ }
  cmd=${cmd//$'\t'/ }
  for _ in /dev/fd/*; do fds=$((fds + 1)); done
  # An `if` keeps a failed write from ending the user's script under errexit. A lost row only
  # costs evidence, which the post hook reports as an empty ledger.
  if ! printf '%s\t%s\t%s:%s\t%s\t%s\t%s\n' "${GITHUB_ACTION:-}" "${BASHPID:-$$}" "$2" "$3" "$1" "$fds" "${cmd:0:300}" >>"$_cts_mute_dir/ledger" 2>/dev/null; then
    return 0
  fi
}
trap '_cts_mute_record "$?" "${BASH_SOURCE[0]:-?}" "$LINENO"' ERR

# The first guarded bash in a step is the step's own shell. It records the step's exit status and
# keeps a copy of both output streams, which the post hook reads to judge a mute failure. An
# atomic mkdir per step id picks it: a variable it exported would not reach a child bash whose
# launcher clears the environment, and that child would then claim the step.
if [[ -n "${GITHUB_ACTION:-}" ]] && mkdir "$_cts_mute_dir/step-$GITHUB_ACTION.claimed" 2>/dev/null; then
  _cts_mute_out="$_cts_mute_dir/step-$GITHUB_ACTION.out"
  # Named pipes and disowned readers, not `> >(tee …)`: bash 5.1 and later make a bare `wait`
  # wait for the last process substitution, and that tee never ends, so the step would hang.
  mkfifo "$_cts_mute_dir/$$.o" "$_cts_mute_dir/$$.e"
  tee -a "$_cts_mute_out" <"$_cts_mute_dir/$$.o" &
  disown
  tee -a "$_cts_mute_out" <"$_cts_mute_dir/$$.e" >&2 &
  disown
  exec >"$_cts_mute_dir/$$.o" 2>"$_cts_mute_dir/$$.e"
  rm -f "$_cts_mute_dir/$$.o" "$_cts_mute_dir/$$.e"
  trap 'printf "%s\t%s\n" "$GITHUB_ACTION" "$?" >>"$_cts_mute_dir/steps"' EXIT
fi
