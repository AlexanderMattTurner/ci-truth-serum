import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { ERROR_WORDS, hasErrorText, report, signalName } from "./report.mjs";

function ledgerDir(files) {
  const dir = mkdtempSync(join(tmpdir(), "mute-report-"));
  for (const [name, text] of Object.entries(files)) writeFileSync(join(dir, name), text);
  return dir;
}

const LEDGER = "build\t41\tbin/lib/msg-emit.bash:19\t1\t7\t_gb_scrub \"$@\"\n";

test("a failed step that printed no error text gets a mute-failure error", () => {
  const dir = ledgerDir({
    steps: "checkout\t0\nbuild\t1\n",
    "step-build.out": "✗ \n",
    ledger: LEDGER,
  });
  const lines = report(dir);
  assert.ok(lines.includes("Step build exited 1."));
  assert.ok(lines.includes(LEDGER.trimEnd()));
  assert.deepEqual(
    lines.filter((line) => line.startsWith("::error")),
    [
      "::error title=mute failure::mute failure: step build exited 1 and printed no line with error text. " +
        "Its last failed commands are in the mute-failure-guard report above.",
    ],
  );
});

test("a failed step that named its error gets the report and no mute-failure error", () => {
  const dir = ledgerDir({
    steps: "build\t1\n",
    "step-build.out": "building\nError: the archive is missing\n",
    ledger: LEDGER,
  });
  const lines = report(dir);
  assert.ok(lines.includes(LEDGER.trimEnd()));
  assert.deepEqual(lines.filter((line) => line.startsWith("::error")), []);
});

test("a consumer's extra error words count as error text", () => {
  const dir = ledgerDir({ steps: "build\t2\n", "step-build.out": "✗ boom\n" });
  assert.equal(report(dir).filter((l) => l.startsWith("::error")).length, 1);
  assert.equal(report(dir, [...ERROR_WORDS, "boom"]).filter((l) => l.startsWith("::error")).length, 0);
});

test("a job with no recorded failed step says so instead of reporting nothing", () => {
  const lines = report(ledgerDir({ steps: "build\t0\n" }));
  assert.ok(lines.some((line) => line.startsWith("No guarded bash step recorded a non-zero exit")));
  assert.deepEqual(lines.filter((line) => line.startsWith("::error")), []);
});

test("only the last 50 ledger rows are printed", () => {
  const rows = Array.from({ length: 60 }, (_, i) => `build\t1\tx.sh:${i + 1}\t1\t5\tfalse`);
  const lines = report(ledgerDir({ steps: "build\t1\n", "step-build.out": "fail\n", ledger: rows.join("\n") }));
  assert.ok(lines.includes("Ledger: the last 50 of 60 failed commands"));
  assert.ok(lines.includes(rows[59]) && lines.includes(rows[10]) && !lines.includes(rows[9]));
});

test("an exit status above 128 names its signal", () => {
  assert.equal(signalName(137), "SIGKILL");
  assert.equal(signalName(143), "SIGTERM");
  assert.equal(signalName(1), "");
});

test("error text is matched per line and ignores case", () => {
  assert.equal(hasErrorText("ok\nFATAL: no disk\n", ERROR_WORDS), true);
  assert.equal(hasErrorText("✗ \n\n", ERROR_WORDS), false);
});
