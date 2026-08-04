import fs from "node:fs";
import path from "node:path";

let loadedRootEnv = false;

export function ensureServerEnv() {
  if (loadedRootEnv) return;
  loadedRootEnv = true;

  const rootEnvPath = path.resolve(process.cwd(), "../../.env.local");
  if (fs.existsSync(rootEnvPath)) {
    const lines = fs.readFileSync(rootEnvPath, "utf8").split(/\r?\n/);
    for (const line of lines) {
      if (!line || line.trimStart().startsWith("#")) continue;
      const match = line.match(/^([A-Z0-9_]+)=(.*)$/);
      if (!match) continue;
      const [, key, rawValue] = match;
      if (!process.env[key]) process.env[key] = rawValue.trim();
    }
  }

  process.env.NEXTAUTH_SECRET ??= process.env.AUTH_SECRET;
  process.env.NEXTAUTH_URL ??= process.env.AUTH_URL ?? process.env.NEXT_PUBLIC_APP_URL;
}
