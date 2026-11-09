# shellcheck shell=bash
# mute-failure-guard: bash reads this file through BASH_ENV before every non-interactive script.
#
# It records failed commands in a ledger, so a step that fails without a message still names the
# command, the file and the line. Three kinds of row, because bash hides failures three ways:
#
#   * ERR — a failed command. `set -E` passes the trap into functions, subshells and command
#     substitutions, so a failure an outer command drops (`f "$(g)"`) is recorded.
#   * subshell exit — a `$(…)`, `<(…)` or `( … )` that exited non-zero. Bash runs no ERR trap
#     inside a condition (`if`, `!`, a non-final `&&`/`||` part) nor in anything a condition
#     calls, so this row is what records `[[ -n "$(f)" ]] || …` and `x="$(f)" || return 1`.
#   * suppressed — a failure that `|| true` or `|| :` discarded.
#
# It never uses `set -x`: that prints expanded values, and CI logs are public. Bash 3.2 safe:
# macOS runners run /bin/bash 3.2.
#
# The action copies this file into the job's ledger directory and writes an `active` marker
# beside it, so the file finds its ledger from its own path. That needs no second variable,
# which matters because a launcher that clears its environment usually keeps BASH_ENV. A copy
# with no marker beside it, such as the one in the action's source tree, does nothing.
_cts_mute_dir=${BASH_SOURCE[0]%/*}
[[ -f "$_cts_mute_dir/active" ]] || return 0

# -T passes the DEBUG trap into functions and subshells, which the subshell rows need.
set -E -T
# The traps below assign these; set here so `set -u` never meets them unset.
_cts_rc=0 _cts_src=0 _cts_drc=0 _cts_dline=0 _cts_cmd=

# _cts_mute_row STATUS FILE LINE COMMAND — append one tab-separated ledger row: step id, pid,
# file:line, status, open descriptors, command text. The handler must never fail: under errexit
# a failed write here would end the user's script, so an `if` holds the write.
_cts_mute_row() {
  local cmd=$4 fds=0 _
  cmd=${cmd//$'\n'/ }
  cmd=${cmd//$'\t'/ }
  for _ in /dev/fd/*; do fds=$((fds + 1)); done
  if ! printf '%s\t%s\t%s:%s\t%s\t%s\t%s\n' "${GITHUB_ACTION:-}" "${BASHPID:-$$}" "$2" "$3" "$1" "$fds" "${cmd:0:300}" >>"$_cts_mute_dir/ledger" 2>/dev/null; then
    return 0
  fi
}

# _cts_mute_cmd TEXT — set _cts_cmd to TEXT, or to a placeholder in a `bash -c "$x"` child,
# whose command text was expanded by its parent and can carry a secret.
_cts_mute_cmd() {
  if [[ -n "${BASH_EXECUTION_STRING:-}" ]]; then _cts_cmd='<bash -c>'; else _cts_cmd=$1; fi
}

trap '_cts_rc=$?; _cts_mute_cmd "$BASH_COMMAND"; _cts_mute_row "$_cts_rc" "${BASH_SOURCE[0]:-?}" "$LINENO" "$_cts_cmd"' ERR

# _cts_mute_enter FILE LINE COMMAND — arm an EXIT trap in a subshell that has just started, so
# its non-zero exit is recorded at the place its first command came from.
_cts_sub_level=$BASH_SUBSHELL
_cts_mute_enter() {
  local where cmd
  _cts_sub_level=$BASH_SUBSHELL
  _cts_mute_cmd "$3"
  printf -v where '%q %q' "$1" "$2"
  printf -v cmd '%q' "subshell exit, first command: $_cts_cmd"
  # shellcheck disable=SC2064 # the location is fixed now, at the subshell's first command
  trap "_cts_src=\$?; ((_cts_src == 0)) || _cts_mute_row \"\$_cts_src\" $where $cmd" EXIT
}

# The DEBUG trap runs before every command. In order, it:
#   1. arms the subshell EXIT trap on the first command of a new subshell;
#   2. records a failure that the `true` or `:` about to run will discard — $? still holds it;
#   3. turns -T off in a shell about to set a RETURN trap. Under -T a RETURN trap also runs when
#      every function it calls returns, so a cleanup there would run too early. That shell keeps
#      its ERR and suppressed rows and loses only the subshell rows.
_cts_prev=
# $LINENO is read on the trap's first line: each later line of this body adds one to it.
trap '_cts_drc=$? _cts_dline=$LINENO
((BASH_SUBSHELL > _cts_sub_level)) && _cts_mute_enter "${BASH_SOURCE[0]:-?}" "$_cts_dline" "$BASH_COMMAND"
((_cts_drc != 0)) && [[ $BASH_COMMAND == true || $BASH_COMMAND == : ]] && _cts_mute_cmd "$_cts_prev" && _cts_mute_row "$_cts_drc" "${BASH_SOURCE[0]:-?}" "$_cts_dline" "suppressed by || true: $_cts_cmd"
[[ $BASH_COMMAND == trap*RETURN ]] && set +T
_cts_prev=$BASH_COMMAND' DEBUG

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
