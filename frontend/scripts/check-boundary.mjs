import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";

const sourceRoot = new URL("../src", import.meta.url).pathname;
const forbiddenPatterns = [
  /\.\.\/backend/,
  /\.\.\/\.\.\/backend/,
  /from\s+["'][^"']*backend/,
  /import\s*\([^)]*backend/,
  /\bfs\b/,
  /readFile|writeFile/
];

function collectFiles(dir) {
  const files = [];
  for (const entry of readdirSync(dir)) {
    const path = join(dir, entry);
    const stat = statSync(path);
    if (stat.isDirectory()) files.push(...collectFiles(path));
    else if (/\.(ts|tsx|js|jsx)$/.test(entry)) files.push(path);
  }
  return files;
}

const violations = [];
for (const file of collectFiles(sourceRoot)) {
  const text = readFileSync(file, "utf-8");
  for (const pattern of forbiddenPatterns) {
    if (pattern.test(text)) {
      violations.push(`${relative(process.cwd(), file)} matches ${pattern}`);
    }
  }
}

if (violations.length) {
  console.error("Frontend boundary violation: frontend/src must use HTTP APIs instead of backend files.");
  console.error(violations.join("\n"));
  process.exit(1);
}
