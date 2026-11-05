import { randomBytes, randomUUID } from "node:crypto";
import { chmod, lstat, mkdir, readFile, rename, rm, writeFile } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

async function readOptionalFile(path) {
  try {
    if (!(await lstat(path)).isFile()) {
      throw new Error(`Refusing to read a non-regular file: ${path}`);
    }
    return await readFile(path, "utf8");
  } catch (error) {
    if (error.code === "ENOENT") return undefined;
    throw error;
  }
}

async function writePrivateFile(path, content) {
  const temporary = `${path}.${randomUUID()}.tmp`;
  try {
    await writeFile(temporary, content, { mode: 0o600, flag: "wx" });
    await rename(temporary, path);
  } finally {
    await rm(temporary, { force: true });
  }
}

function isObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

export async function prepareGateway(home) {
  await mkdir(home, { recursive: true, mode: 0o700 });
  await chmod(home, 0o700);
  const configPath = join(home, "config.json");
  const keyPath = join(home, "gateway-client-key");
  const existingConfig = await readOptionalFile(configPath);
  const initializedConfig = existingConfig === undefined || existingConfig.trim() === "";
  let config = {};
  if (!initializedConfig) {
    try {
      config = JSON.parse(existingConfig);
    } catch (error) {
      if (!(error instanceof SyntaxError)) throw error;
      throw new Error("Gateway configuration is not valid JSON; refusing to overwrite it.");
    }
  }
  if (!isObject(config) || (config.auth !== undefined && !isObject(config.auth))) {
    throw new Error("Gateway configuration and auth must be JSON objects.");
  }
  const auth = config.auth ?? {};
  const keys = auth.apiKeys === undefined ? [] : auth.apiKeys;
  if (!Array.isArray(keys) || keys.some((key) => typeof key !== "string" || !key.trim())) {
    throw new Error("Gateway auth.apiKeys must be an array of nonempty strings.");
  }
  const existingKey = await readOptionalFile(keyPath);
  const key = existingKey === undefined ? randomBytes(32).toString("base64url") : existingKey.trim();
  if (!/^[A-Za-z0-9_-]{43,}$/.test(key)) {
    throw new Error("The local gateway client key is invalid; refusing to replace it.");
  }
  if (existingKey === undefined) await writePrivateFile(keyPath, `${key}\n`);
  await chmod(keyPath, 0o600);
  config.auth = { ...auth, apiKeys: [...new Set([...keys, key])] };
  await writePrivateFile(configPath, `${JSON.stringify(config, null, 2)}\n`);
  return { home, keyPath, initializedConfig };
}

if (process.argv[1] && pathToFileURL(resolve(process.argv[1])).href === import.meta.url) {
  const project = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
  try {
    const report = await prepareGateway(join(project, ".runtime", "copilot-api"));
    if (report.initializedConfig) console.log("Initialized the empty gateway configuration.");
    console.log("Local gateway authentication configured; the client key was not printed.");
  } catch (error) {
    console.error(`Gateway setup failed: ${error.message}`);
    process.exitCode = 1;
  }
}
