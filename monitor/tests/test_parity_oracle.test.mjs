/**
 * The parity harness must describe the real page, not its own idea of it.
 *
 * monitoring/parity/parity_check.py answers "does the bespoke page agree with
 * Grafana?" by computing the page side itself (it has to: the harness runs headless,
 * next to Prometheus). That makes the page model a second implementation of the page,
 * and a second implementation that drifts is worse than none — it would report parity
 * while the browser showed something else.
 *
 * So this test boots the actual app.js in the headless DOM, reads the numbers off the
 * rendered tiles, runs the harness' `page` mode over the same mock server, and
 * demands the two agree. If someone changes app.js's metrics card and not the harness
 * (or the other way round), this fails.
 *
 *   node --test "monitor/tests/*.test.mjs"
 */

import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import net from "node:net";
import path from "node:path";
import { after, before, describe, test } from "node:test";
import { fileURLToPath } from "node:url";

import { loadPage, memoryStorage, textOf, waitFor } from "./frontend_dom.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const STATIC_DIR = path.join(HERE, "..", "static");
const REPO_ROOT = path.join(HERE, "..", "..");
const HARNESS = path.join(REPO_ROOT, "monitoring", "parity", "parity_check.py");

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function freePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.once("error", reject);
    srv.listen(0, "127.0.0.1", () => {
      const { port } = srv.address();
      srv.close(() => resolve(port));
    });
  });
}

const children = [];

/** Spawn a python helper and resolve with the URL it announces. */
function spawnPython(args, { name, port }) {
  const child = spawn("python3", args, { cwd: REPO_ROOT });
  children.push(child);
  const seen = [];
  let timer;
  const started = new Promise((resolve, reject) => {
    let buffer = "";
    let settled = false;
    const consume = (chunk) => {
      buffer += String(chunk);
      let index;
      while ((index = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, index).trim();
        buffer = buffer.slice(index + 1);
        if (!line) continue;
        seen.push(line);
        const match = line.match(/https?:\/\/\S+/);
        if (match && !settled) {
          settled = true;
          clearTimeout(timer);
          resolve(match[0].replace(/\)$/, ""));
        }
      }
    };
    child.stdout.on("data", consume);
    child.stderr.on("data", consume);
    child.once("exit", (code) => {
      if (!settled) {
        settled = true;
        reject(new Error(`${name} exited early (${code}):\n${seen.join("\n")}`));
      }
    });
    timer = setTimeout(() => {
      if (!settled) {
        settled = true;
        reject(new Error(`${name} did not report a URL within 20s:\n${seen.join("\n")}`));
      }
    }, 20000);
    if (timer.unref) timer.unref();
  });
  return { child, started, port };
}

/** Run the harness' `page` mode and resolve with its JSON. */
function harnessPageFigures(upstream) {
  return new Promise((resolve, reject) => {
    const child = spawn(
      "python3",
      [HARNESS, "page", "--upstream", upstream],
      { cwd: REPO_ROOT }
    );
    children.push(child);
    let out = "";
    let err = "";
    child.stdout.on("data", (chunk) => (out += String(chunk)));
    child.stderr.on("data", (chunk) => (err += String(chunk)));
    child.once("exit", (code) => {
      if (code !== 0) return reject(new Error(`harness exited ${code}: ${err}`));
      try {
        resolve(JSON.parse(out));
      } catch (problem) {
        reject(new Error(`harness printed no JSON: ${out}\n${err}\n${problem}`));
      }
    });
  });
}

const ENV = {};

before(async () => {
  const healthy = await spawnPython(
    [path.join("monitor", "tests", "mock_upstream.py"), "--port", "0", "--mode", "healthy"],
    { name: "mock healthy" }
  );
  const port = await freePort();
  const monitor = spawnPython(
    [
      path.join("monitor", "serve.py"),
      "--host", "127.0.0.1",
      "--port", String(port),
      "--url", await healthy.started,
    ],
    { name: "monitor/serve.py", port }
  );
  ENV.upstream = await healthy.started;
  ENV.monitor = await monitor.started;
  for (const child of children) child.unref();
});

after(() => {
  for (const child of children) {
    try {
      child.kill();
    } catch (_) {
      /* already gone */
    }
  }
});

/** "1,416 tok/s" / "62.5%" / "522" -> 1416 / 62.5 / 522. */
function numeric(text) {
  const cleaned = String(text).replace(/,/g, "").match(/-?\d+(\.\d+)?/);
  assert.ok(cleaned, `no number in ${JSON.stringify(text)}`);
  return Number(cleaned[0]);
}

function nearly(actual, want, tolerance = 0.005) {
  const scale = Math.max(Math.abs(want), Math.abs(actual), 1e-9);
  return Math.abs(actual - want) / scale <= tolerance;
}

describe("parity harness: its model of the page is the page", () => {
  let page;
  let harness;

  before(async () => {
    page = loadPage({
      staticDir: STATIC_DIR,
      baseURI: `${ENV.monitor}/`,
      storage: memoryStorage(),
    });
    await waitFor(() => /^(healthy|down|degraded)/.test(String(textOf(page.byId("status-text")))),
      { what: "the first poll" });
    page.byId("refresh-now").dispatch("click");
    await waitFor(() => textOf(page.byId("metrics-badge")).includes("series"),
      { what: "the metrics card to render" });
    harness = await harnessPageFigures(ENV.upstream);
  });

  after(() => page.dispose());

  /** [figure the harness models, tile label on the page, how to read the page value] */
  const PAIRS = [
    ["decode_tok_s", "generate tok/s \u00b7 server avg"],
    ["prompt_tok_s", "prompt tok/s \u00b7 server avg"],
    ["requests_in_flight", "in flight \u00b7 instant"],
    ["requests_total", "requests \u00b7 lifetime"],
  ];

  test("every tile's number is the number the harness computes", () => {
    const tiles = page.byId("metrics-tiles").children.map((tile) => [
      tile.children[0].textContent,
      tile.children[1].textContent,
    ]);
    for (const [figure, label] of PAIRS) {
      const tile = tiles.find(([name]) => name === label);
      assert.ok(tile, `no "${label}" tile on the page to compare against`);
      const want = harness.page[figure];
      assert.ok(want !== null && want !== undefined, `harness has no ${figure}`);
      assert.ok(
        nearly(numeric(tile[1]), want),
        `${label}: page shows ${tile[1]} (${numeric(tile[1])}), harness says ${want}`
      );
    }
  });

  test("the KV gauge is the same figure on both sides", () => {
    const want = harness.page.kv_usage_pct;
    const shown = numeric(textOf(page.byId("metrics-gauge-value")));
    assert.ok(nearly(shown, want), `gauge shows ${shown}, harness says ${want}`);
  });

  test("the figures the page no longer derives stay gone", () => {
    // The harness still models these (the run that justified deleting them has to
    // show them); the page must not render them. If a future change resurrects a
    // ratio tile, this is the test that notices.
    assert.ok(harness.page.draft_accept_pct > 0, "harness should still model the deleted figure");
    assert.ok(harness.page.cache_hit_pct > 0, "harness should still model the deleted figure");
    const tiles = page.byId("metrics-tiles").children.map((tile) => [
      String(tile.children[0].textContent),
      String(tile.children[1].textContent),
    ]);
    assert.ok(!tiles.some(([label]) => /accept|cache hit/i.test(label)),
      `a deleted ratio came back as a tile: ${tiles.map(([l]) => l).join(", ")}`);
    // The only percentage left on that card is the KV gauge, which is a raw ratio.
    assert.ok(!tiles.some(([, value]) => value.trim().endsWith("%")),
      `a derived percentage came back: ${tiles.map(([, v]) => v).join(", ")}`);
  });

  test("the page threw nothing while rendering them", () => {
    assert.deepEqual(page.window.errors, []);
  });
});
