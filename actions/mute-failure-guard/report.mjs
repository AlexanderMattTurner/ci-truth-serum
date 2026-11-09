// The job-end report: the ledger, the failed step, the host's limits, and the mute verdict.
import { execFileSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { constants } from "node:os";
import { join } from "node:path";

export const ERROR_WORDS = [
  "error", "fail", "fatal", "refus", "denied", "cannot", "could not", "not found",
  "no such", "traceback", "exception", "panic", "abort", "killed", "timed out",
];
const LEDGER_ROWS = 50;

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
  out.push(`processes on the host: ${hostFact("ps", ["-A", "-o", "pid="]).split("\n").length}`);
  const rows = read("ledger").split("\n").filter(Boolean);
  out.push(`Ledger: the last ${Math.min(rows.length, LEDGER_ROWS)} of ${rows.length} failed commands`);
  out.push("step\tpid\tfile:line\tstatus\topen fds\tcommand");
  out.push(...rows.slice(-LEDGER_ROWS));
  out.push(
    "A command that failed inside an `if`, `while` or `!` condition, or in a non-final part of a " +
      "`&&` or `||` list, is not in the ledger: bash runs no ERR trap there.",
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
