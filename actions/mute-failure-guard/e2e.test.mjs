import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { setUp } from "./main.mjs";
import { report } from "./report.mjs";

/** Run SCRIPT as a step shell runs it, with the env the setup step wrote to GITHUB_ENV. */
function runStep(root, id, script) {
  const env = { PATH: process.env.PATH, HOME: root, GITHUB_ACTION: id };
  for (const line of readFileSync(join(root, "env"), "utf8").split("\n").filter(Boolean)) {
    const [key, ...value] = line.split("=");
    env[key] = value.join("=");
  }
  const body = join(root, `${id}.sh`);
  writeFileSync(body, script);
  return spawnSync("bash", ["--noprofile", "--norc", "-eo", "pipefail", body], { env, encoding: "utf8" });
}

test("a step that fails through a dropped substitution and prints nothing is reported as mute", () => {
  const root = mkdtempSync(join(tmpdir(), "mute-e2e-"));
  writeFileSync(join(root, "env"), "");
  writeFileSync(join(root, "state"), "");
  const dir = setUp({ RUNNER_TEMP: root, GITHUB_ENV: join(root, "env"), GITHUB_STATE: join(root, "state") });

  assert.equal(runStep(root, "setup", "echo ready\n").status, 0);
  const step = runStep(root, "glyph", 'scrub() { false; }\nrows() { printf "✗ %s\\n" "$1"; }\nrows "$(scrub)"\nexit 1\n');
  assert.equal(step.status, 1);
  assert.equal(step.stdout, "✗ \n");

  const lines = report(dir);
  assert.ok(lines.includes("Step glyph exited 1."), lines.join("\n"));
  assert.ok(lines.some((l) => /^glyph\t\d+\t.*glyph\.sh:1\t1\t\d+\tfalse$/.test(l)), lines.join("\n"));
  assert.ok(lines.some((l) => l.startsWith("::error title=mute failure::mute failure: step glyph exited 1")));
});
