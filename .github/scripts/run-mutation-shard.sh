#!/usr/bin/env bash
# Run one mutation shard: cosmic-ray over the single module named by $SHARD_ID,
# with the per-mutant oracle scoped to that module's own example suite.
#
# The shard config is generated from the committed cosmic-ray.toml by
# mutation_shards.py (SSOT for timeout + operator filters; the shard only swaps
# module-path and narrows the test-command), so the two can never drift.  drift-guard-ok: one generated file derived from a single committed SSOT, no second copy exists to diverge
# A shard never fails on a surviving mutant — survivors are diagnostic, exactly as the
# unsharded run-mutation.sh — so its only failure modes are a red baseline or a
# cosmic-ray error (set -e). It always writes reports/mutation/$SHARD_ID.json so
# the aggregate can demand one report per shard and catch a silently missing slice.
#
# Env: SHARD_ID (required)  SHARD_INDEX/SHARD_TOTAL (mutant slice, default 0/1)
#      HYPOTHESIS_PROFILE (default dev, a fast property budget)
set -euo pipefail

: "${SHARD_ID:?SHARD_ID must be set to a shard id from mutation_shards.py}"
shard_index="${SHARD_INDEX:-0}"
shard_total="${SHARD_TOTAL:-1}"

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
config="cosmic-ray.shard.toml"
session="cr-${SHARD_ID}.sqlite"
report_dir="reports/mutation"

export HYPOTHESIS_PROFILE="${HYPOTHESIS_PROFILE:-dev}"

python "${here}/mutation_shards.py" --write-config "${SHARD_ID}" >/dev/null

# Fresh session each run so a stale partial DB can't mask new mutants.
rm -f "${session}"

echo "::group::cosmic-ray baseline (${SHARD_ID}: unmutated suite must pass)"
cosmic-ray baseline "${config}"
echo "::endgroup::"

echo "::group::cosmic-ray init (${SHARD_ID})"
cosmic-ray init "${config}" "${session}"
# A split module mutates the whole file but this shard runs only its slice of the
# mutants: keep the work items whose deterministic rowid lands in this shard's
# residue class and drop the rest, so the sub-shards partition the module's
# mutants disjointly and completely (init is deterministic, so every runner sees
# the same rowid order). total=1 keeps everything.
if [[ "${shard_total}" -gt 1 ]]; then
  python -c 'import sqlite3, sys; db, total, index = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]); conn = sqlite3.connect(db); conn.execute("DELETE FROM work_items WHERE rowid % ? != ?", (total, index)); conn.commit(); conn.close()' \
    "${session}" "${shard_total}" "${shard_index}"
fi
echo "::endgroup::"

echo "::group::cosmic-ray exec (${SHARD_ID})"
cosmic-ray exec "${config}" "${session}"
echo "::endgroup::"

echo "::group::cosmic-ray report (${SHARD_ID})"
cr-report "${session}" --show-output
echo "::endgroup::"

# Survival rate is diagnostic, not a gate; if cr-rate itself hiccups after a
# clean exec, record it as unknown rather than failing the shard.
if rate="$(cr-rate "${session}")"; then
  echo "Mutation survival rate (${SHARD_ID}): ${rate}"
else
  rate="unknown"
fi

mkdir -p "${report_dir}"
python -c 'import json, sys; json.dump({"id": sys.argv[1], "rate": sys.argv[2]}, open(sys.argv[3], "w"))' \
  "${SHARD_ID}" "${rate}" "${report_dir}/${SHARD_ID}.json"
