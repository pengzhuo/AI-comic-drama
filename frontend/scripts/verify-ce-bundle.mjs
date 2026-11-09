// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab

/**
 * Build-time assertion for instance bundles.
 *
 * Instance config (`VITE_HIDE_*`, the announcement URL, `VITE_INTERNAL_ENTRY_PATH`,
 * the brand attribution) is inlined at build time. On 2026-11-06 a web image was
 * rebuilt with the plain `pnpm build` — production mode, and `.dockerignore`
 * drops `.env.*` anyway — so every one of those values was silently lost: the
 * customer studio link (`/studio?phone=&code=`) started bouncing to the internal
 * app and asking for the internal console key. This script runs right after the
 * bundle is produced (see the Dockerfile) and fails the build when the artifact
 * does not carry the values that were passed in.
 *
 * Only what is observable in the bundle can be asserted here. Flags read through
 * direct `import.meta.env.VITE_*` member access (src/lib/ui-hide-flags.ts) leave
 * no trace in the minified output; those are reported as "supplied" instead. The
 * assertions below cover the values with a visible consequence — together they
 * catch "the instance env never reached the build", which is the failure mode
 * that matters.
 *
 * Real instance builds opt into a stricter check with
 * ``VITE_REQUIRE_INSTANCE_CONFIG=1``: every required key must then be non-empty,
 * so losing a line from the host ``.env`` fails the image build instead of
 * silently shipping a degraded bundle (the 2026-11-06 studio-link incident, or
 * the 2026-11-09 repeat where the same loss re-exposed the header settings
 * entry). Generic CE/upstream builds leave it unset and keep the historical
 * "unset = upstream as shipped" behavior.
 *
 * Usage: node scripts/verify-ce-bundle.mjs [distDir=dist]
 */
import { existsSync, readFileSync, readdirSync } from "node:fs";
import path from "node:path";

const distDir = process.argv[2] ?? "dist";
const assetsDir = path.join(distDir, "assets");

/** Instance-level values the image build is expected to forward. */
const INSTANCE_VARS = [
  "VITE_EDITION",
  "VITE_HIDE_RELEASE_POPUP",
  "VITE_HIDE_PIKO_ENTRY",
  "VITE_HIDE_OFFICIAL_CHANNELS",
  "VITE_HIDE_NOTIFICATION_CENTER",
  "VITE_HIDE_MODEL_GUIDE",
  "VITE_HIDE_GITHUB_STARS",
  "VITE_HIDE_HEADER_SETTINGS",
  "VITE_LOGIN_ANNOUNCEMENTS_URL",
  "VITE_INTERNAL_ENTRY_PATH",
  "VITE_BRAND_ATTRIBUTION",
];

/**
 * Keys that must be non-empty once an instance opts into strict mode.
 * `VITE_EDITION` is excluded (its Dockerfile default is `ce`) and checked
 * separately below.
 */
const REQUIRED_INSTANCE_VARS = INSTANCE_VARS.filter((name) => name !== "VITE_EDITION");

const escapeRe = (value) => value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
const valueOf = (name) => String(process.env[name] ?? "").trim();

const problems = [];

/** Instance build: the host `.env` must carry the whole instance config. */
const strict = ["1", "true"].includes(valueOf("VITE_REQUIRE_INSTANCE_CONFIG"));

if (!existsSync(assetsDir)) {
  problems.push(`no JS assets at ${assetsDir} — build the bundle first`);
}
const assets = existsSync(assetsDir)
  ? readdirSync(assetsDir)
      .filter((name) => name.endsWith(".js"))
      .map((name) => ({ name, text: readFileSync(path.join(assetsDir, name), "utf8") }))
  : [];
const countOf = (needle) => assets.reduce((n, a) => n + a.text.split(needle).length - 1, 0);
const filesWith = (needle) =>
  assets.filter((a) => a.text.includes(needle)).map((a) => a.name);

console.log(`verify-ce-bundle: ${distDir} (${assets.length} js assets)`);
if (strict) console.log("  mode      strict (VITE_REQUIRE_INSTANCE_CONFIG=1)");
for (const name of INSTANCE_VARS) {
  const value = valueOf(name);
  if (value) console.log(`  supplied  ${name}=${value}`);
}

// 1. VITE_HIDE_GITHUB_STARS=1 must stop every api.github.com lookup.
if (valueOf("VITE_HIDE_GITHUB_STARS") === "1") {
  const hits = filesWith("api.github.com");
  if (hits.length) {
    problems.push(
      `VITE_HIDE_GITHUB_STARS=1 but "api.github.com" is still in ${hits.join(", ")}`,
    );
  }
}

// 2. The configured announcement source must be in the bundle, and a local one
//    must actually exist in dist/.
const announcements = valueOf("VITE_LOGIN_ANNOUNCEMENTS_URL");
if (announcements) {
  if (!countOf(announcements)) {
    problems.push(
      `VITE_LOGIN_ANNOUNCEMENTS_URL=${announcements} does not appear in the bundle`,
    );
  }
  if (announcements.startsWith("/") && !existsSync(path.join(distDir, announcements))) {
    problems.push(`${announcements} is not present in ${distDir}/`);
  }
}

// 3. The attribution is plain content: it must survive verbatim.
const attribution = valueOf("VITE_BRAND_ATTRIBUTION");
if (attribution && !countOf(attribution)) {
  problems.push(`VITE_BRAND_ATTRIBUTION=${attribution} does not appear in the bundle`);
}

// 4. Dual entry: src/lib/app-mode.ts reads the aliased `import.meta.env` object,
//    so both the key and the value survive minification. Missing pair = the flag
//    never reached the bundle, i.e. the exact 2026-11-06 regression.
const entryPath = valueOf("VITE_INTERNAL_ENTRY_PATH");
if (entryPath) {
  const re = new RegExp(
    `["']?VITE_INTERNAL_ENTRY_PATH["']?\\s*:\\s*["']${escapeRe(entryPath)}["']`,
  );
  if (!assets.some((a) => re.test(a.text))) {
    problems.push(
      `VITE_INTERNAL_ENTRY_PATH=${entryPath} is not in the inlined import.meta.env object`,
    );
  }
}

// 5. Strict instance build: the whole instance config must be present, so a lost
//    `.env` line fails the build instead of shipping a degraded bundle.
if (strict) {
  const missing = REQUIRED_INSTANCE_VARS.filter((name) => !valueOf(name));
  if (missing.length) {
    problems.push(
      `VITE_REQUIRE_INSTANCE_CONFIG=1 but these instance vars are empty/missing: ${missing.join(", ")}`,
    );
  }
  if (valueOf("VITE_EDITION") !== "ce") {
    problems.push(
      `VITE_REQUIRE_INSTANCE_CONFIG=1 but VITE_EDITION=${valueOf("VITE_EDITION") || "(empty)"} (expected "ce")`,
    );
  }
}

if (problems.length) {
  console.error("\nverify-ce-bundle: FAILED");
  for (const problem of problems) console.error(`  - ${problem}`);
  console.error(
    "\nInstance config is build-time only: build with `pnpm build:ce` and pass the" +
      "\nvalues as build args (frontend/Dockerfile, docker-compose.yml) — see" +
      "\ndocs/agents/web-hotpatch-deploy.md §1/§6." +
      (strict
        ? "\nThis is a strict instance build (VITE_REQUIRE_INSTANCE_CONFIG=1): every" +
          "\ninstance key above must be set in the host `.env`."
        : ""),
  );
  process.exit(1);
}
console.log("verify-ce-bundle: ok");
