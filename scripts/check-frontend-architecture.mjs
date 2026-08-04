import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const web = join(root, "apps/web");
const featuresRoot = join(web, "features");
const errors = [];
const ignoredDirectories = new Set([".next", "node_modules"]);

function walk(directory) {
  return readdirSync(directory).flatMap((entry) => {
    if (ignoredDirectories.has(entry)) return [];
    const path = join(directory, entry);
    return statSync(path).isDirectory() ? walk(path) : [path];
  });
}

const domains = readdirSync(featuresRoot).filter((entry) =>
  statSync(join(featuresRoot, entry)).isDirectory(),
);

for (const domain of domains) {
  const directory = join(featuresRoot, domain);
  if (!existsSync(join(directory, "index.ts"))) {
    errors.push(`features/${domain} must expose an index.ts public API`);
  }
  for (const entry of readdirSync(directory)) {
    if (/\.(tsx|css)$/.test(entry)) {
      errors.push(`features/${domain}/${entry} must live in pages/ or components/`);
    }
  }
  const pages = join(directory, "pages");
  if (!existsSync(pages)) continue;
  for (const page of readdirSync(pages)) {
    const pageDirectory = join(pages, page);
    if (!statSync(pageDirectory).isDirectory()) continue;
    for (const required of [`${page}.tsx`, `${page}.module.css`, "index.ts"]) {
      if (!existsSync(join(pageDirectory, required))) {
        errors.push(`features/${domain}/pages/${page} is missing ${required}`);
      }
    }
  }
}

for (const file of walk(web).filter((path) => /\.(ts|tsx)$/.test(path))) {
  const source = readFileSync(file, "utf8");
  const location = relative(web, file);
  const currentDomain = location.startsWith("features/")
    ? location.split("/")[1]
    : null;
  for (const match of source.matchAll(/from\s+["']@\/features\/([^/"']+)(\/[^"']+)?["']/g)) {
    const importedDomain = match[1];
    const internalPath = match[2];
    if (internalPath && importedDomain !== currentDomain) {
      errors.push(`${location} imports internals of features/${importedDomain}; use its index.ts`);
    }
    if (location.startsWith("app/") && internalPath) {
      errors.push(`${location} must import features/${importedDomain} from its public API`);
    }
  }
  if (location.startsWith("components/ui/") && /from\s+["']@\/features\//.test(source)) {
    errors.push(`${location} cannot depend on a feature`);
  }
}

for (const entry of readdirSync(join(web, "components/ui")).filter((name) => /\.(ts|tsx|css)$/.test(name))) {
  errors.push(`components/ui/${entry} must live in its own component directory`);
}

if (errors.length) {
  console.error("Frontend architecture violations:\n");
  for (const error of errors) console.error(`- ${error}`);
  process.exit(1);
}

console.log(`Frontend architecture OK: ${domains.length} domains checked.`);
