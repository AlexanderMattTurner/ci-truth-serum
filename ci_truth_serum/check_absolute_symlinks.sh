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

violations=""
while IFS= read -r line; do
  [[ "${line}" = "" ]] && continue
  mode=$(printf '%s' "${line}" | awk '{print $1}')
  hash=$(printf '%s' "${line}" | awk '{print $2}')
  path=$(printf '%s' "${line}" | cut -f2-)
  [[ "${mode}" = "120000" ]] || continue
  target=$(git cat-file blob "${hash}")
  case "${target}" in
  /*)
    violations="${violations}${path} -> ${target} (absolute path)"$'\n'
    continue
    ;;
  *) : ;; # relative targets are portable — judged against .gitignore below
  esac

  # Resolve the target the way the filesystem does: relative to the LINK's
  # directory, not to the repo root. `realpath -m` does not require the target to
  # exist, so a link into a directory this clone has not built still resolves.
  dir=$(dirname "${path}")
  resolved=$(realpath -m --relative-to=. "${dir}/${target}")
  # A target outside the repository has no .gitignore verdict to read. Skip it
  # rather than guess.
  case "${resolved}" in
  ../* | /*) continue ;;
  *) : ;;
  esac
  # 0 = ignored, 1 = not ignored, anything else is a git fault this must not read
  # as "not ignored" — that direction loses the finding silently.
  rc=0
  git check-ignore -q -- "${resolved}" || rc=$?
  case "${rc}" in
  0) violations="${violations}${path} -> ${target} (git ignores ${resolved})"$'\n' ;;
  1) : ;; # a target the clone will carry
  *)
    echo "check-absolute-symlinks: git check-ignore failed (${rc}) on ${resolved}" >&2
    exit "${rc}"
    ;;
  esac
done < <(git ls-files -s)

if [[ "${violations}" != "" ]]; then
  echo "::error::Tracked symlinks resolve to paths a fresh clone does not have:"
  printf '%s' "${violations}"
  exit 1
fi
