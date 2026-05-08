#!/usr/bin/env node
/**
 * crosstalk-mcp bootstrap — closes the npm-install gap on first use.
 *
 * Claude Code's plugin loader does NOT run `npm install` for plugin MCP
 * servers (per #127). Without dependencies, server-l2-only.js fails its
 * top-level imports and the MCP fails to attach. Operators currently work
 * around this with a manual `npm install` between `/plugin install 8l-cq`
 * and the next session bounce — a hidden step that breaks the otherwise
 * one-command onboarding flow.
 *
 * This shim:
 *   1. Checks if node_modules exists alongside package.json.
 *   2. If not, runs `npm install --omit=dev --no-audit --no-fund` once,
 *      streaming output to stderr so the operator can see progress.
 *   3. Dynamically imports the real server entry point.
 *
 * The install runs synchronously (spawnSync) and only on the cold-cache
 * path. Once node_modules exists, this shim adds <5ms of overhead.
 */
import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const pkgDir = resolve(__dirname, "..");
const nodeModulesDir = resolve(pkgDir, "node_modules");

if (!existsSync(nodeModulesDir)) {
  process.stderr.write(
    "crosstalk-mcp: node_modules missing, running `npm install` (one-time, ~30s)...\n",
  );
  const result = spawnSync(
    "npm",
    ["install", "--omit=dev", "--no-audit", "--no-fund"],
    {
      cwd: pkgDir,
      stdio: ["ignore", "inherit", "inherit"],
      env: process.env,
    },
  );
  if (result.status !== 0) {
    process.stderr.write(
      `crosstalk-mcp: npm install failed (exit ${result.status}). ` +
        "Falling back to manual install: `cd " +
        pkgDir +
        " && npm install`\n",
    );
    process.exit(1);
  }
  process.stderr.write("crosstalk-mcp: npm install OK, starting server\n");
}

await import("./server-l2-only.js");
