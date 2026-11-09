// At job end, for a failed or cancelled job: print the mute-failure-guard report.
import { ERROR_WORDS, report } from "./report.mjs";

const dir = process.env.STATE_dir;
if (!dir) {
  throw new Error("mute-failure-guard: the main step saved no ledger directory, so there is nothing to report.");
}
const extra = (process.env["INPUT_ERROR-WORDS"] ?? "")
  .split(",")
  .map((word) => word.trim().toLowerCase())
  .filter(Boolean);
console.log(report(dir, [...ERROR_WORDS, ...extra]).join("\n"));
