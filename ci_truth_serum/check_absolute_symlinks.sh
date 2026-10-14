#!/usr/bin/env bash
# Reject a tracked symlink that a fresh clone cannot follow. Two targets do that:
#
#   * an ABSOLUTE path (`/Users/foo/...`) — it names a directory that exists on
#     the author's machine and nowhere else; and
#   * a path git IGNORES (`node_modules/x`, `.venv/bin/y`, `dist/z`) — the target
#     is built by a tool and is never committed, so the link dangles until
#     somebody runs the right install. Whoever clones the repo sees a broken
#     link, and the error names the link rather than the missing install.
#
# The ignored case asks git, not a hardcoded list of tool directories: the repo's
# own .gitignore already says which paths a clone will not carry, so the two
# cannot drift. A target that leaves the repository is out of scope here — the
# absolute rule above covers the common spelling of that mistake.

set -euo pipefail

# A process substitution hides its exit status, so a `git ls-files` that fails
# (no repository, a corrupt index) would read as an empty index and pass. Write
# the index to a file first, so `set -e` stops on the failure.
index_file=$(mktemp)
trap 'rm -f "${index_file}"' EXIT
git ls-files -s -z >"${index_file}"

violations=""
# Each entry is `<mode> SP <hash> SP <stage> TAB <path>`. Split it with parameter
# expansion, because a pipe per field forks three times for every tracked file: on
# a 6,550-entry index that is ~20,000 forks and a minute, to find two symlinks.
# `-z` ends each entry with NUL and leaves the path unquoted, so a path that holds
# a tab or a newline reaches the verdict byte-exact. `read` with an IFS split
# would strip a leading or trailing blank from the path, so the path is the text
# after the first tab, as is.
while IFS= read -r -d '' entry; do
  meta="${entry%%$'\t'*}"
  path="${entry#*$'\t'}"
  mode="${meta%% *}"
  [[ "${mode}" = "120000" ]] || continue
  hash="${meta#* }"
  hash="${hash%% *}"
  # `$(…)` strips trailing newlines, so the `x` keeps the target byte-exact. The
  # `&&` keeps cat-file's exit status as the substitution's, so `set -e` sees it.
  target=$(git cat-file blob "${hash}" && printf x)
  target="${target%x}"
  case "${target}" in
  /*)
    violations="${violations}${path} -> ${target} (absolute path)"$'\n'
    continue
    ;;
  *) : ;; # relative targets are portable — judged against .gitignore below
  esac

  # Join the target to the LINK's directory, not to the repo root, and normalise
  # `.` and `..` in the shell.
  #
  # Two reasons not to call `realpath`. macOS ships the BSD one, which has
  # neither `-m` nor `--relative-to`, so the hook would exit on an illegal option
  # on the first relative link in every consuming repo. And `realpath` follows a
  # symlink it finds along the way: a `node_modules/pkg` that a workspace install
  # points back into `packages/pkg` would resolve to a tracked path and pass,
  # although a fresh clone has no `node_modules/` at all. A lexical join reads
  # the committed tree, which is the tree the verdict is about.
  #
  # Every split below keeps a newline in a name. `$(dirname …)` drops a trailing
  # one, and `read <<<` stops at the first one, so a link in a directory named
  # `a<LF>b` would join from `a` and lose its finding.
  case "${path}" in
  */*) dir="${path%/*}" ;;
  *) dir="." ;;
  esac
  resolved=""
  IFS='/' read -r -d '' -a segments < <(printf '%s\0' "${dir}/${target}")
  for segment in "${segments[@]}"; do
    case "${segment}" in
    "" | .) : ;;
    ..)
      case "${resolved}" in
      "" | .. | ../*) resolved="${resolved:+${resolved}/}.." ;;
      */*) resolved="${resolved%/*}" ;;
      *) resolved="" ;;
      esac
      ;;
    *) resolved="${resolved:+${resolved}/}${segment}" ;;
    esac
  done
  # A target outside the repository, or the repository root itself, has no
  # .gitignore verdict to read. Skip it rather than guess.
  case "${resolved}" in
  "" | ../* | /*) continue ;;
  *) : ;;
  esac

  # Ask git about each prefix of the target, shortest first. A path is ignored as
  # soon as one of its parents is, and asking the parent first is what keeps the
  # question answerable: `git check-ignore` refuses any path that lies beyond a
  # symbolic link in the worktree, so a full-path query dies with exit 128 in the
  # very tree the case above describes.
  #
  # 0 = ignored, 1 = not ignored, anything else is a git fault this must not read
  # as "not ignored" — that direction loses the finding silently.
  ignored=""
  prefix=""
  IFS='/' read -r -d '' -a parts < <(printf '%s\0' "${resolved}")
  for part in "${parts[@]}"; do
    prefix="${prefix:+${prefix}/}${part}"
    rc=0
    git check-ignore -q -- "${prefix}" || rc=$?
    case "${rc}" in
    0)
      ignored="${prefix}"
      break
      ;;
    1) : ;; # no rule covers this prefix; try the next one
    *)
      echo "check-absolute-symlinks: git check-ignore failed (${rc}) on ${prefix}" >&2
      exit "${rc}"
      ;;
    esac
    # This component is a symlink here, so git refuses every deeper prefix. Every
    # prefix up to it says "no rule covers this", which is the verdict.
    # Written as an `if` because a bare `[[ … ]] && break` that evaluates false is
    # the loop body's last command, and `set -e` would end the script there.
    if [[ -L "${prefix}" ]]; then
      break
    fi
  done
  if [[ "${ignored}" != "" ]]; then
    violations="${violations}${path} -> ${target} (git ignores ${ignored})"$'\n'
  fi
done <"${index_file}"

if [[ "${violations}" != "" ]]; then
  echo "::error::Tracked symlinks resolve to paths a fresh clone does not have:"
  printf '%s' "${violations}"
  exit 1
fi
