import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { setUp } from "./main.mjs";

function runnerFiles() {
  const root = mkdtempSync(join(tmpdir(), "mute-main-"));
  const env = {
    RUNNER_TEMP: root,
    GITHUB_ENV: join(root, "env"),
    GITHUB_STATE: join(root, "state"),
  };
  writeFileSync(env.GITHUB_ENV, "");
  writeFileSync(env.GITHUB_STATE, "");
  return env;
}

test("setUp points BASH_ENV at a copy of the guard and saves the directory for the post hook", () => {
  const env = runnerFiles();
  const dir = setUp(env);
  assert.equal(
    readFileSync(env.GITHUB_ENV, "utf8"),
    `BASH_ENV=${join(dir, "guard.bash")}\n`,
  );
  assert.equal(readFileSync(env.GITHUB_STATE, "utf8"), `dir=${dir}\n`);
  assert.equal(
    readFileSync(join(dir, "guard.bash"), "utf8"),
    readFileSync(new URL("./guard.bash", import.meta.url), "utf8"),
  );
});

test("setUp refuses to replace a BASH_ENV that is already set", () => {
  const env = { ...runnerFiles(), BASH_ENV: "/opt/kcov/helper.bash" };
  assert.throws(() => setUp(env), /BASH_ENV is already \/opt\/kcov\/helper\.bash/);
  assert.equal(readFileSync(env.GITHUB_ENV, "utf8"), "");
});

test("setUp refuses to run outside a job step", () => {
  const env = runnerFiles();
  delete env.GITHUB_ENV;
  assert.throws(() => setUp(env), /GITHUB_ENV is not set/);
});
