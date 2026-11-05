import assert from "node:assert/strict";
import { mkdtemp, readFile, rm, stat, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { prepareGateway } from "./prepare.mjs";

async function temporaryHome(t) {
  const home = await mkdtemp(join(tmpdir(), "bridge-gateway-test-"));
  t.after(() => rm(home, { recursive: true, force: true }));
  return home;
}

test("setup creates a private key, preserves config, and is idempotent", async (t) => {
  const home = await temporaryHome(t);
  const configPath = join(home, "config.json");
  await writeFile(
    configPath,
    JSON.stringify({ auth: { adminApiKey: "test-admin", apiKeys: ["test-existing"] }, providers: {} }),
  );
  const result = await prepareGateway(home);
  const key = (await readFile(result.keyPath, "utf8")).trim();
  assert.match(key, /^[A-Za-z0-9_-]{43}$/);
  assert.equal((await stat(home)).mode & 0o777, 0o700);
  assert.equal((await stat(result.keyPath)).mode & 0o777, 0o600);
  assert.equal((await stat(configPath)).mode & 0o777, 0o600);
  await prepareGateway(home);
  const config = JSON.parse(await readFile(configPath, "utf8"));
  assert.equal(config.auth.adminApiKey, "test-admin");
  assert.deepEqual(config.providers, {});
  assert.deepEqual(config.auth.apiKeys, ["test-existing", key]);
  assert.equal((await readFile(result.keyPath, "utf8")).trim(), key);
});

test("invalid configuration is rejected without overwriting or revealing its content", async (t) => {
  const home = await temporaryHome(t);
  const path = join(home, "config.json");
  const invalid = '{"private":"test-private-value",';
  await writeFile(path, invalid);
  await assert.rejects(
    prepareGateway(home),
    (error) => /not valid JSON/.test(error.message) && !error.message.includes("test-private-value"),
  );
  assert.equal(await readFile(path, "utf8"), invalid);
});

test("an empty file created by upstream authentication is explicitly initialized", async (t) => {
  const home = await temporaryHome(t);
  const configPath = join(home, "config.json");
  await writeFile(configPath, "");
  const report = await prepareGateway(home);
  assert.equal(report.initializedConfig, true);
  assert.equal(JSON.parse(await readFile(configPath, "utf8")).auth.apiKeys.length, 1);
});

test("invalid key lists and malformed existing client keys fail explicitly", async (t) => {
  const home = await temporaryHome(t);
  const configPath = join(home, "config.json");
  await writeFile(configPath, JSON.stringify({ auth: { apiKeys: "not-an-array" } }));
  await assert.rejects(prepareGateway(home), /array of nonempty strings/);
  await writeFile(configPath, JSON.stringify({ auth: { apiKeys: null } }));
  await assert.rejects(prepareGateway(home), /array of nonempty strings/);
  await writeFile(configPath, "{}");
  await writeFile(join(home, "gateway-client-key"), "too-short");
  await assert.rejects(prepareGateway(home), /client key is invalid/);
  assert.equal(await readFile(join(home, "gateway-client-key"), "utf8"), "too-short");
});

test("configuration symlinks are not followed", async (t) => {
  const home = await temporaryHome(t);
  const outside = join(home, "outside.json");
  await writeFile(outside, "{}");
  await symlink(outside, join(home, "config.json"));
  await assert.rejects(prepareGateway(home), /non-regular file/);
  assert.equal(await readFile(outside, "utf8"), "{}");
});
