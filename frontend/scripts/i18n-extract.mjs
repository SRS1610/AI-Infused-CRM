#!/usr/bin/env node
// Collect the translatable strings of the client and keep the catalogues in src/i18n/*.json in sync.
//
//   npm run i18n:extract            add missing keys (empty value = falls back to English), list unused ones
//   npm run i18n:extract -- --check  report only; exit 1 when a catalogue lacks keys
//   npm run i18n:extract -- --prune  also remove unused keys
//
// Only literal first arguments are extracted: t("…"), tx("…", …) and both forms of tn("…", "…", n).
// Test files are skipped.
import { readdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";

const STRING = String.raw`("(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*')`;
const SINGLE = new RegExp(String.raw`(?<![\w$.])(?:t|tx)\(\s*${STRING}\s*[,)]`, "g");
const PLURAL = new RegExp(String.raw`(?<![\w$.])tn\(\s*${STRING}\s*,\s*${STRING}\s*,`, "g");
const DYNAMIC = /(?<![\w$.])(?:t|tx|tn)\(\s*(?=[`\w])/g;

function unquote(literal) {
  return literal
    .slice(1, -1)
    .replace(/\\(u[0-9a-fA-F]{4}|.)/g, (_, c) =>
      c[0] === "u" && c.length === 5
        ? String.fromCharCode(parseInt(c.slice(1), 16))
        : ({ n: "\n", t: "\t", r: "\r" }[c] ?? c),
    );
}

/** Translatable strings of one source file, plus calls whose first argument is not a literal. */
export function extractFromSource(code) {
  const keys = [];
  for (const m of code.matchAll(SINGLE)) keys.push(unquote(m[1]));
  for (const m of code.matchAll(PLURAL)) keys.push(unquote(m[1]), unquote(m[2]));
  const dynamic = [];
  for (const m of code.matchAll(DYNAMIC)) {
    const line = code.slice(0, m.index).split("\n").length;
    const before = code.slice(Math.max(0, m.index - 9), m.index);
    if (!/function\s*$/.test(before)) dynamic.push(line);
  }
  return { keys, dynamic };
}

function sourceFiles(dir) {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) return entry.name === "test" ? [] : sourceFiles(path);
    return /\.(ts|tsx)$/.test(entry.name) && !/\.test\.(ts|tsx)$/.test(entry.name) && !/\.d\.ts$/.test(entry.name)
      ? [path]
      : [];
  });
}

/** Every key under `srcDir` → the files using it; `dynamic` lists non-literal calls (file:line). */
export function scanSources(srcDir) {
  const keys = new Map();
  const dynamic = [];
  for (const file of sourceFiles(srcDir)) {
    if (file.endsWith(join("kernel", "i18n.ts"))) continue;
    const found = extractFromSource(readFileSync(file, "utf8"));
    const rel = relative(srcDir, file);
    for (const key of found.keys) keys.set(key, [...new Set([...(keys.get(key) ?? []), rel])]);
    for (const line of found.dynamic) dynamic.push(`${rel}:${line}`);
  }
  return { keys, dynamic };
}

/** `{name}` placeholders of a message, sorted. */
export function placeholders(text) {
  return [...text.matchAll(/\{(\w+)\}/g)].map((m) => m[1]).sort();
}

function main(argv) {
  const root = join(dirname(fileURLToPath(import.meta.url)), "..");
  const srcDir = join(root, "src");
  const catalogueDir = join(srcDir, "i18n");
  const check = argv.includes("--check");
  const prune = argv.includes("--prune");
  const { keys, dynamic } = scanSources(srcDir);
  console.log(`${keys.size} translatable strings in src/`);
  for (const where of dynamic) console.warn(`  warning: non-literal translation call at ${where} (not extractable)`);
  let failed = false;
  for (const name of readdirSync(catalogueDir)
    .filter((f) => f.endsWith(".json"))
    .sort()) {
    const path = join(catalogueDir, name);
    const catalogue = JSON.parse(readFileSync(path, "utf8"));
    const missing = [...keys.keys()].filter((k) => !(k in catalogue));
    const unused = Object.keys(catalogue).filter((k) => !keys.has(k));
    const next = { ...catalogue };
    for (const k of missing) next[k] = "";
    if (prune) for (const k of unused) delete next[k];
    const translated = [...keys.keys()].filter((k) => next[k]).length;
    const coverage = keys.size ? (100 * translated) / keys.size : 100;
    console.log(
      `${name}: ${translated}/${keys.size} translated (${coverage.toFixed(1)}%), ${missing.length} missing, ${unused.length} unused`,
    );
    for (const k of missing) console.log(`  + ${JSON.stringify(k)}  (${keys.get(k).join(", ")})`);
    for (const k of unused) console.log(`  ${prune ? "-" : "?"} ${JSON.stringify(k)}  (unused)`);
    for (const k of [...keys.keys()].filter((key) => next[key])) {
      if (placeholders(k).join() !== placeholders(next[k]).join()) {
        console.log(`  ! ${JSON.stringify(k)}: placeholders differ in ${JSON.stringify(next[k])}`);
        failed = true;
      }
    }
    if (check) {
      if (missing.length) failed = true;
      continue;
    }
    const sorted = Object.fromEntries(
      Object.keys(next)
        .sort()
        .map((k) => [k, next[k]]),
    );
    writeFileSync(path, `${JSON.stringify(sorted, null, 2)}\n`);
  }
  if (failed) process.exitCode = 1;
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) main(process.argv.slice(2));
