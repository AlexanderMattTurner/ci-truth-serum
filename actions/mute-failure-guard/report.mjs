// The job-end report: the ledger, the failed step, the host's limits, and the mute verdict.
import { execFileSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { constants } from "node:os";
import { join } from "node:path";

export const ERROR_WORDS = [
  "error", "fail", "fatal", "refus", "denied", "cannot", "could not", "not found",
  "no such", "traceback", "exception", "panic", "abort", "killed", "timed out",
];
// A failed step's own rows are printed in full up to STEP_ROWS: its first failure is often the
// cause, so a newest-only cut would hide it. Rows from steps that passed are context, newest last.
const STEP_ROWS = 500;
const LEDGER_ROWS = 50;
const LEDGER_HEADER = "step\tpid\tfile:line\tstatus\topen fds\tcommand\t(times seen)";

/**
 * ROWS with repeats collapsed. Rows that differ only in pid count as one: the same failed
 * command at the same line, in the same step and with the same status. Each row keeps the place
 * of its LAST occurrence, so the newest failures stay at the end, and gains its count.
 */
export function distinct(rows) {
  const seen = new Map();
  for (const row of rows) {
    const [step, , where, status, , command] = row.split("\t");
    const key = [step, where, status, command].join("\t");
    const count = (seen.get(key)?.count ?? 0) + 1;
    seen.delete(key);
    seen.set(key, { row, count });
  }
  return [...seen.values()].map(({ row, count }) => `${row}\t(${count}x)`);
}

/** True when TEXT holds a line with one of WORDS, ignoring case. */
export function hasErrorText(text, words) {
  return text
    .toLowerCase()
    .split("\n")
    .some((line) => words.some((word) => line.includes(word)));
}

/** A workflow-command value with its control characters escaped, as the runner requires. */
function escapeCommand(text) {
  return text.replaceAll("%", "%25").replaceAll("\r", "%0D").replaceAll("\n", "%0A");
}

/** The name of the signal behind exit STATUS, or "" when a signal did not end it. */
export function signalName(status) {
  if (status <= 128) return "";
  const number = status - 128;
  return Object.entries(constants.signals).find(([, n]) => n === number)?.[0] ?? `signal ${number}`;
}

/** One host fact from COMMAND, or the reason it could not be read. */
function hostFact(command, args) {
  try {
    return execFileSync(command, args, { encoding: "utf8", timeout: 10_000 }).trim();
  } catch (err) {
    return `unreadable (${err.message.split("\n")[0]})`;
  }
}

function openFiles() {
  if (process.platform !== "linux") return hostFact("sysctl", ["-n", "kern.num_files"]);
  try {
    return readFileSync("/proc/sys/fs/file-nr", "utf8").split(/\s+/)[0];
  } catch (err) {
    return `unreadable (${err.message})`;
  }
}

/** ROWS, or its first and last halves of LIMIT with a line that counts the rows left out. */
export function headAndTail(rows, limit) {
  if (rows.length <= limit) return rows;
  const half = Math.floor(limit / 2);
  return [...rows.slice(0, half), `... ${rows.length - 2 * half} rows not shown ...`, ...rows.slice(-half)];
}

/** The report lines for the ledger directory DIR. */
export function report(dir, words = ERROR_WORDS) {
  const read = (name) => (existsSync(join(dir, name)) ? readFileSync(join(dir, name), "utf8") : "");
  const out = ["::group::mute-failure-guard report"];
  const failed = read("steps")
    .split("\n")
    .filter(Boolean)
    .map((row) => row.split("\t"))
    .filter(([, status]) => status !== "0");
  if (failed.length === 0) {
    out.push(
      "No guarded bash step recorded a non-zero exit. The failure came from a step that is not bash, " +
        "or a step replaced the guard's EXIT trap.",
    );
  }
  for (const [step, status] of failed) {
    const signal = signalName(Number(status));
    out.push(`Step ${step} exited ${status}${signal ? ` (${signal})` : ""}.`);
  }
  out.push(`ulimit -n: ${hostFact("sh", ["-c", "ulimit -n"])}`);
  out.push(`open files on the host: ${openFiles()}`);
  const ps = hostFact("ps", ["-A", "-o", "pid="]);
  out.push(`processes on the host: ${ps.startsWith("unreadable") ? ps : ps.split("\n").length}`);
  const rows = read("ledger").split("\n").filter(Boolean);
  const failedSteps = new Set(failed.map(([step]) => step));
  const stepOf = (row) => row.split("\t")[0];
  out.push(`Ledger columns: ${LEDGER_HEADER}`);
  for (const step of failedSteps) {
    const own = distinct(rows.filter((row) => stepOf(row) === step));
    out.push(`Ledger rows from step ${step}, in the order each row last failed: ${own.length} distinct`);
    out.push(...headAndTail(own, STEP_ROWS));
  }
  const others = distinct(rows.filter((row) => !failedSteps.has(stepOf(row))));
  out.push(
    `Ledger rows from the other steps: the last ${Math.min(others.length, LEDGER_ROWS)} of ` +
      `${others.length} distinct rows (${rows.length} rows in the whole ledger)`,
  );
  out.push(...others.slice(-LEDGER_ROWS));
  out.push(
    "A row marked `subshell exit` is a $(…), <(…) or ( … ) that exited non-zero; one marked " +
      "`suppressed` is a failure that `|| true` or `|| :` discarded. A shell that sets its own ERR, " +
      "DEBUG or EXIT trap replaces the guard's, and a step that is not bash has no rows.",
  );
  out.push("::endgroup::");
  for (const [step, status] of failed) {
    const output = read(`step-${step}.out`);
    if (!hasErrorText(output, words)) {
      out.push(
        `::error title=mute failure::${escapeCommand(
          `mute failure: step ${step} exited ${status} and printed no line with error text. ` +
            "Its last failed commands are in the mute-failure-guard report above.",
        )}`,
      );
    }
  }
  return out;
}
