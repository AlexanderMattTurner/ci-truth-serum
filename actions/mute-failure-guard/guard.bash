# shellcheck shell=bash
# mute-failure-guard: bash reads this file through BASH_ENV before every non-interactive script.
#
# It records failed commands in a ledger, so a step that fails without a message still names the
# command, the file and the line. Four kinds of row, because bash hides failures four ways:
#
#   * ERR — a failed command. `set -E` passes the trap into functions, subshells and command
#     substitutions, so a failure an outer command drops (`f "$(g)"`) is recorded.
#   * subshell exit — a `$(…)`, `<(…)` or `( … )` that exited non-zero. Bash runs no ERR trap
#     inside a condition (`if`, `!`, a non-final `&&`/`||` part) nor in anything a condition
#     calls, so this row is what records `[[ -n "$(f)" ]] || …` and `x="$(f)" || return 1`.
#   * suppressed — a failure that `|| true` or `|| :` discarded.
#   * status saved — a failure whose status a caller saves, as `"$@" || rc=$?` does. Bash runs
#     no ERR trap on the left of `||`, so there this row is the only record of the failure.
#
# It never uses `set -x`: that prints expanded values, and CI logs are public. /bin/bash 3.2 on
# macOS runs it with less recorded: it names no command in an ERR row, and it records no exit
# of a `<(…)`.
#
# A launcher that snapshots and replays shell variables can unset the guard's own, so every
# trap reads them with a default and keeps running under `set -u`.
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
_cts_rc=0 _cts_src=0 _cts_drc=0 _cts_dline=0 _cts_cmd='' _cts_us='' _cts_var='' _cts_psrc='?' _cts_pline=0

# _cts_mute_row STATUS FILE LINE COMMAND — append one tab-separated ledger row: step id, pid,
# file:line, status, open descriptors, command text. The handler must never fail: under errexit
# a failed write here would end the user's script, so an `if` holds the write.
_cts_mute_row() {
  # A function's BASH_SOURCE is the file that defined it, so this finds the ledger beside the
  # guard even after a launcher unsets the guard's variables.
  local cmd=$4 fds=0 dir=${BASH_SOURCE[0]%/*} _
  cmd=${cmd//$'\n'/ }
  cmd=${cmd//$'\t'/ }
  for _ in /dev/fd/*; do fds=$((fds + 1)); done
  if ! printf '%s\t%s\t%s:%s\t%s\t%s\t%s\n' "${GITHUB_ACTION:-}" "${BASHPID:-$$}" "$2" "$3" "$1" "$fds" "${cmd:0:300}" >>"$dir/ledger" 2>/dev/null; then
    return 0
  fi
}

# _cts_mute_cmd TEXT — set _cts_cmd to the command name in TEXT, the first word, with an
# assignment cut to `NAME=`. Only the name is kept: inside `eval`, and in a `bash -c "$x"`
# child, TEXT is already expanded and can carry a secret, and bash marks neither case.
# The row's file:line names the full command in its source.
_cts_mute_cmd() {
  local word
  if [[ -n "${BASH_EXECUTION_STRING:-}" ]]; then
    _cts_cmd='<bash -c>'
    return 0
  fi
  # Bash 3.2 sets BASH_COMMAND to the ERR trap's own command, which holds no user command name.
  if [[ $1 == _cts_* ]]; then
    _cts_cmd='?'
    return 0
  fi
  word=${1%%[[:space:]]*}
  if [[ $word == *=* && ${word%%=*} != *[!A-Za-z0-9_]* ]]; then word="${word%%=*}="; fi
  _cts_cmd=${word:0:64}
}

# Each trap ends with `: "$_cts_us"`, which gives `$_` back the caller's last argument.
trap '_cts_rc=$? _cts_us=$_; _cts_mute_cmd "$BASH_COMMAND"; _cts_mute_row "$_cts_rc" "${BASH_SOURCE[0]:-?}" "$LINENO" "$_cts_cmd"; : "$_cts_us"' ERR

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
#   3. records a failure whose status the assignment about to run saves, at the failed command;
#   4. turns -T off in a shell about to set a RETURN trap, in any position of its signal list.
#      Under -T a RETURN trap also runs when every function it calls returns, so a cleanup there
#      would run too early. That shell keeps its ERR and suppressed rows and loses only the
#      subshell rows. A false match costs the shell its subshell rows and nothing else. Known
#      limit: a trap set through a wrapper function (`add_trap cleanup RETURN`) is not seen.
#   5. gives `$_` back the caller's last argument.
_cts_prev=
# _cts_saved_status TEXT — whether the command TEXT only saves `$?` in a variable, as `rc=$?`,
# `local rc="$?"` and `declare -i rc=${?}` do. It sets _cts_var to the variable's name.
_cts_saved_status() {
  local t=$1
  case $t in
  local\ * | declare\ * | typeset\ * | export\ * | readonly\ *) t=${t#* } ;;
  *) ;;
  esac
  while [[ $t == -*' '* ]]; do t=${t#* }; done
  # shellcheck disable=SC2016 # these are the literal source spellings, which must not expand
  case ${t#*=} in '$?' | '"$?"' | '${?}' | '"${?}"') ;; *) return 1 ;; esac
  _cts_var=${t%%=*}
  [[ -n $_cts_var && $_cts_var != *[!A-Za-z0-9_]* ]]
}
# _cts_sets_return_trap TEXT — whether the command TEXT sets a RETURN trap. It matches with
# `case`, because `=~` would overwrite the caller's BASH_REMATCH.
_cts_sets_return_trap() {
  case "$1 " in
  trap[[:space:]]*[[:space:]]RETURN[[:space:]]* | trap[[:space:]]*[[:space:]]\'RETURN\'[[:space:]]* | trap[[:space:]]*[[:space:]]\"RETURN\"[[:space:]]*) return 0 ;;
  *) return 1 ;;
  esac
}
# $LINENO is read on the trap's first line: each later line of this body adds one to it.
trap '_cts_drc=$? _cts_dline=$LINENO _cts_us=$_
((BASH_SUBSHELL > ${_cts_sub_level:-0})) && _cts_mute_enter "${BASH_SOURCE[0]:-?}" "$_cts_dline" "$BASH_COMMAND"
((_cts_drc != 0)) && [[ $BASH_COMMAND == true || $BASH_COMMAND == : ]] && _cts_mute_cmd "${_cts_prev:-}" && _cts_mute_row "$_cts_drc" "${BASH_SOURCE[0]:-?}" "$_cts_dline" "suppressed by || true: $_cts_cmd"
((_cts_drc != 0)) && _cts_saved_status "$BASH_COMMAND" && _cts_mute_cmd "${_cts_prev:-}" && _cts_mute_row "$_cts_drc" "${_cts_psrc:-?}" "${_cts_pline:-0}" "status saved by ${_cts_var:-?}=\$?: $_cts_cmd"
[[ $BASH_COMMAND == trap[[:space:]]* ]] && _cts_sets_return_trap "$BASH_COMMAND" && set +T
_cts_prev=$BASH_COMMAND _cts_psrc=${BASH_SOURCE[0]:-?} _cts_pline=$_cts_dline
: "$_cts_us"' DEBUG

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
  # The path is fixed now, so a later `unset` of the guard's variables cannot move the record.
  printf -v _cts_steps '%q' "$_cts_mute_dir/steps"
  # shellcheck disable=SC2064 # the path is fixed now, at the step shell's start
  trap "printf '%s\\t%s\\n' \"\$GITHUB_ACTION\" \"\$?\" >>$_cts_steps" EXIT
fi
