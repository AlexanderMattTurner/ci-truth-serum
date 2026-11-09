// Point every later bash step of this job at guard.bash, and remember where its ledger lives.
import { appendFileSync, copyFileSync, mkdtempSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

/** The value of the runner variable NAME in ENV, or a refusal that names it. */
function runnerVar(env, name) {
  const value = env[name];
  if (!value) {
    throw new Error(`mute-failure-guard: ${name} is not set, so this is not a GitHub Actions job step.`);
  }
  return value;
}

/** Set up the guard for the rest of the job: the ledger directory, BASH_ENV, and the post state. */
export function setUp(env = process.env) {
  if (env.BASH_ENV) {
    // A second BASH_ENV (kcov sets one) would silently lose either this guard or that tool.
    throw new Error(
      `mute-failure-guard: BASH_ENV is already ${env.BASH_ENV}, and this action would replace it. ` +
        "Remove this step from that job, because that job already owns BASH_ENV.",
    );
  }
  const dir = mkdtempSync(join(runnerVar(env, "RUNNER_TEMP"), "cts-mute-"));
  const guard = join(dir, "guard.bash");
  copyFileSync(join(dirname(fileURLToPath(import.meta.url)), "guard.bash"), guard);
  writeFileSync(join(dir, "active"), "");
  appendFileSync(runnerVar(env, "GITHUB_ENV"), `BASH_ENV=${guard}\n`);
  appendFileSync(runnerVar(env, "GITHUB_STATE"), `dir=${dir}\n`);
  return dir;
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  setUp();
}
